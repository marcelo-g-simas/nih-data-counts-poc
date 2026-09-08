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

## Quickstart

```bash
uv sync
uv run data-counts-central setup-keys          # generates keys/central_*.pem once
# hand keys/central_public_key.pem to each site out-of-band (Req 5)
# ... sites export patient.parquet packages into inbox/<site_name>/ ...
uv run data-counts-central link                # resolves + links across all inbox/ sites
```

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
| `crypto.py` | RSA keypair generation, AES key derivation, `transcrypt_in` (this platform's only crypto operation — decrypt with our private key, re-encrypt under our own AES key) |
| `ingest.py` | Reads a site's `patient.parquet` export, resolves its ephemeral tokens |
| `linkage.py` | Splink-based cross-site probabilistic linkage over resolved tokens |
| `evaluation.py` | Precision/recall/F1 against a synthetic-demo-only ground truth |
| `security_audit.py`, `hash_chain.py` | Req 11: hash-chained, CEF-formatted security audit log (`security_audit.cef.log`) — `setup-keys`/`link` invocations, key generation. `make verify-logs` detects tampering. Duplicated from `../site/`'s copy, same rationale as `crypto.py`. |
| `cli.py` | `setup-keys`, `link`, `verify-logs` |

This project is the central half of Req 5 and Req 15, and carries its own copy of Req 11. For a
status per RFI requirement across both projects — including the `architecture-only` ones that have
no code counterpart, and the specific gap behind each `partial` — see
**[`../REQUIREMENTS.md`](../REQUIREMENTS.md)**, which is the authoritative index.
