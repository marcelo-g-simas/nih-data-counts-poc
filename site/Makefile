.PHONY: setup gen-data demo test clean lint docker-build docker-run docker-save

DSR ?= dsr/examples/DC-POC-0001.json
IMAGE ?= data-counts-poc:latest
IMAGE_TARBALL ?= data-counts-poc.tar

setup:
	uv sync --all-extras
	mkdir -p data/lake/raw data/lake/views data/work data/review_queue models outbox
	uv run data-counts setup-models

gen-data:
	uv run data-counts stage1-synthea --out data/synthea_ndjson

demo:
	uv run data-counts run-all --dsr $(DSR)

test:
	uv run pytest -q

lint:
	uv run ruff check src tests

clean:
	rm -rf data/lake data/work data/review_queue outbox/*
	mkdir -p data/lake/raw data/lake/views data/work data/review_queue outbox

# Builds an offline-ready image: models are downloaded/exported at build time
# (see Dockerfile), so `docker-run` needs no network access at all.
docker-build:
	docker build -t $(IMAGE) .

# Bind-mounts data/dsr/outbox from the host so a real DSR or bulk-export can
# be substituted for the baked-in demo ones without rebuilding the image.
docker-run:
	docker run --rm \
		-v "$(CURDIR)/data:/app/data" \
		-v "$(CURDIR)/dsr:/app/dsr" \
		-v "$(CURDIR)/outbox:/app/outbox" \
		$(IMAGE) run-all --dsr $(DSR)

# For air-gapped distribution: `docker load < data-counts-poc.tar` on a
# disconnected host reproduces the image with no registry access needed.
docker-save: docker-build
	docker save $(IMAGE) -o $(IMAGE_TARBALL)
