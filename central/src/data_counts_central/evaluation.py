"""Precision/recall/F1 of the linkage result against a known ground truth.

This is a synthetic-demo-only capability: it requires knowing which patients
are *actually* the same person, which is exactly the information the whole
PPRL design exists to keep this platform from ever having. It exists so the
POC can honestly report how well token-based + Splink linkage performs, not
as part of the real pipeline.
"""
from __future__ import annotations

import itertools
import json
from pathlib import Path

import pandas as pd


def load_ground_truth(path: str | Path) -> dict:
    return json.loads(Path(path).read_text())


def label_for(ground_truth: dict, source_dataset: str, local_patient_id: str) -> str | None:
    return ground_truth.get(source_dataset, {}).get(local_patient_id)


def evaluate(clusters_df: pd.DataFrame, ground_truth: dict) -> dict:
    """Pair-level precision/recall/F1: every pair of records that ended up in the
    same predicted cluster is a "predicted match"; every pair sharing a ground-truth
    label is a "true match". Standard record-linkage evaluation convention.
    """
    rows = clusters_df[["cluster_id", "source_dataset", "local_patient_id"]].to_dict("records")

    predicted_matches: set[frozenset] = set()
    for _cluster_id, group in clusters_df.groupby("cluster_id"):
        members = list(group[["source_dataset", "local_patient_id"]].itertuples(index=False, name=None))
        for (s1, p1), (s2, p2) in itertools.combinations(members, 2):
            predicted_matches.add(frozenset({(s1, p1), (s2, p2)}))

    labeled = [
        (r["source_dataset"], r["local_patient_id"], label_for(ground_truth, r["source_dataset"], r["local_patient_id"]))
        for r in rows
    ]
    true_matches: set[frozenset] = set()
    for (s1, p1, l1), (s2, p2, l2) in itertools.combinations(labeled, 2):
        if l1 is not None and l1 == l2:
            true_matches.add(frozenset({(s1, p1), (s2, p2)}))

    tp = len(predicted_matches & true_matches)
    fp = len(predicted_matches - true_matches)
    fn = len(true_matches - predicted_matches)

    precision = tp / (tp + fp) if (tp + fp) else float("nan")
    recall = tp / (tp + fn) if (tp + fn) else float("nan")
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else float("nan")

    return {
        "true_positive_pairs": sorted(tuple(sorted(p)) for p in (predicted_matches & true_matches)),
        "false_positive_pairs": sorted(tuple(sorted(p)) for p in (predicted_matches - true_matches)),
        "false_negative_pairs": sorted(tuple(sorted(p)) for p in (true_matches - predicted_matches)),
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }
