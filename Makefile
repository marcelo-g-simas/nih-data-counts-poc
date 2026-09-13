.PHONY: setup demo linkage-demo verify-logs report-charts test clean

# One-time setup for both projects: installs deps, downloads/exports models
# (site), and generates the central platform's RSA keypair (central).
setup:
	cd site && $(MAKE) setup
	cd central && $(MAKE) setup

# Runs the main leave-behind pipeline (site) end to end.
demo:
	cd site && $(MAKE) demo

# Req 5 evidence: two independently-keyed simulated Healthcare Partner sites,
# each tokenizing under its own key, transcrypt to the central platform, and
# a Splink-based cross-site linkage run with precision/recall/F1 against a
# synthetic-demo-only ground truth. See site/src/data_counts/cli.py's
# export-linkage-demo-sites and central/README.md for the full rationale.
linkage-demo:
	cd site && uv run data-counts export-linkage-demo-sites
	cd central && uv run data-counts-central link --ground-truth ../linkage_ground_truth.json

# Req 11: verify both projects' hash-chained logs are intact (detects any
# edit, deletion, reordering, or truncation of a previously-written entry).
verify-logs:
	cd site && $(MAKE) verify-logs
	cd central && $(MAKE) verify-logs

# REPORT.md §5c evidence: re-runs the main pipeline at 100/1,000/10,000
# patients and the linkage demo at several identity/site scales (all via the
# existing `data-counts`/`data-counts-central` CLIs, never new business
# logic), then renders the results as SVG charts under assets/. Needs both
# projects' `uv` environments set up (`make setup`) and central's RSA
# keypair provisioned; nothing else. Real numbers only -- see
# scripts/render_charts.py's docstring.
report-charts:
	python3 scripts/render_charts.py

test:
	cd site && $(MAKE) test
	cd central && $(MAKE) test

clean:
	cd site && $(MAKE) clean
	cd central && $(MAKE) clean
	rm -f linkage_ground_truth.json
