# Data COUNTS — Central Platform

The Data COUNTS program's side of the OPPRL protocol (Req 5): holds its own RSA keypair,
resolves the ephemeral (RSA-encrypted) tokens that Healthcare Partner sites transmit into
one consistent, comparable token space, and runs cross-site probabilistic record linkage
over the result.

This is a genuinely separate project from `../site/` (its own `pyproject.toml`/`uv.lock`,
its own venv) — not a subpackage of it. The two never share a runtime dependency for the
OPPRL protocol itself (`crypto.py` here is a deliberate, independent re-implementation, not
an import of `site/`'s copy): they interoperate because both correctly implement the same
protocol, which is the actual property Req 5 asks for ("consistent tokens across
independently deployed, site-local instances... without introducing a shared trust
boundary"). This project also carries none of the site's de-identification/leak-gate
dependencies (no PyTorch, transformers, GLiNER, Presidio, spaCy, Philter) — resolving and
linking tokens never requires touching a raw identifier.

That last claim is literally true and narrower than it sounds: the token digest this platform
briefly holds in memory during `transcrypt_in` is unsalted and computed over public, low-entropy
attributes, so it is dictionary-attackable by anyone holding this platform's private key. Read
[`../REPORT.md` §9](../REPORT.md) before treating the resolved token space as de-identified data —
for access-control purposes it is not.

## Quickstart

```bash
uv sync
uv run data-counts-central setup-keys          # keys/central_*.pem + the date-shift secret, once
# hand keys/central_public_key.pem to each site out-of-band (Req 5)
uv run data-counts-central issue-drs-shift-key DC-POC-0001   # per-DRS shift key, also out-of-band (Req 4)
# ... sites export packages into inbox/<site_name>/ ...
uv run data-counts-central link                              # resolves + links across all inbox/ sites
uv run data-counts-central reoffset-dates DC-POC-0001 site_1  # onto one global offset per person
```

`setup-keys` mints two independent secrets. The RSA keypair is published to sites; the date-shift
secret never leaves this platform, and the per-DRS keys derived from it are what sites receive. They
are separate because `derive_shift_days` and token resolution are different trust decisions — see
`../REPORT.md` §9.4 on why one key with three roles is a mitigation gap rather than a convenience.

## Why Splink

A single OPPRL token can only ever answer "identical" or "not" — it has no notion of
"close enough." `site/` computes three token variants per patient (`token1`: exact last
name; `token2`: soundex; `token3`: metaphone), each tolerant of different kinds of noise.
[Splink](https://moj-analytical-services.github.io/splink/) (DuckDB backend — no Spark, no
PyTorch) combines those three binary signals into one graded match probability via a
Fellegi-Sunter model, so a patient whose last name was mistyped at one site (breaking
`token1`) can still resolve as the same person here because `token2`/`token3` — computed
over the same underlying name at that site, before the typo entered anywhere downstream —
still agree. See `linkage.py` for the full reasoning and the hand-set match/non-match
probabilities per token.

## Module map

| Module | Role |
|---|---|
| `crypto.py` | RSA keypair generation, AES key derivation, `transcrypt_in` (decrypt with our private key, re-encrypt under our own AES key). Also the date-shift key hierarchy (Req 4): `generate_central_shift_secret` (32 random bytes, deliberately *not* derived from the RSA keypair — see `../REPORT.md` §9.4), `derive_drs_shift_key` (per-DRS key for out-of-band handoff to responding sites), `derive_shift_days` (the 52-week seasonality lattice, transcribed identically into `site/`), and `resolve_and_shift` (resolves a token *and* returns the re-offset delta). The statements that hold a plaintext digest are the sole PII-adjacent window in this project; see `../REPORT.md` §9.2 for the threat model and §9.5 for what the `TranscryptError` path does and does not bound — `resolve_and_shift` holds the digest longer than `transcrypt_in` does and repeats the same discipline |
| `ingest.py` | Reads a site's package — `patient.parquet` plus whichever clinical domain tables the DRS selected — and resolves its ephemeral tokens. `reoffset_domain_tables` additionally applies the cross-DRS date re-offset: `stored + (shift_global − shift_drs)`, so one individual carries one offset across every DRS and site. **No true calendar date is computed anywhere in this module** — the delta is a difference of two offsets, never of two dates (`../REPORT.md` §5b) |
| `linkage.py` | Splink-based cross-site probabilistic linkage over resolved tokens |
| `evaluation.py` | Precision/recall/F1 against a synthetic-demo-only ground truth |
| `security_audit.py`, `hash_chain.py` | Req 11: hash-chained, CEF-formatted security audit log (`security_audit.cef.log`) — `setup-keys`/`link` invocations, key generation. `make verify-logs` detects tampering. Duplicated from `../site/`'s copy, same rationale as `crypto.py`. |
| `cli.py` | `setup-keys` (RSA keypair + the date-shift secret), `issue-drs-shift-key <request_id>`, `reoffset-dates`, `link`, `verify-logs` |

This project is the central half of Req 5 and Req 15, and carries its own copy of Req 11. For a
status per RFI requirement across both projects — including the `architecture-only` ones that have
no code counterpart, and the specific gap behind each `partial` — see
**[`../REQUIREMENTS.md`](../REQUIREMENTS.md)**, which is the authoritative index.
