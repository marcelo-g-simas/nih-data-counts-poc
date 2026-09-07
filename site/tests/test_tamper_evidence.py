from data_counts.audit import AuditLog
from data_counts.security_audit import SecurityAuditLog


def test_audit_log_verifies_clean(tmp_path):
    log = AuditLog(tmp_path)
    log.record(stage_id="s1", batch_id="b1", record_counts={"n": 1}, outcome="ok", reconciliation_status="ok")
    log.record(stage_id="s2", batch_id="b2", record_counts={"n": 2}, outcome="ok", reconciliation_status="ok")
    result = log.verify()
    assert result["ok"] is True
    assert result["records_checked"] == 2


def test_audit_log_detects_edited_entry(tmp_path):
    log = AuditLog(tmp_path)
    log.record(stage_id="s1", batch_id="b1", record_counts={"n": 1}, outcome="ok", reconciliation_status="ok")
    log.record(stage_id="s2", batch_id="b2", record_counts={"n": 2}, outcome="ok", reconciliation_status="ok")

    lines = log.path.read_text().splitlines()
    lines[0] = lines[0].replace('"n": 1', '"n": 999')  # tamper the first entry's content
    log.path.write_text("\n".join(lines) + "\n")

    result = log.verify()
    assert result["ok"] is False
    assert result["first_break_at"] == 0


def test_audit_log_detects_deleted_entry(tmp_path):
    log = AuditLog(tmp_path)
    log.record(stage_id="s1", batch_id="b1", record_counts={}, outcome="ok", reconciliation_status="ok")
    log.record(stage_id="s2", batch_id="b2", record_counts={}, outcome="ok", reconciliation_status="ok")
    log.record(stage_id="s3", batch_id="b3", record_counts={}, outcome="ok", reconciliation_status="ok")

    lines = log.path.read_text().splitlines()
    del lines[1]  # remove the middle entry -- the tail no longer chains from a valid prev_hash
    log.path.write_text("\n".join(lines) + "\n")

    result = log.verify()
    assert result["ok"] is False


def test_audit_log_detects_truncation_via_chainhead_mismatch(tmp_path):
    log = AuditLog(tmp_path)
    log.record(stage_id="s1", batch_id="b1", record_counts={}, outcome="ok", reconciliation_status="ok")
    log.record(stage_id="s2", batch_id="b2", record_counts={}, outcome="ok", reconciliation_status="ok")

    lines = log.path.read_text().splitlines()
    log.path.write_text(lines[0] + "\n")  # drop the last entry; each remaining line is still internally valid

    result = log.verify()
    assert result["ok"] is False
    assert result["first_break_at"] == "chainhead_mismatch"


def test_security_audit_log_verifies_clean(tmp_path):
    log = SecurityAuditLog(tmp_path)
    log.log_event(category="authentication", name="test", user="alice", action="login",
                  resource="cli", outcome="success")
    log.log_event(category="security_event", name="test halt", user="alice", action="run",
                  resource="cli", outcome="halted")
    result = log.verify()
    assert result["ok"] is True
    assert result["records_checked"] == 2


def test_security_audit_log_detects_edited_entry(tmp_path):
    log = SecurityAuditLog(tmp_path)
    log.log_event(category="authentication", name="test", user="alice", action="login",
                  resource="cli", outcome="success")

    lines = log.log_path.read_text().splitlines()
    lines[0] = lines[0].replace("suser=alice", "suser=mallory")
    log.log_path.write_text("\n".join(lines) + "\n")

    result = log.verify()
    assert result["ok"] is False
    assert result["first_break_at"] == 0


def test_empty_log_verifies_ok(tmp_path):
    assert AuditLog(tmp_path).verify() == {"ok": True, "records_checked": 0, "first_break_at": None}
    assert SecurityAuditLog(tmp_path).verify() == {"ok": True, "records_checked": 0, "first_break_at": None}
