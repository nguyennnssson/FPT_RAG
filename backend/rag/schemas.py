"""Typed data contracts shared across every stage of the pipeline.

These are the interfaces between ingestion, chunking, indexing, retrieval,
reranking, context assembly, generation and evaluation. They follow the
reference contracts in the RAG Engineering Handbook (M.1), adapted to this
system's fields (tenant + ACL + sensitivity + version stamps).

Pure stdlib only — safe to import anywhere, no heavy dependencies.

Design notes:
- ``text`` is always the *raw* extracted source text (what citations show).
- ``contextual_text`` is the augmented, heading-prefixed text we embed/index.
  Never cite contextual_text as if it were source.
- Every retrievable unit carries ``tenant_id``, ``acl_principals`` and
  ``classification`` so the three ACL checkpoints have what they need.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Literal

# --------------------------------------------------------------------------- #
# Enums / aliases
# --------------------------------------------------------------------------- #

Sensitivity = Literal["public", "internal", "confidential", "restricted"]
RiskLevel = Literal["low", "medium", "high"]
Support = Literal["direct", "partial", "contradicted", "unsupported"]
ChunkType = Literal[
    "text", "table_row", "table_summary", "figure_caption", "code_symbol"
]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _sha256(*parts: Any) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(str(p).encode("utf-8"))
        h.update(b"\x1f")  # unit separator, avoids "ab"+"c" == "a"+"bc"
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# Source document (canonical extracted object, pre-chunking)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SourceDocument:
    """A parsed source file before chunking. Handbook M.1 Source contract."""

    tenant_id: str
    doc_id: str                      # stable, human-legible id (filename/slug) — NEVER a hash
    source_system: str
    source_uri: str
    title: str
    content_type: str                # "text/plain", "application/pdf", "text/markdown", ...
    text: str
    acl_principals: list[str]
    sensitivity: Sensitivity = "internal"
    language: str | None = None
    version_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None
    modified_at: datetime | None = None
    extracted_at: datetime = field(default_factory=utcnow)
    extractor_name: str = "manual"
    extractor_version: str = "1.0.0"
    content_hash: str | None = None
    deleted: bool = False

    def with_hash(self, hash_value: str) -> "SourceDocument":
        return replace(self, content_hash=hash_value)


# --------------------------------------------------------------------------- #
# Chunk (the retrievable unit)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Chunk:
    """A retrievable unit. Handbook M.1 Chunk contract.

    ``text`` = raw source span (citation display).
    ``contextual_text`` = heading-prefixed text actually embedded/indexed.
    """

    tenant_id: str
    doc_id: str
    chunk_id: str
    source_system: str
    source_uri: str
    title: str
    text: str
    contextual_text: str
    acl_principals: list[str]
    sensitivity: Sensitivity = "internal"
    section_path: list[str] = field(default_factory=list)
    page_number: int | None = None
    language: str | None = None
    chunk_type: ChunkType = "text"
    metadata: dict[str, Any] = field(default_factory=dict)
    char_start: int | None = None
    char_end: int | None = None
    token_count: int = 0
    # Provenance / version stamps (arch flow step 11).
    version_id: str | None = None
    chunker_version: str | None = None
    parser_version: str | None = None
    embedding_model: str | None = None
    embedding_version: str | None = None

    @staticmethod
    def make_id(
        tenant_id: str,
        source_system: str,
        doc_id: str,
        version_id: str | None,
        chunker_version: str | None,
        char_start: int | None,
        char_end: int | None,
    ) -> str:
        """Deterministic chunk id (handbook M.1 recipe). Same inputs -> same id,
        which is what makes ingestion idempotent."""
        return _sha256(
            tenant_id, source_system, doc_id, version_id or "-",
            chunker_version or "-", char_start, char_end,
        )


# --------------------------------------------------------------------------- #
# User / request context (drives the three ACL checkpoints)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class UserContext:
    """Who is asking, and what they're allowed to see.

    ``principals`` is the full set of identities used for the CP2 acl_principals
    check (e.g. ["user:alice", "group:support", "role:agent"]).
    ``max_classification`` is the most sensitive level this user may read; CP1
    turns it into a scalar Chroma filter.
    """

    tenant_id: str
    user_id: str
    principals: list[str] = field(default_factory=list)
    max_classification: Sensitivity = "internal"
    session_id: str | None = None
    language: str | None = None          # optional explicit UI language tag

    def all_principals(self) -> list[str]:
        """Principals including the user's own id, deduped, stable order."""
        seen: dict[str, None] = {}
        for p in [f"user:{self.user_id}", *self.principals]:
            seen.setdefault(p, None)
        return list(seen)


