#!/usr/bin/env python3
"""Re-run the existing site/central pipelines at larger scale and render real
charts from the results (Part 3 of the demo-impact plan). No fabricated
numbers: every figure drawn here comes from a pipeline invocation this script
just ran, captured from its own output files/stdout.

What this does, in order:

  1. Runs `data-counts run-all` at 100 / 1,000 / 10,000 patients (site/), each
     time capturing the DQ anomaly report and the per-patient date-shift
     lattice values it produced, before the next run overwrites them.
  2. Runs `data-counts export-linkage-demo-sites` (site/) at a few scales --
     the original 5-identity/2-site scenario plus larger multi-site ones added
     by this same change -- and `data-counts-central link` (central/) against
     each, capturing precision/recall/F1 and the resulting cluster sizes.
  3. Renders four SVG charts (hand-written, no chart library added -- see
     scripts/render_charts.py's own docstring below for why) under `assets/`,
     using the accessible categorical/status palette from this repo's dataviz
     house style.
  4. Writes a JSON summary of every number plotted, so REPORT.md's prose can
     cite it and a future re-run can diff against it.

Deliberately uses temp directories for the linkage scale-out (never the
shared `central/inbox`/`../linkage_ground_truth.json` the plain `make
linkage-demo` target uses), so running this script does not disturb that
demo's state. The DQ/date-shift re-runs do use `site/`'s and `central/`'s
default working directories, the same way `make demo` already does --
`run-all` has no override for them and adding one is out of scope for this
change (see the plan this script implements).

No new dependency: SVG is written by hand rather than adding matplotlib (or
similar) to either project's pyproject.toml, per this change's scope --
these are a handful of bar/histogram/status-grid charts, well within reach of
plain SVG, so a plotting dependency wasn't justified.
"""
from __future__ import annotations

import csv
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SITE_DIR = REPO_ROOT / "site"
CENTRAL_DIR = REPO_ROOT / "central"
ASSETS_DIR = REPO_ROOT / "assets"
DRS_REQUEST_ID = "DC-POC-0001"  # the default DRS both `run-all` and this script use

# Reference palette (dataviz skill): validated categorical order + status colors.
# Only the slots this script's charts need.
COLOR = {
    "surface": "#fcfcfb",
    "text_primary": "#0b0b0b",
    "text_secondary": "#52514e",
    "grid": "#e3e2dc",
    "blue": "#2a78d6",     # categorical slot 1 -- precision
    "orange": "#eb6834",   # categorical slot 2 -- recall
    "aqua": "#1baf7a",     # categorical slot 3 -- F1
    "good": "#0ca30c",     # status: pass / no anomaly
}

POPULATIONS = [100, 1_000, 10_000]

LINKAGE_SCENARIOS = [
    {"label": "baseline", "scale": 1, "n_sites": 2},
    {"label": "10x identities, 4 sites", "scale": 10, "n_sites": 4},
    {"label": "40x identities, 6 sites", "scale": 40, "n_sites": 6},
]


def run(cmd: list[str], cwd: Path) -> str:
    print(f"$ (cd {cwd.relative_to(REPO_ROOT)} && {' '.join(cmd)})")
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    if result.returncode != 0:
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(cmd)}")
    return result.stdout


# --------------------------------------------------------------------------
# Pipeline runs
# --------------------------------------------------------------------------

