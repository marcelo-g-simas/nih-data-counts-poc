"""Central platform CLI. [Req 5]"""
from __future__ import annotations

import getpass
from pathlib import Path

import typer
from rich import print as rprint
from rich.table import Table

from . import crypto, evaluation, ingest, linkage
from .security_audit import SecurityAuditLog

app = typer.Typer(help="Data COUNTS central platform: PPRL token resolution + cross-site linkage.")
_operator = getpass.getuser()


@app.command("setup-keys")
def setup_keys(keys_dir: str = "keys"):
    """Provision the central platform's RSA keypair and date-shift secret. Hand
    `central_public_key.pem` to each Healthcare Partner site out-of-band before their first
    transmission (Req 5).

    Idempotent: both secrets are load-or-create, so re-running this is safe. Rotating either
    one invalidates historical packages (see `crypto.generate_central_keys`), so it has to be
    asked for deliberately rather than being a side effect of running setup twice.
    """
    seclog = SecurityAuditLog()
    seclog.log_event(category="authentication", name="CLI invocation", user=_operator,
                      action="setup-keys", resource="central-cli", outcome="success")
    crypto.generate_central_keys(keys_dir)
    seclog.log_event(category="configuration_change", name="Central RSA keypair provisioned",
                      user=_operator, action="generate_central_keys", resource=keys_dir, outcome="success")
    # Load-or-create, so re-running setup-keys does not silently re-shift every
    # date the platform has ever normalized. See generate_central_shift_secret.
    crypto.generate_central_shift_secret(keys_dir)
    seclog.log_event(category="configuration_change", name="Central date-shift secret in use",
                      user=_operator, action="generate_central_shift_secret", resource=keys_dir,
                      outcome="success",
                      message="date-shift secret loaded (generated on first run; independent of the RSA keypair)")
    rprint(f"[green]Central keypair provisioned in {keys_dir}/[/green]")
    rprint(f"  Public key (share with sites): {Path(keys_dir) / 'central_public_key.pem'}")
    rprint(f"  Date-shift secret (never shared): {Path(keys_dir) / crypto.SHIFT_SECRET_FILENAME}")


@app.command("issue-drs-shift-key")
def issue_drs_shift_key(request_id: str, keys_dir: str = "keys", out: str | None = None):
    """Derive the per-DRS date-shift key to hand to every site responding to a DRS. [Req 4]

    Every site responding to `request_id` gets the same key, which is what makes
    their date offsets agree; the central platform then re-offsets everything onto
    one global offset per patient at ingest, so offsets agree across DRSs too.

    This is a genuine secret, not a public key: a holder can compute a patient's
    per-DRS offset and, given a candidate identity, un-shift that DRS's dates.
    Distribute it out-of-band, per DRS, to responding sites only. The central
    shift secret it is derived from is never distributed.
    """
    seclog = SecurityAuditLog()
    seclog.log_event(category="authentication", name="CLI invocation", user=_operator,
                      action="issue-drs-shift-key", resource="central-cli", outcome="success")
    secret = crypto.generate_central_shift_secret(keys_dir)
    shift_key = crypto.derive_drs_shift_key(secret, request_id)
    out_path = Path(out) if out else Path(keys_dir) / "drs_shift_key.bin"
    out_path.write_bytes(shift_key)
    out_path.chmod(0o600)
    seclog.log_event(category="administrative_action", name="Per-DRS date-shift key issued",
                      user=_operator, action="derive_drs_shift_key", resource=request_id,
                      outcome="success", message=f"written to {out_path}")
    rprint(f"[green]Per-DRS shift key for {request_id} written to {out_path}[/green]")
    rprint("  [yellow]Secret material — distribute out-of-band to responding sites only.[/yellow]")


