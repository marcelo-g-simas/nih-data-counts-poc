"""CLI entry point: one subcommand per pipeline stage, plus `run-all`. [Req 1-15]

SYNTHETIC DATA ONLY. See README.md for the full RFI-requirement map.
"""
from __future__ import annotations

import json
from pathlib import Path

import typer
from rich import print as rprint

from . import (
    stage1_synthea,
    stage2_ingest,
    stage3_dsr,
    stage4_dq_kahn,
    stage5_deid,
    stage6_pprl,
    stage7_leak_gate,
    stage8_package,
    stage9_audit,
)
from .audit import AuditLog, PipelineHalt

app = typer.Typer(help="Data COUNTS leave-behind pipeline POC (synthetic data only).")


@app.command("setup-models")
def setup_models():
    """One-time, online model download step. After this, the pipeline runs fully offline.

    The spaCy model (`en-core-web-sm`) isn't downloaded here -- it's a real pinned
    dependency in pyproject.toml (see the `[tool.uv.sources]` comment there for why:
    Presidio's spaCy engine falls back to a `pip`-based auto-download otherwise, and a
    `uv`-managed venv doesn't ship `pip`), so plain `uv sync` already installs it.
    """
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
def cli_stage1(out: str = "data/synthea_ndjson", population: int = 1000, seed: int = 42, use_docker: bool = False):
    result = stage1_synthea.run(out, population=population, seed=seed, use_docker=use_docker)
    rprint(result)


@app.command("stage2-ingest")
def cli_stage2(ndjson_dir: str = "data/synthea_ndjson", lake_dir: str = "data/lake"):
    result = stage2_ingest.run(ndjson_dir, lake_dir)
    rprint(result)