def collect_dq_and_shift_evidence() -> tuple[dict[int, dict], dict[int, list[int]]]:
    """Re-run the main pipeline at each population, capturing (a) the DQ
    anomaly report and (b) every patient's realized date-shift lattice value
    (`_shift_days`, from stage 6's ephemeral token map) before the next
    population's run-all overwrites `site/data/work/`.
    """
    # Idempotent (load-or-create secret underneath): safe to always (re-)issue
    # rather than assume a prior `make setup`/`issue-drs-shift-key` already ran.
    run(["uv", "run", "data-counts-central", "issue-drs-shift-key", DRS_REQUEST_ID], cwd=CENTRAL_DIR)

    dq_by_pop: dict[int, dict] = {}
    shift_days_by_pop: dict[int, list[int]] = {}
    for pop in POPULATIONS:
        run(["uv", "run", "data-counts", "run-all", "--population", str(pop)], cwd=SITE_DIR)
        dq_by_pop[pop] = json.loads((SITE_DIR / "data/work/dq_anomaly_report.json").read_text())
        tokens_path = SITE_DIR / "data/work" / DRS_REQUEST_ID / "pprl/tokens_ephemeral.json"
        tokens = json.loads(tokens_path.read_text())
        shift_days_by_pop[pop] = [
            v["_shift_days"] for v in tokens.values() if v.get("_shift_days") is not None
        ]
    return dq_by_pop, shift_days_by_pop


_METRICS_RE = re.compile(
    r"precision:\s*([0-9.]+)\s+recall:\s*([0-9.]+)\s+F1:\s*([0-9.]+)"
)


def collect_linkage_evidence(tmp_root: Path) -> list[dict]:
    """Run `export-linkage-demo-sites` + `link` at each scenario in
    LINKAGE_SCENARIOS, entirely under `tmp_root` -- never the shared
    `central/inbox`/`../linkage_ground_truth.json` `make linkage-demo` uses.
    """
    results = []
    for sc in LINKAGE_SCENARIOS:
        tag = f"{sc['scale']}x{sc['n_sites']}"
        inbox_dir = tmp_root / f"inbox_{tag}"
        ground_truth_path = tmp_root / f"ground_truth_{tag}.json"
        clusters_csv = tmp_root / f"clusters_{tag}.csv"

        run(
            ["uv", "run", "data-counts", "export-linkage-demo-sites",
             "--inbox-dir", str(inbox_dir), "--ground-truth-path", str(ground_truth_path),
             "--scale", str(sc["scale"]), "--n-sites", str(sc["n_sites"])],
            cwd=SITE_DIR,
        )
        ground_truth = json.loads(ground_truth_path.read_text())
        n_identities = sum(len(site_map) for site_map in ground_truth.values())

        stdout = run(
            ["uv", "run", "data-counts-central", "link",
             "--inbox-dir", str(inbox_dir), "--ground-truth", str(ground_truth_path),
             "--clusters-out", str(clusters_csv)],
            cwd=CENTRAL_DIR,
        )
        match = _METRICS_RE.search(stdout)
        if not match:
            raise RuntimeError(f"could not find precision/recall/F1 in `link` output for {tag}")
        precision, recall, f1 = (float(x) for x in match.groups())

        cluster_sizes: dict[str, int] = {}
        with clusters_csv.open() as fh:
            for row in csv.DictReader(fh):
                cluster_sizes[row["cluster_id"]] = cluster_sizes.get(row["cluster_id"], 0) + 1

        results.append({
            "label": sc["label"], "scale": sc["scale"], "n_sites": sc["n_sites"],
            "n_records": n_identities, "precision": precision, "recall": recall, "f1": f1,
            "cluster_sizes": sorted(cluster_sizes.values()),
        })
    return results


# --------------------------------------------------------------------------
# Minimal hand-written SVG chart rendering
# --------------------------------------------------------------------------

def _svg_open(width: int, height: int) -> list[str]:
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="-apple-system, Segoe UI, Helvetica, Arial, sans-serif">',
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="{COLOR["surface"]}"/>',
    ]


def _text(x: float, y: float, s: str, *, size: int = 13, color: str = None,
          anchor: str = "start", weight: str = "normal") -> str:
    color = color or COLOR["text_primary"]
    return (f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" fill="{color}" '
            f'text-anchor="{anchor}" font-weight="{weight}">{s}</text>')


