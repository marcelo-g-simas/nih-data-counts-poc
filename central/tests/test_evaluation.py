import pandas as pd

from data_counts_central.evaluation import evaluate


def _clusters(rows: list[tuple[str, str, str]]) -> pd.DataFrame:
    """rows: (cluster_id, source_dataset, local_patient_id)"""
    return pd.DataFrame(rows, columns=["cluster_id", "source_dataset", "local_patient_id"])


def test_perfect_linkage_scores_1_0_across_the_board():
    clusters = _clusters([
        ("c1", "site_a", "a-0"), ("c1", "site_b", "b-0"),  # jane <-> jane
        ("c2", "site_a", "a-1"),                            # alice, unlinked
    ])
    ground_truth = {"site_a": {"a-0": "jane", "a-1": "alice"}, "site_b": {"b-0": "jane"}}
    result = evaluate(clusters, ground_truth)
    assert result["precision"] == 1.0
    assert result["recall"] == 1.0
    assert result["f1"] == 1.0


def test_false_positive_lowers_precision_not_recall():
    """Mirrors the twins scenario: a predicted match that isn't a true match."""
    clusters = _clusters([
        ("c1", "site_a", "a-0"), ("c1", "site_b", "b-0"),  # true match
        ("c2", "site_a", "a-1"), ("c2", "site_b", "b-1"),  # false positive: different people
    ])
    ground_truth = {
        "site_a": {"a-0": "jane", "a-1": "twin_a"},
        "site_b": {"b-0": "jane", "b-1": "twin_b"},
    }
    result = evaluate(clusters, ground_truth)
    assert result["precision"] == 0.5
    assert result["recall"] == 1.0
    assert result["false_positive_pairs"] == [(("site_a", "a-1"), ("site_b", "b-1"))]


def test_missed_match_lowers_recall_not_precision():
    clusters = _clusters([
        ("c1", "site_a", "a-0"),  # should have linked to b-0 but didn't
        ("c2", "site_b", "b-0"),
    ])
    ground_truth = {"site_a": {"a-0": "jane"}, "site_b": {"b-0": "jane"}}
    result = evaluate(clusters, ground_truth)
    assert result["recall"] == 0.0
    assert result["false_negative_pairs"] == [(("site_a", "a-0"), ("site_b", "b-0"))]
