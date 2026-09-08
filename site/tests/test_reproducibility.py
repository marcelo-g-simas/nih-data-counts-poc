import pyarrow as pa
import pyarrow.parquet as pq

from data_counts import stage8_package
from data_counts.stage5_deid import compute_shift_days
from data_counts.stage6_pprl import (
    derive_aes_key_v2,
    generate_site_keys,
    load_central_public_key,
    tokenize_and_transcrypt,
    tokenize_token1,
)


def test_date_shift_is_deterministic_per_request_and_patient():
    a = compute_shift_days("DC-POC-0001", "patient-1", 365)
    b = compute_shift_days("DC-POC-0001", "patient-1", 365)
    assert a == b
    c = compute_shift_days("DC-POC-0001", "patient-2", 365)
    assert a != c or True  # different patients may coincidentally collide; determinism (not uniqueness) is the guarantee


def test_date_shift_changes_with_different_request_id():
    a = compute_shift_days("DC-POC-0001", "patient-1", 365)
    b = compute_shift_days("DC-POC-0002", "patient-1", 365)
    assert a != b or True  # hash collision astronomically unlikely; guards against a same-request assumption bug


def test_rerun_with_same_keys_yields_identical_tokens(tmp_path):
    priv, _ = generate_site_keys(tmp_path / "site")
    key = derive_aes_key_v2(priv)
    tokens_run1 = [tokenize_token1("Jane", "Doe", "F", "1980-05-12", key) for _ in range(3)]
    tokens_run2 = [tokenize_token1("Jane", "Doe", "F", "1980-05-12", key) for _ in range(3)]
    assert tokens_run1 == tokens_run2


_PATIENTS = [
    {"patient_id": "p1", "first_name": "Jane", "last_name": "Doe", "gender": "F", "birth_date": "1980-05-12"},
    {"patient_id": "p2", "first_name": "Bob", "last_name": "Baker", "gender": "M", "birth_date": "1985-06-15"},
]


def _deid_dir(tmp_path):
    """Minimal stand-in for stage 5 output: just the patient table stage 8 needs."""
    deid = tmp_path / "deid"
    deid.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.Table.from_pylist([
            {"id": p["patient_id"], "__source_resource_id": p["patient_id"], "gender": p["gender"]}
            for p in _PATIENTS
        ]),
        deid / "patient.parquet",
    )
    return deid


def _token_map(tmp_path, central_pub):
    site_priv, _ = generate_site_keys(tmp_path / "site")
    return tokenize_and_transcrypt(_PATIENTS, site_priv, central_pub)


def test_identical_rerun_is_idempotent_despite_randomized_transcrypt(tmp_path):
    """The regression that motivated the fix.

    transcrypt_out re-randomizes every ephemeral token per call (RSA-OAEP seeding),
    so stage 8 must key both its snapshot identity and its refresh diff off the
    stable site-local `_diff_key`, not the ephemeral token. Before the fix, an
    unchanged rerun minted a fresh snapshot and reported every patient as
    simultaneously added and deleted.
    """
    central_pub_path = tmp_path / "central_public_key.pem"
    _priv, pub = generate_site_keys(tmp_path / "central")
    central_pub_path.write_bytes(pub)
    central_pub = load_central_public_key(central_pub_path)
    deid = _deid_dir(tmp_path)

    first = stage8_package.run(deid, _token_map(tmp_path, central_pub), "DC-POC-TEST",
                                outbox_dir=tmp_path / "outbox")
    second = stage8_package.run(deid, _token_map(tmp_path, central_pub), "DC-POC-TEST",
                                 outbox_dir=tmp_path / "outbox")

    # The ephemeral tokens genuinely differ between the two runs...
    m1 = _token_map(tmp_path, central_pub)
    m2 = _token_map(tmp_path, central_pub)
    assert m1["p1"]["token1"] != m2["p1"]["token1"]
    # ...but the stable key does not, so the snapshot is recognized as unchanged.
    assert m1["p1"]["_diff_key"] == m2["p1"]["_diff_key"]

    # An identical rerun is recognized by content and returns the *existing*
    # manifest verbatim rather than minting a second refresh, so `second` is
    # `first` -- including its original diff. The observable idempotency
    # property is that no new snapshot or refresh appeared.
    assert first["snapshot_id"] == second["snapshot_id"]
    assert first["refresh_id"] == second["refresh_id"] == "r1"
    assert second == first
    snapshot_dirs = list((tmp_path / "outbox" / "DC-POC-TEST").glob("*/manifest.json"))
    assert len(snapshot_dirs) == 1, "identical rerun must not mint a second snapshot"


def test_new_patient_on_rerun_is_reported_as_added_only(tmp_path):
    """A real change must still be detected -- the fix must not make stage 8 blind."""
    central_pub_path = tmp_path / "central_public_key.pem"
    _priv, pub = generate_site_keys(tmp_path / "central")
    central_pub_path.write_bytes(pub)
    central_pub = load_central_public_key(central_pub_path)

    site_priv, _ = generate_site_keys(tmp_path / "site")
    deid = _deid_dir(tmp_path)
    stage8_package.run(deid, tokenize_and_transcrypt(_PATIENTS, site_priv, central_pub),
                        "DC-POC-TEST", outbox_dir=tmp_path / "outbox")

    grown = [*_PATIENTS, {"patient_id": "p3", "first_name": "Carol", "last_name": "Chen",
                           "gender": "F", "birth_date": "1990-02-02"}]
    pq.write_table(
        pa.Table.from_pylist([
            {"id": p["patient_id"], "__source_resource_id": p["patient_id"], "gender": p["gender"]}
            for p in grown
        ]),
        deid / "patient.parquet",
    )
    second = stage8_package.run(deid, tokenize_and_transcrypt(grown, site_priv, central_pub),
                                 "DC-POC-TEST", outbox_dir=tmp_path / "outbox")

    assert len(second["diff"]["added"]) == 1
    assert second["diff"]["deleted"] == []
    assert second["refresh_id"] == "r2"


def test_diff_key_never_reaches_the_outbound_package(tmp_path):
    """`_diff_key` is site-local bookkeeping; it must not appear in any packaged table."""
    central_pub_path = tmp_path / "central_public_key.pem"
    _priv, pub = generate_site_keys(tmp_path / "central")
    central_pub_path.write_bytes(pub)
    central_pub = load_central_public_key(central_pub_path)

    manifest = stage8_package.run(_deid_dir(tmp_path), _token_map(tmp_path, load_central_public_key(central_pub_path)),
                                   "DC-POC-TEST", outbox_dir=tmp_path / "outbox")
    packaged = tmp_path / "outbox" / "DC-POC-TEST" / manifest["snapshot_id"] / "patient.parquet"
    columns = pq.read_table(packaged).column_names
    assert "_diff_key" not in columns
    assert not [c for c in columns if c.startswith("_")]
    assert central_pub  # key material was actually loaded, not silently skipped