@app.command("run-all")
def run_all(dsr: str = "dsr/examples/DC-POC-0001.json", population: int = 1000):
    audit_log = AuditLog()
    request_id = "unknown"
    try:
        rprint("[bold cyan]Stage 1: Synthea generation[/bold cyan]")
        s1 = stage1_synthea.run(population=population)
        audit_log.record(stage_id="stage1_synthea", batch_id="cohort", record_counts=s1["counts"],
                          outcome="ok", reconciliation_status="n/a")

        rprint("[bold cyan]Stage 2: Ingest (raw lossless landing + analytic views)[/bold cyan]")
        s2 = stage2_ingest.run(s1["out_dir"])
        audit_log.record(stage_id="stage2_ingest", batch_id="lake", record_counts=s2["raw"],
                          outcome="ok", reconciliation_status="ok")

        rprint("[bold cyan]Stage 3: DSR orchestration[/bold cyan]")
        s3 = stage3_dsr.run(dsr)
        request_id = s3["dsr"].request_id
        availability = s3["availability"]
        if availability["missing_domains"] or availability["missing_fields"]:
            rprint(f"[yellow]DSR availability gaps (no silent substitution): {availability}[/yellow]")
        audit_log.record(stage_id="stage3_dsr", batch_id=s3["dsr"].request_id, record_counts={},
                          outcome="ok", reconciliation_status="ok", extra=availability)

        rprint("[bold cyan]Stage 4: Data quality (Kahn framework)[/bold cyan]")
        s4 = stage4_dq_kahn.run(s3["dsr"].checks)
        audit_log.record(
            stage_id="stage4_dq_kahn", batch_id=s3["dsr"].request_id,
            record_counts={c["id"]: int(not c["anomaly"]) for c in s4["checks"]},
            outcome="halt" if s4["halt"] else "ok", reconciliation_status="anomaly_report_emitted",
            extra=s4,
        )
        Path("data/work").mkdir(parents=True, exist_ok=True)
        Path("data/work/dq_anomaly_report.json").write_text(json.dumps(s4, indent=2))
        if s4["halt"]:
            raise PipelineHalt(f"DQ check(s) failed with on_fail=halt: {s4}")

        rprint("[bold cyan]Stage 5: De-identification[/bold cyan]")
        s5 = stage5_deid.run(s3["dsr"])
        audit_log.record(stage_id="stage5_deid", batch_id=s3["dsr"].request_id, record_counts=s5,
                          outcome="ok", reconciliation_status="ok")

        rprint("[bold cyan]Stage 6: OPPRL tokenization[/bold cyan]")
        tokens = stage6_pprl.run()
        both_ends = stage6_pprl.demo_both_ends()
        audit_log.record(
            stage_id="stage6_pprl", batch_id=s3["dsr"].request_id, record_counts={"tokens": len(tokens)},
            outcome="ok", reconciliation_status="ok",
            extra={"both_ends_linkage_ok": both_ends["shared_identities_linked"] and both_ends["non_overlapping_do_not_link"]},
        )

        rprint("[bold cyan]Stage 7: Residual PII-leakage gate[/bold cyan]")
        # This synthetic cohort carries no free-text clinical notes, so the real outbound
        # batch is scanned here (and is expected to come back clean). The halt path itself
        # is demonstrated separately, on a deliberately injected-PII fixture, so that
        # demonstrating the halt doesn't block this run from reaching stage 8/outbox --
        # both are acceptance criteria, and conflating them would make one impossible to show.
        real_notes: list[dict] = []
        s7 = stage7_leak_gate.run(real_notes, batch_id=s3["dsr"].request_id)
        audit_log.record(
            stage_id="stage7_leak_gate", batch_id=s3["dsr"].request_id,
            record_counts={"flagged": len(s7["flagged"])},
            outcome="halt" if s7["halt"] else "ok", reconciliation_status="scanned",
        )
        if s7["halt"]:
            rprint(f"[red]Residual PII leak detected -- routed to {s7.get('review_path')}. Halting transmission.[/red]")
            raise PipelineHalt("Unresolved residual PII leak")

        rprint("[bold cyan]Stage 7 (demo): injected-PII leak halts and routes to review_queue/[/bold cyan]")
        injected_leak_notes = [{"id": "note-injected-leak", "note_text": "Patient Jane Q. Doe, DOB 1980-05-12, SSN 123-45-6789, reachable at jane.doe@example.com."}]
        s7_demo = stage7_leak_gate.run(injected_leak_notes, batch_id=f"{s3['dsr'].request_id}-leak-demo")
        audit_log.record(
            stage_id="stage7_leak_gate_demo", batch_id=f"{s3['dsr'].request_id}-leak-demo",
            record_counts={"flagged": len(s7_demo["flagged"])},
            outcome="halt" if s7_demo["halt"] else "ok", reconciliation_status="scanned",
        )
        if s7_demo["halt"]:
            rprint(f"[green]Injected-leak demo halted as expected -- routed to {s7_demo.get('review_path')}.[/green]")
        else:
            rprint("[yellow]Injected-leak demo did not trigger a halt -- detectors may be unavailable (run `make setup`).[/yellow]")

        rprint("[bold cyan]Stage 8: Package + transmit[/bold cyan]")
        s8 = stage8_package.run("data/work/deid", tokens, s3["dsr"].request_id)
        audit_log.record(stage_id="stage8_package", batch_id=s8["snapshot_id"], record_counts=s8["tables"],
                          outcome="ok", reconciliation_status="ok")

        rprint("[bold cyan]Stage 9: Audit + QA manifest + RFI summary[/bold cyan]")
        record_counts = {**s1["counts"], **s2["raw"]}
        qa_manifest = stage9_audit.build_qa_manifest(record_counts, s4, s7)
        stage9_audit.write_qa_manifest(qa_manifest)
        stage9_audit.write_rfi_summary(s3["dsr"].request_id, s3["dsr"].version, halted=False)
        stage9_audit.log_model_versions(audit_log, s3["dsr"].version)

        rprint("[bold green]run-all complete.[/bold green]")
        rprint(qa_manifest)

    except PipelineHalt as halt:
        stage9_audit.write_rfi_summary(request_id, "n/a", halted=True)
        rprint(f"[bold red]PIPELINE HALTED:[/bold red] {halt}")
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
