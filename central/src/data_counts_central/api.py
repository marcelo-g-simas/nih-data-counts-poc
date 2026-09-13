"""FastAPI Program API for the central platform. [Req 15]

Wraps the existing, already-tested central functions (`ingest`, `linkage`,
`evaluation`, `security_audit`) behind HTTP routes, so a Healthcare Partner
site can submit a package and a DRS's status/results can be queried over the
network instead of only via the `data-counts-central` CLI's file-drop model.
No new business logic lives here: every route is a thin adapter over
`ingest.py`/`linkage.py`/`evaluation.py`/`security_audit.py`, mirroring what
the CLI's `link` and `verify-logs` commands already do (see `cli.py`).

SECURITY POSTURE -- architecture-only (Req 12, Req 15)
--------------------------------------------------------
This app runs with **no TLS and no authentication/authorization middleware**.
That is a deliberate scope line for this POC, not an oversight: a real
deployment would sit this behind the program's ingress with mutual TLS and a
per-site credential, meeting the same TLS 1.3 (1.2 floor) requirement Req 12
already documents as `architecture-only` for the pipeline as a whole. Standing
up that ingress is a deployment concern this POC does not fake; see
`REQUIREMENTS.md` (Req 12, Req 15) and `REPORT.md` for the reasoning. Every
route still writes to the same hash-chained `SecurityAuditLog` the CLI uses
(Req 11), which is a genuine control, but it is not a substitute for transport
security or request authentication.

DIRECTORY LAYOUT
-----------------
The CLI's `inbox/` is flat: `inbox/<site>/patient.parquet`, shared across every
DRS a site has ever responded to. The status/linkage routes below are
inherently per-DRS, so this module partitions uploads one level deeper:
`<inbox_dir>/<request_id>/<site_name>/`. This is additive, not a change to
`ingest.py` or the CLI -- pointing `data-counts-central link --inbox-dir
inbox/<request_id>` reproduces the CLI's flat-inbox behavior exactly over one
DRS's packages, since `resolve_all_sites` just iterates whatever directory
it is given.
"""
from __future__ import annotations

import getpass
import math
import os
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Query, UploadFile

from . import evaluation, ingest, linkage
from .security_audit import SecurityAuditLog

app = FastAPI(
    title="Data COUNTS Central Program API",
    description=(
        "Ingest + status + linkage-read surface for the central platform. "
        "No TLS/auth middleware -- architecture-only, see module docstring."
    ),
)

# Overridable via env var (matches the CLI's own `--inbox-dir`/`--keys-dir`
# options) rather than hardcoded, and read fresh on every request rather than
# frozen at import time, so tests can monkeypatch these module attributes
# directly (`monkeypatch.setattr(api, "INBOX_DIR", tmp_path / "inbox")`).
INBOX_DIR = Path(os.environ.get("DATA_COUNTS_CENTRAL_INBOX_DIR", "inbox"))
KEYS_DIR = Path(os.environ.get("DATA_COUNTS_CENTRAL_KEYS_DIR", "keys"))
WORK_DIR = Path(os.environ.get("DATA_COUNTS_CENTRAL_WORK_DIR", "."))

_operator = getpass.getuser()

# The domain filenames `ingest.py` knows how to read from a site directory.
# `patient.parquet` is required; the rest are optional, matching
# `resolve_all_sites`/`reoffset_domain_tables`.
DOMAIN_FILENAMES = ("patient.parquet", "condition.parquet", "observation.parquet",
                     "encounter.parquet", "medication.parquet")


def _seclog() -> SecurityAuditLog:
    return SecurityAuditLog(WORK_DIR)


def _log_request(seclog: SecurityAuditLog, action: str, resource: str) -> None:
    """Opening log entry for every route, mirroring the CLI's "CLI invocation"
    entry that precedes each command's actual work (see `cli.py`).
    """
    seclog.log_event(category="authentication", name="API request", user=_operator,
                      action=action, resource=resource, outcome="success")


def _central_priv() -> bytes:
    path = KEYS_DIR / "central_private_key.pem"
    if not path.exists():
        raise HTTPException(
            status_code=503,
            detail=f"central keypair not provisioned under {KEYS_DIR}/ (run `data-counts-central setup-keys`)",
        )
    return path.read_bytes()


def _json_safe(value: float) -> float | None:
    """NaN has no JSON representation; evaluation.evaluate() returns it when a
    metric's denominator is zero (e.g. no predicted pairs at all).
    """
    return None if isinstance(value, float) and math.isnan(value) else value


