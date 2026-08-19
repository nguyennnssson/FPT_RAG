"""Retrieval evaluation metrics (handbook M.5).

Order-aware measures over a ranked list of ids, given a set of relevant ids.
Doc-level or chunk-level depending on what ids the caller passes in.
"""

from __future__ import annotations

import math
from typing import Sequence


def dedupe_preserve_order(ids: Sequence[str]) -> list[str]:
    seen: dict[str, None] = {}
    for i in ids:
        seen.setdefault(i, None)
    return list(seen)


def recall_at_k(ranked_ids: Sequence[str], relevant_ids: set[str], k: int) -> float:
    if not relevant_ids:
        return 1.0
    return len(set(ranked_ids[:k]) & relevant_ids) / len(relevant_ids)


def precision_at_k(ranked_ids: Sequence[str], relevant_ids: set[str], k: int) -> float:
    if k == 0:
        return 0.0
    return len(set(ranked_ids[:k]) & relevant_ids) / k


def mrr(ranked_ids: Sequence[str], relevant_ids: set[str]) -> float:
    for i, doc_id in enumerate(ranked_ids, start=1):
        if doc_id in relevant_ids:
            return 1.0 / i
    return 0.0


def hit_rate(ranked_ids: Sequence[str], relevant_ids: set[str], k: int) -> float:
    return 1.0 if set(ranked_ids[:k]) & relevant_ids else 0.0


def dcg_at_k(ranked_ids: Sequence[str], graded: dict[str, float], k: int) -> float:
    total = 0.0
    for i, doc_id in enumerate(ranked_ids[:k], start=1):
        rel = graded.get(doc_id, 0.0)
        total += (2**rel - 1) / math.log2(i + 1)
    return total


def ndcg_at_k(ranked_ids: Sequence[str], graded: dict[str, float], k: int) -> float:
    ideal = sorted(graded, key=graded.get, reverse=True)  # type: ignore[arg-type]
    ideal_dcg = dcg_at_k(ideal, graded, k)
    if ideal_dcg == 0:
        return 0.0
    return dcg_at_k(ranked_ids, graded, k) / ideal_dcg
