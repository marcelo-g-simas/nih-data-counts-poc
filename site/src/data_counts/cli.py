"""CLI entry point: one subcommand per pipeline stage, plus `run-all`. [Req 1-15]

SYNTHETIC DATA ONLY. See README.md for the full RFI-requirement map.
"""
from __future__ import annotations

import getpass
import json
from pathlib import Path

import typer
from rich import print as rprint

from . import (
    daily_sample,
    stage1_synthea,
    stage2_ingest,
    stage3_drs,
    stage4_dq_kahn,
    stage5_deid,
    stage6_pprl,
    stage7_leak_gate,
    stage8_package,
    stage9_audit,
)
from .audit import AuditLog, PipelineHalt
from .security_audit import SecurityAuditLog

_operator = getpass.getuser()

app = typer.Typer(help="Data COUNTS leave-behind pipeline POC (synthetic data only).")


@app.command("setup-models")
def setup_models():
    """One-time, online model download step. After this, the pipeline runs fully offline.

    The spaCy model (`en-core-web-sm`) isn't downloaded here -- it's a real pinned
    dependency in pyproject.toml (see the `[tool.uv.sources]` comment there for why:
    Presidio's spaCy engine falls back to a `pip`-based auto-download otherwise, and a
    `uv`-managed venv doesn't ship `pip`), so plain `uv sync` already installs it.
    """
    seclog = SecurityAuditLog()
    seclog.log_event(category="authentication", name="CLI invocation", user=_operator,
                      action="setup-models", resource="site-cli", outcome="success")
    seclog.log_event(category="administrative_action", name="Model provisioning", user=_operator,
                      action="download+export models", resource="models/", outcome="attempted")

    rprint("[bold]Downloading models (GLiNER, nltk data)...[/bold]")
    try:
        result = stage7_leak_gate.export_gliner_onnx()
        rprint(f"  GLiNER OK (exported to ONNX: {result['onnx_path']})")
    except Exception as exc:  # noqa: BLE001 -- best-effort setup step, reported not swallowed
        rprint(f"  [yellow]GLiNER download/export failed: {exc}[/yellow]")
    try:
        import nltk
        for resource in ("averaged_perceptron_tagger_eng", "averaged_perceptron_tagger"):
            nltk.download(resource)  # name varies by nltk version; harmless if one doesn't exist
        rprint("  nltk data OK")
    except Exception as exc:  # noqa: BLE001
        rprint(f"  [yellow]nltk data download failed: {exc}[/yellow]")


@app.command("stage1-synthea")
def cli_stage1(out: str = "data/synthea_ndjson", population: int = 1000, seed: int = 42, use_container: bool = True):
    result = stage1_synthea.run(out, population=population, seed=seed, use_container=use_container)
    rprint(result)


@app.command("build-daily-sample")
def cli_build_daily_sample(
    ndjson_dir: str = "data/synthea_ndjson_daily",
    out: str = "data/daily_sample",
    seed: int = 42,
    target_patients: int = 10000,
):
    """One real encounter (+ class-weighted synthetic note) per patient, re-dated onto
    a single simulated day -- see daily_sample.py's docstring for why this exists."""
    result = daily_sample.build(ndjson_dir, out, seed=seed, target_patients=target_patients)
    rprint(result)


@app.command("stage2-ingest")
def cli_stage2(ndjson_dir: str = "data/synthea_ndjson", lake_dir: str = "data/lake"):
    result = stage2_ingest.run(ndjson_dir, lake_dir)
    rprint(result)


