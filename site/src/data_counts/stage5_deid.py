"""Stage 5: de-identification. [Req 4]

Removes the 18 HIPAA direct identifiers per 45 CFR Sec.164.514(b) and applies a
consistent, deterministic per-patient date-shift that preserves temporal
order. Operates on a *derived copy* (data/work/deid/) -- the source lake
under data/lake/ is never modified.

Identifier disposition (documented pathway, per identifier):
  1-2  Names, geographic subdivisions < state -- never projected past the
       stage 2b analytic views (fhir_views/*.json only select clinical
       fields), so absent by construction in everything downstream of 2b.
  3    Dates related to an individual -- shifted per-patient below (a
       documented, order-preserving alternative to Safe Harbor's
       year-only truncation, matching the DSR's `deidentification.date_shift`
       spec; appropriate for longitudinal research use, as in MIMIC-III).
  4-15 Phone/fax/email/SSN/MRN/health-plan/account/license/vehicle/device/
       URL/IP -- never projected past stage 2b (not in any ViewDefinition).
  16   Biometric identifiers, 17. Full-face photos -- not present in this
       synthetic FHIR cohort at all.
  18   Any other unique identifying number/code -- the internal `id` /
       `patient_id` join keys are retained through this stage *only* to
       support the stage 6 tokenization join; stage 8 (package) drops them
       from the outbound artifact entirely, substituting the OPPRL token.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import duckdb
import pyarrow as pa

from .dsr import DataRequestSpecification

DEID_DATE_FIELDS: dict[str, list[str]] = {
    "patient": ["birth_date"],
    "condition": ["onset_dt"],
    "observation": ["effective_dt"],
    "encounter": ["period_start", "period_end"],
    "medication": ["authored_on"],
}
KEY_FIELDS: dict[str, str] = {
    "patient": "id",
    "condition": "patient_id",
    "observation": "patient_id",
    "encounter": "patient_id",
    "medication": "patient_id",
}


def compute_shift_days(request_id: str, patient_key: str, max_days: int) -> int:
    """Deterministic per-(DSR, patient) shift in [-max_days, +max_days].

    A constant offset applied to every date field of one patient trivially
    preserves that patient's relative date ordering.
    """
    digest = hashlib.sha256(f"{request_id}:{patient_key}".encode()).digest()
    n = int.from_bytes(digest[:8], "big")
    return (n % (2 * max_days + 1)) - max_days


def run(dsr: DataRequestSpecification, views_dir: str | Path = "data/lake/views", out_dir: str | Path = "data/work/deid") -> dict[str, int]:
    """Applies the date-shift via a small per-patient lookup table joined against
    native, vectorized DuckDB date arithmetic -- not a per-row Python UDF. The
    shift itself only depends on (request_id, patient_key, max_days), so it only
    ever needs computing once per *patient*, not once per row; a DuckDB scalar
    UDF still calls into Python once per row regardless of caching inside it
    (measured: this was ~40% of total site runtime at 5,000 patients, entirely
    from Python-call overhead, not from compute_shift_days' own cost). Joining a
    precomputed key->shift_days table and doing `date + INTERVAL` in SQL keeps
    the whole operation inside DuckDB's vectorized execution.
    """
    views_dir, out_dir = Path(views_dir), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    max_days = dsr.deidentification.date_shift.max_days

    con = duckdb.connect()
    counts: dict[str, int] = {}
    for domain, date_fields in DEID_DATE_FIELDS.items():
        src = views_dir / f"{domain}.parquet"
        if not src.exists():
            continue
        key_field = KEY_FIELDS[domain]

        keys = [row[0] for row in con.execute(
            f"SELECT DISTINCT {key_field} FROM read_parquet('{src.as_posix()}') WHERE {key_field} IS NOT NULL"
        ).fetchall()]
        shift_table = pa.Table.from_pylist([
            {"_deid_key": k, "_shift_days": compute_shift_days(dsr.request_id, k, max_days)} for k in keys
        ])
        con.register("_shift_table", shift_table)

        replace_clause = ", ".join(
            f"CASE WHEN d.{f} IS NULL THEN NULL "
            f"ELSE (CAST(d.{f} AS DATE) + (s._shift_days || ' days')::INTERVAL)::VARCHAR END AS {f}"
            for f in date_fields
        )
        out_path = out_dir / f"{domain}.parquet"
        con.execute(
            f"COPY (SELECT d.* REPLACE ({replace_clause}) FROM read_parquet('{src.as_posix()}') d "
            f"LEFT JOIN _shift_table s ON d.{key_field} = s._deid_key) "
            f"TO '{out_path.as_posix()}' (FORMAT parquet)"
        )
        con.unregister("_shift_table")
        counts[domain] = con.execute(f"SELECT count(*) FROM read_parquet('{out_path.as_posix()}')").fetchone()[0]
    con.close()
    return counts
