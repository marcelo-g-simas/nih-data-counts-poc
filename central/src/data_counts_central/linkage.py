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
import splink.comparison_library as cl
from splink import DuckDBAPI, Linker, SettingsCreator, block_on

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


def link_resolved_sites(resolved_tables: dict[str, pa.Table]) -> dict:
    """Run Splink over 2+ sites' resolved token tables, cluster matches, and
    return both the raw pairwise predictions and the resulting clusters.
    """
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
