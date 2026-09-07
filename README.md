# Data COUNTS — Leave-Behind Pipeline POC

> **SYNTHETIC DATA ONLY. No real PHI is generated, stored, or processed anywhere in this repository.**

A proof-of-concept in support of a response to NIH RFI **75N95C26R00005 (Data COUNTS™)** (see
`rfi/`). See **[REPORT.md](REPORT.md)** for a full narrative walkthrough of the POC, including
worked examples and the linkage evaluation metrics.

## Two projects, deliberately

This repo holds two independently deployable, independently dependency-managed projects, not
one project with subpackages:

- **[`site/`](site/README.md)** — the Healthcare Partner Leave-Behind pipeline: extraction,
  data quality, de-identification, OPPRL tokenization (site half), residual-PII leak gate,
  packaging. Everything that ever touches an identifiable attribute runs here.
- **[`central/`](central/README.md)** — the Data COUNTS program's platform: holds its own RSA
  keypair, resolves sites' ephemeral tokens into one consistent token space, and runs
  cross-site probabilistic linkage (Splink) over the result. Never sees a raw identifier.

The split isn't just organizational — it mirrors Req 5 and Req 13's actual language:
identifiable processing is confined to the Healthcare Partner's trust boundary; only
de-identified, tokenized output crosses it; and token consistency across independently
deployed sites must hold *without* a shared trust boundary. Two separate projects that each
correctly implement the same OPPRL protocol (not one shared runtime dependency) is a more
literal proof of that property than a single monolith would be. It also means each project's
install footprint reflects what it actually needs: `central/` never installs PyTorch,
transformers, GLiNER, Presidio, spaCy, or Philter — none of that is a linkage-resolution
concern.

## Quickstart

```bash
make setup          # installs both projects' deps; downloads/exports site's models;
                     # generates central's RSA keypair
make demo           # runs the main pipeline (site/) end to end
make linkage-demo   # Req 5 evidence: cross-site linkage + precision/recall/F1
make test           # both projects' test suites
```

See `site/README.md` for the full RFI-requirement-to-module map, and `central/README.md` for
why Splink is the right tool for turning OPPRL's binary tokens into graded match confidence.