def grouped_bar_chart(path: Path, title: str, subtitle: str, categories: list[str],
                       series: list[tuple[str, str, list[float]]], y_max: float,
                       y_ticks: list[float], y_fmt=lambda v: f"{v:.1f}") -> None:
    """series: list of (name, color, values) -- one value per category, aligned."""
    width, height = 760, 420
    margin = {"left": 60, "right": 24, "top": 70, "bottom": 90}
    plot_w = width - margin["left"] - margin["right"]
    plot_h = height - margin["top"] - margin["bottom"]

    svg = _svg_open(width, height)
    svg.append(_text(margin["left"], 30, title, size=18, weight="600"))
    svg.append(_text(margin["left"], 50, subtitle, size=13, color=COLOR["text_secondary"]))

    # gridlines + y-axis labels
    for tick in y_ticks:
        y = margin["top"] + plot_h - (tick / y_max) * plot_h
        svg.append(f'<line x1="{margin["left"]}" y1="{y:.1f}" x2="{width - margin["right"]}" '
                    f'y2="{y:.1f}" stroke="{COLOR["grid"]}" stroke-width="1"/>')
        svg.append(_text(margin["left"] - 10, y + 4, y_fmt(tick), size=11,
                          color=COLOR["text_secondary"], anchor="end"))

    n_cat = len(categories)
    n_series = len(series)
    group_w = plot_w / n_cat
    bar_w = min(28, group_w / (n_series + 1.4))
    gap = bar_w * 0.25

    for gi, cat in enumerate(categories):
        group_x0 = margin["left"] + gi * group_w
        total_w = n_series * bar_w + (n_series - 1) * gap
        start_x = group_x0 + (group_w - total_w) / 2
        for si, (_name, color, values) in enumerate(series):
            val = values[gi]
            bar_h = (val / y_max) * plot_h
            x = start_x + si * (bar_w + gap)
            y = margin["top"] + plot_h - bar_h
            svg.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{bar_h:.1f}" '
                        f'rx="3" fill="{color}"/>')
            svg.append(_text(x + bar_w / 2, y - 6, y_fmt(val), size=10,
                              color=COLOR["text_secondary"], anchor="middle"))
        svg.append(_text(group_x0 + group_w / 2, margin["top"] + plot_h + 20, cat, size=12,
                          anchor="middle"))

    # axis baseline
    svg.append(f'<line x1="{margin["left"]}" y1="{margin["top"] + plot_h}" '
                f'x2="{width - margin["right"]}" y2="{margin["top"] + plot_h}" '
                f'stroke="{COLOR["text_secondary"]}" stroke-width="1"/>')

    # legend
    legend_y = height - 24
    lx = margin["left"]
    for name, color, _values in series:
        svg.append(f'<rect x="{lx}" y="{legend_y - 10}" width="12" height="12" rx="2" fill="{color}"/>')
        svg.append(_text(lx + 18, legend_y, name, size=12))
        lx += 18 + 10 * len(name) + 28

    svg.append("</svg>")
    path.write_text("\n".join(svg))