@app.post("/sites/{site_name}/packages", status_code=201)
async def upload_site_package(
    site_name: str,
    request_id: str = Query(..., description="DRS request this package responds to"),
    files: list[UploadFile] = File(..., description="patient.parquet plus any domain tables"),  # noqa: B008 -- FastAPI's required idiom for declaring an upload parameter
):
    """Accept one site's exported package for one DRS and resolve it.

    Equivalent to dropping the same files under the CLI's `inbox/<site>/` and
    running `data-counts-central link` -- this route additionally validates
    the package by resolving it immediately (`ingest.resolve_site_package`),
    so a malformed upload fails at submission time rather than at the next
    linkage run.
    """
    seclog = _seclog()
    _log_request(seclog, "upload_site_package", f"{request_id}/{site_name}")

    site_dir = INBOX_DIR / request_id / site_name
    site_dir.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    for upload in files:
        if upload.filename not in DOMAIN_FILENAMES:
            seclog.log_event(category="security_event", name="Rejected upload filename", user=_operator,
                              action="upload_site_package", resource=f"{request_id}/{site_name}/{upload.filename}",
                              outcome="failure")
            raise HTTPException(status_code=400, detail=f"unexpected filename {upload.filename!r}")
        dest = site_dir / upload.filename
        dest.write_bytes(await upload.read())
        saved.append(upload.filename)

    if "patient.parquet" not in saved:
        raise HTTPException(status_code=400, detail="package must include patient.parquet")

    central_priv = _central_priv()
    try:
        resolved = ingest.resolve_site_package(site_dir, site_name, central_priv)
    except Exception as exc:
        seclog.log_event(category="administrative_action", name="Site package ingest", user=_operator,
                          action="resolve_site_package", resource=f"{request_id}/{site_name}",
                          outcome="failure", message=str(exc))
        raise HTTPException(status_code=422, detail=f"failed to resolve site package: {exc}") from exc

    seclog.log_event(category="administrative_action", name="Site package ingest", user=_operator,
                      action="resolve_site_package", resource=f"{request_id}/{site_name}",
                      outcome="success", message=f"files={saved} rows={resolved.num_rows}")
    return {
        "request_id": request_id,
        "site_name": site_name,
        "files_received": saved,
        "rows_resolved": resolved.num_rows,
    }


@app.get("/drs/{request_id}/status")
def drs_status(request_id: str):
    """What stage a DRS request has reached: which sites have a resolvable
    package present in the inbox, and whether there are enough for linkage.
    """
    seclog = _seclog()
    _log_request(seclog, "drs_status", request_id)

    request_dir = INBOX_DIR / request_id
    if not request_dir.exists():
        raise HTTPException(status_code=404, detail=f"no packages found for request {request_id!r}")

    sites_present = sorted(p.name for p in request_dir.iterdir() if p.is_dir())
    sites_resolvable = sorted(
        p.name for p in request_dir.iterdir() if p.is_dir() and (p / "patient.parquet").exists()
    )
    return {
        "request_id": request_id,
        "sites_present": sites_present,
        "sites_resolvable": sites_resolvable,
        "linkage_ready": len(sites_resolvable) >= 2,
    }


@app.get("/drs/{request_id}/linkage")
def drs_linkage(request_id: str, ground_truth: str | None = None):
    """Run cross-site linkage over every resolvable site package under this
    DRS's inbox and return clusters (+ evaluation metrics, if a synthetic-demo
    `ground_truth` path is given). Mirrors `data-counts-central link`.
    """
    seclog = _seclog()
    _log_request(seclog, "drs_linkage", request_id)
    seclog.log_event(category="administrative_action", name="Cross-site linkage run", user=_operator,
                      action="link", resource=request_id, outcome="attempted")

    request_dir = INBOX_DIR / request_id
    if not request_dir.exists():
        raise HTTPException(status_code=404, detail=f"no packages found for request {request_id!r}")

    central_priv = _central_priv()
    resolved = ingest.resolve_all_sites(request_dir, central_priv)
    if len(resolved) < 2:
        seclog.log_event(category="administrative_action", name="Cross-site linkage run", user=_operator,
                          action="link", resource=request_id, outcome="failure",
                          message=f"only {len(resolved)} resolvable site package(s)")
        raise HTTPException(
            status_code=409,
            detail=f"need at least 2 site packages under this request to link; found {len(resolved)}",
        )

    result = linkage.link_resolved_sites(resolved)
    clusters_df = result["clusters"]
    clusters = [
        {
            "cluster_id": str(cluster_id),
            "size": int(size),
            "sites": sorted(clusters_df[clusters_df["cluster_id"] == cluster_id]["source_dataset"].unique()),
        }
        for cluster_id, size in clusters_df.groupby("cluster_id").size().items()
    ]
    response: dict = {
        "request_id": request_id,
        "sites": sorted(resolved),
        "clusters": clusters,
    }

    if ground_truth:
        gt = evaluation.load_ground_truth(ground_truth)
        metrics = evaluation.evaluate(clusters_df, gt)
        response["evaluation"] = {
            "precision": _json_safe(metrics["precision"]),
            "recall": _json_safe(metrics["recall"]),
            "f1": _json_safe(metrics["f1"]),
            "true_positive_pairs": len(metrics["true_positive_pairs"]),
            "false_positive_pairs": len(metrics["false_positive_pairs"]),
            "false_negative_pairs": len(metrics["false_negative_pairs"]),
        }

    seclog.log_event(category="administrative_action", name="Cross-site linkage run", user=_operator,
                      action="link", resource=request_id, outcome="success",
                      message=f"sites={sorted(resolved)} clusters={len(clusters)}")
    return response


@app.get("/audit/verify")
def audit_verify():
    """Verify the hash-chained security audit log (Req 11). Mirrors the CLI's
    `verify-logs` command exactly, just returned as JSON instead of printed.
    """
    seclog = _seclog()
    _log_request(seclog, "audit_verify", str(WORK_DIR))

    result = SecurityAuditLog(WORK_DIR).verify()
    return {"security_audit_log": result}
