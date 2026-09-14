.PHONY: setup gen-data demo verify-logs tui test clean lint image-build image-run image-save

DRS ?= drs/examples/DC-POC-0001.json
IMAGE ?= data-counts-poc:latest
IMAGE_TARBALL ?= data-counts-poc.tar

# Podman, not Docker, is the reference engine for the leave-behind artifact:
# it is daemonless (no root-owned socket to grant staff access to, which is
# what Req 13's "no contractor remote-access pathway" and Req 12a's attack-
# surface minimization both push against), runs rootless by default, and ships
# in RHEL/Rocky base repos where most Healthcare Partner VMs live. The image
# is OCI-standard, so `make ENGINE=docker image-build` works unchanged for
# sites that have standardized on Docker.
ENGINE ?= podman

setup:
	uv sync --all-extras
	mkdir -p data/lake/raw data/lake/views data/work data/review_queue models outbox
	uv run data-counts setup-models

gen-data:
	uv run data-counts stage1-synthea --population 100000 --out data/synthea_ndjson

demo:
	uv run data-counts run-all --drs $(DRS) --population 1000 --use-container

verify-logs:
	uv run data-counts verify-logs

# Live ops console (Textual) over data/work, data/review_queue, outbox/ --
# read-only, demo aid only. Run alongside `make demo` in another terminal
# to watch the run view update as stages complete.
tui:
	uv run data-counts tui

test:
	uv run pytest -q

lint:
	uv run ruff check src tests

clean:
	rm -rf data/lake data/work data/review_queue outbox/*
	mkdir -p data/lake/raw data/lake/views data/work data/review_queue outbox

# Rootless podman maps the container's uid 1000 to a subuid, *not* to the
# invoking host user, so the bind-mounted dirs below would be unwritable
# without keep-id. Docker has no equivalent flag (its default userns already
# gives the desired mapping), so this is engine-conditional.
ifeq ($(ENGINE),podman)
USERNS_FLAGS ?= --userns=keep-id:uid=1000,gid=1000
# podman defaults to a docker-archive; ask for OCI explicitly so the tarball is
# engine-neutral. `docker save` has no --format flag (it always writes
# docker-archive), hence the conditional.
SAVE_FLAGS ?= --format oci-archive
endif

# Builds an offline-ready image: models are downloaded/exported at build time
# (see Containerfile), so `image-run` needs no network access at all.
# -f is explicit because the two engines disagree on the default filename:
# podman/buildah prefer Containerfile, docker only looks for Dockerfile.
image-build:
	$(ENGINE) build -f Containerfile -t $(IMAGE) .

# The program's public key is the one input stage 6 needs from outside this
# project. In a source checkout it resolves as a sibling path (../central/);
# inside the image nothing above /app exists, so it is mounted read-only and
# pointed at by env var. At a real site this is the honest shape anyway -- the
# program's key arrives as a file the operator installs, not as a checkout.
CENTRAL_PUBLIC_KEY ?= $(CURDIR)/../central/keys/central_public_key.pem

# Bind-mounts data/drs/outbox from the host so a real DRS or bulk-export can
# be substituted for the baked-in demo ones without rebuilding the image.
# `:Z` applies a private SELinux label -- required on the RHEL-family hosts
# podman is native to, and accepted (as a no-op where SELinux is absent) by
# both engines. `:ro` on the key: the pipeline only ever reads it.
image-run:
	$(ENGINE) run --rm $(USERNS_FLAGS) \
		-v "$(CURDIR)/data:/app/data:Z" \
		-v "$(CURDIR)/drs:/app/drs:Z" \
		-v "$(CURDIR)/outbox:/app/outbox:Z" \
		-v "$(CENTRAL_PUBLIC_KEY):/app/keys/central_public_key.pem:ro,Z" \
		-e DATA_COUNTS_CENTRAL_PUBLIC_KEY=/app/keys/central_public_key.pem \
		$(IMAGE) run-all --drs $(DRS)

# For air-gapped distribution: `podman load -i data-counts-poc.tar` on a
# disconnected host reproduces the image with no registry access needed.
image-save: image-build
	$(ENGINE) save $(SAVE_FLAGS) $(IMAGE) -o $(IMAGE_TARBALL)
