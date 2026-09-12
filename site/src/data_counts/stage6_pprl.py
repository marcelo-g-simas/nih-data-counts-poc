"""Stage 6: OPPRL tokenization -- SITE side only. [Req 5]

This project (`site/`) is one of two independently deployed, independently
dependency-managed projects in this repo (see `central/` for the other) --
deliberately split apart, not just organized as subpackages, because the
whole point of Req 5 is "consistent tokens across independently deployed,
site-local instances... without introducing a shared trust boundary outside
the Healthcare Partner site." The crypto primitives below are intentionally
duplicated (not imported as a shared runtime dependency) in `central/`'s own
copy: two sites that both happen to implement the same OPPRL protocol
correctly produce compatible tokens by construction, not because they share
code at runtime. That's the actual property Req 5 is asking for.

The site's role is exactly two operations: `tokenize_token*()` (needs the
raw identifying attributes, so it must run here, before de-identification
throws them away) and `transcrypt_out()` (re-encrypts under the central
platform's RSA public key so only ciphertext leaves the site). `transcrypt_in()`
-- decrypting with the *recipient's* private key -- is a central-platform-only
operation and does not exist in this project at all.

`spindle-token`'s public API (`tokenize`/`transcrypt_out`/`transcrypt_in`) is
confirmed Spark-coupled at import time (`spindle_token.opprl.*` raises
`ImportError` unless the `pyspark`-requiring `spark` extra is installed --
verified directly against the installed package, not assumed). Per the
spec's fallback clause, this module reimplements the OPPRL v2 token
transforms in pure Python, mirroring spindle-token's own source line-for-line
(its `_crypto.py` primitives and `opprl/v0.py::_tokenize_impl`/
`_transcrypt_out_impl`, read from the installed 2.4.2 package) so the result
is the real OPPRL v2 algorithm, not an independent reinvention. Token2/token3's
soundex/metaphone normalization uses `jellyfish` -- the same library
spindle-token's own `_utils.py` uses for the identical purpose. We do reuse
spindle-token's own `generate_pem_keys()` directly, since that helper is pure
`cryptography`, no pyspark import required.

Caveat (documented, not hidden): this sandbox has no Java/JVM, so
spindle-token's real Spark path cannot be run here to produce byte-exact
reference vectors. `tests/test_pprl.py` covers determinism; it does not
cross-check against a live spindle-token run.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from datetime import date
from pathlib import Path

import duckdb
import jellyfish
import pyarrow as pa
import pyarrow.parquet as pq
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCMSIV
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    load_pem_private_key,
    load_pem_public_key,
)
from spindle_token import generate_pem_keys

from .drs import DataRequestSpecification, DateShift

_ZERO_NONCE = bytes(12)  # spindle-token intentionally reuses a fixed nonce for AES-GCM-SIV;
                          # see _crypto.py's DEFAULT_NONCE comment on why that's safe for this scheme.

TOKEN_NAMES = ("token1", "token2", "token3")

# The program's public key is the one input this pipeline needs from outside its
# own project. The repo-relative default works for a source checkout, where
# `central/` is a sibling directory; it cannot work inside the container image,
# where nothing above /app exists. DATA_COUNTS_CENTRAL_PUBLIC_KEY is how a
# containerized or otherwise relocated deployment points at a mounted key --
# which is also the realistic shape of it, since at a real site the program's
# key arrives as a file the operator installs, not as a sibling checkout.
CENTRAL_PUBLIC_KEY_ENV = "DATA_COUNTS_CENTRAL_PUBLIC_KEY"
DEFAULT_CENTRAL_PUBLIC_KEY = "../central/keys/central_public_key.pem"


def default_central_public_key_path() -> str:
    return os.environ.get(CENTRAL_PUBLIC_KEY_ENV) or DEFAULT_CENTRAL_PUBLIC_KEY


# ---- attribute normalization (spindle_token.opprl._common, transcribed) ----

def _normalize_text(raw: str | None) -> str | None:
    if raw is None:
        return None
    s = re.sub(r"\s+", " ", raw.upper()).strip()
    return s or None


def _normalize_name(raw: str | None) -> str | None:
    if raw is None:
        return None
    return _normalize_text(re.sub(r"[^a-zA-Z ]", "", raw))


def _first_char(s: str | None) -> str | None:
    return s[0] if s else None


def _soundex_name(raw: str | None) -> str | None:
    """OPPRL token2's name attribute: soundex of the normalized name.

    Tolerant of spelling variants that sound alike (e.g. "Smith"/"Smyth").
    """
    name = _normalize_name(raw)
    return jellyfish.soundex(name) if name else None


def _metaphone_name(raw: str | None) -> str | None:
    """OPPRL token3's name attribute: metaphone of the normalized name."""
    name = _normalize_name(raw)
    return jellyfish.metaphone(name) if name else None


