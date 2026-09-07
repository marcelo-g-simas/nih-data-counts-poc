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
    """Generate the central platform's RSA keypair. Run once; hand `central_public_key.pem`
    to each Healthcare Partner site out-of-band before their first transmission (Req 5).
    """
    seclog = SecurityAuditLog()
    seclog.log_event(category="authentication", name="CLI invocation", user=_operator,
                      action="setup-keys", resource="central-cli", outcome="success")
    priv, pub = crypto.generate_central_keys(keys_dir)
    seclog.log_event(category="configuration_change", name="Central RSA keypair provisioned",
                      user=_operator, action="generate_central_keys", resource=keys_dir, outcome="success")
    rprint(f"[green]Central keypair generated in {keys_dir}/[/green]")
    rprint(f"  Public key (share with sites): {Path(keys_dir) / 'central_public_key.pem'}")


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
