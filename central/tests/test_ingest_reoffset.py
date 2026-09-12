"""The central re-offset pass, end to end over Parquet. [Req 4]

Exercises `ingest.reoffset_domain_tables` against a package shaped like the one
`site/`'s `stage8_package.py` writes: `subject_token1/2/3` columns plus
de-identified clinical columns carrying already-shifted dates.

The site half is simulated here rather than imported -- the two projects share no
runtime code (see `crypto.py`'s module docstring), so this test independently
reimplements what a site does: derive the per-DRS offset from the token1 plaintext
digest, add it to the true dates, and RSA-encrypt the digest under the central
public key. If the two implementations of the lattice ever drift apart, the
identity asserted below stops holding, which is precisely the signal wanted.
"""
import base64
from datetime import date, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.padding import MGF1, OAEP
from cryptography.hazmat.primitives.serialization import load_pem_public_key

from data_counts_central import ingest
from data_counts_central.crypto import (
    derive_drs_shift_key,
    derive_shift_days,
    generate_central_keys,
    generate_central_shift_secret,
)

_REQUEST_ID = "DC-POC-0001"
_MAX_YEARS, _JITTER_WEEKS = 3, 2

# Two patients with distinct digests, so a bug that applies one patient's delta to
# every row cannot pass. Digests stand in for sha512 over normalized identity
# attributes; their content is irrelevant here, only that they differ.
_DIGESTS = {"alice": b"\x11" * 64, "bob": b"\x22" * 64}

# True dates the "site" holds. period_start carries a time-of-day component on
# purpose: a whole-day re-offset must not silently truncate an encounter to
# midnight and take length-of-stay in hours with it.
_TRUE = {
    "alice": {"birth_date": "1980-05-12", "period_start": "2021-02-03T08:30:00Z",
              "period_end": "2021-02-17"},
    "bob": {"birth_date": "1985-06-15", "period_start": "2022-07-01T13:00:00Z",
            "period_end": "2022-07-04"},
}


def _shift_text(value: str, days: int) -> str:
    """Add whole days to an ISO date or dateTime, preserving any clock part."""
    return (date.fromisoformat(value[:10]) + timedelta(days=days)).isoformat() + value[10:]


def _write_site_package(site_dir: Path, central_pub: bytes, drs_shift_key: bytes) -> dict[str, int]:
    """Write a `patient.parquet` + `encounter.parquet` pair as a site would.

    Returns the per-patient offset actually applied, so the test can state the
    expected final dates without recomputing them the same way twice.
    """
    site_dir.mkdir(parents=True, exist_ok=True)
    pub = load_pem_public_key(central_pub)
    oaep = OAEP(mgf=MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None)
    shifts = {
        name: derive_shift_days(drs_shift_key, digest, _MAX_YEARS, _JITTER_WEEKS)
        for name, digest in _DIGESTS.items()
    }
    tokens = {
        name: base64.b64encode(pub.encrypt(digest, oaep)).decode("ascii")
        for name, digest in _DIGESTS.items()
    }

    pq.write_table(
        pa.Table.from_pylist([
            {"subject_token1": tokens[n], "subject_token2": tokens[n],
             "subject_token3": tokens[n], "gender": "F",
             "birth_date": _shift_text(_TRUE[n]["birth_date"], shifts[n])}
            for n in _DIGESTS
        ]),
        site_dir / "patient.parquet",
    )
    pq.write_table(
        pa.Table.from_pylist([
            {"subject_token1": tokens[n], "subject_token2": tokens[n],
             "subject_token3": tokens[n],
             "period_start": _shift_text(_TRUE[n]["period_start"], shifts[n]),
             "period_end": _shift_text(_TRUE[n]["period_end"], shifts[n])}
            for n in _DIGESTS
        ]),
        site_dir / "encounter.parquet",
    )
    return shifts


def test_reoffset_lands_every_domain_on_the_global_offset(tmp_path):
    """Every date column of every domain table moves onto `true + shift_global`."""
    keys = tmp_path / "keys"
    central_priv, central_pub = generate_central_keys(keys)
    secret = generate_central_shift_secret(keys)
    drs_key = derive_drs_shift_key(secret, _REQUEST_ID)
    site_dir = tmp_path / "inbox" / "site_a"
    _write_site_package(site_dir, central_pub, drs_key)

    counts = ingest.reoffset_domain_tables(
        site_dir, central_priv, secret, _REQUEST_ID, tmp_path / "normalized",
        _MAX_YEARS, _JITTER_WEEKS,
    )
    assert counts == {"patient": 2, "encounter": 2}

    globals_ = {
        n: derive_shift_days(secret, d, _MAX_YEARS, _JITTER_WEEKS) for n, d in _DIGESTS.items()
    }
    # The two patients must receive different global offsets, or a bug applying one
    # delta to every row would pass unnoticed.
    assert globals_["alice"] != globals_["bob"]
    expected_patient = {_shift_text(_TRUE[n]["birth_date"], globals_[n]) for n in _DIGESTS}
    expected_start = {_shift_text(_TRUE[n]["period_start"], globals_[n]) for n in _DIGESTS}
    expected_end = {_shift_text(_TRUE[n]["period_end"], globals_[n]) for n in _DIGESTS}

    patients = pq.read_table(tmp_path / "normalized" / "patient.parquet").to_pylist()
    encounters = pq.read_table(tmp_path / "normalized" / "encounter.parquet").to_pylist()
    assert {r["birth_date"] for r in patients} == expected_patient
    assert {r["period_start"] for r in encounters} == expected_start
    assert {r["period_end"] for r in encounters} == expected_end
    # Clock time survived the re-offset verbatim.
    assert all(r["period_start"].endswith("T08:30:00Z") or r["period_start"].endswith("T13:00:00Z")
               for r in encounters)
    # Non-date columns are passed through untouched, and the internal delta column
    # does not leak into the output.
    assert "_delta" not in patients[0]
    assert patients[0]["gender"] == "F"


