import base64

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.padding import MGF1, OAEP
from cryptography.hazmat.primitives.ciphers.aead import AESGCMSIV
from cryptography.hazmat.primitives.serialization import load_pem_public_key

from data_counts_central.crypto import derive_aes_key_v2, generate_central_keys, transcrypt_in


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
