.PHONY: setup link serve verify-logs test lint clean image-build serve-container stop-container container-logs

setup:
	uv sync --all-extras
	mkdir -p inbox keys
	uv run data-counts-central setup-keys

link:
	uv run data-counts-central link

serve:
	uv run data-counts-central serve

verify-logs:
	uv run data-counts-central verify-logs

test:
	uv run pytest -q

lint:
	uv run ruff check src tests

clean:
	rm -rf inbox/* keys/* security_audit.cef.log security_audit.cef.log.chainhead

# Podman is the reference engine (same rationale as site/Makefile: daemonless,
# rootless, no root-owned socket). `make ENGINE=docker ...` works unchanged.
ENGINE ?= podman
IMAGE ?= data-counts-central:latest
CONTAINER ?= data-counts-central-api

ifeq ($(ENGINE),podman)
USERNS_FLAGS ?= --userns=keep-id:uid=1000,gid=1000
endif

image-build:
	$(ENGINE) build -f Containerfile -t $(IMAGE) .

# Publishes the Program API on localhost:8001, with inbox/ and keys/
# bind-mounted so uploaded packages and the RSA keypair survive container
# restarts and stay visible to `data-counts-central link`/`verify-logs` run
# directly on the host. -d + --name so `linkage-demo-api` (root Makefile) can
# `podman cp` the demo ground-truth file in after the container is already
# running, and so `container-logs`/`stop-container` have something to target.
serve-container: image-build
	mkdir -p inbox keys
	-$(ENGINE) rm -f $(CONTAINER) >/dev/null 2>&1
	$(ENGINE) run --rm -d --name $(CONTAINER) $(USERNS_FLAGS) \
		-p 127.0.0.1:8001:8001 \
		-v "$(CURDIR)/inbox:/app/inbox:Z" \
		-v "$(CURDIR)/keys:/app/keys:Z" \
		$(IMAGE)
	@echo "Program API starting in container '$(CONTAINER)' -- http://127.0.0.1:8001"
	@echo "  make container-logs   # watch it (heartbeat + per-request access log)"
	@echo "  make stop-container   # tear it down"

container-logs:
	$(ENGINE) logs -f $(CONTAINER)

stop-container:
	-$(ENGINE) stop $(CONTAINER)
