"""Stage 6: OPPRL tokenization, DuckDB-native on both ends. [Req 5]

`spindle-token`'s public API (`tokenize`/`transcrypt_out`/`transcrypt_in`) is
confirmed Spark-coupled at import time (`spindle_token.opprl.*` raises
`ImportError` unless the `pyspark`-requiring `spark` extra is installed --
verified directly against the installed package, not assumed). Per the
spec's fallback clause, this module reimplements the OPPRL v2 "token1"
per-value transform (first-initial + last-name + gender + birth-date) in
pure Python, mirroring spindle-token's own source line-for-line (its
`_crypto.py` primitives, `opprl/_common.py` normalization rules, and
`opprl/v0.py::_tokenize_impl`/`_transcrypt_*_impl`, all read from the
installed 2.4.2 package) so the result is the real OPPRL v2 algorithm, not
an independent reinvention. We do reuse spindle-token's own
`generate_pem_keys()` directly -- that helper is pure `cryptography`, no
pyspark import required.

Caveat (documented, not hidden): this sandbox has no Java/JVM, so
spindle-token's real Spark path cannot be run here to produce byte-exact
reference vectors. `tests/test_pprl.py` covers determinism and the
both-ends linkage property; it does not cross-check against a live
spindle-token run. See that test file for the exact recipe to do so
wherever a Spark+Java environment is available.

Both ends of the exchange call the *same* functions in this module, which is
what guarantees token consistency across independently deployed sites
without a shared trust boundary.
"""
from __future__ import annotations

import base64
import hashlib
import re
from datetime import date
from pathlib import Path

import duckdb
import pyarrow as pa
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


# ---- tokenize / transcrypt (spindle_token.opprl.v0, transcribed) ----

def tokenize_token1(first_name: str | None, last_name: str | None, gender: str | None,
                     birth_date: str | None, aes_key: bytes) -> str | None:
    """OPPRL v2 token1: first-initial + last-name + gender + birth-date.

    Attribute IDs sort lexicographically as birth_date, first.initial,
    gender, last -- so that's the join order below, matching
    `v0._tokenize_impl`'s `sorted(attribute_ids)` behavior for this token.
    """
    values = [
        _normalize_birth_date(birth_date),
        _first_char(_normalize_name(first_name)),
        _normalize_gender(gender),
        _normalize_name(last_name),
    ]
    if any(v is None for v in values):
        return None
    plaintext = ":".join(values)
    digest = hashlib.sha512(plaintext.encode()).digest()
    ciphertext = AESGCMSIV(aes_key).encrypt(_ZERO_NONCE, digest, None)
    return base64.b64encode(ciphertext).decode("ascii")


def transcrypt_out(token: str | None, aes_key: bytes, recipient_public_key_pem: bytes) -> str | None:
    """Site side: decrypt the site-local AES token, re-encrypt under the recipient's RSA public key."""
    if token is None:
        return None
    plaintext = AESGCMSIV(aes_key).decrypt(_ZERO_NONCE, base64.b64decode(token), None)
    pub = load_pem_public_key(recipient_public_key_pem)
    ciphertext = pub.encrypt(
        plaintext,
        padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
    )
    return base64.b64encode(ciphertext).decode("ascii")


def transcrypt_in(ephemeral_token: str | None, private_key_pem: bytes, aes_key: bytes) -> str | None:
    """Program side: decrypt with our RSA private key, re-encrypt under the program's own AES key."""
    if ephemeral_token is None:
        return None
    priv = load_pem_private_key(private_key_pem, None)
    plaintext = priv.decrypt(
        base64.b64decode(ephemeral_token),
        padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
    )
    ciphertext = AESGCMSIV(aes_key).encrypt(_ZERO_NONCE, plaintext, None)
    return base64.b64encode(ciphertext).decode("ascii")


def generate_site_keys(site_dir: str | Path) -> tuple[bytes, bytes]:
    """Generate (and persist) a site-local RSA keypair. [Req 5]"""
    site_dir = Path(site_dir)
    site_dir.mkdir(parents=True, exist_ok=True)
    priv, pub = generate_pem_keys()
    (site_dir / "private_key.pem").write_bytes(priv)
    (site_dir / "public_key.pem").write_bytes(pub)
    return priv, pub


def register_udfs(con: duckdb.DuckDBPyConnection, private_key_pem: bytes,
                   recipient_public_key_pem: bytes | None = None) -> None:
    """Register the tokenize/transcrypt_out/transcrypt_in scalar UDFs on a DuckDB connection.

    Both the partner site and the program end call this same function against
    their own connection with their own key material -- identical logic on
    both ends is what guarantees token consistency without a shared trust
    boundary.
    """
    aes_key = derive_aes_key_v2(private_key_pem)
    con.create_function(
        "pprl_tokenize",
        lambda fn, ln, g, bd: tokenize_token1(fn, ln, g, bd, aes_key),
        ["VARCHAR", "VARCHAR", "VARCHAR", "VARCHAR"], "VARCHAR",
    )
    if recipient_public_key_pem is not None:
        con.create_function(
            "pprl_transcrypt_out",
            lambda t: transcrypt_out(t, aes_key, recipient_public_key_pem),
            ["VARCHAR"], "VARCHAR",
        )
    con.create_function(
        "pprl_transcrypt_in",
        lambda t: transcrypt_in(t, private_key_pem, aes_key),
        ["VARCHAR"], "VARCHAR",
    )


