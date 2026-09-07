"""Stage 3: DSR-driven orchestration. [Req 2]

Loads the DSR, validates requested domains/fields against what the lake
actually contains, flags gaps (never silently substitutes), and returns the
context (request_id, resolved domains) later stages stamp onto every output.
"""
from __future__ import annotations

from pathlib import Path

import duckdb

from .dsr import DataRequestSpecification


def validate_availability(dsr: DataRequestSpecification, views_dir: str | Path) -> dict:
    """Check DSR-requested domains/fields against what's present in the analytic views.

    Returns a report with `missing_domains` and `missing_fields` -- these are
    surfaced, never silently dropped or substituted.
    """
    views_dir = Path(views_dir)
    available_tables = {p.stem for p in views_dir.glob("*.parquet")}
    missing_domains = [d for d in dsr.domains if d not in available_tables]

    missing_fields = []
    con = duckdb.connect()
    for field in dsr.fields:
        table_path = views_dir / f"{field.domain}.parquet"
        if not table_path.exists():
            missing_fields.append(f"{field.domain}.{field.name}")
            continue
        cols = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{table_path}')").fetchall()}
        if field.name not in cols:
            missing_fields.append(f"{field.domain}.{field.name}")
    con.close()

    return {
        "request_id": dsr.request_id,
        "missing_domains": missing_domains,
        "missing_fields": missing_fields,
        "ambiguous": [],
    }


def run(dsr_path: str | Path, lake_dir: str | Path = "data/lake") -> dict:
    dsr = DataRequestSpecification.load(dsr_path)
    report = validate_availability(dsr, Path(lake_dir) / "views")
    return {"dsr": dsr, "availability": report}
