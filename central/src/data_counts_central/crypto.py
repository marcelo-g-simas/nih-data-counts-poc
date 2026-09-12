"""Central-platform half of the OPPRL v2 protocol. [Req 5]

Deliberately duplicated from `site/`'s copy, not imported as a shared runtime
dependency across the two projects: the whole point of Req 5 is "consistent
tokens across independently deployed, site-local instances... without
introducing a shared trust boundary outside the Healthcare Partner site."
Two independent implementations of the same protocol interoperate by
protocol-conformance, not by sharing code at runtime -- see `site/`'s
`stage6_pprl.py` docstring for the fuller rationale.

The central platform performs exactly one cryptographic operation the site
never does: `transcrypt_in` -- decrypting an ephemeral (RSA-encrypted) token
with *our own* private key and re-encrypting it under *our own* AES key.
That's what "resolves" tokens from independently-keyed sites into one
consistent, comparable token space. This module has no `tokenize_token*()`
functions at all: this project never sees raw identifying attributes, only
already-tokenized ciphertext.
"""
from __future__ import annotations

import base64
import os
from functools import lru_cache
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.padding import MGF1, OAEP
from cryptography.hazmat.primitives.ciphers.aead import AESGCMSIV
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    load_pem_private_key,
)
from spindle_token import generate_pem_keys

_ZERO_NONCE = bytes(12)  # matches site/'s choice; see that project's _crypto rationale


@lru_cache(maxsize=4)
def _load_private_key(private_key_pem: bytes):
    """Parse a PEM private key, memoized on the PEM bytes.

    Parsing an RSA PEM costs ~70 ms, against ~1 ms for the decrypt it enables.
    That ratio is irrelevant when resolving one token per patient and dominant
    when re-offsetting every clinical row of a package: unmemoized, a 14k-row
    package spends 16 minutes parsing the same key 14,000 times and 16 seconds
    doing the actual work.

    This keeps the parsed key alive in the process, which adds no exposure the
    caller did not already have -- it holds the PEM bytes themselves, and both
    live only as long as the process. It is deliberately *not* keyed on a path,
    so a rotated key on disk cannot be served from a stale cache entry.
    """
    return load_pem_private_key(private_key_pem, None)


class TranscryptError(Exception):
    """A `transcrypt_in` failure, deliberately carrying no diagnostic detail.

    Distinct type (rather than re-raising the original) so callers can catch
    this specific failure without the alternative -- letting the underlying
    `cryptography` exception propagate -- putting a digest-bearing frame into a
    traceback. See `transcrypt_in`.
    """


def derive_aes_key_v2(private_key_pem: bytes) -> bytes:
    """OPPRL v2 key derivation (spindle_token.opprl.v2._derive_aes_key, transcribed)."""
    key = load_pem_private_key(private_key_pem, None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise TypeError("Expected an RSA private key")
    canonical = key.private_bytes(
        encoding=Encoding.DER, format=PrivateFormat.PKCS8, encryption_algorithm=NoEncryption()
    )
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"opprl.v2.aes").derive(canonical)


# ---- date-shift key derivation (Req 4; NOT part of the OPPRL protocol) ----
#
# These are this program's own construction, not a transcription of anything in
# spindle-token. They live beside the OPPRL primitives because they consume the
# same value -- the token1 plaintext digest -- but they are a separate mechanism
# with a separate secret, and nothing here affects token consistency.

SHIFT_SECRET_FILENAME = "central_shift_secret.bin"

# 364 = 52 * 7. The lattice quantum is a whole number of weeks so that a shifted
# date lands on the same day of the week as the true one, and within a few days
# of the same day of the year. See REPORT.md's date-shift section for what that
# buys analytically and what it costs in entropy.
_LATTICE_QUANTUM_DAYS = 364
_DAYS_PER_WEEK = 7