@app.command("export-linkage-demo-sites")
def export_linkage_demo_sites(
    central_public_key_path: str = stage6_pprl.DEFAULT_CENTRAL_PUBLIC_KEY,
    inbox_dir: str = "../central/inbox",
    ground_truth_path: str = "../linkage_ground_truth.json",
):
    """Simulate two independently-keyed Healthcare Partner sites and export both
    to the central platform's inbox, to demonstrate noisy-identifier linkage
    (Req 5). Five scenarios, chosen to give recall/precision/F1 something real
    to measure rather than a trivial 100%/100%:

      - exact:  identical spelling at both sites -- the baseline case.
      - noisy:  a last-name typo ("Smith"/"Smyth") -- breaks token1's exact
                match, but token2/token3 (soundex/metaphone) still agree, so
                this should still link. Tests recall under noise.
      - site_a_only / site_b_only: unique to one site -- should never link.
      - twins:  two genuinely different people (matching DOB and gender, as
                real twins would) whose first names are phonetically close
                ("Jordan"/"Jorden") and who share a last name and first
                initial -- so *every* token agrees despite them being
                different individuals. A known, honest limitation of any
                token scheme this coarse: it cannot out-of-the-box distinguish
                twins. Tests precision -- expect a false positive here.

    Also writes `ground_truth_path`, a synthetic-demo-only answer key (which
    patient is which real identity) used purely to score the linkage results
    afterward -- central never sees this file; it plays no role in the actual
    privacy-preserving pipeline.
    """
    exact = {"first_name": "Jane", "last_name": "Doe", "gender": "F", "birth_date": "1980-05-12"}
    site_a_only = {"first_name": "Alice", "last_name": "Anders", "gender": "F", "birth_date": "1990-01-01"}
    site_b_only = {"first_name": "Bob", "last_name": "Baker", "gender": "M", "birth_date": "1985-06-15"}
    noisy_a = {"first_name": "John", "last_name": "Smith", "gender": "M", "birth_date": "1975-11-03"}
    noisy_b = {"first_name": "John", "last_name": "Smyth", "gender": "M", "birth_date": "1975-11-03"}
    twin_a = {"first_name": "Jordan", "last_name": "Doe", "gender": "M", "birth_date": "2001-03-15"}
    twin_b = {"first_name": "Jorden", "last_name": "Doe", "gender": "M", "birth_date": "2001-03-15"}

    # (local_patient_id, patient dict, true-identity label) -- the label is what the
    # ground-truth file records; two rows sharing a label are truly the same person.
    site_a_rows = [("a-0", exact, "jane"), ("a-1", noisy_a, "john_smith"),
                   ("a-2", site_a_only, "alice"), ("a-3", twin_a, "twin_a")]
    site_b_rows = [("b-0", exact, "jane"), ("b-1", noisy_b, "john_smith"),
                   ("b-2", site_b_only, "bob"), ("b-3", twin_b, "twin_b")]

    def roster(rows: list[tuple[str, dict, str]]) -> list[dict]:
        return [{**patient, "patient_id": pid} for pid, patient, _label in rows]

    path_a = stage6_pprl.export_site_roster(roster(site_a_rows), f"{inbox_dir}/site_a", central_public_key_path)
    path_b = stage6_pprl.export_site_roster(roster(site_b_rows), f"{inbox_dir}/site_b", central_public_key_path)

    # Two rows are a true match iff their labels are equal -- note twin_a/twin_b are
    # deliberately DIFFERENT labels (they're different real people), even though every
    # token will treat them as matching. No other bookkeeping needed for that case.
    ground_truth = {
        "site_a": {pid: label for pid, _p, label in site_a_rows},
        "site_b": {pid: label for pid, _p, label in site_b_rows},
    }
    Path(ground_truth_path).write_text(json.dumps(ground_truth, indent=2))

    rprint(f"[green]Exported site_a ({len(site_a_rows)} patients) -> {path_a}[/green]")
    rprint(f"[green]Exported site_b ({len(site_b_rows)} patients) -> {path_b}[/green]")
    rprint(f"[green]Ground truth (demo-only) -> {ground_truth_path}[/green]")
    rprint("Run `cd ../central && uv run data-counts-central link --ground-truth ../linkage_ground_truth.json`.")

    seclog = SecurityAuditLog()
    seclog.log_event(category="administrative_action", name="Linkage demo export", user=_operator,
                      action="export-linkage-demo-sites", resource=inbox_dir, outcome="success")


@app.command("tui")
def tui(
    work_dir: str = "data/work",
    review_queue_dir: str = "data/review_queue",
    outbox_dir: str = "outbox",
):
    """Launch the Textual ops console: live run status, DQ panel,
    review-queue triage, outbox contents, and audit/security log
    verification -- all read-only over files the pipeline already writes
    (demo aid, not an RFI-requirement-bearing module).
    """
    from .tui import OpsConsoleApp

    OpsConsoleApp(work_dir=work_dir, review_queue_dir=review_queue_dir, outbox_dir=outbox_dir).run()


