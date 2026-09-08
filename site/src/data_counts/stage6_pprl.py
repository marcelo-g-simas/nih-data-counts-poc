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


def transcrypt_out(token: str | None, aes_key: bytes, recipient_public_key_pem: bytes) -> str | None:
    """Decrypt the site-local AES token, re-encrypt under the recipient's RSA public key.

    This is the *only* cross-boundary operation the site performs: only
    ciphertext (encrypted under the central platform's own public key) ever
    leaves the site. Decrypting it back down (`transcrypt_in`) requires the
    central platform's private key, which this project never holds.
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


def generate_site_keys(site_dir: str | Path) -> tuple[bytes, bytes]:
    """Generate (and persist) a site-local RSA keypair. [Req 5]"""
    site_dir = Path(site_dir)
    site_dir.mkdir(parents=True, exist_ok=True)
    priv, pub = generate_pem_keys()
    (site_dir / "private_key.pem").write_bytes(priv)
    (site_dir / "public_key.pem").write_bytes(pub)
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


def tokenize_and_transcrypt(patients: list[dict], site_priv: bytes, central_pub: bytes) -> dict[str, dict[str, str]]:
    """Tokenize a site's patient roster (dicts with first_name/last_name/gender/birth_date/patient_id)
    into all three token variants, then transcrypt_out each under the central platform's public key.

    Returns {patient_id: {"token1": ephemeral, "token2": ephemeral, "token3": ephemeral}}.
    The raw `patient_id` here is this function's own internal join key for
    building the return dict -- it is never itself transmitted anywhere; see
    stage8_package.py for how the outbound package drops it.
    """
    con = duckdb.connect()
    register_udfs(con, site_priv, central_pub)
    con.register("patients", pa.Table.from_pylist(patients))
    rows = con.execute(
        "SELECT patient_id, "
        "pprl_transcrypt_out(pprl_tokenize_token1(first_name, last_name, gender, birth_date)) AS token1, "
        "pprl_transcrypt_out(pprl_tokenize_token2(first_name, last_name, gender, birth_date)) AS token2, "
        "pprl_transcrypt_out(pprl_tokenize_token3(first_name, last_name, gender, birth_date)) AS token3 "
        "FROM patients"
    ).fetchall()
    con.close()
    return {pid: {"token1": t1, "token2": t2, "token3": t3} for pid, t1, t2, t3 in rows}


def run(raw_lake_dir: str | Path = "data/lake/raw", work_dir: str | Path = "data/work",
        central_public_key_path: str | Path | None = None,
        site_keys_dir: str | Path | None = None) -> dict[str, dict[str, str]]:
    """Single-site tokenization for the main pipeline: identifying attributes
    are read from the *raw* (identifiable) lake layer -- never from the
    de-identified output -- because tokenization must happen before those
    attributes are discarded. The ephemeral (RSA-encrypted) token map is what
    stage 8 packages into the outbound artifact; only the central platform,
    with its own private key, can ever resolve them into linkable tokens.
    """
    raw_dir, work_dir = Path(raw_lake_dir), Path(work_dir)
    site_priv, _ = generate_site_keys(site_keys_dir or (work_dir / "pprl" / "site"))
    central_pub = load_central_public_key(central_public_key_path or default_central_public_key_path())

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

    token_map = tokenize_and_transcrypt(patients, site_priv, central_pub)

    out_dir = work_dir / "pprl"
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
    rows = [{"local_patient_id": pid, **tokens} for pid, tokens in token_map.items()]
    pq.write_table(pa.Table.from_pylist(rows), out_path)
    return out_path
