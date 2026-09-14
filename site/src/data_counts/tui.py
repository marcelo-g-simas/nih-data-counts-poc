"""Textual TUI ops console for site operators. [demo aid -- not RFI-requirement-bearing]

Reads the same files the pipeline already writes -- no new persistence layer,
no new gate/approval logic. Five panels, each backed by a small set of pure
data-loading functions (kept free of Textual so they're independently
testable and reusable outside the app):

  - Run:            tails ``data/work/audit_log.jsonl`` (per-stage status).
  - DQ:             renders ``dq_anomaly_report.json`` / ``qa_manifest.json``.
  - Review queue:   lists ``data/review_queue/*.json`` (leak-gate flags) with
                     a demo-only "acknowledge" action that moves a file to a
                     ``reviewed/`` subdir -- pure UI triage, no new gate logic.
  - Outbox:         shows ``outbox/<request_id>/`` package contents + the
                     manifest diff ``stage8_package.py`` already writes.
  - Audit/security: chain-head status for both hash-chained logs, plus a
                     manual re-verify keybinding calling ``.verify()`` live
                     (the same call ``verify-logs`` makes -- see cli.py).
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Static,
    TabbedContent,
    TabPane,
    Tree,
)

from .audit import AuditLog
from .security_audit import SecurityAuditLog

REFRESH_INTERVAL_SECONDS = 2.0


# ---------------------------------------------------------------------------
# Pure data-loading helpers -- no Textual dependency, so tests (and the
# panels below) can call them directly against fixture files.
# ---------------------------------------------------------------------------

def read_audit_log(work_dir: str | Path) -> list[dict]:
    """All entries currently in ``audit_log.jsonl``, oldest first."""
    return AuditLog(work_dir).all_records()


def read_json_file(path: str | Path) -> dict | list | None:
    path = Path(path)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


def list_review_queue(review_queue_dir: str | Path) -> list[Path]:
    """Pending (not-yet-acknowledged) flagged batches, i.e. everything directly
    under ``review_queue_dir`` -- the ``reviewed/`` subdir is excluded."""
    review_queue_dir = Path(review_queue_dir)
    if not review_queue_dir.exists():
        return []
    return sorted(p for p in review_queue_dir.glob("*.json") if p.is_file())


def acknowledge_review_item(item_path: str | Path, review_queue_dir: str | Path) -> Path:
    """Demo-only triage action: move a flagged-batch file to a ``reviewed/``
    subdir. This is a pure UI acknowledgement -- the leak-gate halt already
    happened in ``stage7_leak_gate.run()``; nothing here re-evaluates it.
    """
    item_path = Path(item_path)
    reviewed_dir = Path(review_queue_dir) / "reviewed"
    reviewed_dir.mkdir(parents=True, exist_ok=True)
    dest = reviewed_dir / item_path.name
    shutil.move(str(item_path), str(dest))
    return dest


def list_outbox_requests(outbox_dir: str | Path) -> dict[str, list[dict]]:
    """``{request_id: [manifest dict, ...]}``, snapshots oldest-to-newest
    (mirrors what ``stage8_package.py`` writes under each ``<snapshot_id>/``)."""
    outbox_dir = Path(outbox_dir)
    if not outbox_dir.exists():
        return {}
    out: dict[str, list[dict]] = {}
    for request_dir in sorted(p for p in outbox_dir.iterdir() if p.is_dir()):
        manifests = []
        for manifest_path in sorted(request_dir.glob("*/manifest.json")):
            manifest = read_json_file(manifest_path)
            if manifest is not None:
                manifests.append(manifest)
        if manifests:
            out[request_dir.name] = manifests
    return out


def verify_both_logs(work_dir: str | Path) -> dict[str, dict]:
    """Mirrors the exact pattern ``cli.py``'s ``verify-logs`` command uses."""
    return {
        "audit_log": AuditLog(work_dir).verify(),
        "security_log": SecurityAuditLog(work_dir).verify(),
    }


# ---------------------------------------------------------------------------
# Panels
# ---------------------------------------------------------------------------

