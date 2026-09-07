"""Stage 9 support: shared provenance/audit-log writer used by every stage.

Every stage appends one JSON record per batch:
`timestamp, stage_id, batch_id, record_counts, outcome, reconciliation_status`.
The audit log is append-only JSONL under data/work/audit_log.jsonl. [Req 1b, 9, 11]
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


class PipelineHalt(RuntimeError):
    """Raised to stop run-all: failed reconciliation or an unresolved leak."""


class AuditLog:
    def __init__(self, work_dir: str | Path = "data/work"):
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.work_dir / "audit_log.jsonl"

    def record(
        self,
        *,
        stage_id: str,
        batch_id: str,
        record_counts: dict[str, int],
        outcome: str,
        reconciliation_status: str,
        extra: dict[str, Any] | None = None,
    ) -> dict:
        entry = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "stage_id": stage_id,
            "batch_id": batch_id,
            "record_counts": record_counts,
            "outcome": outcome,
            "reconciliation_status": reconciliation_status,
        }
        if extra:
            entry["extra"] = extra
        with self.path.open("a") as f:
            f.write(json.dumps(entry, sort_keys=True) + "\n")
        return entry

    def all_records(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]


def bucket_count(n: int, k_threshold: int = 11, bucket_size: int = 10) -> str:
    """Suppress small cells (<k_threshold) and bucket the rest for the PII-free QA manifest."""
    if n < k_threshold:
        return f"<{k_threshold}"
    floor = (n // bucket_size) * bucket_size
    return f"{floor}-{floor + bucket_size - 1}"