def test_reoffset_is_identical_across_two_drss(tmp_path):
    """The requirement: the same patient's dates agree across DRSs after re-offset.

    Two DRSs, two different shift keys, so the two site packages carry *different*
    stored dates for the same event. After re-offset both must read the same.
    """
    keys = tmp_path / "keys"
    central_priv, central_pub = generate_central_keys(keys)
    secret = generate_central_shift_secret(keys)

    stored, final = [], []
    for request_id in ("DC-POC-0001", "DC-POC-0002"):
        site_dir = tmp_path / "inbox" / request_id
        drs_key = derive_drs_shift_key(secret, request_id)
        _write_site_package(site_dir, central_pub, drs_key)
        stored.append(pq.read_table(site_dir / "encounter.parquet").to_pylist())

        out = tmp_path / "normalized" / request_id
        ingest.reoffset_domain_tables(
            site_dir, central_priv, secret, request_id, out, _MAX_YEARS, _JITTER_WEEKS,
        )
        final.append(pq.read_table(out / "encounter.parquet").to_pylist())

    # The packages genuinely disagreed as transmitted...
    assert {r["period_start"] for r in stored[0]} != {r["period_start"] for r in stored[1]}
    # ...and agree exactly after re-offset.
    assert {r["period_start"] for r in final[0]} == {r["period_start"] for r in final[1]}
    assert {r["period_end"] for r in final[0]} == {r["period_end"] for r in final[1]}


def test_reoffset_preserves_intervals_and_weekday(tmp_path):
    """Length of stay and day of week must survive the re-offset. [Req 4]"""
    keys = tmp_path / "keys"
    central_priv, central_pub = generate_central_keys(keys)
    secret = generate_central_shift_secret(keys)
    site_dir = tmp_path / "inbox" / "site_a"
    _write_site_package(site_dir, central_pub, derive_drs_shift_key(secret, _REQUEST_ID))

    ingest.reoffset_domain_tables(
        site_dir, central_priv, secret, _REQUEST_ID, tmp_path / "normalized",
        _MAX_YEARS, _JITTER_WEEKS,
    )
    encounters = pq.read_table(tmp_path / "normalized" / "encounter.parquet").to_pylist()

    true_intervals = {
        date.fromisoformat(v["period_end"]) - date.fromisoformat(v["period_start"][:10])
        for v in _TRUE.values()
    }
    true_weekdays = {date.fromisoformat(v["period_start"][:10]).weekday() for v in _TRUE.values()}
    for row in encounters:
        start = date.fromisoformat(row["period_start"][:10])
        assert date.fromisoformat(row["period_end"]) - start in true_intervals
        assert start.weekday() in true_weekdays


def test_reoffset_skips_domains_absent_from_the_package(tmp_path):
    """A site sending only some domains is normal (the DRS selects them), not an error."""
    keys = tmp_path / "keys"
    central_priv, central_pub = generate_central_keys(keys)
    secret = generate_central_shift_secret(keys)
    site_dir = tmp_path / "inbox" / "site_a"
    _write_site_package(site_dir, central_pub, derive_drs_shift_key(secret, _REQUEST_ID))
    (site_dir / "encounter.parquet").unlink()

    counts = ingest.reoffset_domain_tables(
        site_dir, central_priv, secret, _REQUEST_ID, tmp_path / "normalized",
        _MAX_YEARS, _JITTER_WEEKS,
    )
    assert counts == {"patient": 2}
    assert not (tmp_path / "normalized" / "encounter.parquet").exists()


def test_resolve_site_package_is_unaffected_by_the_reoffset_work(tmp_path):
    """Linkage input must keep its original shape: the re-offset is a separate pass.

    `resolve_site_package` reads a package carrying clinical columns and returns
    only resolved tokens plus `source_dataset` -- no dates. Linkage neither needs
    nor should see them.
    """
    keys = tmp_path / "keys"
    central_priv, central_pub = generate_central_keys(keys)
    secret = generate_central_shift_secret(keys)
    site_dir = tmp_path / "inbox" / "site_a"
    _write_site_package(site_dir, central_pub, derive_drs_shift_key(secret, _REQUEST_ID))

    resolved = ingest.resolve_site_package(site_dir, "site_a", central_priv)
    assert set(resolved.column_names) == {
        "subject_token1", "subject_token2", "subject_token3", "source_dataset",
    }
    assert resolved.num_rows == 2
    # Two distinct identities resolve to two distinct tokens, deterministically.
    assert len(set(resolved.column("subject_token1").to_pylist())) == 2
