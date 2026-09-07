from data_counts import stage7_leak_gate


def test_leak_gate_halts_and_routes_to_review_queue(tmp_path, monkeypatch):
    def fake_scan(text):
        if "SSN" in text or "@" in text:
            return [{"detector": "fake", "entity_type": "EMAIL_OR_SSN", "start": 0, "end": len(text), "score": 0.99}]
        return []

    monkeypatch.setattr(stage7_leak_gate, "scan_text", fake_scan)

    clean_records = [{"id": "n1", "note_text": "Patient reports feeling well today."}]
    result_clean = stage7_leak_gate.run(clean_records, review_queue_dir=tmp_path, batch_id="clean")
    assert result_clean["halt"] is False

    leaky_records = [{"id": "n2", "note_text": "Contact SSN 123-45-6789 or jane@example.com"}]
    result_leaky = stage7_leak_gate.run(leaky_records, review_queue_dir=tmp_path, batch_id="leaky")
    assert result_leaky["halt"] is True
    assert (tmp_path / "leaky.json").exists()