_GENDER_LOOKUP = {"F": "F", "W": "F", "G": "F", "M": "M", "B": "M"}


def _normalize_gender(raw: str | None) -> str | None:
    if raw is None:
        return None
    fc = _first_char(_normalize_text(raw))
    return _GENDER_LOOKUP.get(fc, "O")


def _normalize_birth_date(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw)[:10]).isoformat()
    except ValueError:
        return None


# ---- key derivation (spindle_token.opprl.v2._derive_aes_key, transcribed) ----

def derive_aes_key_v2(private_key_pem: bytes) -> bytes:
    key = load_pem_private_key(private_key_pem, None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise TypeError("Expected an RSA private key")
    canonical = key.private_bytes(
        encoding=Encoding.DER, format=PrivateFormat.PKCS8, encryption_algorithm=NoEncryption()
    )
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"opprl.v2.aes").derive(canonical)


# ---- date-shift derivation (Req 4; this program's own, NOT part of OPPRL) ----
#
# Transcribed from `central/`'s `crypto.py`, the same way `derive_aes_key_v2`
# above is transcribed from upstream: the two projects share no runtime code, so
# a site and the central platform agree on a patient's offset by both
# implementing the same construction. Keep the two copies identical -- if they
# drift, dates silently stop lining up rather than failing loudly, which is the
# worst failure mode available here.

_LATTICE_QUANTUM_DAYS = 364  # 52 weeks; see central/crypto.derive_shift_days
_DAYS_PER_WEEK = 7


def token1_digest(first_name: str | None, last_name: str | None, gender: str | None,
                   birth_date: str | None) -> bytes | None:
    """The OPPRL token1 *plaintext* digest -- the per-identity date-shift key. [Req 4]

    Exactly the value `_tokenize` hashes for token1, over the same normalized
    attributes in the same sorted-attribute-id order, before AES encryption. It
    is what the central platform recovers in `transcrypt_in`, which is precisely
    why it is the shift key: it is the only value identical at every site for the
    same person *and* available on both sides of the boundary.

    Returns None on any missing attribute, matching `_tokenize` -- a patient with
    no last name has no token1 and therefore no shift, and stage 5 excludes them
    rather than inventing one.

    This value never leaves the site. It is not packaged, not written to disk, and
    not part of the token map; only the integer day count derived from it is. It
    is also an unsalted digest over public, low-entropy attributes -- see
    `REPORT.md` §9 for what that means and why it is a property of the OPPRL
    protocol rather than of this transcription.
    """
    values = [
        _normalize_birth_date(birth_date),
        _first_char(_normalize_name(first_name)),
        _normalize_gender(gender),
        _normalize_name(last_name),
    ]
    if any(v is None for v in values):
        return None
    return hashlib.sha512(":".join(values).encode()).digest()


def derive_shift_days(shift_key: bytes, token_digest: bytes, max_years: int, jitter_weeks: int) -> int:
    """Season- and weekday-preserving day offset. [Req 4]

    `364 * k + 7 * j` -- every term a whole number of weeks, which is what makes
    the day of the week survive exactly. Transcribed from `central/`'s
    `crypto.derive_shift_days`; that docstring carries the full reasoning for the
    lattice, and both copies must produce identical output for identical input.
    """
    if max_years < 0 or jitter_weeks < 0:
        raise ValueError("max_years and jitter_weeks must be non-negative")
    material = HKDF(
        algorithm=hashes.SHA256(), length=16, salt=None, info=b"data-counts.dateshift.days",
    ).derive(shift_key + token_digest)
    k = (int.from_bytes(material[:8], "big") % (2 * max_years + 1)) - max_years
    j = (int.from_bytes(material[8:], "big") % (2 * jitter_weeks + 1)) - jitter_weeks
    return _LATTICE_QUANTUM_DAYS * k + _DAYS_PER_WEEK * j


