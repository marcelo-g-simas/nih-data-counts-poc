import math
from datetime import date, timedelta

import pyarrow as pa
import pyarrow.parquet as pq

from data_counts import stage5_deid, stage8_package
from data_counts.drs import DataRequestSpecification, DateShift
from data_counts.stage6_pprl import (
    derive_aes_key_v2,
    derive_shift_days,
    generate_site_keys,
    load_central_public_key,
    token1_digest,
    tokenize_and_transcrypt,
    tokenize_token1,
)

_DRS_EXAMPLE = "drs/examples/DC-POC-0001.json"
_MAX_YEARS, _JITTER_WEEKS = 3, 2
_DRS_KEY_A = b"\x01" * 32
_DRS_KEY_B = b"\x02" * 32

# A batch, so the "different key => different shift" property can be asserted
# statistically. A single pair can legitimately collide -- the lattice has only
# (2*3+1) * (2*2+1) = 35 possible values by design (see REPORT.md's low-entropy
# admission), so per-pair inequality is not a real invariant.
_IDENTITIES = [
    ("Jane", "Doe", "F", "1980-05-12"),
    ("Bob", "Baker", "M", "1985-06-15"),
    ("Carol", "Chen", "F", "1990-02-02"),
    ("Dan", "Diaz", "M", "1972-08-30"),
    ("Eve", "Evans", "F", "1965-12-24"),
    ("Frank", "Fisher", "M", "2001-03-15"),
    ("Grace", "Gomez", "F", "1958-07-04"),
    ("Hank", "Huang", "M", "1994-10-19"),
]


def _digest(identity):
    digest = token1_digest(*identity)
    assert digest is not None
    return digest


def _shift(key, identity):
    return derive_shift_days(key, _digest(identity), _MAX_YEARS, _JITTER_WEEKS)


def test_date_shift_is_deterministic_for_the_same_identity_and_key():
    """Determinism, not uniqueness, is the guarantee -- but it must be exact."""
    for identity in _IDENTITIES:
        assert _shift(_DRS_KEY_A, identity) == _shift(_DRS_KEY_A, identity)


def test_date_shift_is_keyed_on_identity_not_on_a_site_local_id():
    """The digest is the shift key, so the same person shifts the same way
    everywhere -- and two different people generally shift differently.

    Asserted over a batch because the lattice is deliberately small: any single
    pair may collide, but they must not all agree, which is what a shift ignoring
    its digest input would produce.
    """
    shifts = {_shift(_DRS_KEY_A, i) for i in _IDENTITIES}
    assert len(shifts) > 1


def test_date_shift_changes_with_a_different_drs_shift_key():
    """Per-DRS keys must actually separate DRSs. Same batch reasoning as above:
    some identities will collide across the two keys; most must not.
    """
    differing = sum(
        1 for i in _IDENTITIES if _shift(_DRS_KEY_A, i) != _shift(_DRS_KEY_B, i)
    )
    assert differing >= len(_IDENTITIES) - 1


def test_every_shift_is_a_whole_number_of_weeks_within_bounds():
    """`364 * k + 7 * j` -- the shape both seasonality guarantees rest on.

    Whole weeks is the load-bearing part: a day-granular jitter component would
    leave the offset bounded but rotate the day of the week, which is exactly the
    bug this assertion exists to catch.
    """
    for identity in _IDENTITIES:
        shift = _shift(_DRS_KEY_A, identity)
        assert shift % 7 == 0, (identity, shift)
        assert abs(shift) <= 364 * _MAX_YEARS + 7 * _JITTER_WEEKS


def test_shift_preserves_day_of_week_exactly_and_season_to_within_leap_drift():
    """The requirement the user added: offsets bounded so seasonality survives.

    Day-of-week is exact because every term of the offset is a whole number of
    weeks. Day-of-year drifts by at most the jitter plus accumulated leap days
    (~1.25 per year of offset) -- 18 days at the defaults.

    Note what is deliberately *not* asserted: that the calendar month is
    preserved. It is not, and cannot be for any nonzero jitter -- an event late in
    a month can cross into the next one. The claim the design supports is bounded
    day-of-year drift, which keeps an event in its season; asserting month-exactness
    would be asserting something the arithmetic does not give.
    """
    tolerance = 7 * _JITTER_WEEKS + math.ceil(1.25 * _MAX_YEARS)
    assert tolerance < 45, "drift approaching half a season is not a seasonality claim"
    dates = [date(2020, month, day) for month in range(1, 13) for day in (1, 15, 28)]
    for identity in _IDENTITIES:
        shift = _shift(_DRS_KEY_A, identity)
        for original in dates:
            shifted = original + timedelta(days=shift)
            assert shifted.weekday() == original.weekday()
            drift = abs(shifted.timetuple().tm_yday - original.timetuple().tm_yday)
            assert min(drift, 365 - drift) <= tolerance, (identity, original, shift)


