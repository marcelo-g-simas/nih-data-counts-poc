"""Stage 5: de-identification. [Req 4]

Removes the 18 HIPAA direct identifiers per 45 CFR Sec.164.514(b) and applies a
consistent, deterministic per-patient date-shift that preserves temporal order,
season, and day of week. Operates on a *derived copy*
(data/work/<request_id>/deid/) -- the source lake under data/lake/ is never
modified.

The offset is not computed here. It is derived in stage 6 from the OPPRL token1
plaintext digest -- the one per-identity value that is identical at every site --
and passed in, which is why stage 6 runs first. That is what makes one patient's
dates agree across sites and across DRSs; see stage6_pprl.derive_shift_days for
the 52-week lattice that preserves season, and `run` below for what happens to a
patient whose offset cannot be derived.

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
       Note the shift is a weak obscuring measure and not a confidentiality
       control: preserving season constrains it to a small set of possible
       offsets. REPORT.md states that outright rather than leaving it implied.
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


def shift_map_from_token_map(token_map: dict[str, dict]) -> dict[str, int]:
    """Extract {site_local_patient_id: shift_days} from stage 6's token map. [Req 4]

    Stage 6 keys its map by the site-local patient id and stores the offset under
    the site-local `_shift_days` key, so this is a projection, not a computation.
    Patients whose offset is None (no token1 -- a missing name, gender, or birth
    date) are omitted, which is what makes `run` exclude them rather than emit an
    unshifted true date.
    """
    return {
        pid: tokens["_shift_days"]
        for pid, tokens in token_map.items()
        if tokens.get("_shift_days") is not None
    }


def _shift_one(date_val: str, shift_days: int) -> str:
    """Apply a whole-day offset, preserving any time-of-day component.

    The date fields here are FHIR `dateTime` in several cases
    (`encounter.period_start`, `observation.effective_dt`), so truncating to 10
    characters would silently discard the clock time and take length-of-stay in
    hours with it. A whole-day shift has no reason to touch the time at all: the
    date part moves, the rest is passed through verbatim.
    """
    text = str(date_val)
    day, remainder = text[:10], text[10:]
    shifted = date.fromisoformat(day) + timedelta(days=shift_days)
    return shifted.isoformat() + remainder


def run(drs: DataRequestSpecification, shift_days_by_patient: dict[str, int],
        views_dir: str | Path = "data/lake/views",
        out_dir: str | Path | None = None) -> dict[str, int]:
    """De-identify the analytic views, applying stage 6's per-patient date offset.

    `shift_days_by_patient` comes from stage 6 (`shift_map_from_token_map`), which
    is why stage 6 runs first: the offset is derived from the OPPRL token1 digest,
    the one per-identity value that is the same at every site. That is what makes
    a patient's dates line up across sites and across DRSs -- the offset this
    stage applies is not a site-local secret, it is a function of who the patient
    is. Deriving it here from the site-local `patient_id`, as this stage used to,
    guaranteed the opposite: two sites holding the same patient shifted them
    apart, and no amount of downstream linkage could put them back.

    A patient absent from the map has no derivable offset (no token1) and their
    rows are **dropped**, not passed through: emitting them would put true,
    unshifted dates into the de-identified output. The returned counts are
    post-exclusion, and `_excluded_unshiftable` reports the row total removed so
    the drop is visible in the audit record rather than silent.
    """
    views_dir = Path(views_dir)
    out_dir = Path(out_dir) if out_dir is not None else Path("data/work") / drs.request_id / "deid"
    out_dir.mkdir(parents=True, exist_ok=True)

    def shift_date(patient_key: str | None, date_val: str | None) -> str | None:
        if date_val is None or patient_key is None:
            return date_val
        shift_days = shift_days_by_patient.get(patient_key)
        if shift_days is None:
            return None  # unreachable: the WHERE clause below excludes these rows first
        return _shift_one(date_val, shift_days)

    con = duckdb.connect()
    con.create_function("deid_shift_date", shift_date, ["VARCHAR", "VARCHAR"], "VARCHAR")
    # A one-column table of shiftable patients, so the exclusion is a join in
    # DuckDB rather than a Python predicate evaluated per row.
    con.execute("CREATE TABLE _shiftable (patient_key VARCHAR)")
    if shift_days_by_patient:
        con.executemany("INSERT INTO _shiftable VALUES (?)", [(k,) for k in shift_days_by_patient])

    counts: dict[str, int] = {}
    excluded = 0
    for domain, date_fields in DEID_DATE_FIELDS.items():
        src = views_dir / f"{domain}.parquet"
        if not src.exists():
            continue
        key_field = KEY_FIELDS[domain]
        replace_clause = ", ".join(f"deid_shift_date({key_field}, {f}) AS {f}" for f in date_fields)
        out_path = out_dir / f"{domain}.parquet"
        src_sql = f"read_parquet('{src.as_posix()}')"
        con.execute(
            f"COPY (SELECT * REPLACE ({replace_clause}) FROM {src_sql} "
            f"WHERE {key_field} IN (SELECT patient_key FROM _shiftable)) "
            f"TO '{out_path.as_posix()}' (FORMAT parquet)"
        )
        counts[domain] = con.execute(f"SELECT count(*) FROM read_parquet('{out_path.as_posix()}')").fetchone()[0]
        total = con.execute(f"SELECT count(*) FROM {src_sql}").fetchone()[0]
        excluded += total - counts[domain]
    con.close()
    counts["_excluded_unshiftable"] = excluded
    return counts
