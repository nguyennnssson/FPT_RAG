"""Reciprocal Rank Fusion — merge dense + BM25 into one ranked list.

Dense cosine scores and BM25 scores are not comparable numbers, so we fuse by
RANK, not score (arch flow step 22, handbook D.4). RRF is simple and insensitive
to score scale:

    score(d) = sum over lists of 1 / (k + rank_in_list(d))

Candidates are deduped by ``chunk_id``; a chunk found by both retrievers
accumulates contributions from both, which is exactly the ranking boost hybrid
search is meant to give.
"""

from __future__ import annotations

from typing import Sequence

from .config import RetrievalConfig, get_config
from .retriever import RetrievalBundle
from .schemas import RetrievalResult


class RRFFuser:
    def __init__(self, config: RetrievalConfig | None = None) -> None:
        self._cfg = config or get_config().retrieval

    def fuse(self, bundle: RetrievalBundle) -> list[RetrievalResult]:
        return reciprocal_rank_fusion(
            [bundle.dense, bundle.bm25], k=self._cfg.rrf_k
        )


def reciprocal_rank_fusion(
    result_lists: Sequence[Sequence[RetrievalResult]], k: int = 60
) -> list[RetrievalResult]:
    scores: dict[str, float] = {}
    best: dict[str, RetrievalResult] = {}

    for results in result_lists:
        for rank, result in enumerate(results, start=1):
            cid = result.chunk_id
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank)
            # Keep one representative per chunk, preferring the one that carries
            # an embedding (useful later for dedup) or the higher raw score.
            incumbent = best.get(cid)
            if incumbent is None:
                best[cid] = result
            elif result.embedding is not None and incumbent.embedding is None:
                best[cid] = result

    fused: list[RetrievalResult] = []
    for rank, (cid, score) in enumerate(
        sorted(scores.items(), key=lambda kv: kv[1], reverse=True), start=1
    ):
        rep = best[cid]
        fused.append(
            RetrievalResult(
                chunk=rep.chunk,
                retriever="fusion",
                rank=rank,
                score=rep.score,
                normalized_score=rep.normalized_score,
                fusion_score=score,
                embedding=rep.embedding,
            )
        )
    return fused
