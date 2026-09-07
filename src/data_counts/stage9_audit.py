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

RFI_MAP = [
    ("1a", "Stage 1 (Synthea + bulk-export) / Stage 2a (smart-fetch + cumulus-fhir-support landing)", "Extraction from EMR via a FHIR Bulk Data $export-shaped source"),
    ("1b", "Stage 9 (audit.py)", "Per-stage provenance/audit trail"),
    ("2", "Stage 3 (dsr.py / stage3_dsr.py)", "DSR-driven orchestration, no silent substitutions"),
    ("3", "Stage 4 (stage4_dq_kahn.py) / Stage 7 (halt-on-anomaly)", "Kahn-framework DQ checks; halt on unresolved anomaly"),
    ("4", "Stage 5 (stage5_deid.py)", "18 HIPAA identifiers removed; per-patient date-shift"),
    ("5", "Stage 6 (stage6_pprl.py)", "OPPRL v2 tokenization, DuckDB-native, both-ends transcrypt linkage"),
    ("6", "Stage 2b (fhir_views/*.json + viewdef.py) / Stage 2a (parquet-on-fhir landing)", "Lossless round-trippable raw layer + traceable analytic views"),
    ("8", "Stage 8 (stage8_package.py)", "Versioned package, idempotent reruns, added/updated/deleted/late-arriving"),
    ("9", "Stage 9 (audit.py, this module)", "Audit trail + PII-free QA manifest"),
    ("11", "Stage 9 (audit.py)", "Model versions/hashes + DSR version logged for reproducibility"),
    ("13", "Stage 7 (stage7_leak_gate.py)", "Independent residual PII-leakage QA gate (Presidio + Philter + GLiNER)"),
    ("15", "Stage 8 (stage8_package.py)", "Refresh-cycle record change tracking"),
]

PINNED_MODELS = {
    "gliner": stage7_leak_gate.GLINER_MODEL_ID,
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


def write_rfi_summary(dsr_request_id: str, dsr_version: str, halted: bool,
                       work_dir: str | Path = "data/work") -> Path:
    lines = [
        "Data COUNTS Leave-Behind POC -- run summary",
        "SYNTHETIC DATA ONLY. No real PHI.",
        f"request_id={dsr_request_id}  dsr_version={dsr_version}  python={platform.python_version()}",
        f"outcome: {'HALTED' if halted else 'COMPLETED'}",
        "",
        "RFI requirements exercised:",
    ]
    for req_num, module, description in RFI_MAP:
        lines.append(f"  [Req {req_num}] {module} -- {description}")
    text = "\n".join(lines) + "\n"
    path = Path(work_dir) / "rfi_summary.txt"
    path.write_text(text)
    return path


def log_model_versions(audit_log: AuditLog, dsr_version: str, extra_models: dict | None = None) -> None:
    models = dict(PINNED_MODELS)
    if extra_models:
        models.update(extra_models)
    audit_log.record(
        stage_id="stage9_audit",
        batch_id="model-versions",
        record_counts={},
        outcome="logged",
        reconciliation_status="n/a",
        extra={"dsr_version": dsr_version, "python_version": sys.version, "pinned_models": models},
    )
