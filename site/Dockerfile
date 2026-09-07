# syntax=docker/dockerfile:1
#
# Multi-stage build for the Data COUNTS leave-behind pipeline POC.
#
# Stage "builder" runs `uv sync` and the online model-download/export step
# (the same thing `make setup` does on a bare host) once, at build time --
# so the final image is offline-ready from `docker run` onward with no
# separate setup step, which is a better fit for a "leave-behind" artifact
# than requiring a live run first. Pin FROM by digest before shipping this
# for real (see README's Packaging section) for byte-identical rebuilds.
FROM python:3.11-slim AS builder

# libgomp1: OpenMP runtime torch/onnxruntime's CPU kernels link against;
# not always present on slim base images.
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.10.2 /uv /uvx /usr/local/bin/

WORKDIR /app

# Dependency layer first, so editing source doesn't invalidate the (large,
# slow-to-resolve) dependency install layer.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

# `stage2_ingest.py`'s FHIR_VIEWS_DIR is resolved relative to its own
# checkout location (parent.parent.parent of the source file) -- so the
# image needs the full repo layout, not just an installed wheel. `uv sync`
# does an editable install pointing back at ./src either way.
COPY . .
RUN uv sync --frozen

# Bake the models in: same as `data-counts setup-models`, but at build time
# so the shipped image needs no run-time network access at all (beyond the
# pipeline's own single simulated outbound-to-program step). NLTK_DATA is
# pinned to a location nltk already checks by default regardless of $HOME --
# without it, nltk.download() lands under *this build step's* $HOME (root's,
# mid-build), which the non-root runtime user in the next stage can't see.
# The directory must exist *before* nltk.download() runs: nltk only picks a
# download destination from among the paths that already exist and are
# writable, so setting NLTK_DATA alone (without creating it) is a no-op.
ENV NLTK_DATA=/usr/local/share/nltk_data
RUN mkdir -p "$NLTK_DATA" && uv run data-counts setup-models

# ---------------------------------------------------------------------------
FROM python:3.11-slim AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 datacounts

WORKDIR /app
COPY --from=builder --chown=datacounts:datacounts /app /app

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1

# data/, outbox/ hold run state and output; dsr/ can be overridden with a
# real partner-site DSR. None of this should be baked into the image.
# Created and chowned as root, before dropping privileges -- the datacounts
# user doesn't have write access to create them itself under /app.
RUN mkdir -p data/lake/raw data/lake/views data/work data/review_queue outbox \
    && chown -R datacounts:datacounts data outbox
VOLUME ["/app/data", "/app/outbox", "/app/dsr"]

USER datacounts

ENTRYPOINT ["data-counts"]
CMD ["run-all"]
