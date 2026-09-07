# Claude Code build prompt — Data COUNTS "Leave-Behind" POC

> Paste everything below into Claude Code. It is written as instructions to the coding agent.

---

## Role and objective

You are building a **proof-of-concept** that demonstrates a *Healthcare Partner Leave-Behind* data pipeline in support of a response to NIH RFI **75N95C26R00005 (Data COUNTS™)**. The POC must show that identifiable data can be extracted, quality-checked, de-identified, tokenized, leak-scanned, and packaged **entirely on-site**, emitting only de-identified + tokenized output plus a provenance/audit trail.

This is a capability demonstration on **synthetic data only** — never real PHI. Optimize for a reviewer being able to run one command and see each RFI requirement exercised.

## Hard constraints (do not violate)

- **Python 3.11+, pure CPU. No GPU anywhere** — not for de-id, not for the PII models. If a library defaults to GPU, force CPU.
- **No Spark, no JVM.** The pipeline is pure Python + DuckDB end to end, including PPRL tokenization (see stage 6).
- Runs locally on a laptop or a single VM via one CLI (`typer` or `click`) plus a `Makefile`.
- **Use `uv` for all environment and dependency management** — `uv venv`, `uv sync` / `uv pip install`, and `uv run` for the CLI. `pyproject.toml` is the single source of dependencies; commit a `uv.lock` so installs, and therefore runs, are reproducible.
- **Deterministic and reproducible**: same input + same DSR ⇒ byte-identical outputs and identical tokens. Pin every model version and every random seed; record them in the audit trail.
- **Source data is never modified.** Quality checks *report*; they never mutate source. Any correction happens on a derived copy and is logged.
- **No run-time network calls** except a single simulated "outbound to program" step that writes to a local `outbox/` directory (this models the RFI's single authorized egress path). Models are downloaded once in a separate `setup` step, then the pipeline runs fully offline.
- **Verify each third-party tool's real API from its repo/README before writing code against it.** Several of these are niche and your training data may be stale — check actual method signatures, install extras, and I/O formats first.

## Pipeline stages

Implement as discrete, independently runnable stages. In the README, map each stage to the RFI requirement number(s) it demonstrates (shown in brackets).

1. **Synthetic EHR generation + bulk-export endpoint** — Use **Synthea** (MITRE `synthetic-patient-generator`) to produce a modest FHIR R4 cohort (~1,000 patients). To exercise the RFI's extraction-from-EMR path (Req 1a), make the cohort available behind a **FHIR Bulk Data Access `$export` endpoint**: load the Synthea output into a lightweight test FHIR server (local HAPI FHIR, or the SMART bulk-data reference server), or — simpler — drop Synthea's NDJSON into a folder and treat it as a pre-exported dataset. The downstream ingest is identical either way. [Req 1a]

2. **Ingest → Parquet — two layers: raw Arrow landing + analytic views** — Pull the cohort from the bulk-export endpoint and land it as Parquet in two layers under `data/lake/`, both still identifiable (no de-id here — DQ runs on source at stage 4, de-id at stage 5).

   **2a — Bulk export + raw Arrow/Parquet landing (Cumulus ecosystem, de-id OFF).** Pull the cohort from the `$export` endpoint with **`smart-fetch`** (smart-on-fhir's standalone bulk-export client) into NDJSON — the "deep query / approved extraction" step (Req 1a). Land it as **nested Parquet via `cumulus-fhir-support`**: `pyarrow_schema_from_rows()` derives a PyArrow schema wide and deep enough to cover every field present (down to `extension.extension.valueCoding.system`), so identifiers, extensions, and every CodeableConcept's `system`/`code`/`display` are preserved intact and unmapped. **Do NOT run the `cumulus-etl` ETL command** — its purpose is de-identified/aggregated output (MS Anonymizer + cTAKES NLP); running it would put de-id ahead of the source-data DQ check and duplicate work that OPPRL and stages 5–7 own. You use Cumulus's *export* and *Arrow* libraries only; de-id and tokenization are yours. All of this is pure Python — no Spark, no JVM.

   **Required — faithful, round-trippable nested schema.** The raw layer MUST preserve the full nested FHIR structure and round-trip losslessly (FHIR → Parquet → FHIR with no loss). Use `cumulus-fhir-support` to derive the Arrow schema, and conform the write to the **`aehrc/parquet-on-fhir`** spec (CSIRO, CC0) so the encoding is formally lossless — choice types as `value[x]` fields, primitive extensions as `_`-prefixed groups, repeats as `LIST`, and the additive `__`-annotation namespace that maps almost verbatim onto Req 6 ("without modification or loss; additive, clearly identified; traceable to source"). Where `cumulus-fhir-support`'s derived schema and the spec differ, parquet-on-fhir conformance wins — the round-trip guarantee is the requirement, not a convenience. Prove it with a round-trip test (see acceptance criteria). [Req 1a, 6]

   **2b — Analytic layer via SQL-on-FHIR ViewDefinitions in DuckDB.** From the lossless layer, project flat, task-specific tables with **HL7 SQL-on-FHIR v2 ViewDefinitions**, compiled to DuckDB SQL via **FlatQuack** (DuckDB reads the nested Parquet directly, so this stays pure-DuckDB / no-Spark). One ViewDefinition per resource type → flat tables that stages 3 (cohort), 4 (DQ), and 6 (tokenization) consume. These views are projections (lossy by design — that's fine, the raw layer holds the full record). Preserve `coding.system`/`code`/`display` and carry the source resource `id` + a provenance link on every row so each projected value traces back to its lossless record. Example (Observation, codings preserved via `forEach`):

   ```json
   {
     "resourceType": "ViewDefinition",
     "resource": "Observation",
     "select": [
       { "column": [
         { "name": "id", "path": "getResourceKey()" },
         { "name": "patient_id", "path": "subject.getReferenceKey()" },
         { "name": "effective_dt", "path": "effectiveDateTime" }
       ] },
       { "forEach": "code.coding", "column": [
         { "name": "code_system", "path": "system" },
         { "name": "code", "path": "code" },
         { "name": "code_display", "path": "display" }
       ] }
     ]
   }
   ```

   [Req 6]

3. **DSR-driven orchestration** — Load a **JSON-backed Data Request Specification (DSR)** (schema below) that defines cohort logic, requested source systems / domains / fields, date range, full-vs-incremental mode, refresh cadence, authorization/permission version, **and an embedded list of DQ checks to run locally on the partner site**. Validate data availability against the DSR: flag unavailable or ambiguous fields, make **no silent substitutions**, and stamp the `request_id` onto every batch and output. Support reproducible reruns when source is unchanged. [Req 2]

4. **Data-quality checks — Kahn framework via DuckDB** — Implement checks organized by the **Kahn et al. harmonized DQ framework**: *conformance*, *completeness*, *plausibility* (each verification vs validation). Concretely cover the RFI's named checks — **completeness, uniqueness, data types, date sequencing, records-per-patient**. Run them as **SQL over the Parquet lake with DuckDB** (DuckDB reads Parquet directly). **Report all anomalies before any permitted correction is applied; never modify source.** Emit a structured anomaly report. [Req 3]

5. **De-identification** — Remove all **18 HIPAA direct identifiers per 45 CFR §164.514(b)**; apply a **consistent per-patient date-shifting** spec that preserves temporal order; document the pathway. [Req 4]

6. **PPRL tokenization — OPPRL, DuckDB-native on both ends** — Produce deterministic per-subject **OPPRL** tokens (the `OpprlV2` scheme) with the tokenization and `transcrypt` logic running **natively in DuckDB via a Python scalar UDF** — no Spark, no JVM. Use a **site-local secret RSA key** and exercise the full two-ended flow: the **partner site** tokenizes and `transcrypt_out`s to ephemeral tokens; the **program end** `transcrypt_in`s and links. Both ends call the *same* UDF module, so token consistency across the exchange is guaranteed by construction — this is the RFI's "consistent tokens across independently deployed, site-local instances without a shared trust boundary" point (Req 5).

   **Get the UDF without hand-rolling crypto (preferred):** inspect `spindle-token`'s source (`src/spindle_token/`) for the **per-value OPPRL transform primitives** (attribute normalization → one-way hash → AES key derivation/encrypt). The Spark path is a distribution wrapper around these row-level functions; if they import and run **without constructing a `SparkSession`**, wrap them directly in a DuckDB Python UDF (`con.create_function(...)`). That yields canonical, guaranteed-interoperable tokens with zero Spark.

   **Fallback (only if those primitives are too Spark-coupled to reuse):** implement the transforms in pure Python from the published `PROTOCOL.md` using `hashlib`/`cryptography`, and **validate byte-for-byte against `spindle-token`'s test vectors (`tests/`)**. This keeps the tokens OPPRL-conformant and interoperable with real spindle-token deployments — not merely self-consistent within the POC, which is the weaker guarantee you'd get from an unvalidated reimplementation. [Req 5]

7. **Residual PII-leakage gate** — Independently scan the **de-identified + tokenized output** with **Presidio + Philter + GLiNER**. This is a **QA gate, not the de-id engine** — it must use different detectors than stage 5 and be tuned for **recall over precision**. Any residual hit **HALTS transmission** and routes the batch to a review queue. Requirements for the models: **GLiNER via an ONNX/INT8 PII checkpoint on CPU** (e.g. a `gliner-pii` or `gliner-biomed` model; Presidio can wrap GLiNER as its NER engine); **Philter** runs self-contained/offline; **Presidio** analyzer reports spans with offsets without modifying text. **Encoder NER only — do not use vLLM or any generative-LLM serving stack.** [Req 3 halt-on-anomaly, 9, 13]

8. **Package + "transmit"** — Write a **versioned output package** (Parquet + a JSON manifest) to `outbox/`, representing the single outbound path to the program. Include schema version, `request_id`, snapshot ID, and refresh ID; support **idempotent reruns**; identify added/updated/deleted/late-arriving records across refresh cycles. [Req 8, 15]

9. **Provenance & audit trail** — Every stage emits an audit record: `timestamp, stage_id, batch_id, record_counts, outcome, reconciliation_status`. Also emit a **PII-free QA manifest** (booleans and *bucketed* counts only; suppress any cell below a k-threshold, e.g. <11). Failed reconciliation or an unresolved leak **halts** the pipeline. Log model versions/hashes and the DSR version so runs are auditable and reproducible. [Req 1b, 9, 11]

## JSON DSR schema

Ship at least one concrete example DSR under `dsr/examples/`. Use this shape:

```json
{
  "request_id": "DC-POC-0001",
  "version": "1.0.0",
  "authorization": { "permission_version": "2026-09-01", "irb_ref": "SYNTHETIC" },
  "cohort": {
    "inclusion": [{ "domain": "condition", "code_system": "ICD-10", "codes": ["E11%"] }],
    "exclusion": []
  },
  "source_systems": ["emr"],
  "domains": ["patient", "condition", "observation", "medication"],
  "fields": [
    { "domain": "observation", "name": "value_quantity", "required": true },
    { "domain": "observation", "name": "loinc_code", "required": true }
  ],
  "date_range": { "start": "2015-01-01", "end": "2025-12-31" },
  "extraction_mode": "full",
  "refresh_cadence": "monthly",
  "deidentification": {
    "date_shift": { "per_patient": true, "max_days": 365, "preserve_order": true }
  },
  "checks": [
    { "id": "comp_dob", "kahn_category": "completeness", "target": "patient.birth_date", "type": "completeness", "on_fail": "warn" },
    { "id": "uniq_patient", "kahn_category": "conformance", "target": "patient.id", "type": "uniqueness", "on_fail": "halt" },
    { "id": "dtype_value", "kahn_category": "conformance", "target": "observation.value_quantity", "type": "data_type", "params": { "dtype": "double" }, "on_fail": "halt" },
    { "id": "dateseq_enc", "kahn_category": "plausibility", "target": "encounter", "type": "date_sequence", "params": { "start": "period_start", "end": "period_end" }, "on_fail": "halt" },
    { "id": "rpp_obs", "kahn_category": "plausibility", "target": "observation", "type": "records_per_patient", "params": { "min": 1, "max": 5000 }, "on_fail": "warn" }
  ]
}
```

The `checks[]` array is the "checks to run locally on the partner site." The DQ stage reads it, runs the corresponding DuckDB SQL, and honors `on_fail` (`halt` | `warn`).

## Repo layout

Propose and create a clean layout, roughly:

```
data-counts-poc/
  src/data_counts/
    stage1_synthea.py      stage2_ingest.py       stage3_dsr.py
    stage4_dq_kahn.py      stage5_deid.py         stage6_pprl.py
    stage7_leak_gate.py    stage8_package.py      stage9_audit.py
    dsr.py  (schema + validation)   cli.py
  dsr/examples/
  fhir_views/      # SQL-on-FHIR ViewDefinition JSON, one per resource type
  data/            # gitignored: lake/ (raw/ lossless + views/ analytic), work/, outbox/, review_queue/
  models/          # gitignored: downloaded model artifacts
  tests/
  Makefile  README.md  pyproject.toml  uv.lock
```

CLI subcommands per stage plus `run-all` (invoked via `uv run`). Makefile targets: `setup` (`uv sync` + one-time model download), `gen-data` (Synthea), `demo` (`run-all` end to end), `test`.

## Deliverables / acceptance criteria

- `make setup` uses **`uv`** to create the environment and install deps (from `pyproject.toml` + `uv.lock`), and downloads models once (online); afterwards the pipeline runs **fully offline on CPU**.
- `make demo` runs the whole pipeline on a Synthea cohort and produces: the raw Parquet lake, a DQ anomaly report, a de-identified + tokenized package in `outbox/`, the audit log, the PII-free QA manifest, and a short human-readable summary listing which RFI requirements were exercised.
- A **deliberately injected PII leak** in one clinical note demonstrates the leak gate halting the run and routing to `review_queue/`.
- **Both-ends linkage demo:** generate two simulated partner sites that share a set of overlapping patients, tokenize each site under its own site key, `transcrypt` both to the program end, and show the overlapping patients link there while non-overlapping ones don't — the concrete Req 5 evidence, produced with the same DuckDB UDF on both ends.
- Unit tests for the DQ checks and the leak gate; a **reproducibility test** proving a rerun yields identical tokens and outputs.
- **Round-trip test:** a sample of exported FHIR resources encoded to the raw Parquet layer and reconstructed back to FHIR compares equal — proving the faithful, lossless nested representation (Req 6).
- README maps every module to its RFI requirement number and states the no-GPU / offline / source-immutability guarantees.

## Notes / gotchas to respect

- Prefer small, pinned, self-hostable models; record model name + version/hash in the audit trail.
- **OPPRL / `spindle-token`:** use the `OpprlV2` scheme; deterministic tokens; per-party RSA keys; `transcrypt_out`/`transcrypt_in` for sharing (`transcode_*` are deprecated aliases). Reuse spindle-token's row-level transform primitives inside a DuckDB UDF if they import without Spark; otherwise reimplement from `PROTOCOL.md` and validate against its `tests/` vectors. Same UDF runs on both ends. No Spark, no JVM anywhere in the pipeline.
- **Ingest (Cumulus libraries, de-id OFF):** bulk export via `smart-fetch`; land nested Parquet via `cumulus-fhir-support` (`pyarrow_schema_from_rows`) — pure Python, no Spark, no JVM. Do **not** run the `cumulus-etl` ETL command — that's the de-id/aggregate path. The raw layer MUST be a faithful, round-trippable nested schema: derive the Arrow schema with `cumulus-fhir-support`, conform the encoding to `aehrc/parquet-on-fhir`, and validate round-trip. Analytic views via SQL-on-FHIR ViewDefinitions + FlatQuack on DuckDB. `cumulus-library-data-metrics` is a useful reference for DQ metrics.
- Keep the leak gate's detectors independent from the de-id transform — the whole point is to catch what stage 5 missed.
- Everything is synthetic; add a top-of-README banner stating no real PHI is used.
