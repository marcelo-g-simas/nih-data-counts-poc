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


def derive_aes_key_v2(private_key_pem: bytes) -> bytes:
    """OPPRL v2 key derivation (spindle_token.opprl.v2._derive_aes_key, transcribed)."""
    key = load_pem_private_key(private_key_pem, None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise TypeError("Expected an RSA private key")
    canonical = key.private_bytes(
        encoding=Encoding.DER, format=PrivateFormat.PKCS8, encryption_algorithm=NoEncryption()
    )
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"opprl.v2.aes").derive(canonical)


def transcrypt_in(ephemeral_token: str | None, private_key_pem: bytes, aes_key: bytes) -> str | None:
    """Decrypt an ephemeral token with our RSA private key, re-encrypt under our own AES key.

    This is the resolution step: whatever site sent it, and under whatever
    site-local AES key it started as, the plaintext digest recovered here is
    purely a function of the patient's normalized identity attributes -- so
    two sites' tokens for the same person resolve to the *same* value here,
    without either site ever learning the other's key material or this
    platform ever seeing a raw identifier.
    """
    if ephemeral_token is None:
        return None
    priv = load_pem_private_key(private_key_pem, None)
    plaintext = priv.decrypt(
        base64.b64decode(ephemeral_token),
        OAEP(mgf=MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
    )
    ciphertext = AESGCMSIV(aes_key).encrypt(_ZERO_NONCE, plaintext, None)
    return base64.b64encode(ciphertext).decode("ascii")


def generate_central_keys(keys_dir: str | Path) -> tuple[bytes, bytes]:
    """Generate (and persist) the central platform's RSA keypair. [Req 5]

    The public key half is what gets handed to Healthcare Partner sites
    out-of-band during onboarding (Req 5: "Healthcare Partner approval
    before the first live transmission").
    """
    keys_dir = Path(keys_dir)
    keys_dir.mkdir(parents=True, exist_ok=True)
    priv, pub = generate_pem_keys()
    (keys_dir / "central_private_key.pem").write_bytes(priv)
    (keys_dir / "central_public_key.pem").write_bytes(pub)
    return priv, pub
