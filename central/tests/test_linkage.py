"""Integration test for the Splink linkage step itself.

Uses hand-crafted resolved token tables (bypassing crypto/ingest entirely --
those are covered separately in test_crypto.py) since Splink's linkage logic
only cares about token string equality, not how the tokens were produced.
"""
import pyarrow as pa

from data_counts_central.evaluation import evaluate
from data_counts_central.linkage import link_resolved_sites


def _table(rows: list[dict]) -> pa.Table:
    return pa.Table.from_pylist(rows)


def test_exact_and_noisy_matches_link_while_unrelated_identities_dont():
    # "jane": identical tokens at both sites.
    # "smith"/"smyth": token1 differs (exact last name), token2/token3 agree (phonetic) --
    #   this is the scenario token2/token3 exist to handle.
    # "alice"/"bob": no shared tokens at all -- must never link.
    site_a = _table([
        {"local_patient_id": "a-0", "token1": "JANE_T1", "token2": "JANE_T2", "token3": "JANE_T3"},
        {"local_patient_id": "a-1", "token1": "SMITH_T1", "token2": "SMITHSMYTH_T2", "token3": "SMITHSMYTH_T3"},
        {"local_patient_id": "a-2", "token1": "ALICE_T1", "token2": "ALICE_T2", "token3": "ALICE_T3"},
    ])
    site_b = _table([
        {"local_patient_id": "b-0", "token1": "JANE_T1", "token2": "JANE_T2", "token3": "JANE_T3"},
        {"local_patient_id": "b-1", "token1": "SMYTH_T1", "token2": "SMITHSMYTH_T2", "token3": "SMITHSMYTH_T3"},
        {"local_patient_id": "b-2", "token1": "BOB_T1", "token2": "BOB_T2", "token3": "BOB_T3"},
    ])

    result = link_resolved_sites({"site_a": site_a, "site_b": site_b})
    clusters = result["clusters"]

    ground_truth = {
        "site_a": {"a-0": "jane", "a-1": "smith", "a-2": "alice"},
        "site_b": {"b-0": "jane", "b-1": "smith", "b-2": "bob"},
    }
    metrics = evaluate(clusters, ground_truth)

    assert metrics["precision"] == 1.0
    assert metrics["recall"] == 1.0
    assert (("site_a", "a-0"), ("site_b", "b-0")) in metrics["true_positive_pairs"]
    assert (("site_a", "a-1"), ("site_b", "b-1")) in metrics["true_positive_pairs"]
