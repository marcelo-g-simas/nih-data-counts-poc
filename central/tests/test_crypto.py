import base64
import traceback
from datetime import date, timedelta

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.padding import MGF1, OAEP
from cryptography.hazmat.primitives.ciphers.aead import AESGCMSIV
from cryptography.hazmat.primitives.serialization import load_pem_public_key

from data_counts_central.crypto import (
    TranscryptError,
    derive_aes_key_v2,
    derive_drs_shift_key,
    derive_shift_days,
    generate_central_keys,
    generate_central_shift_secret,
    resolve_and_shift,
    transcrypt_in,
)

_MAX_YEARS, _JITTER_WEEKS = 3, 2


def _ephemeral(pub: bytes, plaintext_digest: bytes) -> str:
    """Stand in for what a site transmits: the plaintext digest RSA-OAEP-encrypted
    under our public key. Deliberately does not import site/'s tokenizers -- the
    two projects share no runtime code (see crypto.py's module docstring).
    """
    return base64.b64encode(
        load_pem_public_key(pub).encrypt(
            plaintext_digest,
            OAEP(mgf=MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
        )
    ).decode("ascii")


def test_transcrypt_in_recovers_the_original_plaintext_digest(tmp_path):
    """Simulates what a site does (RSA-OAEP-encrypt some plaintext digest under our
    public key) without depending on site/'s tokenize functions -- transcrypt_in's
    only job is: decrypt with our private key, re-encrypt under our own AES key.
    """
    priv, pub = generate_central_keys(tmp_path / "keys")
    aes_key = derive_aes_key_v2(priv)

    plaintext_digest = b"\x00" * 64  # stands in for a real sha512 digest
    pub_key_obj = load_pem_public_key(pub)
    ephemeral = base64.b64encode(
        pub_key_obj.encrypt(plaintext_digest, OAEP(mgf=MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
    ).decode("ascii")

    resolved = transcrypt_in(ephemeral, priv, aes_key)
    assert resolved is not None

    # Independently decrypt what transcrypt_in produced, to confirm it really is
    # AES-GCM-SIV(aes_key, plaintext_digest) and nothing got mangled along the way.
    recovered = AESGCMSIV(aes_key).decrypt(bytes(12), base64.b64decode(resolved), None)
    assert recovered == plaintext_digest


def test_central_keys_are_load_or_create_not_regenerated(tmp_path):
    """Re-running setup must not rotate the *published* keypair.

    This keypair is not private state: every package every site has produced is
    encrypted under the public half, and every token already resolved is
    AES-encrypted under a key derived from the private half. A generate-on-every-call
    implementation therefore turns a second `setup-keys` into silent, total loss of
    the historical corpus -- and it surfaces far from its cause, as an
    undecryptable inbox package. Found exactly that way: a verification script
    called `generate_central_keys` on the live keys dir and every existing package
    stopped resolving.
    """
    keys_dir = tmp_path / "keys"
    priv, pub = generate_central_keys(keys_dir)
    assert generate_central_keys(keys_dir) == (priv, pub)
    # Tokens minted under the first public key still resolve after a second call.
    assert transcrypt_in(_ephemeral(pub, b"\x05" * 64), priv, derive_aes_key_v2(priv)) is not None
    # Rotation remains possible, but only when asked for by name.
    assert generate_central_keys(keys_dir, force=True)[0] != priv


def test_transcrypt_in_is_deterministic(tmp_path):
    priv, pub = generate_central_keys(tmp_path / "keys")
    aes_key = derive_aes_key_v2(priv)
    pub_key_obj = load_pem_public_key(pub)

    plaintext_digest = b"\x01" * 64
    # RSA-OAEP is randomized, so two independently-produced ephemeral tokens for the
    # same plaintext will differ -- but both must resolve to the *same* value here.
    e1 = base64.b64encode(pub_key_obj.encrypt(plaintext_digest, OAEP(mgf=MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None))).decode()
    e2 = base64.b64encode(pub_key_obj.encrypt(plaintext_digest, OAEP(mgf=MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None))).decode()
    assert e1 != e2
    assert transcrypt_in(e1, priv, aes_key) == transcrypt_in(e2, priv, aes_key)


def test_transcrypt_in_none_passthrough(tmp_path):
    priv, _ = generate_central_keys(tmp_path / "keys")
    aes_key = derive_aes_key_v2(priv)
    assert transcrypt_in(None, priv, aes_key) is None


def test_transcrypt_in_failure_leaks_no_digest_into_the_traceback(tmp_path):
    """A failure *after* the RSA decrypt must not carry the digest anywhere renderable.

    The digest recovered inside transcrypt_in is an unsalted SHA-512 over
    public, low-entropy identity attributes -- dictionary-attackable by anyone
    who reads it (REPORT.md §9.2). A traceback captures frame locals, so an
    unhandled failure in that window would put it into any log or crash report
    that formats one.

    The failure is forced with a malformed AES key rather than a malformed
    token on purpose: a bad token fails in the decrypt itself, before the
    digest exists, which would pass this test vacuously. A bad AES key fails on
    the *next* statement, when `plaintext` is live -- the case that actually
    matters.
    """
    priv, pub = generate_central_keys(tmp_path / "keys")
    plaintext_digest = b"\xab\xcd" * 32
    ephemeral = base64.b64encode(
        load_pem_public_key(pub).encrypt(
            plaintext_digest,
            OAEP(mgf=MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
        )
    ).decode("ascii")

    with pytest.raises(TranscryptError) as excinfo:
        transcrypt_in(ephemeral, priv, b"\x00" * 7)  # not a valid AES-GCM-SIV key length

    # The full rendering a logger or crash reporter would produce, including
    # every chained exception and every frame -- not just str(exc).
    rendered = "".join(
        traceback.format_exception(type(excinfo.value), excinfo.value, excinfo.value.__traceback__)
    )
    for form in (plaintext_digest, plaintext_digest.hex().encode(), base64.b64encode(plaintext_digest)):
        assert form.decode("latin-1") not in rendered
    assert ephemeral not in rendered
    assert excinfo.value.__cause__ is None and excinfo.value.__context__ is None

    # The digest must also be gone from the frame locals the traceback exposes,
    # since a locals-rendering debugger reads those directly rather than via
    # format_exception.
    for frame, _ in traceback.walk_tb(excinfo.value.__traceback__):
        assert "plaintext" not in frame.f_locals


# ---- date re-offset (Req 4) ----


def test_shift_secret_is_stable_across_calls_and_independent_of_the_rsa_key(tmp_path):
    """Load-or-create, and not derived from the RSA keypair.

    Both properties are load-bearing: a regenerated secret silently re-shifts
    every date the platform has ever normalized, and deriving it from the RSA key
    would give that one file a third role (REPORT.md §9.4).
    """
    keys_dir = tmp_path / "keys"
    priv, _pub = generate_central_keys(keys_dir)
    first = generate_central_shift_secret(keys_dir)
    assert generate_central_shift_secret(keys_dir) == first
    assert (keys_dir / "central_shift_secret.bin").stat().st_mode & 0o777 == 0o600
    assert first != derive_aes_key_v2(priv)
    assert generate_central_shift_secret(keys_dir, force=True) != first


def test_per_drs_shift_keys_differ_by_request_id(tmp_path):
    secret = generate_central_shift_secret(tmp_path / "keys")
    a = derive_drs_shift_key(secret, "DC-POC-0001")
    b = derive_drs_shift_key(secret, "DC-POC-0002")
    assert a != b
    assert a == derive_drs_shift_key(secret, "DC-POC-0001")  # deterministic
    assert a != secret  # the undistributed secret is not what leaves the building


def test_shift_days_is_whole_weeks_within_bounds(tmp_path):
    """The seasonality guarantee, asserted on central's own copy of the lattice.

    `site/tests/test_reproducibility.py` asserts the same property on the site's
    transcription. Both must hold: the two copies produce identical output for
    identical input, and if they ever drift, dates stop lining up silently.
    """
    secret = generate_central_shift_secret(tmp_path / "keys")
    for i in range(64):
        digest = bytes([i]) * 64
        shift = derive_shift_days(secret, digest, _MAX_YEARS, _JITTER_WEEKS)
        assert shift % 7 == 0, (i, shift)
        assert abs(shift) <= 364 * _MAX_YEARS + 7 * _JITTER_WEEKS


def test_resolve_and_shift_returns_the_same_token_as_transcrypt_in(tmp_path):
    """The re-offset path must not perturb token resolution -- linkage depends on
    resolve_and_shift's token being byte-identical to transcrypt_in's.
    """
    priv, pub = generate_central_keys(tmp_path / "keys")
    aes_key = derive_aes_key_v2(priv)
    secret = generate_central_shift_secret(tmp_path / "keys")
    drs_key = derive_drs_shift_key(secret, "DC-POC-0001")
    ephemeral = _ephemeral(pub, b"\x07" * 64)

    resolved, delta = resolve_and_shift(
        ephemeral, priv, aes_key, drs_key, secret, _MAX_YEARS, _JITTER_WEEKS,
    )
    assert resolved == transcrypt_in(ephemeral, priv, aes_key)
    assert isinstance(delta, int)
    assert delta % 7 == 0  # a delta of two whole-week offsets is itself whole weeks


def test_resolve_and_shift_delta_lands_every_drs_on_one_global_offset(tmp_path):
    """The cross-DRS requirement, through the real RSA path. [Req 4]

    Two DRSs shift the same patient by different amounts. Applying each one's
    delta must land both on the *same* final date, and that date must be
    `true + shift_global`.

    What this test does *not* assert is that the final date differs from the true
    one. `shift_global` can legitimately draw 0 -- it is one of 35 lattice points
    -- so "never equals the true date" is not a property of the design and
    asserting it would be asserting a coincidence. The real guarantee is
    structural rather than observable in the output: no code path in this project
    computes a true date, which is why `resolve_and_shift` subtracts the two
    offsets from each other and never from the stored date. The digest below is
    chosen to have a nonzero global offset only so that the arithmetic is
    exercised non-trivially.
    """
    priv, pub = generate_central_keys(tmp_path / "keys")
    aes_key = derive_aes_key_v2(priv)
    secret = generate_central_shift_secret(tmp_path / "keys")
    true_date = date(2024, 1, 15)

    digest = next(
        bytes([i]) * 64 for i in range(256)
        if derive_shift_days(secret, bytes([i]) * 64, _MAX_YEARS, _JITTER_WEEKS) != 0
    )
    ephemeral = _ephemeral(pub, digest)
    shift_global = derive_shift_days(secret, digest, _MAX_YEARS, _JITTER_WEEKS)

    finals, deltas = [], []
    for request_id in ("DC-POC-0001", "DC-POC-0002"):
        drs_key = derive_drs_shift_key(secret, request_id)
        shift_drs = derive_shift_days(drs_key, digest, _MAX_YEARS, _JITTER_WEEKS)
        stored = true_date + timedelta(days=shift_drs)  # what the site packaged
        _resolved, delta = resolve_and_shift(
            ephemeral, priv, aes_key, drs_key, secret, _MAX_YEARS, _JITTER_WEEKS,
        )
        final = stored + timedelta(days=delta)
        assert final == true_date + timedelta(days=shift_global)
        assert final.weekday() == true_date.weekday()
        finals.append(final)
        deltas.append(delta)

    # The two DRSs really did require different corrections...
    assert deltas[0] != deltas[1]
    # ...and still converged on one date. This is the cross-DRS requirement.
    assert finals[0] == finals[1]
    assert finals[0] != true_date  # true by construction of `digest`, above


def test_resolve_and_shift_none_passthrough(tmp_path):
    priv, _pub = generate_central_keys(tmp_path / "keys")
    aes_key = derive_aes_key_v2(priv)
    secret = generate_central_shift_secret(tmp_path / "keys")
    assert resolve_and_shift(None, priv, aes_key, secret, secret, 3, 2) == (None, None)


def test_resolve_and_shift_failure_leaks_no_digest_into_the_traceback(tmp_path):
    """The §9.5 discipline must hold in the *new* window too, not just transcrypt_in's.

    resolve_and_shift holds the digest for longer than transcrypt_in does -- two
    HKDF derivations happen while `plaintext` is live -- so it has more surface,
    not less. Same forced-failure reasoning as the transcrypt_in test above: a
    malformed AES key fails on the statement *after* the decrypt, when the digest
    exists. A malformed token would fail before it does and pass vacuously.
    """
    priv, pub = generate_central_keys(tmp_path / "keys")
    secret = generate_central_shift_secret(tmp_path / "keys")
    plaintext_digest = b"\xde\xad" * 32
    ephemeral = _ephemeral(pub, plaintext_digest)

    with pytest.raises(TranscryptError) as excinfo:
        resolve_and_shift(ephemeral, priv, b"\x00" * 7, secret, secret, 3, 2)

    rendered = "".join(
        traceback.format_exception(type(excinfo.value), excinfo.value, excinfo.value.__traceback__)
    )
    for form in (plaintext_digest, plaintext_digest.hex().encode(), base64.b64encode(plaintext_digest)):
        assert form.decode("latin-1") not in rendered
    assert ephemeral not in rendered
    assert excinfo.value.__cause__ is None and excinfo.value.__context__ is None
    for frame, _ in traceback.walk_tb(excinfo.value.__traceback__):
        assert "plaintext" not in frame.f_locals
