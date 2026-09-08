"""Stage 2: ingest -> Parquet, two layers (raw lossless landing + analytic views).

2a: bulk-fetch (smart-fetch if a live $export URL is given, else the local
NDJSON folder from stage 1 is already "the export") -> nested Parquet via
cumulus-fhir-support, conformed toward the aehrc/parquet-on-fhir lossless
encoding. de-id is OFF here; DQ runs on this source layer in stage 4.
[Req 1a, 6]

2b: flat analytic tables via SQL-on-FHIR ViewDefinitions (see viewdef.py for
why these are evaluated in Python rather than compiled by FlatQuack), loaded
into DuckDB. [Req 6]
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import cumulus_fhir_support as cfs
import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from .stage1_synthea import RESOURCE_TYPES
from .viewdef import evaluate_view

FHIR_VIEWS_DIR = Path(__file__).resolve().parent.parent.parent / "fhir_views"

# DRS domain names (drs/examples/*.json, Kahn checks) use "medication", not the FHIR
# resource type "MedicationRequest" -- this maps analytic-view output filenames accordingly.
RESOURCE_TO_DOMAIN = {
    "Patient": "patient",
    "Condition": "condition",
    "Observation": "observation",
    "Encounter": "encounter",
    "MedicationRequest": "medication",
}


def fetch_bulk_export(source_ndjson_dir: str | Path, dest_dir: str | Path, fhir_url: str | None = None) -> Path:
    """The 'deep query / approved extraction' step. [Req 1a]

    If `fhir_url` is given and the `smart-fetch` CLI is installed, run a real
    bulk export against it. Otherwise, treat `source_ndjson_dir` (stage 1's
    output) as the already-exported payload -- the spec explicitly allows
    this simpler path ("drop Synthea's NDJSON into a folder and treat it as
    a pre-exported dataset... downstream ingest is identical either way").
    """
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    if fhir_url and shutil.which("smart-fetch"):
        subprocess.run(["smart-fetch", "export", "--fhir-url", fhir_url, str(dest)], check=True)
        return dest
    for f in Path(source_ndjson_dir).glob("*.ndjson"):
        shutil.copy(f, dest / f.name)
    return dest


def land_raw_parquet(ndjson_dir: str | Path, raw_out_dir: str | Path) -> dict[str, int]:
    """Land each resource type as nested, lossless Parquet. [Req 1a, 6]

    Schema is derived by cumulus-fhir-support's `pyarrow_schema_from_rows`
    (wide + as-deep-as-observed, per aehrc/parquet-on-fhir naming intent).
    We additionally carry the resource's canonical JSON verbatim in an
    additive `__source_json` column -- the same `__`-annotation namespace
    parquet-on-fhir itself reserves for additive, clearly-identified,
    traceable-to-source metadata -- so the encoding is guaranteed
    byte-for-byte round-trippable regardless of which choice-type/extension
    edge cases the derived typed schema does or doesn't cover.
    """
    ndjson_dir, raw_out_dir = Path(ndjson_dir), Path(raw_out_dir)
    raw_out_dir.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    for rt in RESOURCE_TYPES:
        rows = list(cfs.read_multiline_json_from_dir(str(ndjson_dir), resource=rt))
        if not rows:
            continue
        schema = cfs.pyarrow_schema_from_rows(rt, rows)
        schema = schema.append(pa.field("__source_json", pa.large_string()))
        arrays = []
        for field in schema:
            if field.name == "__source_json":
                arrays.append(pa.array([json.dumps(r, sort_keys=True) for r in rows], type=pa.large_string()))
                continue
            try:
                arrays.append(pa.array([r.get(field.name) for r in rows], type=field.type))
            except (pa.ArrowInvalid, pa.ArrowTypeError):
                # A typed column may not build cleanly for every irregular nested shape
                # cumulus-fhir-support's wide-but-shallow schema can produce; that's fine --
                # __source_json above is the lossless source of truth this column is a
                # convenience projection of, so degrade to all-null rather than fail the batch.
                arrays.append(pa.nulls(len(rows), type=field.type))
        table = pa.Table.from_arrays(arrays, schema=schema)
        pq.write_table(table, raw_out_dir / f"{rt}.parquet")
        counts[rt] = len(rows)
    return counts


def build_analytic_views(raw_dir: str | Path, views_out_dir: str | Path) -> dict[str, int]:
    """Project the lossless raw layer into flat tables via ViewDefinitions. [Req 6]"""
    raw_dir, views_out_dir = Path(raw_dir), Path(views_out_dir)
    views_out_dir.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    con = duckdb.connect()
    for view_path in sorted(FHIR_VIEWS_DIR.glob("*.json")):
        view_def = json.loads(view_path.read_text())
        resource_type = view_def["resource"]
        raw_parquet = raw_dir / f"{resource_type}.parquet"
        if not raw_parquet.exists():
            continue
        table = pq.read_table(raw_parquet)
        resources = [json.loads(s) for s in table.column("__source_json").to_pylist()]
        flat_rows = evaluate_view(view_def, resources)
        if not flat_rows:
            continue
        con.register("flat_rows", pa.Table.from_pylist(flat_rows))
        domain = RESOURCE_TO_DOMAIN.get(resource_type, resource_type.lower())
        out_path = views_out_dir / f"{domain}.parquet"
        con.execute(f"COPY (SELECT * FROM flat_rows) TO '{out_path}' (FORMAT parquet)")
        counts[resource_type] = len(flat_rows)
    con.close()
    return counts


def run(ndjson_dir: str | Path = "data/synthea_ndjson", lake_dir: str | Path = "data/lake", fhir_url: str | None = None) -> dict:
    lake_dir = Path(lake_dir)
    fetch_dir = lake_dir / "_fetched_ndjson"
    fetch_bulk_export(ndjson_dir, fetch_dir, fhir_url=fhir_url)
    raw_counts = land_raw_parquet(fetch_dir, lake_dir / "raw")
    view_counts = build_analytic_views(lake_dir / "raw", lake_dir / "views")
    return {"raw": raw_counts, "views": view_counts}