class RunPanel(Vertical):
    """Tails data/work/audit_log.jsonl -- works whether the TUI is launched
    before or during a `run-all` in another terminal."""

    def __init__(self, work_dir: str | Path, **kwargs):
        super().__init__(**kwargs)
        self.work_dir = Path(work_dir)

    def compose(self) -> ComposeResult:
        yield Static(id="run-summary")
        yield DataTable(id="run-table")

    def on_mount(self) -> None:
        self.query_one("#run-table", DataTable).add_columns(
            "Timestamp", "Stage", "Batch", "Outcome", "Reconciliation", "Record Counts"
        )
        self.refresh_data()

    def refresh_data(self) -> None:
        entries = read_audit_log(self.work_dir)
        table = self.query_one("#run-table", DataTable)
        table.clear()
        for entry in reversed(entries):
            counts = entry.get("record_counts") or {}
            counts_str = ", ".join(f"{k}={v}" for k, v in list(counts.items())[:4])
            outcome = entry.get("outcome", "")
            outcome_str = f"[red]{outcome}[/red]" if outcome == "halt" else outcome
            table.add_row(
                entry.get("timestamp", ""), entry.get("stage_id", ""), entry.get("batch_id", ""),
                outcome_str, entry.get("reconciliation_status", ""), counts_str,
            )
        self.query_one("#run-summary", Static).update(
            f"{len(entries)} stage record(s) in audit_log.jsonl" if entries
            else "No audit_log.jsonl entries yet -- run `data-counts run-all`."
        )


class DQPanel(Vertical):
    """Renders dq_anomaly_report.json / qa_manifest.json -- pass/halt per Kahn check."""

    def __init__(self, work_dir: str | Path, **kwargs):
        super().__init__(**kwargs)
        self.work_dir = Path(work_dir)

    def compose(self) -> ComposeResult:
        yield Static(id="dq-summary")
        yield DataTable(id="dq-table")

    def on_mount(self) -> None:
        self.query_one("#dq-table", DataTable).add_columns(
            "Check", "Kahn Category", "Type", "On Fail", "Result"
        )
        self.refresh_data()

    def refresh_data(self) -> None:
        report = read_json_file(self.work_dir / "dq_anomaly_report.json")
        manifest = read_json_file(self.work_dir / "qa_manifest.json")
        table = self.query_one("#dq-table", DataTable)
        table.clear()
        lines: list[str] = []
        if report is None:
            lines.append("No dq_anomaly_report.json yet -- run the pipeline first.")
        else:
            halt_str = "[red]HALT[/red]" if report.get("halt") else "[green]OK[/green]"
            lines.append(f"DQ overall: {halt_str}")
            for check in report.get("checks", []):
                result = "[red]ANOMALY[/red]" if check.get("anomaly") else "[green]pass[/green]"
                table.add_row(
                    check.get("id", ""), check.get("kahn_category", ""), check.get("type", ""),
                    check.get("on_fail", ""), result,
                )
        if manifest is None:
            lines.append("No qa_manifest.json yet.")
        else:
            lines.append(
                f"qa_manifest: any_dq_halt={manifest.get('any_dq_halt')}  "
                f"any_dq_anomaly={manifest.get('any_dq_anomaly')}  "
                f"leak_gate_halted={manifest.get('leak_gate_halted')}"
            )
        self.query_one("#dq-summary", Static).update("\n".join(lines))


class ReviewQueuePanel(Vertical):
    """Lists data/review_queue/*.json batches with per-detector flagged-span
    breakdown; 'a' acknowledges (moves) the selected row -- a triage view
    only, not a new approval gate."""

    BINDINGS: ClassVar = [Binding("a", "acknowledge_selected", "Acknowledge")]

    def __init__(self, review_queue_dir: str | Path, **kwargs):
        super().__init__(**kwargs)
        self.review_queue_dir = Path(review_queue_dir)
        self._paths: list[Path] = []

    def compose(self) -> ComposeResult:
        yield Static(id="review-summary")
        yield DataTable(id="review-table")

    def on_mount(self) -> None:
        self.query_one("#review-table", DataTable).add_columns("Batch", "Flagged Records", "Detectors")
        self.refresh_data()

    def refresh_data(self) -> None:
        self._paths = list_review_queue(self.review_queue_dir)
        table = self.query_one("#review-table", DataTable)
        table.clear()
        summary = self.query_one("#review-summary", Static)
        if not self._paths:
            summary.update("Review queue empty.")
            return
        summary.update(f"{len(self._paths)} batch(es) awaiting review -- press 'a' to acknowledge selected row.")
        for path in self._paths:
            flagged = read_json_file(path) or []
            detectors = sorted({hit["detector"] for record in flagged for hit in record.get("hits", [])})
            table.add_row(path.stem, str(len(flagged)), ", ".join(detectors) or "-")

    def action_acknowledge_selected(self) -> None:
        table = self.query_one("#review-table", DataTable)
        if not self._paths or table.cursor_row is None:
            return
        try:
            path = self._paths[table.cursor_row]
        except IndexError:
            return
        acknowledge_review_item(path, self.review_queue_dir)
        self.refresh_data()


