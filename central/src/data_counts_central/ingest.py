"""Ingest a Healthcare Partner site's exported package and resolve its ephemeral
OPPRL tokens into this platform's own consistent token space. [Req 5]

A site export is a directory containing a `patient.parquet` with columns
`local_patient_id, token1, token2, token3` -- all three RSA-encrypted under
this platform's public key (see `site/`'s `stage6_pprl.export_site_roster()`
/ `stage8_package.py`). `local_patient_id` is a site-internal join key only;
it is never used here except to keep a resolved row traceable back to one
site's own record during this run, and is dropped before any cross-site
comparison happens.
"""
from __future__ import annotations

from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from .crypto import derive_aes_key_v2, transcrypt_in

TOKEN_COLUMNS = ("token1", "token2", "token3")


def resolve_site_package(site_dir: str | Path, site_name: str, central_priv: bytes) -> pa.Table:
    """Read one site's `patient.parquet`, transcrypt_in each ephemeral token column.

    Returns a table with `source_dataset` (the site name, for Splink's
    link_only mode) and `token1`/`token2`/`token3` now resolved into this
    platform's own AES token space -- the same value for the same person
    regardless of which site sent it, without this platform ever having
    decrypted a raw identifying attribute.
    """
    site_dir = Path(site_dir)
    table = pq.read_table(site_dir / "patient.parquet")
    aes_key = derive_aes_key_v2(central_priv)

    con = duckdb.connect()
    con.create_function(
        "central_transcrypt_in", lambda t: transcrypt_in(t, central_priv, aes_key), ["VARCHAR"], "VARCHAR",
    )
    con.register("site_table", table)
    resolved = con.execute(
        "SELECT local_patient_id, "
        + ", ".join(f"central_transcrypt_in({c}) AS {c}" for c in TOKEN_COLUMNS)
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
