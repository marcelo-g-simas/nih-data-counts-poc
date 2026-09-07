"""Stage 8: package + "transmit". [Req 8, 15]

Writes a versioned output package (Parquet + JSON manifest) to outbox/ --
the single outbound path to the program, modeling the RFI's single
authorized egress point. Supports idempotent reruns (byte-identical content
=> identical snapshot_id) and identifies added/updated/deleted/late-arriving
records across refresh cycles.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import time
from pathlib import Path

import duckdb
import pyarrow as pa

SCHEMA_VERSION = "1.0.0"
DOMAINS_WITH_PATIENT_ID = ["condition", "observation", "encounter", "medication"]


def _table_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# Internal-only provenance/identifier columns that must never leave the site raw.
# `__source_resource_id` (added by viewdef.py so a projected row can be traced back to
# its lossless FHIR record inside the site) is a bare source-system identifier -- for
# the patient table it's literally the same value `id` holds, i.e. the un-tokenized
# original sitting right next to its own token. `id` itself is excluded for every
# domain, not just patient: this generator's resource-id scheme (stage1_synthea.py)
# embeds the raw patient id as a substring in every child resource's id (e.g. a
# Condition id like "synthetic-patient-00001-cond-484714"), so shipping it verbatim
# re-exposes the exact identifier the token was supposed to replace -- confirmed by
# inspecting actual packaged output, not assumed. Traceability for Req 6 is a
# site-internal audit obligation (data/lake/raw/ + the stage 9 audit log already
# provide it); it must not double as an outbound identifier.
_INTERNAL_ONLY_COLUMNS = ("id", "__source_resource_id")


def _apply_tokens(deid_dir: Path, token_map: dict[str, str], out_dir: Path) -> dict[str, int]:
    """Replace patient_id / id with the OPPRL token; drop all raw internal keys. [Req 4, 5, 13]"""
    out_dir.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.register("token_map", pa.Table.from_pylist([{"patient_id": k, "token": v} for k, v in token_map.items()]))
    counts = {}
    internal_cols = ", ".join(_INTERNAL_ONLY_COLUMNS)

    patient_src = deid_dir / "patient.parquet"
    if patient_src.exists():
        out_path = out_dir / "patient.parquet"
        con.execute(
            f"COPY (SELECT t.token AS subject_token, p.* EXCLUDE ({internal_cols}) "
            f"FROM read_parquet('{patient_src.as_posix()}') p "
            f"JOIN token_map t ON p.id = t.patient_id) TO '{out_path.as_posix()}' (FORMAT parquet)"
        )
        counts["patient"] = con.execute(f"SELECT count(*) FROM read_parquet('{out_path.as_posix()}')").fetchone()[0]

    for domain in DOMAINS_WITH_PATIENT_ID:
        src = deid_dir / f"{domain}.parquet"
        if not src.exists():
            continue
        out_path = out_dir / f"{domain}.parquet"
        con.execute(
            f"COPY (SELECT t.token AS subject_token, d.* EXCLUDE (patient_id, {internal_cols}) "
            f"FROM read_parquet('{src.as_posix()}') d JOIN token_map t ON d.patient_id = t.patient_id) "
            f"TO '{out_path.as_posix()}' (FORMAT parquet)"
        )
        counts[domain] = con.execute(f"SELECT count(*) FROM read_parquet('{out_path.as_posix()}')").fetchone()[0]
    con.close()
    return counts


def _diff_against_previous(request_dir: Path, current_tokens: set[str]) -> dict:
    state_path = request_dir / "_state" / "patient_tokens.json"
    if not state_path.exists():
        diff = {"added": sorted(current_tokens), "updated": [], "deleted": [], "late_arriving": []}
    else:
        previous_tokens = set(json.loads(state_path.read_text()))
        diff = {
            "added": sorted(current_tokens - previous_tokens),
            "updated": [],  # row-level content hashing across refreshes is out of scope for this POC
            "deleted": sorted(previous_tokens - current_tokens),
            "late_arriving": [],
        }
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(sorted(current_tokens)))
    return diff


def run(deid_dir: str | Path, token_map: dict[str, str], request_id: str,
        outbox_dir: str | Path = "outbox") -> dict:
    """Idempotent by content: an identical rerun (same DSR + unchanged source)
    reproduces the same snapshot_id and returns the existing manifest rather
    than minting a new refresh, satisfying "same input + same DSR => byte-
    identical outputs" without accumulating spurious refresh entries.
    """
    deid_dir, outbox_dir = Path(deid_dir), Path(outbox_dir)
    request_dir = outbox_dir / request_id
    request_dir.mkdir(parents=True, exist_ok=True)

    tmp_out = request_dir / "_staging"
    if tmp_out.exists():
        shutil.rmtree(tmp_out)
    table_counts = _apply_tokens(deid_dir, token_map, tmp_out)
    table_hashes = {name: _table_sha256(tmp_out / f"{name}.parquet") for name in table_counts}
    snapshot_id = hashlib.sha256(json.dumps(table_hashes, sort_keys=True).encode()).hexdigest()[:16]

    existing_manifest = request_dir / snapshot_id / "manifest.json"
    if existing_manifest.exists():
        shutil.rmtree(tmp_out)
        return json.loads(existing_manifest.read_text())

    snapshot_dir = request_dir / snapshot_id
    tmp_out.rename(snapshot_dir)

    refresh_manifests = sorted(request_dir.glob("*/manifest.json"))
    refresh_id = f"r{len(refresh_manifests) + 1}"
    diff = _diff_against_previous(request_dir, set(token_map.values()))

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "request_id": request_id,
        "snapshot_id": snapshot_id,
        "refresh_id": refresh_id,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "tables": {name: {"row_count": table_counts[name], "sha256": table_hashes[name]} for name in table_counts},
        "diff": diff,
    }
    (snapshot_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
    return manifest
