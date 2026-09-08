# RFI 75N95C26R00005 — requirement index

> **SYNTHETIC DATA ONLY. No real PHI is generated, stored, or processed anywhere in this repository.**

Two artifacts answer this RFI, and they are organized on different axes:

- **`COUNTS-RFI-Architecture-DRAFT.docx`** — the response, organized by RFI requirement number, as
  the RFI's Response Format section requires ("Organize the responses according to the numbered
  requests in this RFI").
- **This repository** — a working POC, organized by *pipeline stage* (1 → 9), because that is the
  order data actually moves.

Nothing enforces agreement between those two axes on its own, which is exactly how a write-up and
its demo drift apart. **This file is the join key.** When either side changes, change the row here
first, then the side that needs to follow.

## How to read the Status column

| Status | Meaning |
|---|---|
| `implemented` | Runnable code in this repo demonstrates the requirement end to end, with a test. |
| `partial` | The core of the requirement runs; specific named sub-clauses do not. The gaps are listed in the row and, where they are deliberate, in [Known divergences](#known-divergences). |
| `architecture-only` | Addressed in the write-up as design and/or procedure. Not code, either because it isn't code (BA attestation, training, staffing) or because it is out of scope for a laptop-scale POC (SIEM deployment, TPM sealing). |
| `not-addressed` | Neither implemented nor designed in detail. There are none of these; the column exists so that a future gap has an honest label available. |

`architecture-only` is not a euphemism for missing. Requirements 10 and 14 are contractual and
capacity-planning answers respectively — there is no code that could demonstrate them. Requirement
12's encryption-in-transit clauses are properties of a deployment, not of this pipeline.

## Index

| Req | RFI subject (abbreviated) | Write-up § | Status | POC module(s) | Notes / named gaps |
|---|---|---|---|---|---|
| 1 | Durable leave-behind technology, operable by trained site staff after contract end | 1 | partial | `site/Containerfile`, `site/Makefile`, `site/README.md` | Packaging and the site-controlled key procedure are demonstrated and verified by an actual build+run: podman OCI image, air-gapped `image-save`, no key material baked in (the program's public key is mounted read-only; the site's private key is generated locally and never leaves). Installation guide, operational runbook, major-release signature verification, and the two-day operator course are `architecture-only`. |
| 1a | Deep query for approved extraction from EMR and other authorized source systems | 1a | partial | `site/…/stage1_synthea.py`, `site/…/stage2_ingest.py` | `stage2_ingest` shells out to `smart-fetch` against a live `$export` URL when one is supplied, and otherwise ingests an NDJSON payload of the same shape. SMART Backend Services client-confidential-asymmetric auth, scope-syntax negotiation, `--since` incremental export, and the MRN-list REST fallback are `architecture-only`. |
| 1b | Capture timestamps and provenance at extraction; maintain an immutable provenance chain | 1b | partial | `site/…/audit.py`, `site/…/hash_chain.py`, `site/…/stage9_audit.py` | Per-stage records are written to a hash-chained append-only log; `make verify-logs` recomputes from genesis. Gaps: per-resource `meta.versionId` / `meta.lastUpdated` are not captured, and the chain head is not carried in (or signed into) the delivery manifest. |
| 2 | Machine-readable, version-controlled, authorized Data Request Specification | 2 | partial | `site/…/drs.py`, `site/…/stage3_drs.py`, `site/drs/examples/DC-POC-0001.json` | The DRS drives cohort, fields, date range, and de-id parameters; availability is validated against it and gaps are reported rather than substituted. Gaps: an unavailable required field warns rather than halting (divergence 1 below), plus `specHash`, the `AMBIGUOUS` third availability state, closed-schema validation (`extra="forbid"`), request identity in Parquet file-level metadata, and `specHash + sourceFingerprint` rerun keying. See [Known divergences](#known-divergences). |
| 3 | Initial automated DQ check; all anomalies reported before any correction; source not modified | 3 | implemented | `site/…/stage4_dq_kahn.py`, halt path in `site/…/cli.py` | Completeness, uniqueness, data types, date sequencing, and records-per-patient, framed on Kahn et al. (conformance / completeness / plausibility). Checks report only; `data/lake/` is read-only after stage 2 and stage 5 writes a derived copy. `tests/test_dq_checks.py`. |
| 4 | De-identification per §164.514(b); 18 identifiers; per-patient order-preserving date shift | 4 | implemented | `site/…/stage5_deid.py` | All 18 identifiers enumerated at §164.514(b)(2) removed, with a documented per-identifier disposition; date shift is per-patient, deterministic, and order-preserving. **The output is deliberately not a Safe Harbor claim** — see the module docstring and `REPORT.md` §8. `tests/test_reproducibility.py`. |
| 5 | On-premises PPRL producing consistent, irreversible tokens across independent site deployments | 5 | implemented | `site/…/stage6_pprl.py` (site half), `central/…/crypto.py`, `central/…/linkage.py` | OPPRL v2 `token1`/`2`/`3` + `transcrypt_out` on-site; `transcrypt_in` + Splink linkage centrally. Token consistency across independently keyed sites is measured, not asserted (`REPORT.md` §5). Gaps: 3 of OPPRL v2's 13 token variants; site-key TPM 2.0 sealing and the pre-first-transmission partner validation are `architecture-only`. |
| 6 | Preserve all provenance, permissions, and ontology fields without modification; additive metadata | 6 | partial | `site/…/stage2_ingest.py`, `site/…/viewdef.py`, `site/fhir_views/*.json` | Lossless raw Parquet layer with a round-trip test (`tests/test_roundtrip.py`); derived columns carry the additive `__` prefix; no ontology mapping or crosswalk is performed anywhere. Gaps: FHIR `decimal` is not preserved in lexical form, the code-system-version map is not written to Parquet file metadata, and the `__`-prefix invariant is documented but not programmatically enforced. |
| 7 | Authoritative eligibility, authorization versioning, cohort-vs-permission exclusions, revocation | 7 | architecture-only | — | No consent ledger, eligibility feed, or revocation post-filter in the POC. The write-up's design (site-side ledger, separate exclusion counts, post-filter assertion that fails the run) is the whole of the answer. This is the largest single POC gap. |
| 8 | Full and incremental extracts; stable tokens; snapshot/refresh IDs; 7 record change classes | 8 | partial | `site/…/stage8_package.py` | Snapshot and refresh IDs are generated per run; reruns are idempotent given a stable site key; added and deleted are computed by diffing against the prior snapshot. Gaps: `updated` and `late_arriving` are emitted as empty lists, and `merged` / `unmerged` / `corrected` are not classified at all. See [Known divergences](#known-divergences). |
| 9 | Per-stage audit records; failed reconciliation or incomplete stage halts transmission | 9 | partial | `site/…/audit.py`, `site/…/stage9_audit.py`, halt paths in `site/…/cli.py` | Every stage records timestamp, stage ID, batch ID, record counts, outcome, and reconciliation status; an unresolved DQ anomaly or any leak-gate hit halts before the outbox is written. Gaps: the SQLite control plane and the write-temp → fsync → atomic-rename → commit protocol described in the write-up are not implemented; run state lives in the log alone. |
| 10 | Attest to HIPAA Security Rule compliance as a Business Associate | 10 | architecture-only | — | Contractual, not code. The write-up accepts BA status and direct liability, distinguishes the site-sensor posture (no persistent access, credentials, or remote pathway) from the Program API posture (unambiguously a BA), and raises the Limited Data Set vs. Expert Determination question the PPRL token forces. That question is the reason Req 4 above is not a Safe Harbor claim. |
| 11 | SIEM-compatible security audit logging, protected against modification, continuous | 11 | implemented | `site/…/security_audit.py`, `central/…/security_audit.py`, `site/…/hash_chain.py` | CEF-formatted (Splunk / Sentinel / QRadar / ArcSight parse it natively) covering authentication, administrative actions, configuration changes, and security events, each with timestamp, account, action, resource, and outcome. Hash-chained, with two deliberate-tamper tests. Honest limit: tamper-*evident*, not tamper-*proof* — `site/README.md` §"Req 9 & 11". SIEM forwarding and halt-on-logging-failure are `architecture-only`. |
| 12 | TLS 1.3 (1.2 floor) in transit; application-level output encryption; VM-level encryption | 12 | architecture-only | — | Properties of a deployment, not of this pipeline. The POC makes no outbound TLS call at all: its single egress is a local `outbox/` directory standing in for the transmission step. Application-level encryption of the output package is not implemented. |
| 12a | Attack-surface minimization; the only authorized connection is outbound TLS to the Program | 12a | implemented | whole-repo property; `site/Containerfile`, `site/Makefile` | Structurally satisfied rather than configured: the POC opens **no** listening socket, exposes no port, and runs as a batch process. Podman (daemonless, rootless — see `site/README.md` §Packaging) means operating it requires no root-owned socket either. |
| 13 | Identifiable processing confined to the partner's boundary; only de-identified output leaves | 13 | implemented | `site/` vs. `central/` project split; `site/…/stage7_leak_gate.py` | Enforced by construction: the two projects have separate `pyproject.toml`, lockfiles, and venvs, and share a protocol rather than runtime code, so nothing in `central/` can reach a raw identifier. An independent residual-PII gate (Presidio + Philter + GLiNER) scans the outbound artifact and halts on any hit. No contractor credential or remote-access pathway exists in the deployed artifact. |
| 14 | Performance at baseline, 2X, 5X; assumed volumes, throughput, CPU/memory/storage/staffing | 14 | architecture-only | — | Sizing and staffing estimates, not code. The POC has no benchmark harness; the write-up's tokenizer throughput figure is a measured laptop number and is labelled as such. |
| 15 | Reliably package, transmit, reconcile, and retry deliveries; package and schema versioning | 15 | partial | `site/…/stage8_package.py`, `central/…/ingest.py` | A versioned package (Parquet + manifest with per-file SHA-256 and row counts) is written to `outbox/` and ingested centrally. Gaps: the FastAPI Program API, resumable chunked upload, manifest signing, idempotency keys, the resource-schema lock, and typed error taxonomy are all `architecture-only`. |

Business Information items A–F (capabilities/COTS split, pricing, past performance, business size)
are answered in the write-up only; they have no code counterpart by nature.

## Known divergences

Places where the write-up and the POC deliberately or knowingly disagree. Each is listed so the
disagreement is a recorded decision rather than a discovery.

1. **An unavailable required field warns rather than halting.** The write-up (§2) argues that silent
   substitution is prevented *structurally*: `onUnavailableField` accepts only `halt` or
   `report-and-halt`, so no configuration exists in which the pipeline proceeds past a
   required-field finding. The POC reports the finding but does not act on it — `cli.py` prints
   "availability gaps (no silent substitution)" and records `outcome="ok"`, then continues to stage
   4. `drs.py`'s `FieldSpec.required` is declared, and set in the example DRS, but never read.

   Note these are two different knobs and only one is divergent. `DQCheck.on_fail` (Req 3) is
   `Literal["halt", "warn"]` and *is* honored — a failing `halt` check stops the run before
   de-identification. It is the Req 2 availability path, not the Req 3 DQ path, that lacks a halt.
   Nothing is silently *substituted* in either case, which is the requirement's literal words; the
   write-up's stronger structural claim is the one to implement.
2. **`updated` and `late_arriving` are hardcoded empty.** `stage8_package._diff_against_previous`
   emits them as `[]` rather than computing them. Detecting either requires the per-resource
   `meta.lastUpdated` that Req 1b does not yet capture, so this gap and that one close together.
3. **The manifest is unsigned and carries no chain head.** The write-up (§1b, §15) makes the signed
   manifest the mechanism that ties the site's audit log to what the Program received. The POC
   writes the manifest but neither signs it nor includes the chain head.
4. **Terminology: "DRS".** The RFI uses the unabbreviated phrase "Data Request Specification"
   throughout and never abbreviates it. Both artifacts use **DRS**. The POC previously used "DSR"
   in code, paths, and CLI flags; that was a transposition, and it was corrected to DRS rather than
   propagated into the write-up.
5. **Stage ordering in `REPORT.md` §1's diagram.** The diagram shows de-identification before
   tokenization. The pipeline (and the write-up §4, "removed after tokenization") does the reverse:
   tokenization needs the raw identifiers that de-identification removes.

## Keeping this file honest

`site/…/stage9_audit.py`'s `RFI_MAP` is the runtime version of the Index table above — it is what
`make demo` prints into `data/work/rfi_summary.txt`. `site/tests/test_requirements_sync.py` parses
this file and asserts the two agree on requirement numbers and status, so adding a row here without
updating the code (or the reverse) fails the test suite rather than drifting quietly.

The write-up has no equivalent automated check. When a `Write-up §` reference here stops matching
the document, this file is what makes that visible.