def generate_central_shift_secret(keys_dir: str | Path, *, force: bool = False) -> bytes:
    """Load the central date-shift secret, generating it only if absent. [Req 4]

    Load-or-create for the same reason `site/`'s `generate_site_keys` is: this
    secret is long-lived state. Every patient's global offset is derived from it,
    so replacing it re-shifts every date the platform has ever normalized, and
    dates re-offset under the old secret stop agreeing with dates re-offset under
    the new one -- silently, since both are valid-looking calendar dates.

    Deliberately *not* derived from the RSA keypair, unlike `derive_aes_key_v2`.
    That function already gives the RSA private key a second role (at-rest token
    encryption), which `REPORT.md` §9.4 names as an unimplemented-mitigation gap;
    a third role would widen the blast radius of the same single file. This
    secret is independent random bytes, so compromise of one does not imply the
    other.
    """
    keys_dir = Path(keys_dir)
    keys_dir.mkdir(parents=True, exist_ok=True)
    path = keys_dir / SHIFT_SECRET_FILENAME
    if not force and path.exists():
        return path.read_bytes()
    secret = os.urandom(32)
    path.write_bytes(secret)
    path.chmod(0o600)
    return secret


def derive_drs_shift_key(central_shift_secret: bytes, request_id: str) -> bytes:
    """Derive the per-DRS shift key handed to every site responding to that DRS. [Req 4]

    Distributed out-of-band alongside the DRS itself. It is an opaque symmetric
    key, *not* a published public key: a site must be able to compute
    `shift_drs` from it, so any party holding it can do the same, and a party who
    also holds a candidate identity can therefore un-shift that DRS's packaged
    dates. That makes this a real secret living outside a single site's boundary
    -- the only such secret in the design -- and it is documented as one in
    `REQUIREMENTS.md`. It is not a token key: it cannot resolve, produce, or
    correlate OPPRL tokens, so it has no bearing on Req 5's irreversibility
    argument or on `REPORT.md` §9.

    Per-DRS rather than global so that a shift key disclosed by one site
    compromises only the dates of the DRS it was issued for. `central_shift_secret`
    itself is never distributed.
    """
    return HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None,
        info=b"data-counts.dateshift.drs." + request_id.encode(),
    ).derive(central_shift_secret)


def derive_shift_days(shift_key: bytes, token_digest: bytes, max_years: int, jitter_weeks: int) -> int:
    """Deterministic per-(key, identity) day offset that preserves season and weekday. [Req 4]

    `shift_days = 364 * k + 7 * j`, with `k` drawn from `[-max_years, +max_years]`
    and `j` from `[-jitter_weeks, +jitter_weeks]`. Every term is a whole number of
    weeks, which is what the two analytic guarantees rest on:

      * **Day of week is preserved exactly**, for every offset this function can
        return. That is the reason the jitter is quantized to weeks rather than
        days: a one-day jitter component would rotate Friday to Thursday and
        silently void the weekday claim, taking weekday/weekend utilization
        analysis with it. A 365-day quantum would do the same, rotating by one day
        per year of offset (365 mod 7 == 1) -- hence 364.
      * **Day of year drifts only by the jitter plus accumulated leap days**
        (roughly 1.25 days per year of offset), so season survives for every event
        except one falling within `7 * jitter_weeks` or so of a season boundary.

    The jitter exists so that two patients do not shift identically merely by
    sharing a `k`; it widens the offset space without breaking either guarantee.

    `token_digest` is the OPPRL token1 plaintext digest: the only value that is
    identical at every site for the same person *and* recoverable centrally. Two
    independently keyed sites therefore derive the *same* offset for the same
    patient without exchanging anything, which is the property this whole
    mechanism exists for.

    Honest limit: the output space is small by construction (`2*max_years+1`
    lattice points times `2*jitter_weeks+1` jitter values -- 35 offsets at the
    defaults). Season-preserving date shifts cannot be otherwise. This function
    obscures calendar anchoring; it is not a confidentiality control, and
    `REPORT.md` says so rather than implying a strength the arithmetic does not
    support.
    """
    if max_years < 0 or jitter_weeks < 0:
        raise ValueError("max_years and jitter_weeks must be non-negative")
    # Two independent draws from one HKDF output rather than two derivations:
    # 8 bytes each, non-overlapping, so k and j are not correlated.
    material = HKDF(
        algorithm=hashes.SHA256(), length=16, salt=None, info=b"data-counts.dateshift.days",
    ).derive(shift_key + token_digest)
    k = (int.from_bytes(material[:8], "big") % (2 * max_years + 1)) - max_years
    j = (int.from_bytes(material[8:], "big") % (2 * jitter_weeks + 1)) - jitter_weeks
    return _LATTICE_QUANTUM_DAYS * k + _DAYS_PER_WEEK * j


