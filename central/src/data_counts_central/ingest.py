"""Ingest a Healthcare Partner site's exported package, resolve its ephemeral
OPPRL tokens into this platform's own consistent token space, and re-offset its
dates onto one global offset per patient. [Req 4, Req 5]

A site export is a directory containing at least a `patient.parquet`, plus
optionally the other domain tables (`condition`, `observation`, `encounter`,
`medication`). Two token-column conventions are accepted, because the site
project emits two shapes for two purposes:

  * `token1, token2, token3` alongside `local_patient_id` -- the lightweight
    roster that `site/`'s `stage6_pprl.export_site_roster()` writes for the
    noisy-linkage validation demo. No clinical columns, no dates.
  * `subject_token1, subject_token2, subject_token3` -- the real outbound
    package from `site/`'s `stage8_package.py`, which carries the de-identified
    clinical columns including shifted dates and deliberately drops every
    site-internal identifier. Every domain table carries these columns, which is
    why each can be re-offset on its own without a join.

All token columns are RSA-encrypted under this platform's public key.
`local_patient_id`, where present, is a site-internal join key only; it is never
used except to keep a resolved row traceable back to one site's own record during
this run, and is dropped before any cross-site comparison happens.

DATE RE-OFFSET (Req 4)
----------------------
Dates arriving here are already shifted by the sending site, using that site's
per-DRS offset. `reoffset_domain_tables` moves them onto this platform's single
global offset for that patient, so one individual's dates agree across every site
and every DRS -- which is what makes an interval measured across two submissions
meaningful. The mechanism is a *delta*, never a round trip through the true date:
see `crypto.resolve_and_shift`. **No true calendar date is computed anywhere in
this module.**
"""
from __future__ import annotations

from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from .crypto import (
    derive_aes_key_v2,
    derive_drs_shift_key,
    resolve_and_shift,
    transcrypt_in,
)

TOKEN_COLUMNS = ("token1", "token2", "token3")
PACKAGE_TOKEN_COLUMNS = ("subject_token1", "subject_token2", "subject_token3")

# Which columns carry a shiftable date, per domain table. Mirrors `site/`'s
# `stage5_deid.DEID_DATE_FIELDS` -- the site shifted exactly these, so these are
# exactly the ones needing the delta applied. A literal rather than inferred from
# the Parquet schema on purpose: a date-typed column the site did *not* shift must
# not be silently shifted here, and schema sniffing cannot tell the difference.
DOMAIN_DATE_COLUMNS: dict[str, tuple[str, ...]] = {
    "patient": ("birth_date",),
    "condition": ("onset_dt",),
    "observation": ("effective_dt",),
    "encounter": ("period_start", "period_end"),
    "medication": ("authored_on",),
}


def _token_columns_of(column_names) -> tuple[str, ...]:
    """Pick whichever of the two token-column conventions this table uses."""
    names = set(column_names)
    if set(PACKAGE_TOKEN_COLUMNS) <= names:
        return PACKAGE_TOKEN_COLUMNS
    if set(TOKEN_COLUMNS) <= names:
        return TOKEN_COLUMNS
    raise ValueError(
        f"table carries neither {TOKEN_COLUMNS} nor {PACKAGE_TOKEN_COLUMNS}; "
        f"found columns {sorted(names)}"
    )


def resolve_site_package(site_dir: str | Path, site_name: str, central_priv: bytes) -> pa.Table:
    """Read one site's `patient.parquet`, transcrypt_in each ephemeral token column.

    Returns a table with `source_dataset` (the site name, for Splink's link_only
    mode) and the three token columns now resolved into this platform's own AES
    token space -- the same value for the same person regardless of which site
    sent it, without this platform ever having decrypted a raw identifying
    attribute.

    Deliberately unchanged by the date-shift work: this is the linkage input, and
    linkage neither needs nor should see clinical dates. The re-offset is a
    separate pass -- see `reoffset_domain_tables`.
    """
    site_dir = Path(site_dir)
    table = pq.read_table(site_dir / "patient.parquet")
    token_cols = _token_columns_of(table.column_names)
    aes_key = derive_aes_key_v2(central_priv)

    con = duckdb.connect()
    con.create_function(
        "central_transcrypt_in", lambda t: transcrypt_in(t, central_priv, aes_key), ["VARCHAR"], "VARCHAR",
    )
    con.register("site_table", table)
    id_select = "local_patient_id, " if "local_patient_id" in table.column_names else ""
    resolved = con.execute(
        f"SELECT {id_select}"
        + ", ".join(f"central_transcrypt_in({c}) AS {c}" for c in token_cols)
        + f", '{site_name}' AS source_dataset FROM site_table"
    ).arrow()
    con.close()
    return resolved