# ---- tokenize / transcrypt_out (spindle_token.opprl.v0, transcribed) ----

def _tokenize(values: list[str | None], aes_key: bytes) -> str | None:
    """Shared token-construction logic: sha512(joined normalized attrs), AES-GCM-SIV encrypt, base64.

    `values` must already be in the token's own sorted-attribute-id order
    (see each `tokenize_token*` docstring for why that order is what it is).
    """
    if any(v is None for v in values):
        return None
    plaintext = ":".join(values)
    digest = hashlib.sha512(plaintext.encode()).digest()
    ciphertext = AESGCMSIV(aes_key).encrypt(_ZERO_NONCE, digest, None)
    return base64.b64encode(ciphertext).decode("ascii")


def tokenize_token1(first_name: str | None, last_name: str | None, gender: str | None,
                     birth_date: str | None, aes_key: bytes) -> str | None:
    """OPPRL v2 token1: first-initial + last-name(exact) + gender + birth-date.

    Attribute IDs sort lexicographically as birth_date, first.initial,
    gender, last -- so that's the join order below, matching
    `v0._tokenize_impl`'s `sorted(attribute_ids)` behavior for this token.
    Breaks on any exact-spelling difference in the last name.
    """
    return _tokenize([
        _normalize_birth_date(birth_date),
        _first_char(_normalize_name(first_name)),
        _normalize_gender(gender),
        _normalize_name(last_name),
    ], aes_key)


def tokenize_token2(first_name: str | None, last_name: str | None, gender: str | None,
                     birth_date: str | None, aes_key: bytes) -> str | None:
    """OPPRL v2 token2: first-soundex + last-soundex + gender + birth-date.

    Sort order: birth_date, first.soundex, gender, last.soundex (same
    alphabetical pattern as token1 -- "first.X" < "gender" < "last.X" holds
    for any suffix X). Tolerant of last-name misspellings that sound the same.
    """
    return _tokenize([
        _normalize_birth_date(birth_date),
        _soundex_name(first_name),
        _normalize_gender(gender),
        _soundex_name(last_name),
    ], aes_key)


def tokenize_token3(first_name: str | None, last_name: str | None, gender: str | None,
                     birth_date: str | None, aes_key: bytes) -> str | None:
    """OPPRL v2 token3: first-metaphone + last-metaphone + gender + birth-date."""
    return _tokenize([
        _normalize_birth_date(birth_date),
        _metaphone_name(first_name),
        _normalize_gender(gender),
        _metaphone_name(last_name),
    ], aes_key)


_TOKENIZERS = {"token1": tokenize_token1, "token2": tokenize_token2, "token3": tokenize_token3}


def stable_diff_key(token: str | None) -> str | None:
    """A site-local, deterministic identity key for a *site-local* token.

    Exists because `transcrypt_out` is deliberately non-deterministic (see its
    docstring): the ephemeral token cannot serve as a stable per-record key
    across refreshes, so anything that needs to recognize "this is the same
    patient as last run" must key off the pre-transcrypt token instead.

    This value never leaves the site -- it is not transmitted, not packaged, and
    not resolvable by the central platform. It is a SHA-256 over an already
    irreversible token, so it discloses nothing the token itself doesn't; it is
    hashed rather than used raw only so that site-local refresh state (which
    lives on disk in cleartext) never holds the AES-encrypted token verbatim.
    """
    if token is None:
        return None
    return hashlib.sha256(token.encode()).hexdigest()