# Ordered from least to most sensitive — used for CP1 scalar comparison.
CLASSIFICATION_ORDER: tuple[Sensitivity, ...] = (
    "public", "internal", "confidential", "restricted",
)


def classification_rank(level: str) -> int:
    try:
        return CLASSIFICATION_ORDER.index(level)  # type: ignore[arg-type]
    except ValueError:
        return len(CLASSIFICATION_ORDER)  # unknown => treat as most restrictive


# --------------------------------------------------------------------------- #
# Retrieval results
# --------------------------------------------------------------------------- #


@dataclass
class RetrievalResult:
    """One retrieved candidate. Mutable so later stages can attach scores.

    Raw retriever scores and normalized/fused scores are kept separate — BM25,
    cosine and reranker numbers are not comparable (handbook M.1).
    """

    chunk: Chunk
    retriever: str                       # "dense" | "bm25" | "fusion" | "rerank"
    rank: int
    score: float
    normalized_score: float | None = None
    fusion_score: float | None = None
    rerank_score: float | None = None        # normalized [0,1] within a request
    raw_rerank_score: float | None = None     # backend-native reranker score
    embedding: list[float] | None = None  # kept when available for dedup

    # Convenience passthroughs -------------------------------------------------
    @property
    def chunk_id(self) -> str:
        return self.chunk.chunk_id

    @property
    def doc_id(self) -> str:
        return self.chunk.doc_id


# --------------------------------------------------------------------------- #
# Sources exposed to the model / UI, and the answer
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Source:
    """A single evidence block shown to the LLM and returned to the UI, keyed
    by a stable citation label like ``S1``."""

    label: str                           # "S1", "S2", ...
    chunk_id: str
    doc_id: str
    title: str
    source_uri: str
    text: str                            # RAW chunk text (never contextual_text)
    section_path: list[str] = field(default_factory=list)
    page_number: int | None = None
    modified_at: str | None = None


@dataclass(frozen=True)
class CitedClaim:
    claim: str
    source_ids: list[str]
    support: Support = "direct"


@dataclass(frozen=True)
class RagAnswer:
    """The final structured response (arch flow step 29 / handbook M.1)."""

    answer: str
    sources: list[Source] = field(default_factory=list)
    cited_claims: list[CitedClaim] = field(default_factory=list)
    abstained: bool = False
    abstention_reason: str | None = None
    language: str = "en"
    model: str | None = None
    prompt_version: str | None = None
    index_version: str | None = None
    trace_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def abstain(
        cls,
        reason: str,
        *,
        language: str = "en",
        trace_id: str | None = None,
        **meta: Any,
    ) -> "RagAnswer":
        text = {
            "vi": "Tôi không tìm thấy đủ thông tin trong tài liệu để trả lời câu hỏi này.",
            "en": "I could not find enough information in the provided sources to answer that.",
        }.get(language, "I could not find enough information in the provided sources to answer that.")
        return cls(
            answer=text,
            abstained=True,
            abstention_reason=reason,
            language=language,
            trace_id=trace_id,
            metadata=dict(meta),
        )


# --------------------------------------------------------------------------- #
# Ingestion result & tombstone
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class IngestionResult:
    doc_id: str
    status: Literal["indexed", "skipped_unchanged", "quarantined", "deleted", "failed"]
    chunks_indexed: int = 0
    chunks_quarantined: int = 0
    content_hash: str | None = None
    version_id: str | None = None
    message: str | None = None


@dataclass(frozen=True)
class Tombstone:
    """Persistent deletion marker — prevents a deleted doc from being silently
    resurrected by a retry or backfill (arch flow step 13)."""

    tenant_id: str
    doc_id: str
    deleted_at: datetime
    reason: str
    source_event_id: str | None = None


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class RAGError(Exception):
    """Base class for all rag/* raised errors."""


class ConfigurationError(RAGError):
    """A required dependency/credential/model is missing at use time."""


class ValidationError(RAGError):
    """Input failed validation (length, charset, empty)."""


class SecurityViolation(RAGError):
    """An access-control invariant would be broken — fail closed."""