def test_shift_preserves_intervals_exactly():
    """One offset per patient applied to every date means every day-count
    interval -- length of stay, time to treatment, progression -- is unchanged.
    """
    admit, discharge = date(2021, 2, 3), date(2021, 2, 17)
    for identity in _IDENTITIES:
        shift = timedelta(days=_shift(_DRS_KEY_A, identity))
        assert (discharge + shift) - (admit + shift) == discharge - admit


def test_two_sites_with_different_rsa_keys_derive_the_same_shift(tmp_path):
    """The assertion this whole change exists to make true. [Req 4, Req 5]

    Two independently keyed sites hold the same patient. Their OPPRL tokens are
    encrypted under different site keys and so differ, but the shift is derived
    from the token1 *plaintext digest*, which does not depend on the site key --
    so both sites shift that patient's dates identically, and a cross-site
    interval measured after linkage is valid.
    """
    site_a_priv, _ = generate_site_keys(tmp_path / "site_a")
    site_b_priv, _ = generate_site_keys(tmp_path / "site_b")
    _priv, central_pub = generate_site_keys(tmp_path / "central")
    patient = {"patient_id": "local-a-1", "first_name": "Jane", "last_name": "Doe",
               "gender": "F", "birth_date": "1980-05-12"}
    shift_spec = DateShift(max_years=_MAX_YEARS, jitter_weeks=_JITTER_WEEKS)

    map_a = tokenize_and_transcrypt([patient], site_a_priv, central_pub, shift_spec, _DRS_KEY_A)
    # Same person, different site-local id -- the shift must not depend on it.
    map_b = tokenize_and_transcrypt([{**patient, "patient_id": "local-b-9"}],
                                     site_b_priv, central_pub, shift_spec, _DRS_KEY_A)

    assert derive_aes_key_v2(site_a_priv) != derive_aes_key_v2(site_b_priv)
    assert map_a["local-a-1"]["_shift_days"] == map_b["local-b-9"]["_shift_days"]


def test_cross_drs_reoffset_lands_on_one_global_offset(tmp_path):
    """Two DRSs shift the same patient differently; central's delta re-offset must
    land both on the *same* final date. [Req 4]

    This asserts the arithmetic identity the central platform relies on --
    `stored + (shift_global - shift_drs) == true + shift_global` -- using the
    site-side transcription of `derive_shift_days`, which is byte-identical to
    central's by construction. `central/tests/test_crypto.py` exercises the same
    identity through `resolve_and_shift`, i.e. through the real RSA path.

    Note what is *not* computed anywhere below: `true` never reappears after the
    site shifts it. The delta is the difference of two offsets, never of two dates.
    """
    identity = ("Jane", "Doe", "F", "1980-05-12")
    digest = _digest(identity)
    central_secret = b"\xab" * 32
    true_date = date(2024, 1, 15)

    finals = []
    for drs_key in (_DRS_KEY_A, _DRS_KEY_B):
        shift_drs = derive_shift_days(drs_key, digest, _MAX_YEARS, _JITTER_WEEKS)
        shift_global = derive_shift_days(central_secret, digest, _MAX_YEARS, _JITTER_WEEKS)
        stored = true_date + timedelta(days=shift_drs)          # what the site packages
        final = stored + timedelta(days=shift_global - shift_drs)  # what central stores
        assert final == true_date + timedelta(days=shift_global)
        finals.append(final)

    # The two DRSs really did shift differently...
    assert (derive_shift_days(_DRS_KEY_A, digest, _MAX_YEARS, _JITTER_WEEKS)
            != derive_shift_days(_DRS_KEY_B, digest, _MAX_YEARS, _JITTER_WEEKS))
    # ...yet agree exactly after re-offset. This is the cross-DRS requirement.
    assert finals[0] == finals[1]


def _views_dir(tmp_path):
    """A two-patient, two-domain stand-in for stage 2's analytic views.

    `period_start` deliberately carries a time-of-day component: a whole-day
    shift has no business truncating an encounter to midnight, and doing so would
    take length-of-stay in hours with it.
    """
    views = tmp_path / "views"
    views.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.Table.from_pylist([
            {"id": "p1", "birth_date": "1980-05-12", "gender": "F"},
            {"id": "p2", "birth_date": "1985-06-15", "gender": "M"},
        ]),
        views / "patient.parquet",
    )
    pq.write_table(
        pa.Table.from_pylist([
            {"id": "e1", "patient_id": "p1", "period_start": "2021-02-03T08:30:00Z",
             "period_end": "2021-02-17"},
            {"id": "e2", "patient_id": "p2", "period_start": "2022-07-01T13:00:00Z",
             "period_end": "2022-07-04"},
        ]),
        views / "encounter.parquet",
    )
    return views


