"""Security Audit Logging, central-platform half. [Req 11]

Duplicated from `site/`'s copy (same rationale as `crypto.py`: two
independently deployed projects, no shared runtime dependency), with
`DEVICE_PRODUCT` changed so a SIEM ingesting logs from both projects can tell
them apart. See `site/src/data_counts/security_audit.py` for the full
rationale on CEF as the emission format.
"""
from __future__ import annotations

import time
from pathlib import Path

from .hash_chain import ChainedLog

CEF_VERSION = "0"
DEVICE_VENDOR = "NIHDataCOUNTS"
DEVICE_PRODUCT = "central-platform"
DEVICE_VERSION = "1.0.0"

EVENT_CLASSES = {
    "authentication": {"id": "AUTH", "severity": 3},
    "administrative_action": {"id": "ADMIN", "severity": 3},
    "configuration_change": {"id": "CONFIG", "severity": 4},
    "security_event": {"id": "SECURITY", "severity": 8},
}


def _escape_header(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("|", "\\|")


def _escape_extension(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("=", "\\=").replace(" ", "_")


def _build_content(name: str, severity: int, event_class_id: str, timestamp_ms: int,
                    user: str, action: str, resource: str, outcome: str, message: str) -> str:
    header = "|".join([
        f"CEF:{CEF_VERSION}", DEVICE_VENDOR, DEVICE_PRODUCT, DEVICE_VERSION,
        _escape_header(event_class_id), _escape_header(name), str(severity),
    ])
    extension = " ".join([
        f"rt={timestamp_ms}",
        f"suser={_escape_extension(user)}",
        f"act={_escape_extension(action)}",
        f"outcome={_escape_extension(outcome)}",
        f"cs1Label=Resource", f"cs1={_escape_extension(resource)}",
        f"msg={_escape_extension(message)}",
    ])
    return f"{header}|{extension}"


class SecurityAuditLog(ChainedLog):
    def __init__(self, work_dir: str | Path = "."):
        self.work_dir = Path(work_dir)
        super().__init__(self.work_dir / "security_audit.cef.log")

    def _format_entry(self, entry: dict, prev_hash: str, entry_hash: str) -> str:
        content = entry["_content"]
        return f"{content} cs2Label=PrevHash cs2={prev_hash} cs3Label=EntryHash cs3={entry_hash}"

    def _parse_entry(self, line: str) -> tuple[bytes, str, str]:
        parts = line.rsplit(" ", 4)
        if len(parts) != 5 or not parts[1].startswith("cs2Label=") or not parts[3].startswith("cs3Label="):
            raise ValueError(f"Malformed security audit log line: {line!r}")
        content = parts[0]
        prev_hash = parts[2].split("cs2=", 1)[1]
        entry_hash = parts[4].split("cs3=", 1)[1]
        return content.encode(), prev_hash, entry_hash

    def log_event(self, *, category: str, name: str, user: str, action: str,
                  resource: str, outcome: str, message: str = "") -> str:
        event_class = EVENT_CLASSES[category]
        timestamp_ms = int(time.time() * 1000)
        content = _build_content(
            name, event_class["severity"], event_class["id"], timestamp_ms,
            user, action, resource, outcome, message,
        )
        return self.append({"_content": content}, content.encode())