class OutboxPanel(Vertical):
    """Shows outbox/<request_id>/ contents and the manifest diff (added/
    updated/deleted) stage8_package.py already writes."""

    def __init__(self, outbox_dir: str | Path, **kwargs):
        super().__init__(**kwargs)
        self.outbox_dir = Path(outbox_dir)

    def compose(self) -> ComposeResult:
        yield Static(id="outbox-summary")
        yield Tree("outbox", id="outbox-tree")

    def on_mount(self) -> None:
        self.refresh_data()

    def refresh_data(self) -> None:
        requests = list_outbox_requests(self.outbox_dir)
        tree = self.query_one("#outbox-tree", Tree)
        tree.clear()
        summary = self.query_one("#outbox-summary", Static)
        if not requests:
            summary.update("outbox/ empty -- no packages produced yet.")
            return
        summary.update(f"{len(requests)} request(s) in outbox/.")
        for request_id, manifests in requests.items():
            request_node = tree.root.add(request_id, expand=True)
            for manifest in manifests:
                diff = manifest.get("diff", {})
                snapshot_label = (
                    f"{manifest.get('snapshot_id')} ({manifest.get('refresh_id')}): "
                    f"+{len(diff.get('added', []))} added / "
                    f"{len(diff.get('updated', []))} updated / "
                    f"-{len(diff.get('deleted', []))} deleted"
                )
                snapshot_node = request_node.add(snapshot_label, expand=False)
                for table_name, info in manifest.get("tables", {}).items():
                    snapshot_node.add_leaf(f"{table_name}: {info.get('row_count')} rows")


class AuditPanel(Vertical):
    """Chain-head status for both hash-chained logs, plus a manual
    re-verify keybinding calling .verify() live -- `make verify-logs` made
    interactive."""

    BINDINGS: ClassVar = [Binding("v", "reverify", "Re-verify")]
    can_focus = True

    def __init__(self, work_dir: str | Path, **kwargs):
        super().__init__(**kwargs)
        self.work_dir = Path(work_dir)

    def compose(self) -> ComposeResult:
        yield Static(id="audit-status")

    def on_mount(self) -> None:
        self.refresh_data()

    def refresh_data(self) -> None:
        results = verify_both_logs(self.work_dir)
        lines = []
        for label, result in (("Processing audit log (Req 9)", results["audit_log"]),
                               ("Security audit log (Req 11)", results["security_log"])):
            status = "[green]INTACT[/green]" if result["ok"] else "[red]TAMPERING DETECTED[/red]"
            lines.append(f"{label}: {status}  ({result['records_checked']} records checked)")
            if not result["ok"]:
                lines.append(f"  first break at: {result['first_break_at']}")
        lines.append("")
        lines.append("[dim]press 'v' to re-verify live[/dim]")
        self.query_one("#audit-status", Static).update("\n".join(lines))

    def action_reverify(self) -> None:
        self.refresh_data()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

class OpsConsoleApp(App):
    """Pipeline ops console: run status, DQ, review-queue triage, outbox
    contents, and audit/security log verification, all reading files the
    pipeline already writes."""

    TITLE = "Data COUNTS -- Site Ops Console"
    BINDINGS: ClassVar = [Binding("r", "refresh_all", "Refresh"), Binding("q", "quit", "Quit")]

    def __init__(
        self,
        work_dir: str | Path = "data/work",
        review_queue_dir: str | Path = "data/review_queue",
        outbox_dir: str | Path = "outbox",
        refresh_interval: float = REFRESH_INTERVAL_SECONDS,
    ):
        super().__init__()
        self.work_dir = Path(work_dir)
        self.review_queue_dir = Path(review_queue_dir)
        self.outbox_dir = Path(outbox_dir)
        self.refresh_interval = refresh_interval

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent():
            with TabPane("Run", id="tab-run"):
                yield RunPanel(self.work_dir, id="run-panel")
            with TabPane("DQ", id="tab-dq"):
                yield DQPanel(self.work_dir, id="dq-panel")
            with TabPane("Review Queue", id="tab-review"):
                yield ReviewQueuePanel(self.review_queue_dir, id="review-panel")
            with TabPane("Outbox", id="tab-outbox"):
                yield OutboxPanel(self.outbox_dir, id="outbox-panel")
            with TabPane("Audit/Security", id="tab-audit"):
                yield AuditPanel(self.work_dir, id="audit-panel")
        yield Footer()

    def on_mount(self) -> None:
        if self.refresh_interval:
            self.set_interval(self.refresh_interval, self.action_refresh_all)

    def action_refresh_all(self) -> None:
        self.query_one(RunPanel).refresh_data()
        self.query_one(DQPanel).refresh_data()
        self.query_one(ReviewQueuePanel).refresh_data()
        self.query_one(OutboxPanel).refresh_data()
        self.query_one(AuditPanel).refresh_data()


def main(
    work_dir: str = "data/work",
    review_queue_dir: str = "data/review_queue",
    outbox_dir: str = "outbox",
) -> None:
    OpsConsoleApp(work_dir=work_dir, review_queue_dir=review_queue_dir, outbox_dir=outbox_dir).run()


if __name__ == "__main__":
    main()
