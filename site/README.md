# Data COUNTS — Site Project

> **SYNTHETIC DATA ONLY. No real PHI is generated, stored, or processed anywhere in this repository.**

The Healthcare Partner Leave-Behind side of the Data COUNTS POC (see `../README.md` for why this
is a separate project from `../central/`, not a subpackage of it). It extracts, quality-checks,
de-identifies, tokenizes, leak-scans, and packages data **entirely on-site**, emitting only
de-identified + tokenized output plus a provenance/audit trail. Everything that ever touches a
raw identifiable attribute runs here; `../central/` never sees one.

## Guarantees

- **CPU only, no GPU** — nothing in this pipeline (de-id, GLiNER, Presidio) requests a GPU device.
- **No Spark, no JVM** — pure Python + DuckDB end to end, including PPRL tokenization (see the
  Stage 6 note below on why that required a documented deviation from a literal reading of the spec).
- **Deterministic and reproducible** — same input + same DRS ⇒ byte-identical outputs and tokens
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
| 2 | `drs.py`, `stage3_drs.py` | DRS-driven orchestration; flags gaps, never silently substitutes |
| 3 | `stage4_dq_kahn.py`, halt path in `cli.py` | Kahn-framework DQ checks (conformance/completeness/plausibility); halt on unresolved anomaly |
| 4 | `stage5_deid.py` | 18 HIPAA identifiers (45 CFR §164.514(b)(2)) removed; per-patient order-preserving date-shift. Not a Safe Harbor claim — see the module docstring and `../REPORT.md` §8 |
| 5 | `stage6_pprl.py` (site half: tokenize + transcrypt_out); see `../central/` for the resolution + linkage half | OPPRL v2 tokenization (token1/2/3), DuckDB-native |
| 6 | `stage2_ingest.py` (2a/2b), `fhir_views/*.json`, `viewdef.py` | Lossless, round-trippable raw Parquet layer + traceable flat analytic views |
| 8 | `stage8_package.py` | Versioned package, idempotent reruns, added/updated/deleted tracking |
| 9 | `audit.py`, `stage9_audit.py` | Processing audit trail (per-stage record counts/outcome/reconciliation) + PII-free, k-anonymized (k=11) QA manifest |
| 11 | `stage9_audit.py` (model versions), `security_audit.py`, `hash_chain.py` | Model versions/hashes + DRS version logged; hash-chained, CEF-formatted security audit log (auth/admin/config-change/security events) protected against undetected tampering -- `make verify-logs` |
| 13 | `stage7_leak_gate.py` | Independent residual-PII QA gate (Presidio + Philter + GLiNER); halts on any hit |
| 15 | `stage8_package.py` | Refresh-cycle diffing across snapshots |

Run `make demo` to see a human-readable version of this table (`data/work/rfi_summary.txt`),
generated from the actual run.

This table covers what this project implements. For the full picture — including the
architecture-only requirements (7, 10, 12, 14) that have no code counterpart, a status per
requirement, and the specific gap behind each `partial` — see **[`../REQUIREMENTS.md`](../REQUIREMENTS.md)**,
which is the authoritative index. `tests/test_requirements_sync.py` asserts that it agrees with
`stage9_audit.RFI_MAP`, so the index and the demo output can't drift apart.

## Repo layout

