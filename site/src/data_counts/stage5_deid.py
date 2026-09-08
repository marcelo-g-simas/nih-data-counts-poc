"""Stage 5: de-identification. [Req 4]

Removes the 18 HIPAA direct identifiers per 45 CFR Sec.164.514(b) and applies a
consistent, deterministic per-patient date-shift that preserves temporal
order. Operates on a *derived copy* (data/work/deid/) -- the source lake
under data/lake/ is never modified.

WHAT THIS STAGE DOES *NOT* ESTABLISH -- Safe Harbor
--------------------------------------------------
Removing the 18 identifiers is necessary for Sec.164.514(b)(2) Safe Harbor but
is NOT sufficient here, and the pipeline's output should not be characterized
as Safe Harbor de-identified. Two independent reasons:

  * Sec.164.514(c) permits a re-identification code only if it is "not derived
    from or related to information about the individual." A PPRL token is by
    construction a keyed hash of identifying attributes (name, gender, DOB --
    see stage6_pprl.tokenize_token1), so it is derived from exactly that
    information. Attaching one to a record forecloses Safe Harbor regardless
    of how thorough the identifier removal was.
  * Safe Harbor requires dates related to an individual be reduced to year
    only. This stage shifts dates instead (see item 3 below), preserving
    day-level temporal order because longitudinal research needs it.

Both are deliberate design choices, not gaps to close: the program's linkage
requirement (Req 5) and its longitudinal-research purpose are incompatible with
Safe Harbor as written. The defensible characterizations of the output are a
**Limited Data Set** disclosed under a data use agreement (Sec.164.514(e)), or
**de-identified by Expert Determination** under Sec.164.514(b)(1). Which
applies is a program-level determination, not something this code can assert.
See the write-up's Section 10 and REPORT.md's Stage 5 note.

Identifier disposition (documented pathway, per identifier):
  1-2  Names, geographic subdivisions < state -- never projected past the
       stage 2b analytic views (fhir_views/*.json only select clinical
       fields), so absent by construction in everything downstream of 2b.
  3    Dates related to an individual -- shifted per-patient below. This is a
       documented, order-preserving *departure from* (not a variant of) Safe
       Harbor's year-only truncation, matching the DRS's
       `deidentification.date_shift` spec. Precedent: MIMIC-III, released
       under Expert Determination rather than Safe Harbor for this reason.
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
from datetime import date, timedelta
from pathlib import Path

import duckdb

from .drs import DataRequestSpecification

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
    """Deterministic per-(DRS, patient) shift in [-max_days, +max_days].

    A constant offset applied to every date field of one patient trivially
    preserves that patient's relative date ordering.
    """
    digest = hashlib.sha256(f"{request_id}:{patient_key}".encode()).digest()
    n = int.from_bytes(digest[:8], "big")
    return (n % (2 * max_days + 1)) - max_days


def run(drs: DataRequestSpecification, views_dir: str | Path = "data/lake/views", out_dir: str | Path = "data/work/deid") -> dict[str, int]:
    views_dir, out_dir = Path(views_dir), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    max_days = drs.deidentification.date_shift.max_days
    shift_cache: dict[str, int] = {}

    def shift_date(patient_key: str | None, date_val: str | None) -> str | None:
        if date_val is None or patient_key is None:
            return date_val
        if patient_key not in shift_cache:
            shift_cache[patient_key] = compute_shift_days(drs.request_id, patient_key, max_days)
        d = date.fromisoformat(str(date_val)[:10])
        shifted = d + timedelta(days=shift_cache[patient_key])
        return shifted.isoformat()

    con = duckdb.connect()
    con.create_function("deid_shift_date", shift_date, ["VARCHAR", "VARCHAR"], "VARCHAR")
    counts: dict[str, int] = {}
    for domain, date_fields in DEID_DATE_FIELDS.items():
        src = views_dir / f"{domain}.parquet"
        if not src.exists():
            continue
        key_field = KEY_FIELDS[domain]
        replace_clause = ", ".join(f"deid_shift_date({key_field}, {f}) AS {f}" for f in date_fields)
        out_path = out_dir / f"{domain}.parquet"
        con.execute(
            f"COPY (SELECT * REPLACE ({replace_clause}) FROM read_parquet('{src.as_posix()}')) "
            f"TO '{out_path.as_posix()}' (FORMAT parquet)"
        )
        counts[domain] = con.execute(f"SELECT count(*) FROM read_parquet('{out_path.as_posix()}')").fetchone()[0]
    con.close()
    return counts