def transcrypt_out(token: str | None, aes_key: bytes, recipient_public_key_pem: bytes) -> str | None:
    """Decrypt the site-local AES token, re-encrypt under the recipient's RSA public key.

    This is the *only* cross-boundary operation the site performs: only
    ciphertext (encrypted under the central platform's own public key) ever
    leaves the site. Decrypting it back down (`transcrypt_in`) requires the
    central platform's private key, which this project never holds.

    NOTE -- the return value is deliberately NOT deterministic. RSA-OAEP mixes a
    fresh random seed into every encryption (EME-OAEP encoding), so the same
    token re-encrypted under the same public key yields different ciphertext
    each call. That is required for RSA to be semantically secure -- a
    deterministic RSA envelope would let anyone holding the public key confirm a
    guessed plaintext by encrypting it themselves -- and it matches upstream
    spindle-token (`_crypto.py`'s `encrypt_rsa`, `opprl/v0.py::_transcrypt_out_impl`).
    Determinism lives one layer down, in the AES-GCM-SIV token that `_tokenize`
    produces; that is the layer OPPRL's linkage property depends on, and the
    central platform restores determinism in `transcrypt_in` by re-encrypting
    under its own AES key. Callers needing a stable per-record key must use
    `stable_diff_key` on the *site-local* token, never this function's output.
    """
    if token is None:
        return None
    plaintext = AESGCMSIV(aes_key).decrypt(_ZERO_NONCE, base64.b64decode(token), None)
    pub = load_pem_public_key(recipient_public_key_pem)
    ciphertext = pub.encrypt(
        plaintext,
        padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
    )
    return base64.b64encode(ciphertext).decode("ascii")


def generate_site_keys(site_dir: str | Path, *, force: bool = False) -> tuple[bytes, bytes]:
    """Load the site-local RSA keypair, generating it only if absent. [Req 5]

    The site key is *long-lived state*, not per-run state. Because
    `derive_aes_key_v2` derives the token-encrypting AES key from this key's DER
    encoding, regenerating it silently changes every OPPRL token this site has
    ever produced -- so a site that regenerated per run would report its entire
    cohort as simultaneously added and deleted on every refresh (Req 8's change
    classes), and could never satisfy Req 5's "consistent tokens across
    independent site deployments" across time. Load-or-create is what makes the
    determinism the tokens already have observable across process boundaries.

    `force=True` provisions a fresh keypair, overwriting any existing one. That
    is a deliberate, destructive re-key: every previously transmitted token
    becomes unlinkable to anything this site produces afterward. It exists for
    tests that need an isolated key and for a genuine operator-driven re-key.
    """
    site_dir = Path(site_dir)
    site_dir.mkdir(parents=True, exist_ok=True)
    priv_path, pub_path = site_dir / "private_key.pem", site_dir / "public_key.pem"

    if not force and priv_path.exists() and pub_path.exists():
        return priv_path.read_bytes(), pub_path.read_bytes()

    priv, pub = generate_pem_keys()
    priv_path.write_bytes(priv)
    pub_path.write_bytes(pub)
    # 0600: the site private key is the root of the token namespace. Anyone who
    # reads it can re-derive the AES key and thus recompute any token this site
    # emits, which is the whole irreversibility property Req 5 rests on.
    priv_path.chmod(0o600)
    return priv, pub


def load_central_public_key(path: str | Path) -> bytes:
    """Read the central platform's public key -- handed to the site out-of-band
    during onboarding in a real deployment (matches Req 5's "Healthcare Partner
    approval before the first live transmission"). Here, read from a file path.
    """
    return Path(path).read_bytes()


def register_udfs(con: duckdb.DuckDBPyConnection, private_key_pem: bytes,
                   recipient_public_key_pem: bytes) -> None:
    """Register the tokenize_token1/2/3 + transcrypt_out scalar UDFs on a DuckDB connection.

    Runs natively in DuckDB via Python scalar UDFs, per the hard constraint
    (no Spark/JVM anywhere, including PPRL tokenization).
    """
    aes_key = derive_aes_key_v2(private_key_pem)
    for name, fn in _TOKENIZERS.items():
        def _make_tokenizer(tokenize_fn):
            return lambda fn_val, ln, g, bd: tokenize_fn(fn_val, ln, g, bd, aes_key)

        con.create_function(
            f"pprl_tokenize_{name}", _make_tokenizer(fn),
            ["VARCHAR", "VARCHAR", "VARCHAR", "VARCHAR"], "VARCHAR",
        )
    con.create_function(
        "pprl_transcrypt_out",
        lambda t: transcrypt_out(t, aes_key, recipient_public_key_pem),
        ["VARCHAR"], "VARCHAR",
    )
    con.create_function("pprl_stable_diff_key", stable_diff_key, ["VARCHAR"], "VARCHAR")


