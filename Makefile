.PHONY: setup gen-data demo test clean lint

DSR ?= dsr/examples/DC-POC-0001.json

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
