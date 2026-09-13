"""HTTP client for the central platform's Program API (Req 15).

Used by `export-linkage-demo-sites --central-url ...` and `fetch-linkage-
result` (see `cli.py`) as the network-facing alternative to those commands'
default file-drop behavior (writing straight into `../central/inbox/`).
Nothing here is required for the rest of the pipeline: `stage8_package.py`'s
`outbox/` output is untouched, and this module is only reached when a caller
explicitly asks to talk to the API.

Every call goes through `_with_retry`: exponential backoff with full jitter
(the AWS-recommended jitter strategy -- spreads out retries from multiple
callers instead of having them all collide on the same schedule) on network
errors and 5xx responses. A 4xx is a real rejection from the API (bad upload,
central's keypair not provisioned, not enough sites to link yet) and is never
retried -- waiting longer does not fix a malformed request.

NOT SCALE-TESTED (Req 15) -- every request below uses a flat, hardcoded
timeout regardless of package size, and `upload_package_files` retries the
whole multi-file upload from scratch on failure rather than resuming it; the
API side buffers each file fully into memory rather than streaming it (see
`api.py`'s `upload_site_package` docstring). Fine at this POC's demo scale
(tested at 1,000 patients / 5 tables in under a second); a real deployment
needs resumable chunked upload before larger packages go through this path.
See `REQUIREMENTS.md` Req 15.
"""
from __future__ import annotations

import random
import time
from pathlib import Path

import httpx
from rich import print as rprint

_RETRYABLE_STATUS = {500, 502, 503, 504}


class CentralAPIError(RuntimeError):
    """Raised when the central Program API rejects a request outright, or
    remains unreachable/erroring after every retry has been exhausted."""


def _with_retry(operation: str, fn, *, max_retries: int = 5, base_delay: float = 1.0,
                 max_delay: float = 30.0, quiet: bool = False):
    attempt = 0
    while True:
        try:
            return fn()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in _RETRYABLE_STATUS or attempt >= max_retries:
                detail = exc.response.text[:300]
                raise CentralAPIError(f"{operation} failed ({exc.response.status_code}): {detail}") from exc
        except httpx.TransportError as exc:
            if attempt >= max_retries:
                raise CentralAPIError(
                    f"{operation} failed after {attempt + 1} attempt(s): {exc}"
                ) from exc
        attempt += 1
        delay = random.uniform(0, min(max_delay, base_delay * (2 ** (attempt - 1))))
        if not quiet:
            rprint(f"  [yellow]{operation}: retry {attempt}/{max_retries} in {delay:.1f}s[/yellow]")
        time.sleep(delay)


def wait_until_reachable(base_url: str, *, max_retries: int = 10, base_delay: float = 1.0,
                          max_delay: float = 10.0) -> None:
    """Poll until the API responds -- useful right after launching its
    container, whose uvicorn startup takes a moment. FastAPI always serves
    `/docs`, so it doubles as a cheap, route-independent liveness probe.
    """
    def _do() -> None:
        with httpx.Client(timeout=5.0) as client:
            client.get(f"{base_url}/docs").raise_for_status()

    _with_retry("waiting for central API", _do, max_retries=max_retries,
                base_delay=base_delay, max_delay=max_delay)


def upload_package_files(base_url: str, request_id: str, site_name: str,
                          file_paths: list[str | Path], **retry_kwargs) -> dict:
    """POST one or more of a site's package files (`patient.parquet` plus any
    domain tables `stage8_package.py` wrote alongside it) to
    `/sites/{site_name}/packages` in a single multipart request -- the same
    endpoint and the same `files: list[UploadFile]` shape whether this is the
    linkage demo's roster-only export or the real pipeline's full outbox
    package (see `api.py`'s `DOMAIN_FILENAMES`, which matches
    `stage8_package.py`'s table names 1:1).

    All retried attempts re-read the files from disk, since a single
    multipart POST consumes its file handles -- cheap here (these are small
    demo-scale packages), not a streaming design.
    """
    paths = [Path(p) for p in file_paths]

    def _do() -> dict:
        files = [
            ("files", (path.name, path.open("rb"), "application/octet-stream"))
            for path in paths
        ]
        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.post(
                    f"{base_url}/sites/{site_name}/packages",
                    params={"request_id": request_id},
                    files=files,
                )
                resp.raise_for_status()
                return resp.json()
        finally:
            for _field, (_name, fh, _ctype) in files:
                fh.close()

    return _with_retry(f"upload {site_name} package ({len(paths)} file(s))", _do, **retry_kwargs)


def upload_package(base_url: str, request_id: str, site_name: str,
                    patient_parquet_path: str | Path, **retry_kwargs) -> dict:
    """POST a single file (typically `patient.parquet`) -- the linkage demo's
    roster-export path. Thin wrapper over `upload_package_files`.
    """
    return upload_package_files(base_url, request_id, site_name, [patient_parquet_path], **retry_kwargs)


def get_status(base_url: str, request_id: str, **retry_kwargs) -> dict:
    def _do() -> dict:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(f"{base_url}/drs/{request_id}/status")
            resp.raise_for_status()
            return resp.json()

    return _with_retry(f"status for {request_id}", _do, **retry_kwargs)


def get_linkage(base_url: str, request_id: str, ground_truth: str | None = None,
                 **retry_kwargs) -> dict:
    def _do() -> dict:
        with httpx.Client(timeout=30.0) as client:
            params = {"ground_truth": ground_truth} if ground_truth else {}
            resp = client.get(f"{base_url}/drs/{request_id}/linkage", params=params)
            resp.raise_for_status()
            return resp.json()

    return _with_retry(f"linkage for {request_id}", _do, **retry_kwargs)
