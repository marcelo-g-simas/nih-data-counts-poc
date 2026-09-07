"""Security Audit Logging. [Req 11]

Emits CEF (Common Event Format) lines -- the vendor-neutral format Splunk,
Microsoft Sentinel, IBM QRadar, and ArcSight all parse natively, either via a
file-monitoring forwarder (Splunk Universal Forwarder, Filebeat, syslog-ng
tailing this file) or a syslog relay, without a bespoke connector. This POC
has no live syslog daemon/network sink to send to, so it writes CEF lines to
a file (`data/work/security_audit.cef.log`) that any of those forwarders can
tail -- "ready to be ingested by COTS SIEM-compatible solutions" without this
pipeline needing to know which one a given Healthcare Partner site runs.

Distinct from `audit.py`'s `AuditLog`: that's the Req 9 *processing* audit
trail (per-stage record counts/outcome/reconciliation). This is the Req 11
*security* audit log -- authentication, administrative actions, configuration
changes, and security events, each with timestamp/user/action/resource/outcome
per the RFI's explicit field list. Both are hash-chained the same way (see
hash_chain.py); this one additionally survives being read by a SIEM that has
no idea what a hash chain is -- the chain fields are just two more CEF custom
extensions (`cs2`/`cs3`) that an unaware parser ignores or indexes as extra
searchable fields, either way harmlessly.
"""
from __future__ import annotations

from pathlib import Path

from .hash_chain import ChainedLog

CEF_VERSION = "0"
DEVICE_VENDOR = "NIHDataCOUNTS"
DEVICE_PRODUCT = "site-leave-behind"
DEVICE_VERSION = "1.0.0"

# RFI Req 11 groups events into four categories; EVENT_CLASSES gives each a
# stable Event Class ID (CEF's `deviceEventClassId`) and a default severity
# (CEF 0-10 scale) so a SIEM's out-of-the-box CEF rules can triage without
# per-deployment configuration.
EVENT_CLASSES = {
    "authentication": {"id": "AUTH", "severity": 3},
    "administrative_action": {"id": "ADMIN", "severity": 3},
    "configuration_change": {"id": "CONFIG", "severity": 4},
    "security_event": {"id": "SECURITY", "severity": 8},
}


def _escape_header(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("|", "\\|")


def _escape_extension(value: str) -> str:
    # Kept space-free at the call site (see log_event) so this POC's own reader
    # can round-trip lines with simple whitespace-delimited extension parsing,
    # rather than implementing CEF's full quoted-value grammar.
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
    def __init__(self, work_dir: str | Path = "data/work"):
        self.work_dir = Path(work_dir)
        super().__init__(self.work_dir / "security_audit.cef.log")

    def _format_entry(self, entry: dict, prev_hash: str, entry_hash: str) -> str:
        content = entry["_content"]
        return f"{content} cs2Label=PrevHash cs2={prev_hash} cs3Label=EntryHash cs3={entry_hash}"

    def _parse_entry(self, line: str) -> tuple[bytes, str, str]:
        # The hash-chain extensions are always the last two key=value pairs this
        # module writes (see _format_entry); strip exactly those to recover the
        # content string that was hashed at write time.
        parts = line.rsplit(" ", 4)
        if len(parts) != 5 or not parts[1].startswith("cs2Label=") or not parts[3].startswith("cs3Label="):
            raise ValueError(f"Malformed security audit log line: {line!r}")
        content = parts[0]
        prev_hash = parts[2].split("cs2=", 1)[1]
        entry_hash = parts[4].split("cs3=", 1)[1]
        return content.encode(), prev_hash, entry_hash

    def log_event(self, *, category: str, name: str, user: str, action: str,
                  resource: str, outcome: str, message: str = "") -> str:
        """Record one security-relevant event. `category` selects the RFI's four
        event groupings (authentication / administrative_action /
        configuration_change / security_event) and their default severity.
        """
        import time

        event_class = EVENT_CLASSES[category]
        timestamp_ms = int(time.time() * 1000)
        content = _build_content(
            name, event_class["severity"], event_class["id"], timestamp_ms,
            user, action, resource, outcome, message,
        )
        return self.append({"_content": content}, content.encode())
