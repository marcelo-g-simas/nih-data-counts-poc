"""Tests for the Textual ops console (site/src/data_counts/tui.py).

Demo aid, not RFI-requirement-bearing -- kept light: pure-function coverage
for every panel's data source, plus a pilot-driven rendering check for the
DQ panel and review-queue panel (the two the plan calls out explicitly).
"""
import json

import pytest

from data_counts import tui
from data_counts.audit import AuditLog
from data_counts.security_audit import SecurityAuditLog

# ---------------------------------------------------------------------------
# Pure data-loading helpers
# ---------------------------------------------------------------------------

def test_read_audit_log_empty_and_populated(tmp_path):
    assert tui.read_audit_log(tmp_path) == []
    AuditLog(tmp_path).record(stage_id="stage1", batch_id="b1", record_counts={"Patient": 3},
                               outcome="ok", reconciliation_status="n/a")
    entries = tui.read_audit_log(tmp_path)
    assert len(entries) == 1
    assert entries[0]["stage_id"] == "stage1"


def test_read_json_file_missing_and_present(tmp_path):
    assert tui.read_json_file(tmp_path / "missing.json") is None
    path = tmp_path / "present.json"
    path.write_text(json.dumps({"a": 1}))
    assert tui.read_json_file(path) == {"a": 1}


