"""Candidate generator — dense (meaning) + BM25 (keyword) retrieval.

Two notions of "relevant" run together (arch flow step 21): dense cosine search
catches paraphrase/concept matches, BM25 catches exact terms (IDs, names,
codes) that dense vectors miss. Running only one loses recall the other would
have caught.

Both retrievers respect Checkpoint 1's scalar cut (tenant + classification),
enforced inside the stores. Fusion, CP2, and reranking happen downstream.

BM25 tokenization is language-aware and must be IDENTICAL at index and query
time, so the analyzer lives here and is reused by the Ingester:
- Vietnamese: ``pyvi`` word segmentation (lazy).
- English:    Porter stemming (lazy, via nltk) with a simple fallback.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from .config import RAGConfig, get_config
from .schemas import Chunk, RetrievalResult, UserContext
from .vectorstore import BM25Store, VectorStore

_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)


def normalize_lexical_text(text: str) -> str:
    """Return stable, case-folded text with Unicode punctuation as boundaries.

    BM25 and the offline reranker must see the same lexical surface. NFKC
    normalization makes compatibility forms deterministic while replacing all
    Unicode punctuation (curly quotes, dashes, full-width punctuation, etc.)
    prevents punctuation from becoming part of a token.
    """
    normalized = unicodedata.normalize("NFKC", text or "").casefold()
    without_punctuation = "".join(
        " " if unicodedata.category(char).startswith("P") else char
        for char in normalized
    )
    return " ".join(without_punctuation.split())


# --------------------------------------------------------------------------- #
# Language-aware BM25 analyzer (shared by index + query)
# --------------------------------------------------------------------------- #


class BM25Analyzer:
    """Tokenizes text for BM25, per language. Backends load lazily."""

    def __init__(self) -> None:
        self._pyvi = None            # pyvi tokenizer fn or False
        self._porter = None          # nltk PorterStemmer or False

    def analyze(self, text: str, language: str | None) -> list[str]:
        text = normalize_lexical_text(text)
        if language == "vi":
            return self._analyze_vi(text)
        return self._analyze_en(text)

    def _analyze_vi(self, text: str) -> list[str]:
        if self._pyvi is None:
            try:
                from pyvi import ViTokenizer  # type: ignore

                self._pyvi = ViTokenizer.tokenize
            except Exception:
                self._pyvi = False
        if self._pyvi:
            try:
                segmented = self._pyvi(text)  # underscores join compound words
                return [
                    token
                    for token in segmented.split()
                    if any(char.isalnum() for char in token)
                ]
            except Exception:
                pass
        return _WORD_RE.findall(text)

    def _analyze_en(self, text: str) -> list[str]:
        tokens = _WORD_RE.findall(text)
        if self._porter is None:
            try:
                from nltk.stem import PorterStemmer  # type: ignore

                self._porter = PorterStemmer()
            except Exception:
                self._porter = False
        if self._porter:
            try:
                return [self._porter.stem(t) for t in tokens]
            except Exception:
                pass
        return tokens


_shared_analyzer: BM25Analyzer | None = None


def get_analyzer() -> BM25Analyzer:
    global _shared_analyzer
    if _shared_analyzer is None:
        _shared_analyzer = BM25Analyzer()
    return _shared_analyzer


def analyze(text: str, language: str | None) -> list[str]:
    return get_analyzer().analyze(text, language)


# --------------------------------------------------------------------------- #
# Retriever
# --------------------------------------------------------------------------- #


@dataclass
class RetrievalBundle:
    """The two ranked candidate lists handed to fusion."""

    dense: list[RetrievalResult] = field(default_factory=list)
    bm25: list[RetrievalResult] = field(default_factory=list)


class Retriever:
    def __init__(
        self,
        vector_store: VectorStore | None = None,
        bm25_store: BM25Store | None = None,
        config: RAGConfig | None = None,
    ) -> None:
        self._cfg = config or get_config()
        self._vs = vector_store or VectorStore(self._cfg)
        self._bm25 = bm25_store or BM25Store(self._cfg)
        self._analyzer = get_analyzer()

    def _top_k(self, user: UserContext) -> int:
        """Over-fetch for restrictive principals so CP2 does not starve the
        candidate set below what the reranker needs (arch flow step 23 note)."""
        rc = self._cfg.retrieval
        n_principals = len(user.all_principals())
        if n_principals <= rc.restrictive_principal_threshold:
            return rc.restrictive_top_k
        return max(rc.dense_top_k, rc.bm25_top_k)

    def retrieve(
        self,
        query_text: str,
        query_embedding: list[float],
        user: UserContext,
        query_language: str | None = None,
        doc_ids: set[str] | None = None,
    ) -> RetrievalBundle:
        top_k = self._top_k(user)
        dense = (
            self._vs.search(query_embedding, user, top_k=top_k, doc_ids=doc_ids)
            if doc_ids
            else self._vs.search(query_embedding, user, top_k=top_k)
        )
        query_tokens = self._analyzer.analyze(query_text, query_language)
        bm25 = (
            self._bm25.search(query_tokens, user, top_k=top_k, doc_ids=doc_ids)
            if doc_ids
            else self._bm25.search(query_tokens, user, top_k=top_k)
        )
        return RetrievalBundle(dense=dense, bm25=bm25)
