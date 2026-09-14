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
capacity-planning answers respectively — no stage in this repo treats either as something `run-all`
exercises, so neither appears in `RFI_MAP` below. Req 14 is the more nuanced of the two, though: its
throughput/memory/storage figures are measured from this POC's own code (`REPORT.md` §10) — real
numbers, at a real health-system scale, from an actual laptop-scale run — even though the sharding
they recommend for production volume isn't implemented; see its row below. Requirement 12's
encryption-in-transit clauses are properties of a deployment, not of this pipeline.

## Index

| Req | RFI subject (abbreviated) | Write-up § | Status | POC module(s) | Notes / named gaps |
|---|---|---|---|---|---|
| 1 | Durable leave-behind technology, operable by trained site staff after contract end | 1 | partial | `site/Containerfile`, `site/Makefile`, `site/README.md` | Packaging and the site-controlled key procedure are demonstrated and verified by an actual build+run: podman OCI image, air-gapped `image-save`, no key material baked in (the program's public key is mounted read-only; the site's private key is generated locally and never leaves). Installation guide, operational runbook, major-release signature verification, and the two-day operator course are `architecture-only`. |
| 1a | Deep query for approved extraction from EMR and other authorized source systems | 1a | partial | `site/…/stage1_synthea.py`, `site/…/stage2_ingest.py` | `stage2_ingest` shells out to `smart-fetch` against a live `$export` URL when one is supplied, and otherwise ingests an NDJSON payload of the same shape. SMART Backend Services client-confidential-asymmetric auth, scope-syntax negotiation, `--since` incremental export, and the MRN-list REST fallback are `architecture-only`. |
| 1b | Capture timestamps and provenance at extraction; maintain an immutable provenance chain | 1b | partial | `site/…/audit.py`, `site/…/hash_chain.py`, `site/…/stage9_audit.py` | Per-stage records are written to a hash-chained append-only log; `make verify-logs` recomputes from genesis. Gaps: per-resource `meta.versionId` / `meta.lastUpdated` are not captured, and the chain head is not carried in (or signed into) the delivery manifest. |
| 2 | Machine-readable, version-controlled, authorized Data Request Specification | 2 | partial | `site/…/drs.py`, `site/…/stage3_drs.py`, `site/drs/examples/DC-POC-0001.json` | The DRS drives cohort, fields, date range, and de-id parameters; availability is validated against it and gaps are reported rather than substituted. Gaps: an unavailable required field warns rather than halting (divergence 1 below), plus `specHash`, the `AMBIGUOUS` third availability state, closed-schema validation (`extra="forbid"`), request identity in Parquet file-level metadata, and `specHash + sourceFingerprint` rerun keying. See [Known divergences](#known-divergences). |
| 3 | Initial automated DQ check; all anomalies reported before any correction; source not modified | 3 | implemented | `site/…/stage4_dq_kahn.py`, halt path in `site/…/cli.py` | Completeness, uniqueness, data types, date sequencing, and records-per-patient, framed on Kahn et al. (conformance / completeness / plausibility). Checks report only; `data/lake/` is read-only after stage 2 and stage 5 writes a derived copy. `tests/test_dq_checks.py`. |
| 4 | De-identification per §164.514(b); 18 identifiers; per-patient order-preserving date shift | 4 | implemented | `site/…/stage5_deid.py`, `site/…/stage6_pprl.py` (offset derivation), `central/…/crypto.py` + `central/…/ingest.py` (delta re-offset) | All 18 identifiers enumerated at §164.514(b)(2) removed, with a documented per-identifier disposition. The date shift is per-patient, deterministic, order-preserving, and **consistent across independently deployed sites and across DRSs**: the offset is keyed on the OPPRL `token1` plaintext digest rather than on a site-local patient id, so one individual receives one offset everywhere, and central reconciles per-DRS offsets by applying a *delta* — `shift_global − shift_drs` — which never computes a true calendar date anywhere in `central/`. The offset is drawn from a 52-week lattice (`364·k + 7·j`), so **seasonality and day-of-week are preserved** along with every day-count interval and age-at-event; calendar-month exactness is not claimed. `REPORT.md` §5b has the design and its costs. **The output is deliberately not a Safe Harbor claim** — see the module docstring and `REPORT.md` §8. Honest limits: date shifting is not a confidentiality control (35 possible offsets — inherent to preserving season), and the two structural gaps in [Known divergences](#known-divergences) 5 and 6. Fixed during Req 14 benchmarking against real Synthea data: `viewdef.py`'s `getReferenceKey()` didn't normalize `urn:uuid:` references (only `ResourceType/id`), so the patient-id join stage 5 depends on silently found no match and excluded 99.2% of rows from de-identification with no error — see `REPORT.md` §10.2. `tests/test_reproducibility.py`, `central/tests/test_crypto.py`, `central/tests/test_ingest_reoffset.py`. |
| 5 | On-premises PPRL producing consistent, irreversible tokens across independent site deployments | 5 | implemented | `site/…/stage6_pprl.py` (site half), `central/…/crypto.py`, `central/…/linkage.py` | OPPRL v2 `token1`/`2`/`3` + `transcrypt_out` on-site; `transcrypt_in` + Splink linkage centrally. Token consistency across independently keyed sites is measured, not asserted (`REPORT.md` §5). **On "irreversible": the tokens are irreversible without the program's private key, and dictionary-attackable with it** — OPPRL's underlying digest is an unsalted SHA-512 over public, low-entropy attributes, so irreversibility is a property of key custody, not of the hash. This is a property of the OPPRL protocol, not of this transcription of it, and a pepper is structurally unavailable to it (`REPORT.md` §9 has the threat model and the reasoning). Gaps: 3 of OPPRL v2's 13 token variants; site-key TPM 2.0 sealing and the pre-first-transmission partner validation are `architecture-only`; and the central platform derives its at-rest AES key from the same RSA key it publishes to sites, so splitting those into two independent keypairs (`REPORT.md` §9.4) is a named, unimplemented mitigation. Also fixed during Req 14 sizing work: `linkage.link_resolved_sites` hardcoded the lightweight-roster column convention (`token1/2/3` + `local_patient_id`) and had never actually been exercised against a real `stage8_package` output (`subject_token1/2/3`, no `local_patient_id` by design) — it would raise a DuckDB column-not-found error. `_normalize_for_linkage` now accepts both conventions; see `REPORT.md` §10.10. |
| 6 | Preserve all provenance, permissions, and ontology fields without modification; additive metadata | 6 | partial | `site/…/stage2_ingest.py`, `site/…/viewdef.py`, `site/fhir_views/*.json` | Lossless raw Parquet layer with a round-trip test (`tests/test_roundtrip.py`); derived columns carry the additive `__` prefix; no ontology mapping or crosswalk is performed anywhere. Gaps: FHIR `decimal` is not preserved in lexical form, the code-system-version map is not written to Parquet file metadata, and the `__`-prefix invariant is documented but not programmatically enforced. `getReferenceKey()`'s `urn:uuid:` handling (a real bug found benchmarking Req 14 against real Synthea data — see Req 4's row and `REPORT.md` §10.2) is fixed as of this pass. |
| 7 | Authoritative eligibility, authorization versioning, cohort-vs-permission exclusions, revocation | 7 | architecture-only | — | No consent ledger, eligibility feed, or revocation post-filter in the POC. The write-up's design (site-side ledger, separate exclusion counts, post-filter assertion that fails the run) is the whole of the answer. This is the largest single POC gap. |
| 8 | Full and incremental extracts; stable tokens; snapshot/refresh IDs; 7 record change classes | 8 | partial | `site/…/stage8_package.py` | Snapshot and refresh IDs are generated per run; reruns are idempotent given a stable site key; added and deleted are computed by diffing against the prior snapshot. Gaps: `updated` and `late_arriving` are emitted as empty lists, and `merged` / `unmerged` / `corrected` are not classified at all. See [Known divergences](#known-divergences). |
| 9 | Per-stage audit records; failed reconciliation or incomplete stage halts transmission | 9 | partial | `site/…/audit.py`, `site/…/stage9_audit.py`, halt paths in `site/…/cli.py` | Every stage records timestamp, stage ID, batch ID, record counts, outcome, and reconciliation status; an unresolved DQ anomaly or any leak-gate hit halts before the outbox is written. Gaps: the SQLite control plane and the write-temp → fsync → atomic-rename → commit protocol described in the write-up are not implemented; run state lives in the log alone. |
| 10 | Attest to HIPAA Security Rule compliance as a Business Associate | 10 | architecture-only | — | Contractual, not code. The write-up accepts BA status and direct liability, distinguishes the site-sensor posture (no persistent access, credentials, or remote pathway) from the Program API posture (unambiguously a BA), and raises the Limited Data Set vs. Expert Determination question the PPRL token forces. That question is the reason Req 4 above is not a Safe Harbor claim. |
| 11 | SIEM-compatible security audit logging, protected against modification, continuous | 11 | implemented | `site/…/security_audit.py`, `central/…/security_audit.py`, `site/…/hash_chain.py` | CEF-formatted (Splunk / Sentinel / QRadar / ArcSight parse it natively) covering authentication, administrative actions, configuration changes, and security events, each with timestamp, account, action, resource, and outcome. Hash-chained, with two deliberate-tamper tests. Honest limit: tamper-*evident*, not tamper-*proof* — `site/README.md` §"Req 9 & 11". SIEM forwarding and halt-on-logging-failure are `architecture-only`. |
| 12 | Defense in depth: TLS 1.3 (1.2 floor) in transit; application-level output encryption; VM-level encryption | 12 | architecture-only | — | Properties of a deployment, not of this pipeline. The POC makes no outbound TLS call at all: its single egress is a local `outbox/` directory standing in for the transmission step. Application-level encryption of the output package is not implemented. The RFI's "defense in depth" framing is answered more broadly in `REPORT.md` §9.6 against published hardening baselines — DISA STIG/SRG (General Purpose OS SRG / RHEL 9 STIG for the host, Container Platform SRG for the podman runtime, Application Security and Development STIG for the pipeline code) and the corresponding NIST SP 800-53 families — rather than only against this row's three named encryption clauses. Also `architecture-only`, with one exception noted there: the ASD STIG's "diagnostics must not disclose sensitive information" control is implemented, in `central/…/crypto.py`'s `TranscryptError` path. |
| 12a | Attack-surface minimization; the only authorized connection is outbound TLS to the Program | 12a | implemented | whole-repo property; `site/Containerfile`, `site/Makefile` | Structurally satisfied rather than configured: the POC opens **no** listening socket, exposes no port, and runs as a batch process. Podman (daemonless, rootless — see `site/README.md` §Packaging) means operating it requires no root-owned socket either. |
| 13 | Identifiable processing confined to the partner's boundary; only de-identified output leaves | 13 | implemented | `site/` vs. `central/` project split; `site/…/stage7_leak_gate.py` | Enforced by construction: the two projects have separate `pyproject.toml`, lockfiles, and venvs, and share a protocol rather than runtime code, so nothing in `central/` can reach a raw identifier. An independent residual-PII gate (Presidio + Philter + GLiNER) scans the outbound artifact and halts on any hit. No contractor credential or remote-access pathway exists in the deployed artifact. Scope note: what crosses the boundary is tokenized, but a token's plaintext digest is briefly live in central's memory during `transcrypt_in` and is dictionary-attackable by a holder of central's key — so the central token space is not de-identified data for access-control purposes. `REPORT.md` §9.2, §9.5. |
| 14 | Performance at baseline, 2X, 5X; assumed volumes, throughput, CPU/memory/storage/staffing | 14 | architecture-only | — | Sizing estimates, not a runtime-audited stage — no code path treats "Req 14" as something `run-all` exercises, so it stays out of `RFI_MAP` on principle (see the note above the index). But unlike Req 10, the numbers behind it are not merely assumed: `REPORT.md` §10 anchors baseline to Emory Healthcare's published scale (7.1M patients, 5.27M annual encounters) and derives throughput/memory/storage from an actual 11,463-patient, 1,422,672-resource stages-2-through-8 run against **real MITRE Synthea output** (not this POC's pure-Python placeholder) on stated hardware — 55.8 patients/sec end-to-end, 9.83GB peak RSS, 1,941 bytes/resource on disk — extrapolated to baseline/2X/5X compute, memory, and storage (881M resources / 1.55TB / ~101GB RAM at baseline), plus a daily-cadence variant (§10.7, ~74k resources/day, ~11s compute/day) and a 90-day site-lake retention estimate (§10.7, ~11.7GB steady state). Producing this measurement against real Synthea data surfaced and fixed a real Req 4/6 bug — see that row and `REPORT.md` §10.2. §10.8 closes the free-text gap this row used to name: measured Presidio+Philter+GLiNER throughput (1.01 notes/sec, 848-word synthetic notes sourced from published note-length studies) turns the ~11s/day structured-only figure into ~51 minutes/day, and the one-time baseline backfill into 423.6 parallel-days (5.4 CPU-years single-threaded) — stage 7, not stage 5/6, dominates real-world compute. Gaps: no benchmark harness/CI regression tracking these numbers over time; sharding (by patient, and by note volume for stage 7) is the sizing conclusion, not an implemented pipeline feature; retention/pruning is prescribed, not implemented; staffing is not estimated at all; and real Synthea's own generation cost is non-linear in population (§10.4), so it was excluded from the throughput measured, not because it's free. §10.9 adds measured transmission-time figures (the packaged `outbox/` deliverable only — 57.5 bytes/resource measured — not the raw lake or any note text, since neither ever leaves the site), and §10.10 extends this sizing to `central/`: real measured rates for token resolution (1,001.5 patients/sec) and date re-offset (4,943 rows/sec, both embarrassingly parallel) versus Splink linkage (blocking-key-sharded, not arbitrary-replica-friendly), plus a second real bug this work found and fixed — see Req 5's row. |
| 15 | Reliably package, transmit, reconcile, and retry deliveries; package and schema versioning | 15 | partial | `site/…/stage8_package.py`, `central/…/ingest.py` | A versioned package (Parquet + manifest with per-file SHA-256 and row counts) is written to `outbox/` and ingested centrally. Gaps: the FastAPI Program API, resumable chunked upload, manifest signing, idempotency keys, the resource-schema lock, and typed error taxonomy are all `architecture-only` — confirmed concretely by `REPORT.md` §10.10: `central/` today is a batch CLI (`grep -rn fastapi central/` finds nothing), not a service, so "how many replicas" has no subject yet. |

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
5. **The date shift is digest-exact; linkage is probabilistic.** The per-patient offset is keyed on
   the `token1` plaintext digest, which changes with any difference in last-name spelling. Two
   records that Splink links via `token2`/`token3` — the Smith/Smyth pair in `REPORT.md` §5.4 is
   exactly this case — therefore receive *different* `shift_global` values and stay misaligned after
   central's re-offset. Cross-site intervals are valid for exact-digest matches, not for
   phonetically recovered ones. The alternative, named and not implemented: a second re-offset pass
   keyed on the Splink cluster id, run after `linkage.py` rather than in the ingest path. Re-offset
   in the ingest path was chosen deliberately; this is its cost.
6. **The per-DRS shift key is a real secret outside a single site's trust boundary.** `drs_shift_key`
   is shared with every site responding to a DRS. A holder who also has a candidate identity can
   recompute that DRS's `shift_drs` and un-shift its packaged dates. It is not a token key, and it
   does not affect Req 5's token consistency or the irreversibility bound in `REPORT.md` §9 — but it
   is the one secret in this design that crosses a boundary, and Req 5's "without introducing a
   shared trust boundary" language should be read with it in view. The undistributed
   `central_shift_secret` never leaves the central platform.

## Keeping this file honest

`site/…/stage9_audit.py`'s `RFI_MAP` is the runtime version of the Index table above — it is what
`make demo` prints into `data/work/rfi_summary.txt`. `site/tests/test_requirements_sync.py` parses
this file and asserts the two agree on requirement numbers and status, so adding a row here without
updating the code (or the reverse) fails the test suite rather than drifting quietly.

The write-up has no equivalent automated check. When a `Write-up §` reference here stops matching
the document, this file is what makes that visible.