def test_stage5_shifts_every_domain_by_that_patients_single_offset(tmp_path):
    """End-to-end through `stage5_deid.run` -- the first test to call it at all.

    Checks the three properties the stage is responsible for at once: every date
    of one patient moves by that patient's offset (so intervals hold across
    domains, including birth_date, so age-at-event holds), the day of week is
    preserved, and the time-of-day component survives.
    """
    site_priv, _ = generate_site_keys(tmp_path / "site")
    _priv, central_pub = generate_site_keys(tmp_path / "central")
    patients = [
        {"patient_id": "p1", "first_name": "Jane", "last_name": "Doe",
         "gender": "F", "birth_date": "1980-05-12"},
        {"patient_id": "p2", "first_name": "Bob", "last_name": "Baker",
         "gender": "M", "birth_date": "1985-06-15"},
    ]
    token_map = tokenize_and_transcrypt(patients, site_priv, central_pub,
                                        DateShift(), _DRS_KEY_A)
    shift_map = stage5_deid.shift_map_from_token_map(token_map)
    assert set(shift_map) == {"p1", "p2"}

    drs = DataRequestSpecification.load(_DRS_EXAMPLE)
    counts = stage5_deid.run(drs, shift_map, views_dir=_views_dir(tmp_path),
                             out_dir=tmp_path / "deid")
    assert counts["patient"] == 2
    assert counts["encounter"] == 2
    assert counts["_excluded_unshiftable"] == 0

    shifted_patients = {
        r["id"]: r for r in
        pq.read_table(tmp_path / "deid" / "patient.parquet").to_pylist()
    }
    shifted_encounters = {
        r["patient_id"]: r for r in
        pq.read_table(tmp_path / "deid" / "encounter.parquet").to_pylist()
    }

    originals = {"p1": ("1980-05-12", "2021-02-03T08:30:00Z", "2021-02-17"),
                 "p2": ("1985-06-15", "2022-07-01T13:00:00Z", "2022-07-04")}
    for pid, (birth, start, end) in originals.items():
        offset = timedelta(days=shift_map[pid])
        assert shifted_patients[pid]["birth_date"] == (
            date.fromisoformat(birth) + offset).isoformat()
        enc = shifted_encounters[pid]
        # Time-of-day preserved verbatim, day part shifted.
        assert enc["period_start"] == (
            (date.fromisoformat(start[:10]) + offset).isoformat() + start[10:])
        assert enc["period_end"] == (date.fromisoformat(end) + offset).isoformat()
        # Interval across the two columns of one domain is untouched...
        assert (date.fromisoformat(enc["period_end"])
                - date.fromisoformat(enc["period_start"][:10])
                == date.fromisoformat(end) - date.fromisoformat(start[:10]))
        # ...and so is the interval spanning two domains (age at encounter).
        assert (date.fromisoformat(enc["period_start"][:10])
                - date.fromisoformat(shifted_patients[pid]["birth_date"])
                == date.fromisoformat(start[:10]) - date.fromisoformat(birth))
        # Weekday preserved, which is what the 52-week lattice buys.
        assert (date.fromisoformat(enc["period_start"][:10]).weekday()
                == date.fromisoformat(start[:10]).weekday())

    # The two patients were genuinely shifted by different amounts -- otherwise
    # the per-patient properties above would hold trivially under one global shift.
    assert shift_map["p1"] != shift_map["p2"]


def test_stage5_drops_patients_with_no_derivable_offset(tmp_path):
    """A patient absent from the shift map must be excluded, not passed through:
    passing them through would put true, unshifted dates in de-identified output.
    """
    drs = DataRequestSpecification.load(_DRS_EXAMPLE)
    counts = stage5_deid.run(drs, {"p1": 7}, views_dir=_views_dir(tmp_path),
                             out_dir=tmp_path / "deid")
    assert counts["patient"] == 1
    assert counts["encounter"] == 1
    assert counts["_excluded_unshiftable"] == 2  # p2's patient row and encounter
    remaining = pq.read_table(tmp_path / "deid" / "patient.parquet").to_pylist()
    assert [r["id"] for r in remaining] == ["p1"]
    assert remaining[0]["birth_date"] == "1980-05-19"  # 1980-05-12 + 7


def test_shift_days_is_absent_when_identity_attributes_are_incomplete(tmp_path):
    """No last name -> no token1 -> no derivable offset. Stage 5 must then drop
    the patient rather than emit an unshifted true date.
    """
    site_priv, _ = generate_site_keys(tmp_path / "site")
    _priv, central_pub = generate_site_keys(tmp_path / "central")
    incomplete = {"patient_id": "p-nolast", "first_name": "Jane", "last_name": None,
                  "gender": "F", "birth_date": "1980-05-12"}
    token_map = tokenize_and_transcrypt([incomplete], site_priv, central_pub,
                                         DateShift(), _DRS_KEY_A)
    assert token_map["p-nolast"]["_shift_days"] is None
    assert stage5_deid.shift_map_from_token_map(token_map) == {}


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
