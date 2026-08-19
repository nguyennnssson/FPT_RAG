"""Pydantic request/response models for the FastAPI service.

These are the HTTP contract the frontend codes against. They are intentionally
separate from the internal ``rag.schemas`` dataclasses: the API shape can stay
stable even if internal types evolve.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints

Sensitivity = Literal["public", "internal", "confidential", "restricted"]
Risk = Literal["low", "medium", "high"]
AttachmentDocId = Annotated[
    str,
    StringConstraints(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9._-]+$"),
]


class UserBody(BaseModel):
    """Optional user identity carried in the request body (fallback when the
    SSO/proxy headers are absent — e.g. local dev)."""

    tenant_id: str | None = None
    user_id: str | None = None
    principals: list[str] = Field(default_factory=list)
    classification: Sensitivity = "internal"
    session_id: str | None = None
    language: str | None = None


class QueryRequest(BaseModel):
    # Bound the body at the API edge (the pipeline also enforces a token cap);
    # the direct :8000 port bypasses nginx's body-size limit.
    query: str = Field(..., max_length=8000)
    risk: Risk = "medium"
    conversation_id: str | None = Field(default=None, min_length=36, max_length=36)
    regenerate_message_id: str | None = Field(default=None, min_length=36, max_length=36)
    # Chat attachments have already been ingested. Supplying their stable ids
    # scopes retrieval to those files so an attached screenshot cannot silently
    # lose to an unrelated document from the global corpus.
    attachment_doc_ids: list[AttachmentDocId] = Field(default_factory=list, max_length=5)
    user: UserBody | None = None


class SourceModel(BaseModel):
    label: str
    chunk_id: str
    doc_id: str
    title: str
    source_uri: str
    # Exact raw retrieved chunk used as evidence for this citation.
    text: str = ""
    section_path: list[str] = Field(default_factory=list)
    page_number: int | None = None


class QueryResponse(BaseModel):
    answer: str
    abstained: bool
    abstention_reason: str | None = None
    language: str
    model: str | None = None
    trace_id: str | None = None
    sources: list[SourceModel] = Field(default_factory=list)


class IngestRequest(BaseModel):
    # doc_id is interpolated into quarantine/tombstone filenames, so it must be
    # path-safe (no slashes, dots-only, etc.) as well as bounded.
    doc_id: str = Field(
        ..., min_length=1, max_length=256, pattern=r"^[A-Za-z0-9._-]+$",
        description="Stable, human-legible id (filename/slug), NOT a hash.",
    )
    text: str = Field(..., max_length=2_000_000)  # ~2 MB of text; guards OOM
    title: str | None = None
    source_system: str = "api"
    source_uri: str | None = None
    content_type: str = "text/plain"
    # Required and non-empty: an ACL must be an explicit decision. Pass ["*"]
    # deliberately for world-readable content — it is never the silent default.
    acl_principals: list[str] = Field(..., min_length=1)
    sensitivity: Sensitivity = "internal"
    user: UserBody | None = None


class IngestResponse(BaseModel):
    doc_id: str
    status: str
    chunks_indexed: int = 0
    chunks_quarantined: int = 0
    message: str | None = None


class DocumentModel(BaseModel):
    doc_id: str
    title: str
    source_uri: str = ""
    folder: str = "Other"
    file_type: str = "OTHER"
    status: Literal["active"] = "active"
    chunks: int = 0
    indexed_at: str = ""


class DocumentsResponse(BaseModel):
    documents: list[DocumentModel] = Field(default_factory=list)


class DocumentPreviewModel(BaseModel):
    doc_id: str
    title: str
    folder: str = "Other"
    file_type: str = "OTHER"
    indexed_at: str = ""
    content: str
    truncated: bool = False
    has_original: bool = False
    content_type: str = ""
    file_name: str = ""


class SearchRequest(BaseModel):
    """Retrieval-only search — ranked passages, no answer generation."""

    query: str = Field(..., min_length=1, max_length=8000)
    top_k: int = Field(default=10, ge=1, le=50)
    user: UserBody | None = None


class SearchResult(BaseModel):
    chunk_id: str
    doc_id: str
    title: str
    source_uri: str = ""
    section_path: list[str] = Field(default_factory=list)
    page_number: int | None = None
    text: str
    score: float


class SearchResponse(BaseModel):
    query: str
    results: list[SearchResult] = Field(default_factory=list)


class FeedbackRequest(BaseModel):
    query: str = Field(..., max_length=4000)
    rating: Literal["up", "down"]
    trace_id: str | None = Field(default=None, max_length=64)
    answer: str | None = Field(default=None, max_length=8000)
    comment: str | None = Field(default=None, max_length=2000)
    doc_ids: list[str] = Field(default_factory=list, max_length=50)
    user: UserBody | None = None


class FeedbackResponse(BaseModel):
    status: str = "recorded"


# ---------------------------------------------------------------------------
# Durable conversations and long-term user memories
# ---------------------------------------------------------------------------


class ConversationCreate(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    user: UserBody | None = None


class ConversationUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    pinned: bool | None = None
    user: UserBody | None = None


class ConversationModel(BaseModel):
    id: str
    title: str
    pinned: bool = False
    created_at: str
    updated_at: str
    deleted_at: str | None = None
    purge_after: str | None = None


class HistoryMessageModel(BaseModel):
    id: str
    role: Literal["user", "assistant"]
    content: str
    status: Literal["streaming", "complete", "error"] = "complete"
    created_at: str
    trace_id: str | None = None
    model: str | None = None
    abstained: bool = False
    abstention_reason: str | None = None
    feedback: Literal["up", "down"] | None = None
    sources: list[SourceModel] = Field(default_factory=list)


class ConversationDetail(ConversationModel):
    messages: list[HistoryMessageModel] = Field(default_factory=list)


class ConversationSummaryResponse(BaseModel):
    summary: str
    message_count: int


class ConversationsResponse(BaseModel):
    conversations: list[ConversationModel] = Field(default_factory=list)


class MemoryCreate(BaseModel):
    content: str = Field(..., min_length=2, max_length=500)
    kind: Literal["preference", "fact", "instruction"] = "preference"
    user: UserBody | None = None


class MemoryUpdate(BaseModel):
    content: str = Field(..., min_length=2, max_length=500)
    kind: Literal["preference", "fact", "instruction"]
    user: UserBody | None = None


class MemoryModel(BaseModel):
    id: str
    kind: Literal["preference", "fact", "instruction"]
    content: str
    is_explicit: bool
    confidence: float
    source_conversation_id: str | None = None
    created_at: str
    updated_at: str


class MemoriesResponse(BaseModel):
    memories: list[MemoryModel] = Field(default_factory=list)


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    version: str
    index_version: str
    embedder_backend: str
    vectorstore_backend: str
    llm_provider: str
    redis: Literal["connected", "unavailable"]
    bm25_docs: int
