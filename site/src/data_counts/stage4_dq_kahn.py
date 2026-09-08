"""Stage 4: data-quality checks, organized per the Kahn et al. harmonized DQ
framework (conformance / completeness / plausibility), run as SQL over the
Parquet analytic views with DuckDB. Checks *report*; they never modify
source. [Req 3]
"""
from __future__ import annotations

from pathlib import Path

import duckdb

from .drs import DQCheck


def _table_ref(views_dir: Path, domain: str) -> str:
    return f"read_parquet('{(views_dir / f'{domain}.parquet').as_posix()}')"


def _run_completeness(con, views_dir: Path, check: DQCheck) -> dict:
    domain, field = check.target.split(".", 1)
    tbl = _table_ref(views_dir, domain)
    total, nonnull = con.execute(
        f"SELECT count(*), count({field}) FROM {tbl}"
    ).fetchone()
    passed = total == 0 or nonnull == total
    return {"total": total, "non_null": nonnull, "passed": passed}


def _run_uniqueness(con, views_dir: Path, check: DQCheck) -> dict:
    domain, field = check.target.split(".", 1)
    tbl = _table_ref(views_dir, domain)
    total, distinct = con.execute(
        f"SELECT count(*), count(DISTINCT {field}) FROM {tbl}"
    ).fetchone()
    return {"total": total, "distinct": distinct, "passed": total == distinct}


def _run_data_type(con, views_dir: Path, check: DQCheck) -> dict:
    domain, field = check.target.split(".", 1)
    tbl = _table_ref(views_dir, domain)
    dtype = check.params.get("dtype", "double")
    bad = con.execute(
        f"SELECT count(*) FROM {tbl} WHERE {field} IS NOT NULL "
        f"AND TRY_CAST({field} AS {dtype}) IS NULL"
    ).fetchone()[0]
    return {"non_castable": bad, "passed": bad == 0}


def _run_date_sequence(con, views_dir: Path, check: DQCheck) -> dict:
    tbl = _table_ref(views_dir, check.target)
    start, end = check.params["start"], check.params["end"]
    bad = con.execute(
        f"SELECT count(*) FROM {tbl} WHERE {start} IS NOT NULL AND {end} IS NOT NULL "
        f"AND {start} > {end}"
    ).fetchone()[0]
    return {"out_of_sequence": bad, "passed": bad == 0}


def _run_records_per_patient(con, views_dir: Path, check: DQCheck) -> dict:
    tbl = _table_ref(views_dir, check.target)
    lo, hi = check.params.get("min", 0), check.params.get("max", 10**9)
    violators = con.execute(
        f"SELECT count(*) FROM ("
        f"  SELECT patient_id, count(*) AS n FROM {tbl} GROUP BY patient_id"
        f") WHERE n < {lo} OR n > {hi}"
    ).fetchone()[0]
    return {"violating_patients": violators, "passed": violators == 0}


_DISPATCH = {
    "completeness": _run_completeness,
    "uniqueness": _run_uniqueness,
    "data_type": _run_data_type,
    "date_sequence": _run_date_sequence,
    "records_per_patient": _run_records_per_patient,
}


def run(checks: list[DQCheck], views_dir: str | Path = "data/lake/views") -> dict:
    views_dir = Path(views_dir)
    con = duckdb.connect()
    results = []
    any_halt_failed = False
    for check in checks:
        handler = _DISPATCH[check.type]
        try:
            detail = handler(con, views_dir, check)
        except duckdb.Error as exc:
            detail = {"passed": False, "error": str(exc)}
        anomaly = not detail.get("passed", False)
        if anomaly and check.on_fail == "halt":
            any_halt_failed = True
        results.append({
            "id": check.id,
            "kahn_category": check.kahn_category,
            "type": check.type,
            "target": check.target,
            "on_fail": check.on_fail,
            "anomaly": anomaly,
            "detail": detail,
        })
    con.close()
    return {"checks": results, "halt": any_halt_failed}
