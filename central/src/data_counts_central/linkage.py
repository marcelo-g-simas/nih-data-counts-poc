"""Cross-site probabilistic record linkage on resolved OPPRL tokens, via Splink
(DuckDB backend -- no Spark, no PyTorch, no `pyspark` extra installed). [Req 5]

Each of the three resolved tokens is an independent, binary (exact-match-or-
not) signal of varying tolerance:
  - token1: first-initial + exact last name + gender + birth-date -- breaks
    on any spelling difference in the last name.
  - token2: first/last name *soundex* + gender + birth-date -- tolerant of
    misspellings that sound alike.
  - token3: first/last name *metaphone* + gender + birth-date -- likewise,
    via a different phonetic algorithm.

A single exact-hash token can only ever answer "identical" or "not"; it has
no notion of "close enough." Splink's Fellegi-Sunter model is what turns
three such binary signals into one graded match-probability, so a person
whose last name was mistyped at one site (token1 disagrees) can still
resolve as the same patient at the central platform because token2 and
token3 -- computed over the *same* underlying raw name at that site, before
any noise was introduced downstream -- still agree.

The m/u probabilities below are hand-set, not EM-trained: this platform
never sees enough volume in one linkage run to train reliably, and for
tokens whose semantics are fixed by construction (exact-hash-or-not), fixed
priors are more defensible than fitting them to a handful of synthetic rows.
"""
from __future__ import annotations

import pyarrow as pa
import pyarrow.compute as pc
import splink.comparison_library as cl
from splink import DuckDBAPI, Linker, SettingsCreator, block_on

from .ingest import PACKAGE_TOKEN_COLUMNS, TOKEN_COLUMNS

# (m_probability, u_probability) per token, for the "exact match" comparison level.
# m: P(token matches | truly the same person). u: P(token matches | different people).
# token1 is least noise-tolerant (lower m: real typos break it) but most discriminating
# when it does match (lowest u: essentially no chance of an accidental 512-bit-hash
# collision). token2/token3 are more noise-tolerant (higher m) but coarser -- soundex/
# metaphone codes are short, so unrelated people can coincidentally share one (higher u).
_TOKEN_PROBABILITIES = {
    "token1": {"m": 0.70, "u": 0.0001},
    "token2": {"m": 0.92, "u": 0.02},
    "token3": {"m": 0.90, "u": 0.02},
}


def _build_settings() -> SettingsCreator:
    comparisons = []
    for token, probs in _TOKEN_PROBABILITIES.items():
        cc = cl.ExactMatch(token).configure(
            m_probabilities=[probs["m"], 1 - probs["m"]],
            u_probabilities=[probs["u"], 1 - probs["u"]],
        )
        comparisons.append(cc)

    return SettingsCreator(
        link_type="link_only",
        unique_id_column_name="local_patient_id",
        comparisons=comparisons,
        # Block on token2 (soundex -- the most tolerant signal): any pair sharing it
        # becomes a scored candidate, including the token1-typo-but-phonetic-match
        # case this module exists to validate. Pairs that don't even share a soundex
        # code are never compared at all -- correctly, since they're not plausible
        # matches under any of these three signals.
        blocking_rules_to_generate_predictions=[block_on("token2")],
        retain_matching_columns=True,
        probability_two_random_records_match=0.01,
    )


def _normalize_for_linkage(table: pa.Table) -> pa.Table:
    """Make a `resolve_site_package()` output usable by `_build_settings()`.

    `resolve_site_package` deliberately preserves whichever token-column
    convention the input used (see `ingest._token_columns_of`) -- real site
    packages (`stage8_package.py`'s output, ingest.PACKAGE_TOKEN_COLUMNS)
    carry `subject_token1/2/3` and no `local_patient_id` at all (Req 13: no
    site-internal identifier leaves the site), while the lightweight roster
    convention (`export_site_roster`, `ingest.TOKEN_COLUMNS`) carries
    `token1/2/3` plus `local_patient_id`. `_build_settings()` hardcodes the
    roster's column names, so a real package was never actually linkable
    through this function -- confirmed by trying it: `Linker` raised a
    DuckDB "column not found" error for both `token1` and `local_patient_id`
    on real `stage8_package` output. Fixed here rather than in
    `_build_settings()` so the Splink settings stay a fixed, simple
    contract and every caller (real packages and the roster-shaped demo
    alike) normalizes to it in one place.
    """
    names = set(table.column_names)
    if set(PACKAGE_TOKEN_COLUMNS) <= names:
        for pkg_col, roster_col in zip(PACKAGE_TOKEN_COLUMNS, TOKEN_COLUMNS):
            table = table.set_column(table.schema.get_field_index(pkg_col), roster_col, table.column(pkg_col))
    elif not set(TOKEN_COLUMNS) <= names:
        raise ValueError(f"table carries neither {TOKEN_COLUMNS} nor {PACKAGE_TOKEN_COLUMNS}; found {sorted(names)}")
    if "local_patient_id" not in table.column_names:
        # Splink's own within-run join key only -- never the site-local identifier
        # Req 13 excludes from real packages, and never persisted past this call.
        table = table.append_column("local_patient_id", pc.cast(pa.array(range(table.num_rows)), pa.string()))
    return table


def link_resolved_sites(resolved_tables: dict[str, pa.Table]) -> dict:
    """Run Splink over 2+ sites' resolved token tables, cluster matches, and
    return both the raw pairwise predictions and the resulting clusters.
    """
    resolved_tables = {name: _normalize_for_linkage(t) for name, t in resolved_tables.items()}
    db_api = DuckDBAPI()
    linker = Linker(
        list(resolved_tables.values()),
        _build_settings(),
        db_api,
        input_table_aliases=list(resolved_tables.keys()),
    )
    predictions = linker.inference.predict(threshold_match_probability=0.01)
    clusters = linker.clustering.cluster_pairwise_predictions_at_threshold(
        predictions, threshold_match_probability=0.5
    )
    return {
        "predictions": predictions.as_pandas_dataframe(),
        "clusters": clusters.as_pandas_dataframe(),
    }
