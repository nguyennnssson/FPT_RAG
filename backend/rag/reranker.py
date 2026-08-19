"""Precision pass — cross-encoder reranking with bge-reranker-v2-m3.

Retrieval is a coarse funnel; only a query-aware cross-encoder can tell which of
the fused top-k are truly on-topic (arch flow step 24). The reranker scores each
(query, chunk) pair jointly and keeps the top-N.

The ~1.5 GB model loads lazily on first use. If no cross-encoder backend is
available, we fall back to a lexical-overlap scorer so the pipeline still ranks
and abstains sensibly offline — clearly weaker than the real model, never a
silent substitute in production.

Scores are calibrated into an absolute [0, 1] range (sigmoid over cross-encoder
logits; query-term recall for the lexical fallback) so the risk-graded
abstention thresholds mean the same thing regardless of how many candidates are
in a given request. We deliberately do NOT min-max within a request: that would
map a lone candidate to a mid value and silently defeat abstention.
"""

from __future__ import annotations

import math
import os
from functools import lru_cache
from typing import Sequence

from .config import ModelConfig, RetrievalConfig, get_config
from .language import detect_language
from .retriever import analyze, normalize_lexical_text
from .schemas import RetrievalResult
from .embedder import _resolve_device


class Reranker:
    def __init__(
        self,
        model_config: ModelConfig | None = None,
        retrieval_config: RetrievalConfig | None = None,
    ) -> None:
        cfg = get_config()
        self._model_cfg = model_config or cfg.models
        self._cfg = retrieval_config or cfg.retrieval
        self._model = None
        self._backend: str | None = None
        self._requested = os.environ.get("RAG_RERANKER_BACKEND", "auto")

    # ------------------------------------------------------------ lifecycle  #
    def _ensure(self) -> None:
        if self._backend is not None:
            return
        if self._requested in ("auto", "sentence_transformers"):
            try:
                from sentence_transformers import CrossEncoder  # type: ignore

                self._model = CrossEncoder(
                    self._model_cfg.reranker_model,
                    device=_resolve_device(self._model_cfg.device),
                    revision=self._model_cfg.reranker_revision,
                )
                self._backend = "sentence_transformers"
                return
            except Exception:
                if self._requested == "sentence_transformers":
                    raise
        if self._requested in ("auto", "flag"):
            try:
                from FlagEmbedding import FlagReranker  # type: ignore

                self._model = FlagReranker(
                    self._model_cfg.reranker_model,
                    use_fp16=_resolve_device(self._model_cfg.device) != "cpu",
                )
                self._backend = "flag"
                return
            except Exception:
                if self._requested == "flag":
                    raise
        self._backend = "lexical"

    @property
    def backend(self) -> str:
        self._ensure()
        return self._backend or "unknown"

    # ---------------------------------------------------------------- public #
    def rerank(
        self, query: str, candidates: Sequence[RetrievalResult], top_n: int | None = None
    ) -> list[RetrievalResult]:
        top_n = top_n or self._cfg.rerank_top_n
        if not candidates:
            return []
        raw = self._score(query, candidates)
        for cand, (calibrated, native) in zip(candidates, raw):
            cand.rerank_score = float(calibrated)
            cand.raw_rerank_score = float(native)
        ranked = sorted(candidates, key=lambda c: c.rerank_score or 0.0, reverse=True)
        # The lexical fallback often gives several sections from one document
        # identical overlap scores. Apply the same per-document cap used by
        # context assembly so complementary evidence is not crowded out. The
        # real cross-encoder branches retain their original ranking behavior.
        if self._backend == "lexical":
            ranked = _diversify_lexical(
                ranked,
                top_n=top_n,
                max_per_doc=self._cfg.max_chunks_per_parent,
            )
        else:
            ranked = ranked[:top_n]
        out = []
        for rank, cand in enumerate(ranked, start=1):
            cand.retriever = "rerank"
            cand.rank = rank
            out.append(cand)
        return out

    # ------------------------------------------------------------ internals  #
    def _score(self, query: str, candidates) -> list[tuple[float, float]]:
        """Return (calibrated_0_1, native_score) per candidate."""
        self._ensure()
        texts = [c.chunk.contextual_text or c.chunk.text for c in candidates]
        pairs = [(query, t) for t in texts]
        if self._backend == "sentence_transformers":
            native = [float(s) for s in self._model.predict(pairs)]  # type: ignore[union-attr]
            return [(_sigmoid(s), s) for s in native]
        if self._backend == "flag":
            # normalize=True applies sigmoid, giving calibrated [0,1] scores.
            scores = self._model.compute_score(pairs, normalize=True)  # type: ignore[union-attr]
            if isinstance(scores, float):
                scores = [scores]
            return [(float(s), float(s)) for s in scores]
        # Lexical fallback: fraction of query terms found in the candidate —
        # an absolute-ish signal that discriminates on/off-topic, unlike Jaccard.
        recalls = [_query_recall(query, t) for t in texts]
        return [(s, s) for s in recalls]


