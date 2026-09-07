.PHONY: setup link test lint clean

setup:
	uv sync --all-extras
	mkdir -p inbox keys
	uv run data-counts-central setup-keys

link:
	uv run data-counts-central link

test:
	uv run pytest -q

lint:
	uv run ruff check src tests

clean:
	rm -rf inbox/* keys/*