def resolve_all_sites(inbox_dir: str | Path, central_priv: bytes) -> dict[str, pa.Table]:
    """Resolve every site subdirectory under `inbox_dir` (each holding a `patient.parquet`)."""
    inbox_dir = Path(inbox_dir)
    resolved = {}
    for site_dir in sorted(p for p in inbox_dir.iterdir() if p.is_dir()):
        if (site_dir / "patient.parquet").exists():
            resolved[site_dir.name] = resolve_site_package(site_dir, site_dir.name, central_priv)
    return resolved


def _shift_date_sql(column: str, delta_column: str) -> str:
    """SQL applying a whole-day delta while preserving any time-of-day part.

    The site preserves the clock time on FHIR `dateTime` columns
    (`stage5_deid._shift_one`), so this must too: re-offsetting an encounter's
    `period_start` must not quietly reduce it to midnight and take length-of-stay
    in hours with it. Columns arrive as VARCHAR, so the day part is shifted as a
    date and the remainder concatenated back verbatim.
    """
    day = (
        f"strftime(CAST(substr({column}, 1, 10) AS DATE) "
        f"+ CAST({delta_column} AS INTEGER) * INTERVAL 1 DAY, '%Y-%m-%d')"
    )
    return f"CASE WHEN {column} IS NULL THEN NULL ELSE {day} || substr({column}, 11) END"


def reoffset_domain_tables(
    site_dir: str | Path, central_priv: bytes, shift_secret: bytes, request_id: str,
    out_dir: str | Path, max_years: int = 3, jitter_weeks: int = 2,
) -> dict[str, int]:
    """Re-offset every date column of every domain table onto the global offset. [Req 4]

    Reads each `<domain>.parquet` in `site_dir`, computes the per-patient delta
    from that row's `subject_token1`, applies it to that domain's date columns,
    and writes the result to `out_dir`. Returns per-domain row counts.

    Correctness rests on the delta being derived from **token1 specifically**:
    the site derived its own offset from the token1 plaintext digest, so a delta
    derived from any other token would not cancel the site's offset. It also rests
    on the delta being identical for every row of every table belonging to one
    patient, which it is -- it is a pure function of that patient's digest. That is
    what preserves intervals: every date of one patient moves by the same amount,
    so every difference between two of them is unchanged.

    Tables whose token1 does not resolve, or whose delta cannot be derived, are a
    hard error rather than a passthrough: a row silently left on the sending
    site's offset would be indistinguishable from a correctly re-offset one and
    would corrupt any interval measured against it.
    """
    site_dir, out_dir = Path(site_dir), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    aes_key = derive_aes_key_v2(central_priv)
    drs_shift_key = derive_drs_shift_key(shift_secret, request_id)

    def _delta(token: str | None) -> int | None:
        return resolve_and_shift(
            token, central_priv, aes_key, drs_shift_key, shift_secret, max_years, jitter_weeks,
        )[1]

    con = duckdb.connect()
    con.create_function("central_shift_delta", _delta, ["VARCHAR"], "BIGINT")

    counts: dict[str, int] = {}
    for domain, date_cols in DOMAIN_DATE_COLUMNS.items():
        src = site_dir / f"{domain}.parquet"
        if not src.exists():
            continue
        src_sql = f"read_parquet('{src.as_posix()}')"
        available = set(pq.read_schema(src).names)
        token1_col = _token_columns_of(available)[0]
        present = [c for c in date_cols if c in available]
        if not present:
            continue
        replace_clause = ", ".join(
            f"{_shift_date_sql(c, '_delta')} AS {c}" for c in present
        )
        out_path = out_dir / f"{domain}.parquet"
        con.execute(
            f"COPY (WITH d AS (SELECT *, central_shift_delta({token1_col}) AS _delta FROM {src_sql}) "
            f"SELECT * EXCLUDE (_delta) REPLACE ({replace_clause}) FROM d) "
            f"TO '{out_path.as_posix()}' (FORMAT parquet)"
        )
        counts[domain] = con.execute(
            f"SELECT count(*) FROM read_parquet('{out_path.as_posix()}')"
        ).fetchone()[0]
    con.close()
    return counts