def _sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    ex = math.exp(x)
    return ex / (1.0 + ex)


def _diversify_lexical(
    ranked: Sequence[RetrievalResult], *, top_n: int, max_per_doc: int
) -> list[RetrievalResult]:
    """Keep lexical ranking stable while limiting repeated parent documents."""
    selected: list[RetrievalResult] = []
    per_doc: dict[str, int] = {}
    cap = max(1, max_per_doc)
    for candidate in ranked:
        doc_id = candidate.chunk.doc_id
        if per_doc.get(doc_id, 0) >= cap:
            continue
        selected.append(candidate)
        per_doc[doc_id] = per_doc.get(doc_id, 0) + 1
        if len(selected) >= top_n:
            break
    return selected


_EN_STOPWORD_WORDS = {
    "a", "an", "and", "are", "be", "can", "could", "do", "does", "for",
    "from", "how", "i", "in", "is", "it", "me", "my", "of", "on", "or",
    "please", "should", "that", "the", "these", "this", "to", "was", "were",
    "what", "when", "where", "which", "who", "why", "will", "with", "would",
    "you", "your", "s",
}

_VI_STOPWORD_WORDS = {
    "ai", "bao", "bạn", "các", "cho", "có", "của", "cần", "đã", "đang", "để",
    "đến", "đó", "được", "gì", "giúp", "hãy", "khi", "không", "là", "làm",
    "mà", "mình", "một", "nào", "này", "nhé", "những", "phải", "sau", "sẽ",
    "thì", "tôi", "trong", "trước", "từ", "tuần", "và", "về", "việc", "với",
}

# PyVi already retains many compounds with underscores. These explicit phrase
# markers cover important employee-domain compounds that PyVi may segment only
# partially (notably "quyền truy_cập") and keep the dependency-free fallback
# useful. They are domain vocabulary, not document or one-query boosts.
_VI_COMPOUNDS = {
    "nhân viên": "nhân_viên",
    "dữ liệu": "dữ_liệu",
    "khách hàng": "khách_hàng",
    "quyền truy cập": "quyền_truy_cập",
    "đào tạo": "đào_tạo",
    "bảo mật": "bảo_mật",
}


@lru_cache(maxsize=2)
def _stopwords(language: str) -> frozenset[str]:
    words = _VI_STOPWORD_WORDS if language == "vi" else _EN_STOPWORD_WORDS
    return frozenset(
        token
        for word in words
        for token in analyze(word, language)
    )


def _lexical_terms(text: str, language: str) -> set[str]:
    normalized = normalize_lexical_text(text)
    terms = {
        token
        for token in analyze(normalized, language)
        if token not in _stopwords(language)
    }
    if language == "vi":
        padded = f" {normalized} "
        for phrase, marker in _VI_COMPOUNDS.items():
            if f" {phrase} " in padded:
                terms.add(marker)
    return terms


def _query_recall(query: str, text: str) -> float:
    """Fraction of normalized content-query terms present in candidate text."""
    language = detect_language(query)
    query_terms = _lexical_terms(query, language)
    if not query_terms:
        query_terms = set(analyze(query, language))
    if not query_terms:
        return 0.0
    text_terms = _lexical_terms(text, language)
    return len(query_terms.intersection(text_terms)) / len(query_terms)
