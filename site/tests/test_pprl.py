from data_counts.stage6_pprl import (
    derive_aes_key_v2,
    generate_site_keys,
    stable_diff_key,
    tokenize_token1,
    tokenize_token2,
    tokenize_token3,
    transcrypt_out,
)


def test_tokenize_is_deterministic(tmp_path):
    priv, _ = generate_site_keys(tmp_path / "site")
    key = derive_aes_key_v2(priv)
    t1 = tokenize_token1("Jane", "Doe", "F", "1980-05-12", key)
    t2 = tokenize_token1("Jane", "Doe", "F", "1980-05-12", key)
    assert t1 == t2
    assert t1 is not None


def test_tokenize_normalizes_case_and_whitespace(tmp_path):
    priv, _ = generate_site_keys(tmp_path / "site")
    key = derive_aes_key_v2(priv)
    t1 = tokenize_token1("jane", "doe", "female", "1980-05-12", key)
    t2 = tokenize_token1("JANE", "DOE", "F", "1980-05-12", key)
    assert t1 == t2  # both normalize to first-initial J / DOE / gender F


def test_tokenize_null_on_missing_attribute(tmp_path):
    priv, _ = generate_site_keys(tmp_path / "site")
    key = derive_aes_key_v2(priv)
    assert tokenize_token1(None, "Doe", "F", "1980-05-12", key) is None


def test_token2_and_token3_tolerate_a_soundalike_last_name(tmp_path):
    priv, _ = generate_site_keys(tmp_path / "site")
    key = derive_aes_key_v2(priv)

    # token1 (exact last name) must break on the misspelling...
    assert tokenize_token1("John", "Smith", "M", "1975-11-03", key) != \
        tokenize_token1("John", "Smyth", "M", "1975-11-03", key)

    # ...but token2 (soundex) and token3 (metaphone) must still agree.
    assert tokenize_token2("John", "Smith", "M", "1975-11-03", key) == \
        tokenize_token2("John", "Smyth", "M", "1975-11-03", key)
    assert tokenize_token3("John", "Smith", "M", "1975-11-03", key) == \
        tokenize_token3("John", "Smyth", "M", "1975-11-03", key)


def test_token2_and_token3_still_differ_for_unrelated_names(tmp_path):
    priv, _ = generate_site_keys(tmp_path / "site")
    key = derive_aes_key_v2(priv)
    assert tokenize_token2("Alice", "Anders", "F", "1990-01-01", key) != \
        tokenize_token2("Bob", "Baker", "M", "1985-06-15", key)
    assert tokenize_token3("Alice", "Anders", "F", "1990-01-01", key) != \
        tokenize_token3("Bob", "Baker", "M", "1985-06-15", key)


def test_transcrypt_out_is_decryptable_only_by_the_recipient(tmp_path):
    """The site can produce an ephemeral token; it cannot resolve it back itself
    (it never holds the recipient's private key) -- transcrypt_in doesn't exist
    in this project at all. This test only checks transcrypt_out runs and
    produces ciphertext distinct from the input; resolution is central/'s job
    and is tested there.
    """
    site_priv, _ = generate_site_keys(tmp_path / "site")
    _recipient_priv, recipient_pub = generate_site_keys(tmp_path / "recipient")
    site_key = derive_aes_key_v2(site_priv)

    token = tokenize_token1("Jane", "Doe", "F", "1980-05-12", site_key)
    ephemeral = transcrypt_out(token, site_key, recipient_pub)
    assert ephemeral is not None
    assert ephemeral != token


def test_transcrypt_out_is_randomized_per_call(tmp_path):
    """RSA-OAEP mixes a fresh seed into every encryption, so the same token
    re-encrypted under the same key must NOT repeat. This is a security property,
    not a defect -- a deterministic RSA envelope would let a public-key holder
    confirm a guessed plaintext. Pinned as a test so nobody "fixes" the
    non-determinism by weakening the padding; use stable_diff_key for stability.
    """
    site_priv, _ = generate_site_keys(tmp_path / "site")
    _recipient_priv, recipient_pub = generate_site_keys(tmp_path / "recipient")
    site_key = derive_aes_key_v2(site_priv)

    token = tokenize_token1("Jane", "Doe", "F", "1980-05-12", site_key)
    assert transcrypt_out(token, site_key, recipient_pub) != \
        transcrypt_out(token, site_key, recipient_pub)


def test_stable_diff_key_is_deterministic_and_not_the_token(tmp_path):
    """The site-local refresh key must be stable across calls (that is the whole
    point) and must not be the token itself.
    """
    priv, _ = generate_site_keys(tmp_path / "site")
    key = derive_aes_key_v2(priv)
    token = tokenize_token1("Jane", "Doe", "F", "1980-05-12", key)

    assert stable_diff_key(token) == stable_diff_key(token)
    assert stable_diff_key(token) != token
    assert stable_diff_key(None) is None
    # distinct patients get distinct keys
    other = tokenize_token1("Bob", "Baker", "M", "1985-06-15", key)
    assert stable_diff_key(other) != stable_diff_key(token)


def test_generate_site_keys_persists_across_calls(tmp_path):
    """Load-or-create: a second call must return the SAME keypair, because the
    derived AES key -- and therefore every token this site has ever emitted --
    depends on it. Regenerating per run was the bug that made stage 8 report the
    whole cohort as added-and-deleted on every refresh.
    """
    priv1, pub1 = generate_site_keys(tmp_path / "site")
    priv2, pub2 = generate_site_keys(tmp_path / "site")
    assert priv1 == priv2
    assert pub1 == pub2
    assert derive_aes_key_v2(priv1) == derive_aes_key_v2(priv2)

    token_a = tokenize_token1("Jane", "Doe", "F", "1980-05-12", derive_aes_key_v2(priv1))
    token_b = tokenize_token1("Jane", "Doe", "F", "1980-05-12", derive_aes_key_v2(priv2))
    assert token_a == token_b


def test_generate_site_keys_force_rekeys_and_invalidates_tokens(tmp_path):
    """force=True is a deliberate destructive re-key: previously emitted tokens
    become unlinkable to anything produced afterward.
    """
    priv1, _ = generate_site_keys(tmp_path / "site")
    priv2, _ = generate_site_keys(tmp_path / "site", force=True)
    assert priv1 != priv2

    token_before = tokenize_token1("Jane", "Doe", "F", "1980-05-12", derive_aes_key_v2(priv1))
    token_after = tokenize_token1("Jane", "Doe", "F", "1980-05-12", derive_aes_key_v2(priv2))
    assert token_before != token_after


def test_site_private_key_is_not_group_or_world_readable(tmp_path):
    priv_path = tmp_path / "site" / "private_key.pem"
    generate_site_keys(tmp_path / "site")
    assert priv_path.stat().st_mode & 0o077 == 0