def tokenize_site_patients(patients: list[dict], private_key_pem: bytes) -> dict[str, str]:
    """Tokenize a site's patient roster (list of dicts with first_name/last_name/gender/birth_date/patient_id).

    Runs natively in DuckDB via a Python scalar UDF, per the hard constraint
    (no Spark/JVM anywhere, including PPRL tokenization).
    """
    con = duckdb.connect()
    register_udfs(con, private_key_pem)
    con.register("patients", pa.Table.from_pylist(patients))
    rows = con.execute(
        "SELECT patient_id, pprl_tokenize(first_name, last_name, gender, birth_date) AS token FROM patients"
    ).fetchall()
    con.close()
    return {pid: token for pid, token in rows}


def run(raw_lake_dir: str | Path = "data/lake/raw", work_dir: str | Path = "data/work",
        program_dir: str | Path = "data/work/pprl/program") -> dict[str, str]:
    """Single-site tokenization for the main pipeline: identifying attributes
    are read from the *raw* (identifiable) lake layer -- never from the
    de-identified output -- because tokenization must happen before those
    attributes are discarded. The token map is what stage 8 substitutes for
    `patient_id` in the outbound package.
    """
    import json

    import pyarrow.parquet as pq

    raw_dir, work_dir, program_dir = Path(raw_lake_dir), Path(work_dir), Path(program_dir)
    site_priv, _ = generate_site_keys(work_dir / "pprl" / "site")
    program_priv, program_pub = generate_site_keys(program_dir)

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

    site_aes_key = derive_aes_key_v2(site_priv)
    program_aes_key = derive_aes_key_v2(program_priv)
    con = duckdb.connect()
    con.create_function(
        "pprl_tokenize", lambda fn, ln, g, bd: tokenize_token1(fn, ln, g, bd, site_aes_key),
        ["VARCHAR", "VARCHAR", "VARCHAR", "VARCHAR"], "VARCHAR",
    )
    con.create_function(
        "pprl_transcrypt_out", lambda t: transcrypt_out(t, site_aes_key, program_pub), ["VARCHAR"], "VARCHAR",
    )
    con.create_function(
        "pprl_transcrypt_in", lambda t: transcrypt_in(t, program_priv, program_aes_key), ["VARCHAR"], "VARCHAR",
    )
    con.register("patients", pa.Table.from_pylist(patients))
    rows = con.execute(
        "SELECT patient_id, "
        "pprl_transcrypt_in(pprl_transcrypt_out(pprl_tokenize(first_name, last_name, gender, birth_date))) "
        "AS program_token FROM patients"
    ).fetchall()
    con.close()

    out_dir = work_dir / "pprl"
    out_dir.mkdir(parents=True, exist_ok=True)
    token_map = {pid: token for pid, token in rows}
    (out_dir / "tokens.json").write_text(json.dumps(token_map, indent=2))
    return token_map


def demo_both_ends(out_dir: str | Path = "data/work/pprl_demo") -> dict:
    """Req 5 evidence: two independently-keyed simulated sites, overlapping
    patients, both transcrypt to the program end, and only the overlapping
    identities link there. [Req 5]
    """
    out_dir = Path(out_dir)
    site_a_dir, site_b_dir, program_dir = out_dir / "site_a", out_dir / "site_b", out_dir / "program"

    site_a_priv, _ = generate_site_keys(site_a_dir)
    site_b_priv, _ = generate_site_keys(site_b_dir)
    program_priv, program_pub = generate_site_keys(program_dir)
    program_aes_key = derive_aes_key_v2(program_priv)

    shared_identities = [
        {"first_name": "Jane", "last_name": "Doe", "gender": "F", "birth_date": "1980-05-12"},
        {"first_name": "John", "last_name": "Smith", "gender": "M", "birth_date": "1975-11-03"},
    ]
    site_a_only = [{"first_name": "Alice", "last_name": "Anders", "gender": "F", "birth_date": "1990-01-01"}]
    site_b_only = [{"first_name": "Bob", "last_name": "Baker", "gender": "M", "birth_date": "1985-06-15"}]

    def site_program_tokens(patients: list[dict], site_priv: bytes) -> list[str]:
        aes_key = derive_aes_key_v2(site_priv)
        out = []
        for p in patients:
            local_token = tokenize_token1(p["first_name"], p["last_name"], p["gender"], p["birth_date"], aes_key)
            ephemeral = transcrypt_out(local_token, aes_key, program_pub)
            program_token = transcrypt_in(ephemeral, program_priv, program_aes_key)
            out.append(program_token)
        return out

    site_a_tokens = site_program_tokens(shared_identities + site_a_only, site_a_priv)
    site_b_tokens = site_program_tokens(shared_identities + site_b_only, site_b_priv)

    linked = set(site_a_tokens[: len(shared_identities)]) == set(site_b_tokens[: len(shared_identities)])
    no_false_link = not (set(site_a_tokens[len(shared_identities):]) & set(site_b_tokens[len(shared_identities):]))

    return {
        "site_a_tokens": site_a_tokens,
        "site_b_tokens": site_b_tokens,
        "shared_identities_linked": linked,
        "non_overlapping_do_not_link": no_false_link,
    }
