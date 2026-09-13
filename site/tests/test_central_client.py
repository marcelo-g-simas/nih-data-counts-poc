"""Unit tests for central_client.py's retry/backoff logic and request shape --
no real central server involved (that's central/tests/test_api.py's job;
site/ deliberately never depends on the central package, see README.md's
"Two projects, deliberately"). httpx.Client is monkeypatched so these run
fast and offline.
"""
from __future__ import annotations

import httpx
import pytest

from data_counts import central_client


class _FakeResponse:
    def __init__(self, status_code: int = 200, json_body: dict | None = None):
        self.status_code = status_code
        self._json_body = json_body or {}
        self.text = str(json_body)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("GET", "http://x")
            raise httpx.HTTPStatusError("error", request=request,
                                          response=httpx.Response(self.status_code, request=request))

    def json(self) -> dict:
        return self._json_body


class _FakeClient:
    """Stand-in for `httpx.Client` used as a context manager; `responses` is a
    list consumed one per call (get/post), in order, so a test can script a
    sequence of failures followed by success.
    """
    def __init__(self, responses):
        self._responses = list(responses)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def _next(self):
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def get(self, *_args, **_kwargs):
        return self._next()

    def post(self, *_args, **_kwargs):
        return self._next()


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    # Keep retry tests fast; also pins jitter to its upper bound so delay
    # assertions (where made) are deterministic.
    monkeypatch.setattr(central_client.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(central_client.random, "uniform", lambda _lo, hi: hi)


def test_with_retry_succeeds_after_transient_network_errors():
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise httpx.ConnectError("refused", request=httpx.Request("GET", "http://x"))
        return "ok"

    assert central_client._with_retry("op", flaky, max_retries=5, quiet=True) == "ok"
    assert attempts["n"] == 3


def test_with_retry_gives_up_after_max_retries():
    def always_fails():
        raise httpx.ConnectError("refused", request=httpx.Request("GET", "http://x"))

    with pytest.raises(central_client.CentralAPIError):
        central_client._with_retry("op", always_fails, max_retries=2, quiet=True)


def test_with_retry_does_not_retry_4xx():
    calls = {"n": 0}

    def bad_request():
        calls["n"] += 1
        request = httpx.Request("POST", "http://x")
        raise httpx.HTTPStatusError("bad", request=request, response=httpx.Response(400, request=request))

    with pytest.raises(central_client.CentralAPIError):
        central_client._with_retry("op", bad_request, max_retries=5, quiet=True)
    assert calls["n"] == 1  # no retry attempted for a non-retryable 4xx


def test_with_retry_retries_5xx_then_succeeds():
    calls = {"n": 0}

    def flaky_server():
        calls["n"] += 1
        if calls["n"] == 1:
            request = httpx.Request("GET", "http://x")
            raise httpx.HTTPStatusError("bad", request=request, response=httpx.Response(503, request=request))
        return "recovered"

    assert central_client._with_retry("op", flaky_server, max_retries=3, quiet=True) == "recovered"


def test_upload_package_posts_patient_parquet(tmp_path, monkeypatch):
    patient_file = tmp_path / "patient.parquet"
    patient_file.write_bytes(b"fake-parquet-bytes")

    captured = {}

    def fake_client_ctor(**_kwargs):
        def _post(url, params=None, files=None):
            captured["url"] = url
            captured["params"] = params
            captured["filenames"] = [name for _field, (name, _fh, _ctype) in files]
            return _FakeResponse(201, {"rows_resolved": 1, "files_received": ["patient.parquet"]})
        client = _FakeClient([])
        client.post = _post
        return client

    monkeypatch.setattr(central_client.httpx, "Client", fake_client_ctor)

    result = central_client.upload_package("http://127.0.0.1:8001", "req-1", "site_a", patient_file)

    assert result == {"rows_resolved": 1, "files_received": ["patient.parquet"]}
    assert captured["url"] == "http://127.0.0.1:8001/sites/site_a/packages"
    assert captured["params"] == {"request_id": "req-1"}
    assert captured["filenames"] == ["patient.parquet"]


def test_upload_package_files_sends_all_files_in_one_request(tmp_path, monkeypatch):
    patient_file = tmp_path / "patient.parquet"
    condition_file = tmp_path / "condition.parquet"
    patient_file.write_bytes(b"patient-bytes")
    condition_file.write_bytes(b"condition-bytes")

    captured = {}

    def fake_client_ctor(**_kwargs):
        def _post(url, params=None, files=None):
            captured["filenames"] = [name for _field, (name, _fh, _ctype) in files]
            return _FakeResponse(201, {"rows_resolved": 4, "files_received": captured["filenames"]})
        client = _FakeClient([])
        client.post = _post
        return client

    monkeypatch.setattr(central_client.httpx, "Client", fake_client_ctor)

    result = central_client.upload_package_files(
        "http://127.0.0.1:8001", "req-1", "site_a", [patient_file, condition_file],
    )

    assert set(captured["filenames"]) == {"patient.parquet", "condition.parquet"}
    assert result["rows_resolved"] == 4


def test_get_linkage_passes_ground_truth_param(monkeypatch):
    captured = {}

    def fake_client_ctor(**_kwargs):
        def _get(url, params=None):
            captured["url"] = url
            captured["params"] = params
            return _FakeResponse(200, {"clusters": [], "request_id": "req-1", "sites": []})
        client = _FakeClient([])
        client.get = _get
        return client

    monkeypatch.setattr(central_client.httpx, "Client", fake_client_ctor)

    result = central_client.get_linkage("http://127.0.0.1:8001", "req-1", ground_truth="/app/gt.json")

    assert captured["params"] == {"ground_truth": "/app/gt.json"}
    assert result["request_id"] == "req-1"