def transcrypt_in(ephemeral_token: str | None, private_key_pem: bytes, aes_key: bytes) -> str | None:
    """Decrypt an ephemeral token with our RSA private key, re-encrypt under our own AES key.

    This is the resolution step: whatever site sent it, and under whatever
    site-local AES key it started as, the plaintext digest recovered here is
    purely a function of the patient's normalized identity attributes -- so
    two sites' tokens for the same person resolve to the *same* value here,
    without either site ever learning the other's key material or this
    platform ever seeing a raw identifier.

    That last clause is true and is also weaker than it sounds: the recovered
    digest is an *unsalted* SHA-512 over public, low-entropy attributes, so it
    is dictionary-attackable by anyone who can also read it. See `REPORT.md`
    §9 for the full threat model, and the `try`/`except` below for why the two
    statements that hold it in memory are wrapped.
    """
    if ephemeral_token is None:
        return None
    priv = _load_private_key(private_key_pem)
    # A traceback captures every frame's locals, and `plaintext` here is
    # digest-equivalent to the patient's name/gender/DOB (REPORT.md §9). An
    # unhandled failure between the decrypt and the re-encrypt would carry it
    # into the traceback, and from there into any log, crash report, or
    # locals-rendering debugger that formats one. Three things below, together:
    #   - `del plaintext` unbinds it from *this* frame, which is itself in the
    #     traceback of whatever we raise;
    #   - the raise happens *outside* the `except` block, so no exception is
    #     being handled at raise time and `__context__` is never set. Doing it
    #     inside with `from None` is not equivalent: that sets
    #     `__suppress_context__`, which stops `format_exception` from
    #     *rendering* the original but leaves `__context__` populated for any
    #     handler that reads the attribute directly.
    #   - the message names neither the digest nor the input token.
    # This does not zeroize the bytes -- CPython offers no way to, which is why
    # REPORT.md §9.5's no-swap/no-core-dump posture is load-bearing rather than
    # belt-and-braces. It bounds what a *rendered traceback* can leak, not what
    # a memory dump can.
    plaintext = None
    failed_with: str | None = None
    try:
        plaintext = priv.decrypt(
            base64.b64decode(ephemeral_token),
            OAEP(mgf=MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
        )
        ciphertext = AESGCMSIV(aes_key).encrypt(_ZERO_NONCE, plaintext, None)
    except Exception as exc:  # noqa: BLE001 -- deliberately blind: any escaping
        # exception type would carry a digest-bearing frame, so breadth is the point.
        failed_with = type(exc).__name__
    finally:
        del plaintext
    if failed_with is not None:
        raise TranscryptError(
            f"transcrypt_in failed ({failed_with}); details suppressed because "
            "this frame holds identity-bearing material"
        )
    return base64.b64encode(ciphertext).decode("ascii")


def resolve_and_shift(
    ephemeral_token: str | None, private_key_pem: bytes, aes_key: bytes,
    drs_shift_key: bytes, central_shift_secret: bytes, max_years: int, jitter_weeks: int,
) -> tuple[str | None, int | None]:
    """`transcrypt_in`, plus the day delta that re-offsets this patient's dates. [Req 4, Req 5]

    Returns `(resolved_token, delta_days)`. The delta carries a site's per-DRS
    offset over to this platform's single global offset for that individual:

        stored_date = true_date + shift_drs          (applied at the site)
        delta       = shift_global - shift_drs       (computed here)
        final_date  = stored_date + delta = true_date + shift_global

    **`true_date` is never computed here, and no code path in this project
    computes one.** The arithmetic is deliberately arranged so the true date is
    not an intermediate: the two shifts are subtracted from each other, never
    from the stored date. That is the substantive difference from the more
    obvious design -- decrypt to true dates centrally, re-offset, then delete
    them -- which would put every patient's real calendar dates in this
    platform's memory and make their deletion a control that has to be trusted.
    Here there is nothing to delete.

    Because `shift_global` depends only on the identity digest, every DRS and
    every site converges on the same offset for the same individual, so
    intervals are preserved across the whole corpus and not merely within one
    submission.

    Separate function rather than a wider `transcrypt_in` return: that function's
    contract is that the plaintext digest does not escape it, and
    `test_transcrypt_in_failure_leaks_no_digest_into_the_traceback` enforces it.
    This function needs the digest, so it repeats the window and the same
    discipline -- see the comment in `transcrypt_in` for why each of the three
    parts matters, and note that the digest still never escapes *this* function
    either: only a day count derived from it does.
    """
    if ephemeral_token is None:
        return None, None
    priv = _load_private_key(private_key_pem)
    plaintext = None
    failed_with: str | None = None
    try:
        plaintext = priv.decrypt(
            base64.b64decode(ephemeral_token),
            OAEP(mgf=MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
        )
        ciphertext = AESGCMSIV(aes_key).encrypt(_ZERO_NONCE, plaintext, None)
        shift_drs = derive_shift_days(drs_shift_key, plaintext, max_years, jitter_weeks)
        shift_global = derive_shift_days(central_shift_secret, plaintext, max_years, jitter_weeks)
    except Exception as exc:  # noqa: BLE001 -- deliberately blind, same reasoning as transcrypt_in
        failed_with = type(exc).__name__
    finally:
        del plaintext
    if failed_with is not None:
        raise TranscryptError(
            f"resolve_and_shift failed ({failed_with}); details suppressed because "
            "this frame holds identity-bearing material"
        )
    return base64.b64encode(ciphertext).decode("ascii"), shift_global - shift_drs


def generate_central_keys(keys_dir: str | Path, *, force: bool = False) -> tuple[bytes, bytes]:
    """Load (or first generate) the central platform's RSA keypair. [Req 5]

    The public key half is what gets handed to Healthcare Partner sites
    out-of-band during onboarding (Req 5: "Healthcare Partner approval
    before the first live transmission").

    **Load-or-create, not generate-every-time**, matching
    `generate_central_shift_secret` and site/'s `generate_site_keys`. This
    keypair is *published*: every package every site has ever produced is
    RSA-encrypted under the public half, and every token already resolved is
    AES-encrypted under a key derived from the private half
    (`derive_aes_key_v2`). Silently rotating it therefore does not lose one
    key -- it invalidates the entire historical corpus, and the failure surfaces
    later and elsewhere, as an undecryptable inbox package rather than as an
    error at the point of rotation. `force=True` exists for the case where that
    is genuinely wanted (key compromise), so that the destructive path has to be
    asked for by name.
    """
    keys_dir = Path(keys_dir)
    keys_dir.mkdir(parents=True, exist_ok=True)
    priv_path = keys_dir / "central_private_key.pem"
    pub_path = keys_dir / "central_public_key.pem"
    if not force and priv_path.exists() and pub_path.exists():
        return priv_path.read_bytes(), pub_path.read_bytes()
    priv, pub = generate_pem_keys()
    priv_path.write_bytes(priv)
    pub_path.write_bytes(pub)
    return priv, pub
