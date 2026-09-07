# Data COUNTS — Leave-Behind Pipeline POC

> **SYNTHETIC DATA ONLY. No real PHI is generated, stored, or processed anywhere in this repository.**

A proof-of-concept in support of a response to NIH RFI **75N95C26R00005 (Data COUNTS™)**. It
demonstrates a *Healthcare Partner Leave-Behind* pipeline that extracts, quality-checks,
de-identifies, tokenizes, leak-scans, and packages data **entirely on-site**, emitting only
de-identified + tokenized output plus a provenance/audit trail.

## Guarantees

- **CPU only, no GPU** — nothing in this pipeline (de-id, GLiNER, Presidio) requests a GPU device.
- **No Spark, no JVM** — pure Python + DuckDB end to end, including PPRL tokenization (see the
  Stage 6 note below on why that required a documented deviation from a literal reading of the spec).
- **Deterministic and reproducible** — same input + same DSR ⇒ byte-identical outputs and tokens
  (see `tests/test_reproducibility.py`). Model versions and seeds are pinned and logged.
- **Source is never modified** — DQ checks (stage 4) report only; de-id (stage 5) writes to
  `data/work/deid/`, a derived copy. `data/lake/` is read-only after stage 2.
- **Single egress path** — the only place this pipeline writes outside `data/` is `outbox/`,
  modeling the RFI's single authorized outbound-to-program step. No other run-time network calls
  occur; models are downloaded once via `make setup`, after which `make demo` runs fully offline.

## Quickstart

```bash
make setup   # uv sync + one-time model download (online)
make gen-data
make demo    # run-all, fully offline afterwards
make test
```

## RFI requirement map

| Req | Module(s) | What it demonstrates |
|---|---|---|
| 1a | `stage1_synthea.py`, `stage2_ingest.py` (2a) | Extraction from an EMR-shaped source via a FHIR Bulk Data `$export`-style NDJSON payload, pulled with `smart-fetch` |
| 1b | `audit.py` | Per-stage provenance/audit record on every batch |
| 2 | `dsr.py`, `stage3_dsr.py` | DSR-driven orchestration; flags gaps, never silently substitutes |
| 3 | `stage4_dq_kahn.py`, halt path in `cli.py` | Kahn-framework DQ checks (conformance/completeness/plausibility); halt on unresolved anomaly |
| 4 | `stage5_deid.py` | 18 HIPAA identifiers (45 CFR §164.514(b)) removed; per-patient order-preserving date-shift |
| 5 | `stage6_pprl.py` | OPPRL v2 tokenization, DuckDB-native both ends, RSA-based transcrypt linkage |
| 6 | `stage2_ingest.py` (2a/2b), `fhir_views/*.json`, `viewdef.py` | Lossless, round-trippable raw Parquet layer + traceable flat analytic views |
| 8 | `stage8_package.py` | Versioned package, idempotent reruns, added/updated/deleted tracking |
| 9 | `audit.py`, `stage9_audit.py` | Audit trail + PII-free, k-anonymized (k=11) QA manifest |
| 11 | `stage9_audit.py` | Model versions/hashes + DSR version logged |
| 13 | `stage7_leak_gate.py` | Independent residual-PII QA gate (Presidio + Philter + GLiNER); halts on any hit |
| 15 | `stage8_package.py` | Refresh-cycle diffing across snapshots |

Run `make demo` to see a human-readable version of this table (`data/work/rfi_summary.txt`),
generated from the actual run.

## Repo layout

```
src/data_counts/   stage1_synthea.py … stage9_audit.py, dsr.py, viewdef.py, audit.py, cli.py
dsr/examples/      example Data Request Specification
fhir_views/        SQL-on-FHIR ViewDefinition JSON, one per resource type
data/              gitignored: lake/ (raw/ lossless + views/ analytic), work/, review_queue/
models/            gitignored: downloaded model artifacts
outbox/            gitignored: versioned output packages (the single egress path)
tests/
```

## Deviations from a literal reading of the spec (verified against upstream source, not assumed)

Three of the named third-party integrations turned out to need a different shape than the spec's
"preferred path," discovered by inspecting the actual installed packages rather than trusting
possibly-stale training data (as the spec itself warns to do):

- **`smart-fetch`** is a CLI tool, not an importable Python library. `stage2_ingest.py` shells out
  to it (`subprocess`) when a live `$export` URL is given, and otherwise treats stage 1's NDJSON
  folder as the already-exported payload — the spec explicitly allows this simpler path.
- **`spindle-token`** (2.4.2, the current OPPRL implementation) confirms at import time that its
  tokenize/transcrypt API is Spark-only (`spindle_token.opprl.*` raises `ImportError` unless the
  `pyspark`-requiring `spark` extra is installed). Since "no Spark, no JVM" is a hard constraint,
  `stage6_pprl.py` reimplements the OPPRL v2 "token1" transform in pure Python
  (`hashlib`/`cryptography`), mirroring spindle-token's own `_crypto.py` and `opprl/v0.py` source
  line-for-line. It does reuse spindle-token's `generate_pem_keys()` directly, since that helper
  has no pyspark dependency. **Caveat:** this sandbox has no Java/JVM, so real spindle-token's
  Spark path could not be run here to produce byte-exact reference vectors — see the docstring in
  `stage6_pprl.py` for the exact recipe to do that validation wherever Spark+Java is available.
- **FlatQuack** (the SQL-on-FHIR ViewDefinition → DuckDB compiler) has no published PyPI package,
  only a source checkout. `viewdef.py` evaluates the small subset of the ViewDefinition grammar
  our `fhir_views/*.json` use directly in Python, then loads the flat rows into DuckDB — so all
  downstream querying is still pure DuckDB SQL. Swapping in real FlatQuack later only requires
  replacing `evaluate_view()`; the ViewDefinition JSON files are unchanged.

Everything else (`cumulus-fhir-support`, `aehrc/parquet-on-fhir` conformance, Presidio, Philter)
matched the spec as written. GLiNER didn't quite — see below.

## Stage 7: GLiNER via ONNX Runtime

`gliner`'s own ONNX support (`export_to_onnx()` / `from_pretrained(..., load_onnx_model=True)`)
does **not** drop PyTorch from the install: verified directly against the installed source, its
`model.py`, `onnx/model.py`, and `decoding/decoder.py` all `import torch` unconditionally for
tokenization/collation/decoding glue, even when the encoder forward pass itself runs through ONNX
Runtime. Torch stays a dependency either way (used for the one-time export in `setup-models`); what
ONNX buys here is real inference speed (~1.7x faster on our test text, matching PyTorch's output
bit-for-bit) at the same accuracy, not a smaller footprint.

We export the **fp32** ONNX model, not the INT8-quantized one `export_to_onnx(quantize=True)` also
produces. In our own side-by-side comparison the quantized model missed a date-of-birth entity that
both the PyTorch and fp32-ONNX models caught, and shifted other entity boundaries — a bad trade for
a detector whose whole design brief is recall over precision. `make setup` exports both; only the
fp32 one is loaded by default (`stage7_leak_gate.GLINER_ONNX_FILE`).

## Stage 1: Synthea

No Java/JVM is available in every deployment target, so the default cohort generator
(`stage1_synthea.py`) is a deterministic, seeded, pure-Python FHIR R4 generator that emits the same
per-resource-type NDJSON shape Synthea's own bulk-export produces. Pass `--use-docker` to instead
run the real MITRE Synthea Docker image — the downstream pipeline is identical either way.