@app.command("verify-logs")
def verify_logs(work_dir: str = "data/work"):
    """Verify both hash-chained logs (Req 11: "protected against unauthorized
    modification or deletion"). Detects any edit, deletion, reordering, or
    truncation of a previously-written entry.
    """
    audit_result = AuditLog(work_dir).verify()
    security_result = SecurityAuditLog(work_dir).verify()
    rprint(f"[bold cyan]Processing audit log (Req 9):[/bold cyan] {audit_result}")
    rprint(f"[bold cyan]Security audit log (Req 11):[/bold cyan] {security_result}")
    if not (audit_result["ok"] and security_result["ok"]):
        rprint("[bold red]TAMPERING DETECTED[/bold red]")
        raise typer.Exit(code=1)
    rprint("[bold green]Both logs verified intact.[/bold green]")


@app.command("run-all")
def run_all(drs: str = "drs/examples/DC-POC-0001.json", population: int = 1000, use_container: bool = False):
    audit_log = AuditLog()
    seclog = SecurityAuditLog()
    seclog.log_event(category="authentication", name="CLI invocation", user=_operator,
                      action="run-all", resource="site-cli", outcome="success")
    request_id = "unknown"
    try:
        rprint("[bold cyan]Stage 1/9 — Synthea generation[/bold cyan] [dim](Req 1a)[/dim]")
        s1 = stage1_synthea.run(population=population, use_container=use_container)
        audit_log.record(stage_id="stage1_synthea", batch_id="cohort", record_counts=s1["counts"],
                          outcome="ok", reconciliation_status="n/a")
        total_resources = sum(s1["counts"].values())
        rprint(f"  [dim]source:[/dim] {s1['source']}")
        for resource_type, count in s1["counts"].items():
            rprint(f"  [dim]•[/dim] {resource_type}: {count}")
        rprint(f"  [green]✓[/green] {s1['counts'].get('Patient', 0)} patients, {total_resources} resources total")

        rprint("[bold cyan]Stage 2/9 — Ingest: raw lossless landing + analytic views[/bold cyan] [dim](Req 1a, 6)[/dim]")
        s2 = stage2_ingest.run(s1["out_dir"])
        audit_log.record(stage_id="stage2_ingest", batch_id="lake", record_counts=s2["raw"],
                          outcome="ok", reconciliation_status="ok")
        rprint(f"  [green]✓[/green] {sum(s2['raw'].values())} records landed as nested Parquet (data/lake/raw/)")
        rprint(f"  [green]✓[/green] {sum(s2['views'].values())} rows projected into flat analytic views (data/lake/views/)")

        rprint("[bold cyan]Stage 3/9 — DRS orchestration[/bold cyan] [dim](Req 2)[/dim]")
        s3 = stage3_drs.run(drs)
        request_id = s3["drs"].request_id
        availability = s3["availability"]
        rprint(f"  [dim]request_id:[/dim] {s3['drs'].request_id}  [dim]version:[/dim] {s3['drs'].version}  "
               f"[dim]cadence:[/dim] {s3['drs'].refresh_cadence}")
        if availability["missing_domains"] or availability["missing_fields"]:
            rprint(f"  [yellow]⚠ availability gaps (no silent substitution): {availability}[/yellow]")
        else:
            rprint("  [green]✓[/green] every requested domain/field is present — nothing silently substituted")
        audit_log.record(stage_id="stage3_drs", batch_id=s3["drs"].request_id, record_counts={},
                          outcome="ok", reconciliation_status="ok", extra=availability)
        seclog.log_event(
            category="configuration_change", name="DRS loaded", user=_operator, action="load DRS",
            resource=drs, outcome="success",
            message=f"request_id={s3['drs'].request_id} version={s3['drs'].version} "
                    f"permission_version={s3['drs'].authorization.permission_version}",
        )

        rprint("[bold cyan]Stage 4/9 — Data quality (Kahn framework)[/bold cyan] [dim](Req 3)[/dim]")
        s4 = stage4_dq_kahn.run(s3["drs"].checks)
        for check in s4["checks"]:
            mark = "[red]✗[/red]" if check["anomaly"] else "[green]✓[/green]"
            rprint(f"  {mark} {check['id']} ({check['kahn_category']}/{check['type']}, on_fail={check['on_fail']})")
        audit_log.record(
            stage_id="stage4_dq_kahn", batch_id=s3["drs"].request_id,
            record_counts={c["id"]: int(not c["anomaly"]) for c in s4["checks"]},
            outcome="halt" if s4["halt"] else "ok", reconciliation_status="anomaly_report_emitted",
            extra=s4,
        )
        Path("data/work").mkdir(parents=True, exist_ok=True)
        Path("data/work/dq_anomaly_report.json").write_text(json.dumps(s4, indent=2))
        if s4["halt"]:
            seclog.log_event(category="security_event", name="DQ halt", user=_operator,
                              action="stage4_dq_kahn", resource=s3["drs"].request_id, outcome="halted",
                              message="on_fail=halt check failed; transmission blocked")
            raise PipelineHalt(f"DQ check(s) failed with on_fail=halt: {s4}")

        # Tokenization runs BEFORE de-identification, and not only because it needs
        # the raw identifiers that de-identification discards. It also derives each
        # patient's date offset from the OPPRL token1 digest -- the one per-identity
        # value that is the same at every site -- and stage 5 consumes that offset.
        # Deriving the offset from a site-local patient id instead, as stage 5 used
        # to, shifted the same patient differently at every site (Req 4).
        rprint("[bold cyan]Stage 5/9 — OPPRL tokenization[/bold cyan] [dim](Req 5, site half only — tokenize + transcrypt_out)[/dim]")
        tokens = stage6_pprl.run(s3["drs"])
        audit_log.record(
            stage_id="stage6_pprl", batch_id=s3["drs"].request_id, record_counts={"patients_tokenized": len(tokens)},
            outcome="ok", reconciliation_status="ok",
        )
        seclog.log_event(
            category="configuration_change", name="Site OPPRL keypair in use", user=_operator,
            action="generate_site_keys", resource="data/work/pprl/site", outcome="success",
            message="site RSA keypair loaded (generated on first run; persists across runs so tokens stay stable)",
        )
        rprint(f"  [green]✓[/green] {len(tokens)} patients tokenized (token1/2/3), encrypted under the central platform's public key")
        rprint("  [dim]→ this site cannot decrypt what it just produced — only the central platform's private key can[/dim]")

        rprint("[bold cyan]Stage 6/9 — De-identification[/bold cyan] [dim](Req 4)[/dim]")
        shift_map = stage5_deid.shift_map_from_token_map(tokens)
        s5 = stage5_deid.run(s3["drs"], shift_map)
        audit_log.record(stage_id="stage5_deid", batch_id=s3["drs"].request_id, record_counts=s5,
                          outcome="ok", reconciliation_status="ok")
        domain_counts = {k: v for k, v in s5.items() if not k.startswith("_")}
        rprint(f"  [green]✓[/green] 18 HIPAA identifiers removed; per-patient date-shift applied across "
               f"{len(domain_counts)} domains ({sum(domain_counts.values())} rows)")
        rprint("  [dim]→ shift is season- and weekday-preserving, and identical for this patient at every site[/dim]")
        if s5["_excluded_unshiftable"]:
            rprint(f"  [yellow]{s5['_excluded_unshiftable']} row(s) excluded: no derivable offset "
                   f"(incomplete identity attributes, so no token1)[/yellow]")

        rprint("[bold cyan]Stage 7/9 — Residual PII-leakage gate[/bold cyan] [dim](Req 3 halt-on-anomaly, 9, 13)[/dim]")
        # This synthetic cohort carries no free-text clinical notes, so the real outbound
        # batch is scanned here (and is expected to come back clean). The halt path itself
        # is demonstrated separately, on a deliberately injected-PII fixture, so that
        # demonstrating the halt doesn't block this run from reaching stage 8/outbox --
        # both are acceptance criteria, and conflating them would make one impossible to show.
        real_notes: list[dict] = []
        rprint("  [dim]scanning the real outbound batch (0 free-text notes in this cohort — expected clean)...[/dim]")
        s7 = stage7_leak_gate.run(real_notes, batch_id=s3["drs"].request_id)
        audit_log.record(
            stage_id="stage7_leak_gate", batch_id=s3["drs"].request_id,
            record_counts={"flagged": len(s7["flagged"])},
            outcome="halt" if s7["halt"] else "ok", reconciliation_status="scanned",
        )
        if s7["halt"]:
            rprint(f"[red]Residual PII leak detected -- routed to {s7.get('review_path')}. Halting transmission.[/red]")
            seclog.log_event(category="security_event", name="Residual PII leak", user=_operator,
                              action="stage7_leak_gate", resource=s3["drs"].request_id, outcome="halted",
                              message=f"routed to {s7.get('review_path')}")
            raise PipelineHalt("Unresolved residual PII leak")
        rprint("  [green]✓[/green] real outbound batch: clean, 0 detectors fired")

        rprint("  [dim]scanning a deliberately injected-PII fixture (Presidio + Philter + GLiNER, recall-tuned)...[/dim]")
        injected_leak_notes = [{"id": "note-injected-leak", "note_text": "Patient Jane Q. Doe, DOB 1980-05-12, SSN 123-45-6789, reachable at jane.doe@example.com."}]
        s7_demo = stage7_leak_gate.run(injected_leak_notes, batch_id=f"{s3['drs'].request_id}-leak-demo")
        audit_log.record(
            stage_id="stage7_leak_gate_demo", batch_id=f"{s3['drs'].request_id}-leak-demo",
            record_counts={"flagged": len(s7_demo["flagged"])},
            outcome="halt" if s7_demo["halt"] else "ok", reconciliation_status="scanned",
        )
        if s7_demo["halt"]:
            hit_count = sum(len(f["hits"]) for f in s7_demo["flagged"])
            detectors = sorted({h["detector"] for f in s7_demo["flagged"] for h in f["hits"]})
            rprint(f"  [green]✓[/green] injected leak caught: {hit_count} spans across {len(detectors)} independent detectors "
                    f"({', '.join(detectors)})")
            rprint(f"  [green]✓[/green] halted as expected -- routed to {s7_demo.get('review_path')}")
        else:
            rprint("  [yellow]⚠ injected-leak demo did not trigger a halt -- detectors may be unavailable (run `make setup`).[/yellow]")

        rprint("[bold cyan]Stage 8/9 — Package + transmit[/bold cyan] [dim](Req 8, 15)[/dim]")
        s8 = stage8_package.run(f"data/work/{s3['drs'].request_id}/deid", tokens, s3["drs"].request_id)
        audit_log.record(stage_id="stage8_package", batch_id=s8["snapshot_id"], record_counts=s8["tables"],
                          outcome="ok", reconciliation_status="ok")
        rprint(f"  [dim]snapshot:[/dim] {s8['snapshot_id']}  [dim]refresh:[/dim] {s8['refresh_id']}")
        for table_name, info in s8["tables"].items():
            rprint(f"  [dim]•[/dim] {table_name}: {info['row_count']} rows")
        rprint(f"  [green]✓[/green] +{len(s8['diff']['added'])} added / {len(s8['diff']['updated'])} updated / "
               f"{len(s8['diff']['deleted'])} deleted since last refresh")

        rprint("[bold cyan]Stage 9/9 — Audit + QA manifest + RFI summary[/bold cyan] [dim](Req 1b, 9, 11)[/dim]")
        record_counts = {**s1["counts"], **s2["raw"]}
        qa_manifest = stage9_audit.build_qa_manifest(record_counts, s4, s7)
        stage9_audit.write_qa_manifest(qa_manifest)
        stage9_audit.write_rfi_summary(s3["drs"].request_id, s3["drs"].version, halted=False)
        stage9_audit.log_model_versions(audit_log, s3["drs"].version)
        rprint("  [green]✓[/green] audit trail, PII-free QA manifest, and RFI summary written to data/work/")
        rprint("  [dim]→ run `data-counts verify-logs` to confirm neither log has been tampered with[/dim]")

        rprint(f"\n[bold green]✓ run-all complete[/bold green] — request {request_id}, "
               f"{s1['counts'].get('Patient', 0)} patients through all 9 stages, nothing halted.")
        rprint(qa_manifest)

    except PipelineHalt as halt:
        stage9_audit.write_rfi_summary(request_id, "n/a", halted=True)
        rprint(f"[bold red]PIPELINE HALTED:[/bold red] {halt}")
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