def tokenize_and_transcrypt(patients: list[dict], site_priv: bytes, central_pub: bytes,
                             date_shift: DateShift | None = None,
                             drs_shift_key: bytes | None = None) -> dict[str, dict[str, str]]:
    """Tokenize a site's patient roster (dicts with first_name/last_name/gender/birth_date/patient_id)
    into all three token variants, then transcrypt_out each under the central platform's public key.

    Returns {patient_id: {"token1": ephemeral, "token2": ephemeral, "token3": ephemeral,
    "_diff_key": stable}}. The raw `patient_id` here is this function's own
    internal join key for building the return dict -- it is never itself
    transmitted anywhere; see stage8_package.py for how the outbound package
    drops it, along with the underscore-prefixed site-local `_diff_key`.

    When `date_shift` and `drs_shift_key` are supplied, each entry also carries
    `_shift_days`: the per-patient offset stage 5 applies to every date of that
    patient (Req 4). Underscore-prefixed by the same convention as `_diff_key`,
    which is what keeps it site-local -- `stage8_package.py` strips those keys
    before packaging, so the offset is never transmitted even though the shifted
    dates it produced are. Both arguments are optional so the linkage demo, which
    exports a roster with no dates at all, needs no shift key.
    """
    con = duckdb.connect()
    register_udfs(con, site_priv, central_pub)
    con.register("patients", pa.Table.from_pylist(patients))
    # token1 is computed once and used twice -- transcrypted for transmission and
    # hashed for the site-local refresh key -- via a CTE rather than two calls,
    # since a second pprl_tokenize_token1 call would be wasted work.
    rows = con.execute(
        "WITH t AS (SELECT patient_id, "
        "  pprl_tokenize_token1(first_name, last_name, gender, birth_date) AS local1, "
        "  pprl_tokenize_token2(first_name, last_name, gender, birth_date) AS local2, "
        "  pprl_tokenize_token3(first_name, last_name, gender, birth_date) AS local3 "
        "FROM patients) "
        "SELECT patient_id, "
        "pprl_transcrypt_out(local1) AS token1, "
        "pprl_transcrypt_out(local2) AS token2, "
        "pprl_transcrypt_out(local3) AS token3, "
        "pprl_stable_diff_key(local1) AS diff_key "
        "FROM t"
    ).fetchall()
    con.close()
    token_map = {
        pid: {"token1": t1, "token2": t2, "token3": t3, "_diff_key": dk}
        for pid, t1, t2, t3, dk in rows
    }

    if date_shift is not None and drs_shift_key is not None:
        # Computed here in Python, from the roster, rather than as a DuckDB UDF:
        # the digest is identity-bearing (REPORT.md §9), and a UDF would hand it
        # across the Python/DuckDB boundary and into a query result for no gain.
        # Only the derived integer reaches the token map.
        for p in patients:
            digest = token1_digest(p.get("first_name"), p.get("last_name"),
                                    p.get("gender"), p.get("birth_date"))
            entry = token_map.get(p["patient_id"])
            if entry is None:
                continue
            entry["_shift_days"] = (
                None if digest is None
                else derive_shift_days(drs_shift_key, digest,
                                        date_shift.max_years, date_shift.jitter_weeks)
            )
    return token_map


def load_drs_shift_key(path: str | Path) -> bytes:
    """Read the per-DRS date-shift key, handed to the site out-of-band with the DRS. [Req 4]

    Issued by `data-counts-central issue-drs-shift-key <request_id>`. Every site
    responding to a given DRS receives the same key, which is what makes their
    offsets agree; see `central/`'s `derive_drs_shift_key` for why it is a real
    secret and what its disclosure would cost.
    """
    return Path(path).read_bytes()


