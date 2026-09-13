.PHONY: setup demo demo-api linkage-demo linkage-demo-api verify-logs report-charts test clean

# Podman is the reference engine everywhere in this repo (see site/Makefile
# and central/Makefile) -- daemonless, rootless, no root-owned socket.
ENGINE ?= podman
CENTRAL_CONTAINER ?= data-counts-central-api

# One-time setup for both projects: installs deps, downloads/exports models
# (site), and generates the central platform's RSA keypair (central).
setup:
	cd site && $(MAKE) setup
	cd central && $(MAKE) setup

# Runs the main leave-behind pipeline (site) end to end. Stage 8 always
# writes to outbox/ (Req 8) -- this never talks to central. See `demo-api`
# for the variant that additionally transmits over the Program API.
demo:
	cd site && $(MAKE) demo

# Same 9-stage pipeline as `demo`, but Stage 8 additionally transmits its
# packaged output (all 5 domain tables) to the central Program API (Req 15)
# running in a podman container, over HTTP with exponential-backoff retries
# -- instead of `outbox/` only being a file the demo leaves for a separate
# step to notice. `demo`'s outbox/ write is unaffected either way.
demo-api:
	cd central && $(MAKE) serve-container
	cd site && uv run data-counts run-all --central-url http://127.0.0.1:8001 --site-name site_a
	@echo "API container '$(CENTRAL_CONTAINER)' left running -- 'curl http://127.0.0.1:8001/drs/DC-POC-0001/status', 'cd central && make container-logs' to watch it, 'cd central && make stop-container' to tear it down."

# Req 5 evidence: two independently-keyed simulated Healthcare Partner sites,
# each tokenizing under its own key, transcrypt to the central platform, and
# a Splink-based cross-site linkage run with precision/recall/F1 against a
# synthetic-demo-only ground truth. See site/src/data_counts/cli.py's
# export-linkage-demo-sites and central/README.md for the full rationale.
# File-drop transport: site writes straight into ../central/inbox/, central
# reads it via its CLI. See `linkage-demo-api` for the HTTP-API equivalent.
linkage-demo:
	cd site && uv run data-counts export-linkage-demo-sites
	cd central && uv run data-counts-central link --ground-truth ../linkage_ground_truth.json

# Same scenario as `linkage-demo`, but transmitted to the central platform
# over its Program API (Req 15) -- running in a podman container, per-request
# HTTP logging + a liveness heartbeat visible via `container-logs` -- instead
# of the file-drop `linkage-demo` uses. Demonstrates the API actually being
# exercised by the site demo, not just present. Exponential-backoff retries
# live in site/src/data_counts/central_client.py, covering both the site
# waiting for the container's uvicorn startup and any transient 5xx/network
# hiccup on every call.
linkage-demo-api:
	cd central && $(MAKE) serve-container
	cd site && uv run data-counts export-linkage-demo-sites \
		--central-url http://127.0.0.1:8001 --request-id linkage-demo
	$(ENGINE) cp linkage_ground_truth.json $(CENTRAL_CONTAINER):/app/ground_truth.json
	cd site && uv run data-counts fetch-linkage-result \
		--central-url http://127.0.0.1:8001 --request-id linkage-demo \
		--ground-truth /app/ground_truth.json
	@echo "API container '$(CENTRAL_CONTAINER)' left running -- 'cd central && make container-logs' to watch it, 'cd central && make stop-container' to tear it down."

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
