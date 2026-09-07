"""Tamper-evident append-only log chaining. [Req 11]

Not "tamper-proof" in the sense of write-once storage this POC doesn't have --
what this gives you is tamper-*evident*: every entry's hash is computed over
its own content plus the previous entry's hash, so editing or deleting any
entry (or reordering them, or truncating the file) breaks the chain from
that point forward in a way `verify_chain()` detects. A file editor could
still recompute the whole chain to hide a single edit *if* they also control
the sidecar `.chainhead` file this module keeps outside the log itself -- see
`ChainedLog.verify()`'s docstring for why that sidecar matters.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

GENESIS_HASH = "0" * 64


def compute_hash(canonical_bytes: bytes, prev_hash: str) -> str:
    return hashlib.sha256(prev_hash.encode() + canonical_bytes).hexdigest()


class ChainedLog:
    """Base class for an append-only log where each entry embeds a hash of
    itself chained to the previous entry's hash. Subclasses implement the
    actual on-disk line format (`_format_entry`/`_parse_entry`); this class
    handles the chaining, the sidecar chain-head file, and verification.
    """

    def __init__(self, log_path: str | Path):
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        # The chain head lives in its own small file, not derived by re-reading
        # the (potentially large) log on every append -- and, importantly, not
        # itself part of the log: an attacker who edits one entry AND
        # recomputes every hash after it in the log file still has to also
        # patch this separate file to make the chain look unbroken, which is
        # one more thing that has to go right for tampering to go undetected.
        self.chainhead_path = self.log_path.with_suffix(self.log_path.suffix + ".chainhead")

    def _read_chainhead(self) -> str:
        if self.chainhead_path.exists():
            return self.chainhead_path.read_text().strip()
        return GENESIS_HASH

    def _write_chainhead(self, new_hash: str) -> None:
        self.chainhead_path.write_text(new_hash)

    def _format_entry(self, entry: dict, prev_hash: str, entry_hash: str) -> str:
        raise NotImplementedError

    def _parse_entry(self, line: str) -> tuple[bytes, str, str]:
        """Return (canonical_bytes_to_hash, prev_hash, claimed_entry_hash) for one line."""
        raise NotImplementedError

    def append(self, entry: dict, canonical_bytes: bytes) -> str:
        prev_hash = self._read_chainhead()
        entry_hash = compute_hash(canonical_bytes, prev_hash)
        line = self._format_entry(entry, prev_hash, entry_hash)
        with self.log_path.open("a") as f:
            f.write(line + "\n")
        self._write_chainhead(entry_hash)
        return entry_hash

    def verify(self) -> dict:
        """Recompute the chain from genesis and compare against both the hashes
        embedded in each line and the current chain-head sidecar file.
        """
        if not self.log_path.exists():
            return {"ok": True, "records_checked": 0, "first_break_at": None}

        prev_hash = GENESIS_HASH
        records_checked = 0
        for i, line in enumerate(self.log_path.read_text().splitlines()):
            if not line.strip():
                continue
            canonical_bytes, claimed_prev, claimed_hash = self._parse_entry(line)
            expected_hash = compute_hash(canonical_bytes, prev_hash)
            if claimed_prev != prev_hash or claimed_hash != expected_hash:
                return {"ok": False, "records_checked": records_checked, "first_break_at": i}
            prev_hash = expected_hash
            records_checked += 1

        chainhead_ok = self._read_chainhead() == prev_hash
        return {
            "ok": chainhead_ok,
            "records_checked": records_checked,
            "first_break_at": None if chainhead_ok else "chainhead_mismatch",
        }
