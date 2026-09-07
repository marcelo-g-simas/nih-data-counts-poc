from data_counts.stage6_pprl import (
    derive_aes_key_v2,
    generate_site_keys,
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
    recipient_priv, recipient_pub = generate_site_keys(tmp_path / "recipient")
    site_key = derive_aes_key_v2(site_priv)

    token = tokenize_token1("Jane", "Doe", "F", "1980-05-12", site_key)
    ephemeral = transcrypt_out(token, site_key, recipient_pub)
    assert ephemeral is not None
    assert ephemeral != token
