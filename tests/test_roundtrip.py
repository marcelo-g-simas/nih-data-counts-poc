import json

import pyarrow.parquet as pq

from data_counts.stage2_ingest import land_raw_parquet


def test_raw_parquet_round_trips_losslessly(tmp_path):
    ndjson_dir = tmp_path / "ndjson"
    ndjson_dir.mkdir()
    patients = [
        {
            "resourceType": "Patient",
            "id": "p1",
            "name": [{"family": "Doe", "given": ["Jane"]}],
            "gender": "female",
            "birthDate": "1980-05-12",
            "extension": [{"url": "http://example.org/ext", "valueCoding": {"system": "http://example.org/sys", "code": "X"}}],
        },
        {"resourceType": "Patient", "id": "p2", "gender": "male"},
    ]
    with (ndjson_dir / "Patient.ndjson").open("w") as f:
        for p in patients:
            f.write(json.dumps(p) + "\n")

    raw_out = tmp_path / "raw"
    land_raw_parquet(ndjson_dir, raw_out)

    table = pq.read_table(raw_out / "Patient.parquet")
    reconstructed = [json.loads(s) for s in table.column("__source_json").to_pylist()]
    assert reconstructed == patients