```
src/data_counts/   stage1_synthea.py … stage9_audit.py, drs.py, viewdef.py, audit.py, cli.py
drs/examples/      example Data Request Specification
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
  `stage6_pprl.py` reimplements the OPPRL v2 `token1`/`token2`/`token3` transforms in pure Python
  (`hashlib`/`cryptography`/`jellyfish`), mirroring spindle-token's own `_crypto.py` and
  `opprl/v0.py` source line-for-line. It does reuse spindle-token's `generate_pem_keys()` directly,
  since that helper has no pyspark dependency. **Caveat:** this sandbox has no Java/JVM, so real
  spindle-token's Spark path could not be run here to produce byte-exact reference vectors — see
  the docstring in `stage6_pprl.py` for the exact recipe to do that validation wherever Spark+Java
  is available. `token2`/`token3` (soundex/metaphone name variants, tolerant of spelling noise)
  were added specifically to give `../central/`'s Splink-based linkage step more than one binary
  signal to combine — see `../central/README.md`.
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

## Stage 8: a real de-identification gap, found and fixed

Every outbound table originally carried a raw internal resource `id` (and a duplicate
`__source_resource_id` provenance column) alongside the patient's token — for `patient.parquet`
specifically, that `id` was **the exact un-tokenized value the token was supposed to replace**,
sitting in the next column over. Worse, `stage1_synthea.py`'s synthetic ID scheme embedded the raw
patient ID as a literal substring in every child resource's id (`synthetic-patient-00001-cond-...`),
so it also leaked through reference fields like `observation.encounter_id`. Found by inspecting
actual packaged output (not assumed), fixed at the root — resource IDs are now opaque
122-bit random values with no relationship to any patient identifier — and defensively in
`stage8_package.py`, which now excludes `id`/`__source_resource_id` from every outbound table
regardless of what a given ID scheme happens to contain.

## Req 9 & 11: tamper-evident, not tamper-proof — said plainly

Both logs (`data/work/audit_log.jsonl` for Req 9's processing trail, `data/work/security_audit.cef.log`
for Req 11's security events) are hash-chained (`hash_chain.py`): each entry's hash covers its own
content plus the previous entry's hash, so editing, deleting, reordering, or truncating any entry
breaks the chain from that point forward. `make verify-logs` recomputes the chain from genesis and
reports the first break, if any — demonstrated end to end (including two deliberate-tamper tests)
in `tests/test_tamper_evidence.py`.

This is **tamper-evident**, not what a strict reading of "tamper-proof" would require: a local file
is not write-once storage, and someone with filesystem access to *both* the log and its small
`.chainhead` sidecar file could in principle recompute the entire chain to hide an edit. The sidecar
matters precisely because it raises the bar — an attacker has to get two files consistent, not one —
but it's not cryptographic proof against a fully capable local attacker. Real write-once guarantees
would need external anchoring (e.g., forwarding to a WORM-configured SIEM or periodically publishing
chain heads somewhere the site can't retroactively edit) that a laptop-scale POC doesn't have.

The security log is CEF (Common Event Format) — the format Splunk, Sentinel, QRadar, and ArcSight
all parse natively — so a Healthcare Partner's existing SIEM can ingest it directly (file-tail
forwarder, no bespoke connector) rather than this pipeline needing to know which SIEM a given site
runs. The hash-chain fields ride along as two extra CEF custom extensions (`cs2`/`cs3`); a SIEM that
doesn't know what they mean just indexes them as ordinary searchable fields.

## Stage 1: Synthea

No Java/JVM is available in every deployment target, so the default cohort generator
(`stage1_synthea.py`) is a deterministic, seeded, pure-Python FHIR R4 generator that emits the same
per-resource-type NDJSON shape Synthea's own bulk-export produces. Pass `--use-container` to instead
run the real MITRE Synthea image — the downstream pipeline is identical either way. That path
resolves `podman` first and falls back to `docker` (`CONTAINER_ENGINES`), matching the packaging
choice below.

## Packaging: podman is the reference engine

`make image-build` / `image-run` / `image-save` build and run the leave-behind artifact from
`Containerfile`. **Podman is the default engine**, for three reasons that map onto the RFI rather
than onto taste:

- **Daemonless.** There is no root-owned socket, so letting trained site staff operate the pipeline
  (Req 1) does not mean granting them a root-equivalent capability. Under Docker, membership in the
  `docker` group is effectively root on the host.
- **Rootless by default.** The container runs as an unprivileged user in a user namespace, which
  keeps Req 12a's attack-surface minimization and Req 13's "identifiable processing confined to the
  Healthcare Partner's VM, no contractor remote-management pathway" honest — there is no long-lived
  privileged service listening for anyone at all, contractor or otherwise.
- **Already present.** Podman ships in the RHEL/Rocky/Alma base repositories that most Healthcare
  Partner VMs run, so the artifact installs without adding a third-party package repository.

Nothing in the image is podman-specific: `Containerfile` is plain Dockerfile syntax with no
BuildKit-only directives, and `make ENGINE=docker image-build` produces the same image. Two flags
in the Makefile *are* engine-conditional and are commented as such — `--userns=keep-id` (rootless
podman maps container uid 1000 to a subuid, not to the invoking user, so bind mounts would otherwise
be unwritable) and `save --format oci-archive` (podman defaults to a docker-archive; `docker save`
has no `--format` flag at all).

For air-gapped sites, `make image-save` writes an OCI archive that either engine can `load`. Pin the
`FROM` lines by digest before shipping for real — the Containerfile notes where — so rebuilds are
byte-identical.

**No key material is baked into the image.** Stage 6 needs exactly one input from outside this
project: the *program's* RSA public key, which it encrypts site tokens under. In a source checkout
that resolves as a sibling path (`../central/keys/`); inside the image nothing above `/app` exists,
so `image-run` mounts it read-only at `/app/keys/` and points `DATA_COUNTS_CENTRAL_PUBLIC_KEY` at it.
Override the host path with `make CENTRAL_PUBLIC_KEY=/path/to/key.pem image-run`. This is also the
realistic shape of it: at a real site the program's public key arrives as a file the operator
installs, not as a sibling checkout. The *site's* own private key is generated locally and never
leaves — nothing in the packaging path transmits or embeds it.