DRS_SHIFT_KEY_ENV = "DATA_COUNTS_DRS_SHIFT_KEY"
DEFAULT_DRS_SHIFT_KEY = "../central/keys/drs_shift_key.bin"


def default_drs_shift_key_path() -> str:
    """Same relocation story as `default_central_public_key_path`: a source
    checkout finds `central/` as a sibling, a container mounts the key instead.
    """
    return os.environ.get(DRS_SHIFT_KEY_ENV) or DEFAULT_DRS_SHIFT_KEY


def run(drs: DataRequestSpecification | None = None, raw_lake_dir: str | Path = "data/lake/raw",
        work_dir: str | Path = "data/work",
        central_public_key_path: str | Path | None = None,
        site_keys_dir: str | Path | None = None,
        drs_shift_key_path: str | Path | None = None) -> dict[str, dict[str, str]]:
    """Single-site tokenization for the main pipeline: identifying attributes
    are read from the *raw* (identifiable) lake layer -- never from the
    de-identified output -- because tokenization must happen before those
    attributes are discarded. The ephemeral (RSA-encrypted) token map is what
    stage 8 packages into the outbound artifact; only the central platform,
    with its own private key, can ever resolve them into linkable tokens.

    When `drs` is given, each token-map entry also carries the site-local
    `_shift_days` that stage 5 consumes (Req 4), and the output is namespaced
    under `data/work/<request_id>/`. This stage therefore runs *before* stage 5
    in the pipeline -- it produces the shift map that de-identification applies.
    """
    raw_dir, work_dir = Path(raw_lake_dir), Path(work_dir)
    # The site keypair is deliberately NOT namespaced per DRS, unlike the
    # intermediates below. It is the root of this site's token namespace, and
    # per-DRS keys would give the same patient a different token in every DRS --
    # destroying exactly the cross-DRS linkage Req 5 asks for. See
    # generate_site_keys on why it is long-lived state.
    out_root = work_dir / drs.request_id if drs is not None else work_dir
    site_priv, _ = generate_site_keys(site_keys_dir or (work_dir / "pprl" / "site"))
    central_pub = load_central_public_key(central_public_key_path or default_central_public_key_path())

    date_shift = drs.deidentification.date_shift if drs is not None else None
    drs_shift_key = (
        load_drs_shift_key(drs_shift_key_path or default_drs_shift_key_path())
        if drs is not None else None
    )

    patient_table = pq.read_table(raw_dir / "Patient.parquet")
    patients = []
    for row_json in patient_table.column("__source_json").to_pylist():
        r = json.loads(row_json)
        name = (r.get("name") or [{}])[0]
        patients.append({
            "patient_id": r["id"],
            "first_name": (name.get("given") or [None])[0],
            "last_name": name.get("family"),
            "gender": r.get("gender"),
            "birth_date": r.get("birthDate"),
        })

    token_map = tokenize_and_transcrypt(patients, site_priv, central_pub, date_shift, drs_shift_key)

    out_dir = out_root / "pprl"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "tokens_ephemeral.json").write_text(json.dumps(token_map, indent=2))
    return token_map


def export_site_roster(patients: list[dict], site_dir: str | Path, central_public_key_path: str | Path) -> Path:
    """Export a lightweight `patient.parquet` (ephemeral token1/2/3 columns only) for the
    central platform to ingest -- used by the noisy-linkage validation demo, and
    structurally identical to what stage8_package.py produces for the real pipeline.
    """
    site_dir = Path(site_dir)
    site_priv, _ = generate_site_keys(site_dir / "keys")
    central_pub = load_central_public_key(central_public_key_path)
    token_map = tokenize_and_transcrypt(patients, site_priv, central_pub)

    out_path = site_dir / "patient.parquet"
    # Underscore-prefixed keys are site-local (`_diff_key`, and `_shift_days` when
    # a DRS shift key was supplied) and must not cross the boundary -- same
    # exclusion stage8_package.py applies.
    rows = [
        {"local_patient_id": pid, **{k: v for k, v in tokens.items() if not k.startswith("_")}}
        for pid, tokens in token_map.items()
    ]
    pq.write_table(pa.Table.from_pylist(rows), out_path)
    return out_path
