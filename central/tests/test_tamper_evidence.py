from data_counts_central.security_audit import SecurityAuditLog


def test_verifies_clean(tmp_path):
    log = SecurityAuditLog(tmp_path)
    log.log_event(category="authentication", name="test", user="alice", action="login",
                  resource="cli", outcome="success")
    log.log_event(category="configuration_change", name="keys", user="alice", action="generate_central_keys",
                  resource="keys/", outcome="success")
    result = log.verify()
    assert result["ok"] is True
    assert result["records_checked"] == 2


def test_detects_edited_entry(tmp_path):
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
    assert SecurityAuditLog(tmp_path).verify() == {"ok": True, "records_checked": 0, "first_break_at": None}