@app.command("reoffset-dates")
def reoffset_dates(request_id: str, site: str, inbox_dir: str = "inbox", keys_dir: str = "keys",
                    out_dir: str | None = None, max_years: int = 3, jitter_weeks: int = 2):
    """Re-offset one site package's dates onto this platform's global offset. [Req 4]

    The site shifted its dates by a per-DRS offset. This applies the delta that
    carries them to the single global offset for that patient, so the same
    individual's dates agree across every site and every DRS -- making intervals
    measured across submissions valid.

    No true calendar date is computed: the two offsets are subtracted from each
    other, never from the stored date. There is consequently nothing to delete
    afterward. See `crypto.resolve_and_shift`.
    """
    seclog = SecurityAuditLog()
    seclog.log_event(category="authentication", name="CLI invocation", user=_operator,
                      action="reoffset-dates", resource="central-cli", outcome="success")
    secret = crypto.generate_central_shift_secret(keys_dir)
    central_priv = (Path(keys_dir) / "central_private_key.pem").read_bytes()
    site_dir = Path(inbox_dir) / site
    target = Path(out_dir) if out_dir else Path("normalized") / request_id / site
    counts = ingest.reoffset_domain_tables(
        site_dir, central_priv, secret, request_id, target, max_years, jitter_weeks,
    )
    seclog.log_event(category="administrative_action", name="Date re-offset applied", user=_operator,
                      action="reoffset_domain_tables", resource=f"{request_id}/{site}",
                      outcome="success", message=f"domains={sorted(counts)} rows={sum(counts.values())}")
    if not counts:
        rprint(f"[yellow]No date-bearing domain tables found under {site_dir}/[/yellow]")
        return
    for domain, n in sorted(counts.items()):
        rprint(f"  [green]✓[/green] {domain}: {n} row(s) re-offset")
    rprint(f"[bold green]Written to {target}/[/bold green]")


@app.command("verify-logs")
def verify_logs(work_dir: str = "."):
    """Verify the hash-chained security audit log (Req 11: "protected against
    unauthorized modification or deletion").
    """
    result = SecurityAuditLog(work_dir).verify()
    rprint(f"[bold cyan]Security audit log (Req 11):[/bold cyan] {result}")
    if not result["ok"]:
        rprint("[bold red]TAMPERING DETECTED[/bold red]")
        raise typer.Exit(code=1)
    rprint("[bold green]Log verified intact.[/bold green]")


@app.command("link")
def link(inbox_dir: str = "inbox", keys_dir: str = "keys", ground_truth: str | None = None,
         predictions_out: str | None = None, clusters_out: str | None = None):
    """Ingest every site package under `inbox_dir`, resolve their ephemeral tokens,
    and run cross-site probabilistic linkage. [Req 5]

    `--ground-truth` is a synthetic-demo-only answer key (see
    `export-linkage-demo-sites` in the site project); when given, also prints
    pair-level precision/recall/F1 -- this is never something the real
    pipeline has access to.
    """
    seclog = SecurityAuditLog()
    seclog.log_event(category="authentication", name="CLI invocation", user=_operator,
                      action="link", resource="central-cli", outcome="success")
    seclog.log_event(category="administrative_action", name="Cross-site linkage run", user=_operator,
                      action="link", resource=inbox_dir, outcome="attempted")
    central_priv = (Path(keys_dir) / "central_private_key.pem").read_bytes()
    resolved = ingest.resolve_all_sites(inbox_dir, central_priv)
    if len(resolved) < 2:
        rprint(f"[yellow]Need at least 2 site packages under {inbox_dir}/ to demonstrate cross-site linkage; found {len(resolved)}.[/yellow]")
        raise typer.Exit(code=1)

    rprint(f"[bold cyan]Resolved {len(resolved)} site package(s): {list(resolved.keys())}[/bold cyan]")
    result = linkage.link_resolved_sites(resolved)
    clusters_df = result["clusters"]

    if predictions_out:
        result["predictions"].to_csv(predictions_out, index=False)
    if clusters_out:
        clusters_df.to_csv(clusters_out, index=False)

    cluster_sizes = clusters_df.groupby("cluster_id").size()
    cross_site_clusters = clusters_df.groupby("cluster_id")["source_dataset"].nunique()
    linked_across_sites = int((cross_site_clusters > 1).sum())

    table = Table(title="Linkage clusters")
    table.add_column("cluster_id")
    table.add_column("size")
    table.add_column("sites")
    for cid, size in cluster_sizes.items():
        sites = sorted(clusters_df[clusters_df["cluster_id"] == cid]["source_dataset"].unique())
        table.add_row(str(cid), str(size), ", ".join(sites))
    rprint(table)
    rprint(f"[bold green]{linked_across_sites} identity(ies) linked across sites.[/bold green]")

    if ground_truth:
        gt = evaluation.load_ground_truth(ground_truth)
        metrics = evaluation.evaluate(clusters_df, gt)
        rprint("\n[bold cyan]Linkage evaluation (synthetic-demo-only ground truth)[/bold cyan]")
        rprint(f"  precision: {metrics['precision']:.3f}   recall: {metrics['recall']:.3f}   F1: {metrics['f1']:.3f}")
        rprint(f"  true positives:  {metrics['true_positive_pairs']}")
        rprint(f"  false positives: {metrics['false_positive_pairs']}")
        rprint(f"  false negatives: {metrics['false_negative_pairs']}")


if __name__ == "__main__":
    app()
