.PHONY: setup link serve verify-logs test lint clean

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