def histogram_chart(path: Path, title: str, subtitle: str, labels: list[str],
                     values: list[int], color: str, x_label: str, y_label: str) -> None:
    width, height = 900, 380
    margin = {"left": 56, "right": 24, "top": 66, "bottom": 74}
    plot_w = width - margin["left"] - margin["right"]
    plot_h = height - margin["top"] - margin["bottom"]
    y_max = max(values) * 1.15 if values else 1

    svg = _svg_open(width, height)
    svg.append(_text(margin["left"], 28, title, size=18, weight="600"))
    svg.append(_text(margin["left"], 48, subtitle, size=13, color=COLOR["text_secondary"]))

    for frac in (0, 0.25, 0.5, 0.75, 1.0):
        y = margin["top"] + plot_h - frac * plot_h
        svg.append(f'<line x1="{margin["left"]}" y1="{y:.1f}" x2="{width - margin["right"]}" '
                    f'y2="{y:.1f}" stroke="{COLOR["grid"]}" stroke-width="1"/>')
        svg.append(_text(margin["left"] - 8, y + 4, f"{round(frac * y_max)}", size=11,
                          color=COLOR["text_secondary"], anchor="end"))

    n = max(len(labels), 1)
    bar_w = plot_w / n
    for i, (label, val) in enumerate(zip(labels, values)):
        bar_h = (val / y_max) * plot_h if y_max else 0
        x = margin["left"] + i * bar_w
        y = margin["top"] + plot_h - bar_h
        svg.append(f'<rect x="{x + 1:.1f}" y="{y:.1f}" width="{max(bar_w - 2, 1):.1f}" '
                    f'height="{bar_h:.1f}" fill="{color}"/>')
        if n <= 40:
            svg.append(_text(x + bar_w / 2, margin["top"] + plot_h + 16, label, size=9,
                              anchor="middle", color=COLOR["text_secondary"]))

    svg.append(f'<line x1="{margin["left"]}" y1="{margin["top"] + plot_h}" '
                f'x2="{width - margin["right"]}" y2="{margin["top"] + plot_h}" '
                f'stroke="{COLOR["text_secondary"]}" stroke-width="1"/>')
    svg.append(_text(width / 2, height - 8, x_label, size=12, anchor="middle",
                      color=COLOR["text_secondary"]))
    svg.append(_text(16, margin["top"] - 10, y_label, size=11, color=COLOR["text_secondary"]))
    svg.append("</svg>")
    path.write_text("\n".join(svg))


