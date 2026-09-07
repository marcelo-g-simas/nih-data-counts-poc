from data_counts.stage6_pprl import (
    demo_both_ends,
    derive_aes_key_v2,
    generate_site_keys,
    tokenize_token1,
    transcrypt_in,
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


def test_transcrypt_round_trip_preserves_identity_fingerprint(tmp_path):
    site_priv, _ = generate_site_keys(tmp_path / "site")
    program_priv, program_pub = generate_site_keys(tmp_path / "program")
    site_key = derive_aes_key_v2(site_priv)
    program_key = derive_aes_key_v2(program_priv)

    token = tokenize_token1("Jane", "Doe", "F", "1980-05-12", site_key)
    ephemeral = transcrypt_out(token, site_key, program_pub)
    program_token = transcrypt_in(ephemeral, program_priv, program_key)

    # Same identity tokenized directly under the program's own key must match --
    # this is the crux of the both-ends linkage guarantee.
    direct_program_token = tokenize_token1("Jane", "Doe", "F", "1980-05-12", program_key)
    assert program_token == direct_program_token


def test_both_ends_demo_links_only_overlapping_patients():
    result = demo_both_ends()
    assert result["shared_identities_linked"] is True
    assert result["non_overlapping_do_not_link"] is True
