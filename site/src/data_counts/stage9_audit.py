"""Stage 9: provenance & audit trail, PII-free QA manifest, RFI summary. [Req 1b, 9, 11]

The per-stage audit log itself is written incrementally by every stage via
`audit.AuditLog` (see audit.py). This module assembles the run-level
PII-free QA manifest (booleans + bucketed/suppressed counts only) and the
human-readable RFI-requirement summary emitted at the end of `run-all`.
"""
from __future__ import annotations

import json
import platform
import sys
from pathlib import Path

from . import stage7_leak_gate
from .audit import AuditLog, bucket_count

# The runtime half of ../../../REQUIREMENTS.md, which is the authoritative index
# joining the RFI's requirement numbering to this repo's stage numbering. Only
# requirements with running code appear here; REQUIREMENTS.md additionally
# carries the architecture-only ones (7, 10, 12, 14) and the named gaps behind
# each `partial`. tests/test_requirements_sync.py asserts the two agree on
# requirement numbers and statuses, so editing one without the other fails.
#
# Fields: (req_num, status, module, description). `status` mirrors
# REQUIREMENTS.md's Status column: "implemented" or "partial".
RFI_MAP = [
    ("1", "partial", "Containerfile / Makefile (podman OCI packaging, site-generated keys)", "Durable leave-behind artifact operable by site staff"),
    ("1a", "partial", "Stage 1 (Synthea + bulk-export) / Stage 2a (smart-fetch + cumulus-fhir-support landing)", "Extraction from EMR via a FHIR Bulk Data $export-shaped source"),
    ("1b", "partial", "Stage 9 (audit.py, hash_chain.py)", "Per-stage provenance/audit trail on a hash-chained append-only log"),
    ("2", "partial", "Stage 3 (drs.py / stage3_drs.py)", "DRS-driven orchestration, no silent substitutions"),
    ("3", "implemented", "Stage 4 (stage4_dq_kahn.py) / halt path in cli.py", "Kahn-framework DQ checks; halt on unresolved anomaly"),
    ("4", "implemented", "Stage 5 (stage5_deid.py)", "18 identifiers per 164.514(b)(2) removed; per-patient order-preserving date-shift (not a Safe Harbor claim)"),
    ("5", "implemented", "Stage 6 (stage6_pprl.py)", "OPPRL v2 tokenization, DuckDB-native, both-ends transcrypt linkage"),
    ("6", "partial", "Stage 2b (fhir_views/*.json + viewdef.py) / Stage 2a (parquet-on-fhir landing)", "Lossless round-trippable raw layer + traceable analytic views"),
    ("8", "partial", "Stage 8 (stage8_package.py)", "Versioned package, idempotent reruns, snapshot/refresh IDs, added/deleted"),
    ("9", "partial", "Stage 9 (audit.py, this module)", "Audit trail + PII-free QA manifest; halt before egress on anomaly"),
    ("11", "implemented", "security_audit.py, hash_chain.py, Stage 9 (model versions)", "Hash-chained CEF security audit log; model versions + DRS version logged"),
    ("12a", "implemented", "whole-repo property; Containerfile / Makefile", "No listening socket, no exposed port, rootless daemonless runtime"),
    ("13", "implemented", "site/ vs central/ project split; Stage 7 (stage7_leak_gate.py)", "Identifiable processing confined on-site; independent residual-PII gate (Presidio + Philter + GLiNER)"),
    ("15", "partial", "Stage 8 (stage8_package.py)", "Versioned package with per-file digests + row counts; refresh-cycle change tracking"),
]

PINNED_MODELS = {
    "gliner": f"{stage7_leak_gate.GLINER_MODEL_ID} (fast_gliner: Rust/gline-rs + ONNX Runtime, fp32, CPU)",
    "presidio_nlp_engine": stage7_leak_gate.PRESIDIO_SPACY_MODEL,
}


def build_qa_manifest(record_counts: dict[str, int], dq_result: dict, leak_result: dict,
                       k_threshold: int = 11) -> dict:
    return {
        "any_dq_halt": dq_result.get("halt", False),
        "any_dq_anomaly": any(c["anomaly"] for c in dq_result.get("checks", [])),
        "leak_gate_halted": leak_result.get("halt", False),
        "record_counts_bucketed": {k: bucket_count(v, k_threshold) for k, v in record_counts.items()},
    }


def write_qa_manifest(manifest: dict, work_dir: str | Path = "data/work") -> Path:
    path = Path(work_dir) / "qa_manifest.json"
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    return path


def write_rfi_summary(drs_request_id: str, drs_version: str, halted: bool,
                       work_dir: str | Path = "data/work") -> Path:
    lines = [
        "Data COUNTS Leave-Behind POC -- run summary",
        "SYNTHETIC DATA ONLY. No real PHI.",
        f"request_id={drs_request_id}  drs_version={drs_version}  python={platform.python_version()}",
        f"outcome: {'HALTED' if halted else 'COMPLETED'}",
        "",
        "RFI requirements exercised (see REQUIREMENTS.md for the full index,",
        "including the architecture-only requirements and each gap behind 'partial'):",
    ]
    for req_num, status, module, description in RFI_MAP:
        lines.append(f"  [Req {req_num}: {status}] {module} -- {description}")
    text = "\n".join(lines) + "\n"
    path = Path(work_dir) / "rfi_summary.txt"
    path.write_text(text)
    return path


def log_model_versions(audit_log: AuditLog, drs_version: str, extra_models: dict | None = None) -> None:
    models = dict(PINNED_MODELS)
    if extra_models:
        models.update(extra_models)
    audit_log.record(
        stage_id="stage9_audit",
        batch_id="model-versions",
        record_counts={},
        outcome="logged",
        reconciliation_status="n/a",
        extra={"drs_version": drs_version, "python_version": sys.version, "pinned_models": models},
    )