def status_grid_chart(path: Path, title: str, subtitle: str, row_labels: list[str],
                       col_labels: list[str], all_pass: bool) -> None:
    width = 140 + 130 * len(col_labels)
    height = 90 + 46 * len(row_labels)
    margin_left, top = 140, 90
    cell_w, cell_h = 110, 36

    svg = _svg_open(width, height)
    svg.append(_text(20, 28, title, size=18, weight="600"))
    svg.append(_text(20, 48, subtitle, size=13, color=COLOR["text_secondary"]))

    for ci, col in enumerate(col_labels):
        cx = margin_left + ci * cell_w + cell_w / 2
        svg.append(_text(cx, top - 14, col, size=12, anchor="middle", weight="600"))

    for ri, row in enumerate(row_labels):
        ry = top + ri * cell_h
        svg.append(_text(margin_left - 12, ry + cell_h / 2 + 4, row, size=12, anchor="end"))
        for ci in range(len(col_labels)):
            cx = margin_left + ci * cell_w
            fill = COLOR["good"] if all_pass else COLOR["grid"]
            svg.append(f'<rect x="{cx + 3}" y="{ry + 3}" width="{cell_w - 6}" height="{cell_h - 6}" '
                       f'rx="4" fill="{fill}" opacity="0.16"/>')
            mark = "✓ pass" if all_pass else "?"
            svg.append(_text(cx + cell_w / 2, ry + cell_h / 2 + 4, mark, size=12, anchor="middle",
                              color=COLOR["good"] if all_pass else COLOR["text_secondary"],
                              weight="600"))
    svg.append("</svg>")
    path.write_text("\n".join(svg))


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> None:
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)

    print("== Re-running the main pipeline at 100 / 1,000 / 10,000 patients ==")
    dq_by_pop, shift_days_by_pop = collect_dq_and_shift_evidence()

    print("\n== Re-running the linkage demo at multiple scales ==")
    with tempfile.TemporaryDirectory(prefix="data_counts_charts_") as tmp:
        linkage_results = collect_linkage_evidence(Path(tmp))

    # ---- Chart 1: precision/recall/F1 vs. cohort size ----
    categories = [f"{r['label']}\n({r['n_records']} records)" for r in linkage_results]
    categories = [c.replace("\n", " -- ") for c in categories]
    series = [
        ("precision", COLOR["blue"], [r["precision"] for r in linkage_results]),
        ("recall", COLOR["orange"], [r["recall"] for r in linkage_results]),
        ("F1", COLOR["aqua"], [r["f1"] for r in linkage_results]),
    ]
    grouped_bar_chart(
        ASSETS_DIR / "linkage_metrics_vs_scale.svg",
        "Cross-site linkage: precision / recall / F1 vs. cohort size",
        "Same 4-case scenario (exact / noisy-typo / twins / site-only), replicated across more identities and sites",
        categories, series, y_max=1.0, y_ticks=[0.0, 0.25, 0.5, 0.75, 1.0],
        y_fmt=lambda v: f"{v:.2f}",
    )

    # ---- Chart 2: cluster-size distribution from the largest linkage run ----
    largest = linkage_results[-1]
    size_counts: dict[int, int] = {}
    for s in largest["cluster_sizes"]:
        size_counts[s] = size_counts.get(s, 0) + 1
    sizes_sorted = sorted(size_counts)
    histogram_chart(
        ASSETS_DIR / "cluster_size_distribution.svg",
        "Linkage cluster-size distribution",
        f"{largest['label']} -- {largest['n_records']} records across {largest['n_sites']} sites, "
        f"{len(largest['cluster_sizes'])} clusters",
        [str(s) for s in sizes_sorted], [size_counts[s] for s in sizes_sorted],
        COLOR["blue"], "cluster size (records linked together)", "number of clusters",
    )

    # ---- Chart 3: DQ anomaly rate breakdown by check, across cohort sizes ----
    check_ids = [c["id"] for c in dq_by_pop[POPULATIONS[0]]["checks"]]
    any_anomaly = any(
        c["anomaly"] for pop in POPULATIONS for c in dq_by_pop[pop]["checks"]
    )
    status_grid_chart(
        ASSETS_DIR / "dq_anomaly_by_check.svg",
        "DQ anomaly status by Kahn check, across cohort sizes",
        "Every check re-run at each population; cell = anomaly/no-anomaly for that check at that scale",
        check_ids, [f"{pop:,}" for pop in POPULATIONS], all_pass=not any_anomaly,
    )

    # ---- Chart 4: distribution of realized per-patient date-shift lattice values ----
    largest_pop = POPULATIONS[-1]
    shifts = shift_days_by_pop[largest_pop]
    shift_counts: dict[int, int] = {}
    for v in shifts:
        shift_counts[v] = shift_counts.get(v, 0) + 1
    shifts_sorted = sorted(shift_counts)
    histogram_chart(
        ASSETS_DIR / "date_shift_lattice_distribution.svg",
        "Realized per-patient date-shift offsets",
        f"{largest_pop:,}-patient run -- {len(shifts)} patients, {len(shifts_sorted)} distinct lattice values realized",
        [str(v) for v in shifts_sorted], [shift_counts[v] for v in shifts_sorted],
        COLOR["blue"], "shift_days (lattice value)", "number of patients",
    )

    # ---- Summary JSON, for REPORT.md's prose and future diffing ----
    summary = {
        "dq_by_population": {
            pop: {
                "any_anomaly": any(c["anomaly"] for c in dq["checks"]),
                "checks": {c["id"]: {"anomaly": c["anomaly"], "detail": c["detail"]} for c in dq["checks"]},
            }
            for pop, dq in dq_by_pop.items()
        },
        "date_shift_by_population": {
            pop: {
                "n_patients": len(shifts),
                "n_distinct_values": len(set(shifts)),
                "min": min(shifts), "max": max(shifts),
            }
            for pop, shifts in shift_days_by_pop.items()
        },
        "linkage_by_scenario": [
            {k: v for k, v in r.items() if k != "cluster_sizes"} | {"n_clusters": len(r["cluster_sizes"])}
            for r in linkage_results
        ],
    }
    (ASSETS_DIR / "render_charts_summary.json").write_text(json.dumps(summary, indent=2))

    print("\n== Summary ==")
    print(json.dumps(summary, indent=2))
    print(f"\nCharts written to {ASSETS_DIR}")


if __name__ == "__main__":
    main()
