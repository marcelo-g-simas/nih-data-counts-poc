"""HTTP-route tests for the FastAPI Program API (Req 15).

Same direct-fixture style `test_linkage.py`/`test_ingest_reoffset.py` already
use -- hand-built site packages, no dependency on `site/`'s CLI -- just
exercised through `TestClient` routes instead of bare function calls.
"""
from __future__ import annotations

import base64
import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.padding import MGF1, OAEP
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from fastapi.testclient import TestClient

from data_counts_central import api
from data_counts_central.crypto import generate_central_keys
from data_counts_central.security_audit import SecurityAuditLog

_REQUEST_ID = "DC-API-TEST-01"

# Stand-in digests for three identities. "jane" is given the *same* digest at
# both sites (so she must link across sites); "alice"/"bob" are site-only.
_DIGESTS = {"jane": b"\x11" * 64, "alice": b"\x22" * 64, "bob": b"\x33" * 64}


def _encrypt(pub: bytes, digest: bytes) -> str:
    """Stand in for what a site transmits: RSA-OAEP(digest) under the central
    public key, base64-encoded -- same shape as `crypto.transcrypt_in` expects,
    reused from `test_ingest_reoffset.py`'s `_write_site_package` helper.
    """
    pub_key = load_pem_public_key(pub)
    oaep = OAEP(mgf=MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None)
    return base64.b64encode(pub_key.encrypt(digest, oaep)).decode("ascii")


def _patient_bytes(pub: bytes, rows: list[tuple[str, str]]) -> bytes:
    """Build a `patient.parquet`'s bytes for `[(local_patient_id, identity), ...]`.

    All three token columns carry the same encrypted digest per identity --
    sufficient for Splink's exact-match comparisons to link/not-link correctly
    without needing distinct soundex/metaphone values (that nuance is already
    covered by `test_linkage.py`).

    Uses the lightweight-roster token column names (`token1/2/3`), not the
    `subject_token1/2/3` package shape -- `linkage.py`'s Splink settings are
    hard-coded to `token1/2/3` (see `test_linkage.py`), and `ingest.py`'s
    module docstring confirms that's the shape meant for linkage input; the
    `subject_token*` shape is for `reoffset_domain_tables`, a separate pass
    this route does not exercise.
    """
    table = pa.Table.from_pylist([
        {
            "local_patient_id": pid,
            "token1": _encrypt(pub, _DIGESTS[identity]),
            "token2": _encrypt(pub, _DIGESTS[identity]),
            "token3": _encrypt(pub, _DIGESTS[identity]),
        }
        for pid, identity in rows
    ])
    buf = pa.BufferOutputStream()
    pq.write_table(table, buf)
    return buf.getvalue().to_pybytes()


@pytest.fixture
def client(tmp_path, monkeypatch):
    keys_dir = tmp_path / "keys"
    inbox_dir = tmp_path / "inbox"
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    _, pub = generate_central_keys(keys_dir)

    monkeypatch.setattr(api, "KEYS_DIR", keys_dir)
    monkeypatch.setattr(api, "INBOX_DIR", inbox_dir)
    monkeypatch.setattr(api, "WORK_DIR", work_dir)

    with TestClient(api.app) as c:
        yield c, pub, inbox_dir, work_dir


def test_upload_then_status_reflects_resolved_sites(client):
    c, pub, _inbox_dir, _work_dir = client

    body = _patient_bytes(pub, [("a-0", "jane"), ("a-1", "alice")])
    resp = c.post(
        f"/sites/site_a/packages?request_id={_REQUEST_ID}",
        files=[("files", ("patient.parquet", body, "application/octet-stream"))],
    )
    assert resp.status_code == 201, resp.text
    payload = resp.json()
    assert payload["site_name"] == "site_a"
    assert payload["files_received"] == ["patient.parquet"]
    assert payload["rows_resolved"] == 2

    status = c.get(f"/drs/{_REQUEST_ID}/status")
    assert status.status_code == 200
    body = status.json()
    assert body["sites_resolvable"] == ["site_a"]
    assert body["linkage_ready"] is False  # only one site so far


def test_upload_rejects_unexpected_filename(client):
    c, _pub, _inbox_dir, _work_dir = client
    resp = c.post(
        f"/sites/site_a/packages?request_id={_REQUEST_ID}",
        files=[("files", ("not_a_domain_table.parquet", b"junk", "application/octet-stream"))],
    )
    assert resp.status_code == 400


def test_status_404_for_unknown_request(client):
    c, _pub, _inbox_dir, _work_dir = client
    resp = c.get("/drs/no-such-request/status")
    assert resp.status_code == 404


def test_linkage_needs_at_least_two_sites(client):
    c, pub, _inbox_dir, _work_dir = client
    body = _patient_bytes(pub, [("a-0", "jane")])
    c.post(
        f"/sites/site_a/packages?request_id={_REQUEST_ID}",
        files=[("files", ("patient.parquet", body, "application/octet-stream"))],
    )
    resp = c.get(f"/drs/{_REQUEST_ID}/linkage")
    assert resp.status_code == 409


def test_two_site_round_trip_links_jane_across_sites_and_reports_metrics(client, tmp_path):
    c, pub, _inbox_dir, _work_dir = client

    site_a_body = _patient_bytes(pub, [("a-0", "jane"), ("a-1", "alice")])
    site_b_body = _patient_bytes(pub, [("b-0", "jane"), ("b-1", "bob")])

    for site, body in (("site_a", site_a_body), ("site_b", site_b_body)):
        resp = c.post(
            f"/sites/{site}/packages?request_id={_REQUEST_ID}",
            files=[("files", ("patient.parquet", body, "application/octet-stream"))],
        )
        assert resp.status_code == 201, resp.text

    status = c.get(f"/drs/{_REQUEST_ID}/status").json()
    assert status["sites_resolvable"] == ["site_a", "site_b"]
    assert status["linkage_ready"] is True

    ground_truth = {
        "site_a": {"a-0": "jane", "a-1": "alice"},
        "site_b": {"b-0": "jane", "b-1": "bob"},
    }
    gt_path = tmp_path / "ground_truth.json"
    gt_path.write_text(json.dumps(ground_truth))

    resp = c.get(f"/drs/{_REQUEST_ID}/linkage", params={"ground_truth": str(gt_path)})
    assert resp.status_code == 200, resp.text
    payload = resp.json()

    assert sorted(payload["sites"]) == ["site_a", "site_b"]
    assert len(payload["clusters"]) >= 1
    cross_site_clusters = [cl for cl in payload["clusters"] if len(cl["sites"]) > 1]
    assert len(cross_site_clusters) == 1  # exactly "jane" links across sites

    evaluation = payload["evaluation"]
    assert evaluation["precision"] == 1.0
    assert evaluation["recall"] == 1.0
    assert evaluation["f1"] == 1.0


def test_audit_verify_reports_ok_and_writes_a_log_entry(client):
    c, _pub, _inbox_dir, work_dir = client
    resp = c.get("/audit/verify")
    assert resp.status_code == 200
    body = resp.json()
    assert body["security_audit_log"]["ok"] is True

    # The request itself must have produced a hash-chained entry (Req 11) --
    # every route logs via SecurityAuditLog, same as the existing CLI commands.
    log = SecurityAuditLog(work_dir)
    verify_result = log.verify()
    assert verify_result["ok"] is True
    assert verify_result["records_checked"] >= 1