def test_read_json_file_malformed_returns_none(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not valid json")
    assert tui.read_json_file(path) is None


def test_list_review_queue_excludes_reviewed_subdir(tmp_path):
    (tmp_path / "batch1.json").write_text("[]")
    (tmp_path / "batch2.json").write_text("[]")
    reviewed = tmp_path / "reviewed"
    reviewed.mkdir()
    (reviewed / "batch0.json").write_text("[]")
    paths = tui.list_review_queue(tmp_path)
    assert [p.name for p in paths] == ["batch1.json", "batch2.json"]


def test_list_review_queue_missing_dir(tmp_path):
    assert tui.list_review_queue(tmp_path / "nope") == []


def test_acknowledge_review_item_moves_to_reviewed(tmp_path):
    item = tmp_path / "leaky.json"
    item.write_text("[]")
    dest = tui.acknowledge_review_item(item, tmp_path)
    assert dest == tmp_path / "reviewed" / "leaky.json"
    assert dest.exists()
    assert not item.exists()
    assert tui.list_review_queue(tmp_path) == []


def test_list_outbox_requests(tmp_path):
    assert tui.list_outbox_requests(tmp_path) == {}
    request_dir = tmp_path / "DC-POC-0001"
    snapshot_dir = request_dir / "abc123"
    snapshot_dir.mkdir(parents=True)
    manifest = {
        "request_id": "DC-POC-0001", "snapshot_id": "abc123", "refresh_id": "r1",
        "tables": {"patient": {"row_count": 10, "sha256": "x"}},
        "diff": {"added": ["t1"], "updated": [], "deleted": [], "late_arriving": []},
    }
    (snapshot_dir / "manifest.json").write_text(json.dumps(manifest))
    result = tui.list_outbox_requests(tmp_path)
    assert list(result.keys()) == ["DC-POC-0001"]
    assert result["DC-POC-0001"][0]["snapshot_id"] == "abc123"


def test_verify_both_logs_ok_on_untouched_logs(tmp_path):
    AuditLog(tmp_path).record(stage_id="s1", batch_id="b1", record_counts={}, outcome="ok",
                               reconciliation_status="ok")
    SecurityAuditLog(tmp_path).log_event(category="authentication", name="x", user="u",
                                          action="a", resource="r", outcome="success")
    result = tui.verify_both_logs(tmp_path)
    assert result["audit_log"]["ok"] is True
    assert result["security_log"]["ok"] is True


def test_verify_both_logs_detects_tampering(tmp_path):
    AuditLog(tmp_path).record(stage_id="s1", batch_id="b1", record_counts={}, outcome="ok",
                               reconciliation_status="ok")
    log_path = tmp_path / "audit_log.jsonl"
    tampered = log_path.read_text().replace('"s1"', '"tampered"')
    log_path.write_text(tampered)
    result = tui.verify_both_logs(tmp_path)
    assert result["audit_log"]["ok"] is False


# ---------------------------------------------------------------------------
# Widget-level rendering (via Textual's pilot harness)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dq_panel_renders_checks_and_manifest(tmp_path):
    report = {
        "halt": True,
        "checks": [
            {"id": "c1", "kahn_category": "completeness", "type": "completeness",
             "on_fail": "warn", "anomaly": False, "detail": {}},
            {"id": "c2", "kahn_category": "conformance", "type": "uniqueness",
             "on_fail": "halt", "anomaly": True, "detail": {}},
        ],
    }
    (tmp_path / "dq_anomaly_report.json").write_text(json.dumps(report))
    manifest = {"any_dq_halt": True, "any_dq_anomaly": True, "leak_gate_halted": False,
                "record_counts_bucketed": {}}
    (tmp_path / "qa_manifest.json").write_text(json.dumps(manifest))

    app = tui.OpsConsoleApp(work_dir=tmp_path, review_queue_dir=tmp_path / "rq",
                             outbox_dir=tmp_path / "outbox", refresh_interval=0)
    async with app.run_test() as _pilot:
        panel = app.query_one(tui.DQPanel)
        summary_text = panel.query_one("#dq-summary").content
        assert "HALT" in str(summary_text)
        assert "any_dq_halt=True" in str(summary_text)
        table = panel.query_one("#dq-table")
        assert table.row_count == 2


@pytest.mark.asyncio
async def test_review_queue_panel_lists_batches_and_acknowledges(tmp_path):
    review_dir = tmp_path / "review_queue"
    review_dir.mkdir()
    flagged = [
        {"record_id": "n1", "text": "...", "hits": [{"detector": "presidio", "entity_type": "PERSON",
                                                       "start": 0, "end": 3, "score": 0.9}]},
    ]
    (review_dir / "batch1.json").write_text(json.dumps(flagged))

    app = tui.OpsConsoleApp(work_dir=tmp_path / "work", review_queue_dir=review_dir,
                             outbox_dir=tmp_path / "outbox", refresh_interval=0)
    async with app.run_test() as pilot:
        panel = app.query_one(tui.ReviewQueuePanel)
        table = panel.query_one("#review-table")
        assert table.row_count == 1
        summary_text = str(panel.query_one("#review-summary").content)
        assert "1 batch" in summary_text

        # Move focus into the review panel's table and acknowledge the row.
        table.focus()
        table.move_cursor(row=0)
        await pilot.press("a")
        assert not (review_dir / "batch1.json").exists()
        assert (review_dir / "reviewed" / "batch1.json").exists()
        assert table.row_count == 0


@pytest.mark.asyncio
async def test_run_panel_reflects_audit_log_entries(tmp_path):
    work_dir = tmp_path / "work"
    AuditLog(work_dir).record(stage_id="stage1_synthea", batch_id="cohort",
                               record_counts={"Patient": 5}, outcome="ok",
                               reconciliation_status="n/a")

    app = tui.OpsConsoleApp(work_dir=work_dir, review_queue_dir=tmp_path / "rq",
                             outbox_dir=tmp_path / "outbox", refresh_interval=0)
    async with app.run_test() as _pilot:
        panel = app.query_one(tui.RunPanel)
        table = panel.query_one("#run-table")
        assert table.row_count == 1


@pytest.mark.asyncio
async def test_outbox_panel_shows_manifest_diff(tmp_path):
    outbox_dir = tmp_path / "outbox"
    snapshot_dir = outbox_dir / "DC-POC-0001" / "abc123"
    snapshot_dir.mkdir(parents=True)
    manifest = {
        "request_id": "DC-POC-0001", "snapshot_id": "abc123", "refresh_id": "r1",
        "tables": {"patient": {"row_count": 10, "sha256": "x"}},
        "diff": {"added": ["t1", "t2"], "updated": [], "deleted": [], "late_arriving": []},
    }
    (snapshot_dir / "manifest.json").write_text(json.dumps(manifest))

    app = tui.OpsConsoleApp(work_dir=tmp_path / "work", review_queue_dir=tmp_path / "rq",
                             outbox_dir=outbox_dir, refresh_interval=0)
    async with app.run_test() as _pilot:
        panel = app.query_one(tui.OutboxPanel)
        summary_text = str(panel.query_one("#outbox-summary").content)
        assert "1 request" in summary_text


@pytest.mark.asyncio
async def test_audit_panel_reverify_binding(tmp_path):
    work_dir = tmp_path / "work"
    AuditLog(work_dir).record(stage_id="s1", batch_id="b1", record_counts={}, outcome="ok",
                               reconciliation_status="ok")

    app = tui.OpsConsoleApp(work_dir=work_dir, review_queue_dir=tmp_path / "rq",
                             outbox_dir=tmp_path / "outbox", refresh_interval=0)
    async with app.run_test() as pilot:
        panel = app.query_one(tui.AuditPanel)
        status_text = str(panel.query_one("#audit-status").content)
        assert "INTACT" in status_text

        # Tamper after initial render, then re-verify live via the binding.
        log_path = work_dir / "audit_log.jsonl"
        log_path.write_text(log_path.read_text().replace('"s1"', '"tampered"'))
        panel.focus()
        await pilot.press("v")
        status_text = str(panel.query_one("#audit-status").content)
        assert "TAMPERING DETECTED" in status_text
