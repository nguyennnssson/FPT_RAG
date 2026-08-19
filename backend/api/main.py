"""FastAPI service — the only thing the frontend is allowed to call.

Exposes the RAG backend over HTTP/SSE (arch flow "API layer"):

  POST /query          -> grounded, cited answer (blocking)
  POST /query/stream   -> Server-Sent Events, token-by-token, then a final event
  POST /ingest         -> ingest a single document (admin/document-management)
  DELETE /ingest/{id}  -> tombstone a document
  GET  /health         -> liveness + backend status (no heavy model load)

User identity for the ACL checkpoints is resolved header-first (SSO/proxy),
body-fallback (see api/auth.py). Configure with the same RAG_* env vars as the
library — e.g. RAG_EMBEDDER_BACKEND=hash + RAG_LLM_PROVIDER=extractive to run
the service offline without models or an LLM key.
"""

from __future__ import annotations

import json
import logging
import os
import re
import asyncio
import time
from contextlib import asynccontextmanager
from contextlib import suppress
from dataclasses import replace

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from rag import Ingester, RAGConfig, RAGPipeline, SourceDocument, __version__
from rag.audit import get_audit_logger
from rag.cache import get_cache
from rag.config import load_env_file
from rag.extract import ExtractionError, extract_bytes, is_supported
from rag.feedback import Feedback, get_feedback_store
from rag.observability import get_metrics
from rag.persistence import (
    NotFoundError,
    ValidationError as PersistenceValidationError,
    get_chat_repository,
    migrate_schema,
)
from rag.schemas import RagAnswer, Source, UserContext

from .auth import require_admin, resolve_user
from .schemas import (
    DocumentModel,
    DocumentPreviewModel,
    DocumentsResponse,
    ConversationCreate,
    ConversationDetail,
    ConversationModel,
    ConversationSummaryResponse,
    ConversationsResponse,
    ConversationUpdate,
    FeedbackRequest,
    FeedbackResponse,
    IngestRequest,
    IngestResponse,
    QueryRequest,
    QueryResponse,
    MemoriesResponse,
    MemoryCreate,
    MemoryModel,
    MemoryUpdate,
    SearchRequest,
    SearchResponse,
    SearchResult,
    SourceModel,
    UserBody,
)

load_env_file()

log = logging.getLogger("rag.api")

# Reject uploads larger than this (bytes) before reading them into memory.
_MAX_UPLOAD_BYTES = int(os.environ.get("RAG_MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))

# doc_id must be path-safe (interpolated into quarantine/tombstone filenames)
# and match IngestRequest's pattern ^[A-Za-z0-9._-]+$.
_DOC_ID_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _doc_id_from_filename(name: str) -> str:
    stem = os.path.basename(name or "").strip() or "document"
    safe = _DOC_ID_RE.sub("-", stem).strip("-")
    return (safe or "document")[:256]


def _stream_error_message(stage: str) -> str:
    if stage == "generation":
        return "The language model could not finish the answer."
    if stage in {"embedding", "retrieval", "authorization", "reranking", "context"}:
        return "The indexed document search could not be completed."
    return "The chat request could not be completed."


def _safe_error_detail(exc: Exception) -> str:
    """Expose useful diagnostics while redacting common credential formats."""
    detail = " ".join(str(exc).split()) or type(exc).__name__
    detail = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", detail)
    detail = re.sub(r"(?i)(api[_-]?key\s*[=:]\s*)\S+", r"\1[REDACTED]", detail)
    detail = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "[REDACTED_KEY]", detail)
    return detail[:600]

# Built once; every heavy dependency inside is still lazy.
_cfg = RAGConfig.load()
_pipeline = RAGPipeline(_cfg)
_ingester = Ingester(_cfg)
_history = get_chat_repository()


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    auto_migrate = os.environ.get("RAG_AUTO_MIGRATE", "true").strip().lower()
    if auto_migrate in ("1", "true", "yes", "on"):
        migrate_schema()
    _history.purge_deleted()

    async def purge_loop() -> None:
        while True:
            await asyncio.sleep(3600)
            await asyncio.to_thread(_history.purge_deleted)

    purge_task = asyncio.create_task(purge_loop())
    try:
        yield
    finally:
        purge_task.cancel()
        with suppress(asyncio.CancelledError):
            await purge_task

app = FastAPI(
    title="FPT RAG API",
    version=__version__,
    summary="Bilingual (VI/EN) hybrid-retrieval RAG backend.",
    lifespan=_lifespan,
)

# CORS so the (separately hosted) React frontend can call the API in dev.
# Credentials are only allowed with an explicit origin list — wildcard +
# credentials would let any website make credentialed requests (Starlette
# mirrors the Origin header in that combination).
_cors_origins = [o for o in os.environ.get("RAG_CORS_ORIGINS", "*").split(",") if o]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials="*" not in _cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Server-side floor for the risk level: the client may raise risk (stricter
# abstention) but never lower it below RAG_RISK_FLOOR.
_RISK_RANK = {"low": 0, "medium": 1, "high": 2}


def _effective_risk(requested: str) -> str:
    floor = os.environ.get("RAG_RISK_FLOOR", "low")
    if _RISK_RANK.get(requested, 1) < _RISK_RANK.get(floor, 0):
        return floor
    return requested


def _admin_gate_enabled() -> bool:
    return bool(os.environ.get("RAG_ADMIN_PRINCIPALS"))


def _require_admin_if_gated(request: Request) -> None:
    """Enforce admin on ops endpoints only when RAG_ADMIN_PRINCIPALS is set;
    open in local dev (unset) so liveness probes / quick checks still work."""
    if _admin_gate_enabled():
        require_admin(resolve_user(request, None))


def _is_admin(request: Request) -> bool:
    if not _admin_gate_enabled():
        return True
    try:
        require_admin(resolve_user(request, None))
        return True
    except Exception:
        return False


def _to_response(answer) -> QueryResponse:
    return QueryResponse(
        answer=answer.answer,
        abstained=answer.abstained,
        abstention_reason=answer.abstention_reason,
        language=answer.language,
        model=answer.model,
        trace_id=answer.trace_id,
        sources=[
            SourceModel(
                label=s.label, chunk_id=s.chunk_id, doc_id=s.doc_id, title=s.title,
                source_uri=s.source_uri, text=s.text, section_path=s.section_path,
                page_number=s.page_number,
            )
            for s in answer.sources
        ],
    )


def _conversation_user(user: UserContext, conversation_id: str | None) -> UserContext:
    """Scope short-term follow-up memory to the durable conversation id."""
    if not conversation_id:
        return user
    _history.get_conversation(user, conversation_id)
    return replace(user, session_id=conversation_id)


def _answer_from_event(event: dict) -> RagAnswer:
    return RagAnswer(
        answer=event.get("answer", ""),
        abstained=bool(event.get("abstained", False)),
        abstention_reason=event.get("abstention_reason"),
        language=event.get("language", "en"),
        model=event.get("model"),
        trace_id=event.get("trace_id"),
        sources=[
            Source(
                label=s["label"], chunk_id=s["chunk_id"], doc_id=s["doc_id"],
                title=s.get("title", s["doc_id"]), source_uri=s.get("source_uri", ""),
                text=s.get("text", ""), section_path=s.get("section_path", []),
                page_number=s.get("page_number"),
            )
            for s in event.get("sources", [])
        ],
        metadata={"persisted_from_stream": True},
    )


def _demo_user() -> UserBody:
    return UserBody(
        tenant_id="demo",
        user_id="web-user",
        principals=["group:all-employees"],
        classification="internal",
    )


def _persistence_error(exc: Exception) -> HTTPException:
    if isinstance(exc, NotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=422, detail=str(exc))


_ATTACHMENT_MARKER_RE = re.compile(r"\s*\[\[attachment:([A-Za-z0-9._-]+)\]\]")


def _stored_query(query: str, attachment_doc_ids: list[str]) -> str:
    """Persist attachment identity without mixing it into retrieval text."""
    markers = "".join(
        f"\n\n[[attachment:{doc_id}]]"
        for doc_id in dict.fromkeys(attachment_doc_ids)
    )
    return query + markers


def _query_and_attachments(stored: str) -> tuple[str, list[str]]:
    attachments = _ATTACHMENT_MARKER_RE.findall(stored)
    return _ATTACHMENT_MARKER_RE.sub("", stored).strip(), list(dict.fromkeys(attachments))


_CITATION_META_TERM_RE = re.compile(
    r"\b(source|sources|citation|citations|nguồn|trích\s*dẫn)\b", re.I
)
_CITATION_META_CONTEXT_RE = re.compile(
    r"(source|nguồn)\s*\d+|trích\s*dẫn|không\s*(?:có|hiện)|bị\s*(?:thiếu|bỏ)|"
    r"output|hiển\s*thị|đánh\s*số|missing|skipped|shown|display|number",
    re.I,
)


def _citation_status_answer(query: str, messages: list[dict]) -> RagAnswer | None:
    """Answer questions about the previous answer's citation UI deterministically.

    Running a document search for "why is source 3 missing?" made the model
    describe newly retrieved S3/S4 chunks as if they were the prior UI state.
    The durable assistant message already contains the authoritative labels, so
    use that metadata and do not invent a document-grounded explanation.
    """
    if not _CITATION_META_TERM_RE.search(query) or not _CITATION_META_CONTEXT_RE.search(query):
        return None
    previous = next(
        (
            message for message in reversed(messages)
            if message.get("role") == "assistant" and message.get("status") == "complete"
        ),
        None,
    )
    if previous is None:
        return None
    labels = list(dict.fromkeys(
        str(source.get("label", "")).upper()
        for source in previous.get("sources", [])
        if re.fullmatch(r"S\d+", str(source.get("label", "")).upper())
    ))
    is_vi = bool(re.search(r"[À-ỹ]|\b(nguồn|tại sao|không|hiện|trích dẫn)\b", query, re.I))
    if not labels:
        text = (
            "Câu trả lời ngay trước đó không có nguồn trích dẫn nào được trả về."
            if is_vi else
            "The immediately preceding answer did not return any cited sources."
        )
    else:
        expected = [f"S{index}" for index in range(1, len(labels) + 1)]
        label_list = ", ".join(labels)
        if labels != expected:
            text = (
                f"Bạn nhận xét đúng: câu trả lời trước có {len(labels)} nguồn được trích dẫn, "
                f"nhưng nhãn context nội bộ là {label_list}. Nhãn bị bỏ qua không phải là "
                "một thẻ nguồn bị mất; đoạn context đó không được câu trả lời trích dẫn nên "
                f"không được trả về trong danh sách nguồn. Giao diện phải đánh số {len(labels)} "
                f"nguồn đang hiển thị liên tục từ 1 đến {len(labels)}."
                if is_vi else
                f"You are right: the previous answer cited {len(labels)} sources, while its "
                f"internal context labels were {label_list}. A skipped label is not a missing "
                "source card; that context block was not cited and therefore was not returned. "
                f"The UI must number the {len(labels)} displayed sources contiguously from 1 "
                f"through {len(labels)}."
            )
        else:
            text = (
                f"Câu trả lời trước có {len(labels)} nguồn và chúng đã được đánh số liên tục "
                f"từ 1 đến {len(labels)}."
                if is_vi else
                f"The previous answer has {len(labels)} sources, numbered contiguously from 1 "
                f"through {len(labels)}."
            )
    return RagAnswer(
        answer=text,
        language="vi" if is_vi else "en",
        trace_id=os.urandom(8).hex(),
        metadata={"provider": "conversation_citation_status"},
    )


def _conversation_citation_status(
    user: UserContext,
    conversation_id: str | None,
    query: str,
    attachment_doc_ids: list[str],
) -> RagAnswer | None:
    # An attached screenshot is new evidence and must be inspected; do not
    # replace that request with a transcript-only status response.
    if not conversation_id or attachment_doc_ids:
        return None
    detail = _history.get_conversation(user, conversation_id)
    return _citation_status_answer(query, detail.get("messages", []))


def _record_citation_status(user: UserContext, query: str, answer: RagAnswer) -> None:
    get_audit_logger().log_query(
        trace_id=answer.trace_id or "citation-status",
        user=user,
        query=query,
        decision="answered",
        answer=answer.answer,
        language=answer.language,
        detail={"provider": "conversation_citation_status"},
    )
    get_metrics().record("answered", 0, answer.language, citation_valid=True)


@app.get("/health")
def health(request: Request) -> dict:
    """Liveness check. Unauthenticated callers get only {status, version}; the
    full backend detail (which reveals config + index size) is admin-only when
    the admin gate is enabled. Never triggers a model load."""
    if not _is_admin(request):
        return {"status": "ok", "version": __version__}
    cache = get_cache()
    from rag.vectorstore import BM25Store

    return {
        "status": "ok",
        "version": __version__,
        "index_version": _cfg.versions.index_version,
        "embedder_backend": os.environ.get("RAG_EMBEDDER_BACKEND", "auto"),
        "vectorstore_backend": os.environ.get("RAG_VECTORSTORE_BACKEND", "auto"),
        "llm_provider": _cfg.generator.provider,
        "cache_backend": cache.backend_name,
        "memory_backend": _cfg.memory.backend,
        "bm25_docs": BM25Store(_cfg).size,
    }


@app.post("/query", response_model=QueryResponse)
def query(req: QueryRequest, request: Request) -> QueryResponse:
    user = resolve_user(request, req.user)
    assistant_message_id: str | None = None
    effective_query = req.query
    attachment_doc_ids = list(req.attachment_doc_ids)
    citation_status: RagAnswer | None = None
    try:
        user = _conversation_user(user, req.conversation_id)
        if req.regenerate_message_id and not req.conversation_id:
            raise PersistenceValidationError(
                "Regeneration requires a conversation id."
            )
        if req.conversation_id:
            if req.regenerate_message_id:
                assistant_message_id = req.regenerate_message_id
                stored_query = _history.regeneration_query(
                    user, req.conversation_id, assistant_message_id
                )
                effective_query, stored_attachments = _query_and_attachments(stored_query)
                attachment_doc_ids = attachment_doc_ids or stored_attachments
            else:
                citation_status = _conversation_citation_status(
                    user, req.conversation_id, req.query, attachment_doc_ids
                )
                turn = _history.start_turn(
                    user, req.conversation_id,
                    _stored_query(req.query, attachment_doc_ids),
                )
                assistant_message_id = turn["assistant_message"]["id"]
        memories = _history.memory_context(user)
    except (NotFoundError, PersistenceValidationError) as exc:
        raise _persistence_error(exc) from exc
    try:
        answer = citation_status or _pipeline.query(
            effective_query,
            user,
            risk=_effective_risk(req.risk),
            user_memory=memories,
            bypass_cache=bool(req.regenerate_message_id),
            attachment_doc_ids=attachment_doc_ids,
        )
        if citation_status is not None:
            _record_citation_status(user, effective_query, answer)
        if req.conversation_id and assistant_message_id:
            _history.complete_turn(
                user, req.conversation_id, assistant_message_id, answer
            )
    except Exception:
        if req.conversation_id and assistant_message_id:
            with suppress(Exception):
                _history.fail_turn(user, req.conversation_id, assistant_message_id)
        raise
    return _to_response(answer)


@app.post("/query/stream")
def query_stream(req: QueryRequest, request: Request) -> StreamingResponse:
    user = resolve_user(request, req.user)
    assistant_message_id: str | None = None
    effective_query = req.query
    attachment_doc_ids = list(req.attachment_doc_ids)
    citation_status: RagAnswer | None = None
    try:
        user = _conversation_user(user, req.conversation_id)
        if req.regenerate_message_id and not req.conversation_id:
            raise PersistenceValidationError(
                "Regeneration requires a conversation id."
            )
        if req.conversation_id:
            if req.regenerate_message_id:
                assistant_message_id = req.regenerate_message_id
                stored_query = _history.regeneration_query(
                    user, req.conversation_id, assistant_message_id
                )
                effective_query, stored_attachments = _query_and_attachments(stored_query)
                attachment_doc_ids = attachment_doc_ids or stored_attachments
            else:
                citation_status = _conversation_citation_status(
                    user, req.conversation_id, req.query, attachment_doc_ids
                )
                turn = _history.start_turn(
                    user, req.conversation_id,
                    _stored_query(req.query, attachment_doc_ids),
                )
                assistant_message_id = turn["assistant_message"]["id"]
        memories = _history.memory_context(user)
    except (NotFoundError, PersistenceValidationError) as exc:
        raise _persistence_error(exc) from exc

    def event_source():
        # If the client disconnects mid-stream, the pipeline's own _finalize
        # (audit/metrics/memory) never runs — Starlette throws GeneratorExit at
        # the yield. Record the abort here so aborted streams aren't invisible.
        completed = False
        terminal_error = False
        current_stage = "request"
        stream_start = time.perf_counter()
        try:
            if citation_status is not None:
                event = _answer_event(citation_status)
                _record_citation_status(user, effective_query, citation_status)
                if req.conversation_id and assistant_message_id:
                    completed_message = _history.complete_turn(
                        user, req.conversation_id, assistant_message_id, citation_status,
                    )
                    event["message_id"] = completed_message["id"]
                completed = True
                yield f"data: {json.dumps({'type': 'delta', 'text': citation_status.answer}, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'type': 'final', **event}, ensure_ascii=False)}\n\n"
                yield "event: end\ndata: {}\n\n"
                return
            for event in _pipeline.query_stream(
                effective_query,
                user,
                risk=_effective_risk(req.risk),
                user_memory=memories,
                bypass_cache=bool(req.regenerate_message_id),
                attachment_doc_ids=attachment_doc_ids,
            ):
                if event.get("type") == "progress":
                    current_stage = str(event.get("stage") or current_stage)
                if event.get("type") == "final":
                    if req.conversation_id and assistant_message_id:
                        completed_message = _history.complete_turn(
                            user,
                            req.conversation_id,
                            assistant_message_id,
                            _answer_from_event(event),
                        )
                        event["message_id"] = completed_message["id"]
                    completed = True
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            yield "event: end\ndata: {}\n\n"
        except Exception as exc:
            # Headers have already been sent for a streaming response. Convert
            # provider/pipeline failures into a terminal SSE event so the UI can
            # stop its spinner and show an actionable error instead of receiving
            # an unexplained, truncated HTTP 200 body.
            terminal_error = True
            log.exception("Chat stream failed during %s", current_stage)
            if req.conversation_id and assistant_message_id:
                with suppress(Exception):
                    _history.fail_turn(user, req.conversation_id, assistant_message_id)
            elapsed_ms = round((time.perf_counter() - stream_start) * 1000)
            get_metrics().record("error", elapsed_ms)
            get_audit_logger().log_query(
                trace_id="stream-error", user=user, query=effective_query,
                decision="error", latency_ms=elapsed_ms,
                detail={"stage": current_stage, "error_type": type(exc).__name__},
            )
            error_event = {
                "type": "error",
                "stage": current_stage,
                "message": _stream_error_message(current_stage),
                "detail": _safe_error_detail(exc),
                "retryable": True,
                "elapsed_ms": elapsed_ms,
            }
            if assistant_message_id:
                error_event["message_id"] = assistant_message_id
            yield f"data: {json.dumps(error_event, ensure_ascii=False)}\n\n"
            yield "event: end\ndata: {}\n\n"
        finally:
            if not completed and not terminal_error:
                if req.conversation_id and assistant_message_id:
                    with suppress(Exception):
                        _history.fail_turn(
                            user, req.conversation_id, assistant_message_id
                        )
                get_metrics().record("aborted")
                get_audit_logger().log_query(
                    trace_id="stream-aborted", user=user, query=effective_query,
                    decision="aborted",
                )

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/ingest", response_model=IngestResponse)
def ingest(req: IngestRequest, request: Request) -> IngestResponse:
    user = resolve_user(request, req.user or UserBody())
    require_admin(user)
    doc = SourceDocument(
        tenant_id=user.tenant_id,
        doc_id=req.doc_id,
        source_system=req.source_system,
        source_uri=req.source_uri or f"api://{req.doc_id}",
        title=req.title or req.doc_id,
        content_type=req.content_type,
        text=req.text,
        acl_principals=req.acl_principals,
        sensitivity=req.sensitivity,
    )
    res = _ingester.ingest(doc)
    return IngestResponse(
        doc_id=res.doc_id, status=res.status, chunks_indexed=res.chunks_indexed,
        chunks_quarantined=res.chunks_quarantined, message=res.message,
    )


@app.post("/upload", response_model=IngestResponse)
async def upload(
    request: Request,
    file: UploadFile = File(...),
    title: str | None = Form(default=None),
    sensitivity: str = Form(default="internal"),
    acl: str = Form(default="*"),
) -> IngestResponse:
    """Upload a real file (.docx/.pdf/.md/.csv/.html/...), extract its text
    server-side, and run the full ingestion pipeline. This is the binary-capable
    counterpart to /ingest (which takes already-extracted text)."""
    # Header-first identity; demo fallback so local dev works without SSO.
    user = resolve_user(request, _demo_user())
    require_admin(user)

    filename = file.filename or "document"
    if not is_supported(filename):
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported file type for '{filename}'. Supported: "
                   ".txt, .md, .rst, .pdf, .docx, .pptx, .html, .csv, "
                   ".png, .jpg, .webp, .gif, .bmp, .tif",
        )

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    if len(data) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds the {_MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit.",
        )

    try:
        # Parsing/OCR can be CPU-heavy. Keep it off the async event loop so one
        # scanned PDF does not freeze unrelated API requests.
        text, content_type = await asyncio.to_thread(extract_bytes, filename, data)
    except ExtractionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not text.strip():
        raise HTTPException(
            status_code=422,
            detail=f"No extractable text or visual content in '{filename}'.",
        )

    doc_id = _doc_id_from_filename(filename)
    principals = [p.strip() for p in acl.split(",") if p.strip()] or ["*"]
    doc = SourceDocument(
        tenant_id=user.tenant_id,
        doc_id=doc_id,
        source_system="upload",
        source_uri=f"upload://{filename}",
        title=title or filename,
        content_type=content_type,
        text=text,
        acl_principals=principals,
        sensitivity=sensitivity if sensitivity in
        ("public", "internal", "confidential", "restricted") else "internal",
    )
    res = _ingester.ingest(doc)
    if res.status in ("indexed", "skipped_unchanged"):
        # Retrieval uses extracted text; faithful preview/download uses these
        # original bytes through the ACL-checked endpoint below.
        _ingester.store_original(
            user.tenant_id, doc_id, filename, content_type, data
        )
    return IngestResponse(
        doc_id=res.doc_id, status=res.status, chunks_indexed=res.chunks_indexed,
        chunks_quarantined=res.chunks_quarantined, message=res.message,
    )


@app.post("/search", response_model=SearchResponse)
def search(req: SearchRequest, request: Request) -> SearchResponse:
    """Retrieval-only search: dense + BM25 + fusion + ACL + rerank, returning
    ranked passages WITHOUT generating an answer. Powers the "search inside
    documents" mode on the Documents page."""
    user = resolve_user(
        request, req.user or _demo_user()
    )
    candidates = _pipeline.rank_candidates(req.query, user, top_k=req.top_k)
    results = [
        SearchResult(
            chunk_id=c.chunk.chunk_id,
            doc_id=c.chunk.doc_id,
            title=c.chunk.title or c.chunk.doc_id,
            source_uri=c.chunk.source_uri or "",
            section_path=c.chunk.section_path,
            page_number=c.chunk.page_number,
            text=c.chunk.text,
            score=round(float(c.rerank_score if c.rerank_score is not None else c.score), 4),
        )
        for c in candidates
    ]
    return SearchResponse(query=req.query, results=results)


@app.get("/documents", response_model=DocumentsResponse)
def documents(request: Request) -> DocumentsResponse:
    """List the documents indexed for the caller's tenant, so the UI reflects
    the real index. Open in dev; identity comes from headers or body-fallback."""
    user = resolve_user(request, UserBody(tenant_id="demo", user_id="viewer"))
    docs = _ingester.list_documents(user.tenant_id)
    return DocumentsResponse(documents=[DocumentModel(**d) for d in docs])


@app.get("/documents/{doc_id}/preview", response_model=DocumentPreviewModel)
def document_preview(doc_id: str, request: Request) -> DocumentPreviewModel:
    """Preview extracted text after applying the same ACL checks as retrieval."""
    user = resolve_user(request, UserBody(tenant_id="demo", user_id="viewer"))
    preview = _ingester.document_preview(user, doc_id)
    if preview is None:
        raise HTTPException(status_code=404, detail="Document preview not found.")
    return DocumentPreviewModel(**preview)


@app.get("/documents/{doc_id}/file")
def document_file(doc_id: str, request: Request) -> FileResponse:
    """Serve an original upload inline after applying document ACL checks."""
    user = resolve_user(request, UserBody(tenant_id="demo", user_id="viewer"))
    original = _ingester.document_original(user, doc_id)
    if original is None:
        # Match preview semantics: do not disclose existence on ACL denial.
        raise HTTPException(status_code=404, detail="Original document not found.")
    return FileResponse(
        path=original.path,
        media_type=original.content_type,
        filename=original.filename,
        content_disposition_type="inline",
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.delete("/ingest/{doc_id}", response_model=IngestResponse)
def delete(doc_id: str, request: Request) -> IngestResponse:
    user = resolve_user(request, UserBody())
    require_admin(user)
    res = _ingester.delete(user.tenant_id, doc_id)
    return IngestResponse(doc_id=res.doc_id, status=res.status, message=res.message)


@app.post("/feedback", response_model=FeedbackResponse)
def feedback(req: FeedbackRequest, request: Request) -> FeedbackResponse:
    """Thumbs up/down — down-votes become golden-set candidates (step 33)."""
    user = resolve_user(request, req.user or UserBody())
    get_feedback_store().record(Feedback(
        trace_id=req.trace_id, tenant_id=user.tenant_id, user_id=user.user_id,
        query=req.query, rating=req.rating, answer=req.answer,
        comment=req.comment, doc_ids=req.doc_ids,
    ))
    _history.set_feedback(user, req.trace_id, req.rating)
    return FeedbackResponse()


# ---------------------------------------------------------------------------
# Durable conversation history
# ---------------------------------------------------------------------------


@app.get("/conversations", response_model=ConversationsResponse)
def conversations(
    request: Request, limit: int = 100, offset: int = 0, deleted: bool = False
) -> ConversationsResponse:
    user = resolve_user(request, _demo_user())
    items = _history.list_conversations(
        user, limit=max(1, min(limit, 200)), offset=max(0, offset), deleted=deleted
    )
    return ConversationsResponse(conversations=[ConversationModel(**item) for item in items])


@app.post("/conversations", response_model=ConversationModel, status_code=201)
def create_conversation(req: ConversationCreate, request: Request) -> ConversationModel:
    user = resolve_user(request, req.user or _demo_user())
    return ConversationModel(**_history.create_conversation(user, req.title))


@app.get("/conversations/{conversation_id}", response_model=ConversationDetail)
def conversation_detail(conversation_id: str, request: Request) -> ConversationDetail:
    user = resolve_user(request, _demo_user())
    try:
        detail = _history.get_conversation(user, conversation_id)
        missing = [
            source["chunk_id"]
            for message in detail.get("messages", [])
            for source in message.get("sources", [])
            if not source.get("text")
        ]
        if missing:
            hydrated = _ingester.cited_texts(user, missing)
            for message in detail.get("messages", []):
                for source in message.get("sources", []):
                    if not source.get("text"):
                        source["text"] = hydrated.get(source["chunk_id"], "")
        return ConversationDetail(**detail)
    except (NotFoundError, PersistenceValidationError) as exc:
        raise _persistence_error(exc) from exc


@app.post(
    "/conversations/{conversation_id}/summary",
    response_model=ConversationSummaryResponse,
)
def conversation_summary(
    conversation_id: str, request: Request
) -> ConversationSummaryResponse:
    """Generate an on-demand overview without adding a message or memory."""
    user = resolve_user(request, _demo_user())
    try:
        detail = _history.get_conversation(user, conversation_id)
    except (NotFoundError, PersistenceValidationError) as exc:
        raise _persistence_error(exc) from exc
    messages = [
        {"role": item["role"], "content": item["content"]}
        for item in detail.get("messages", [])
        if item.get("status") == "complete" and item.get("content", "").strip()
    ]
    return ConversationSummaryResponse(
        summary=_pipeline.summarize_conversation(messages, user),
        message_count=len(messages),
    )


@app.patch("/conversations/{conversation_id}", response_model=ConversationModel)
def update_conversation(
    conversation_id: str, req: ConversationUpdate, request: Request
) -> ConversationModel:
    user = resolve_user(request, req.user or _demo_user())
    try:
        item = _history.update_conversation(
            user, conversation_id, title=req.title, pinned=req.pinned
        )
        return ConversationModel(**item)
    except (NotFoundError, PersistenceValidationError) as exc:
        raise _persistence_error(exc) from exc


@app.delete("/conversations/{conversation_id}", status_code=204)
def delete_conversation(conversation_id: str, request: Request) -> None:
    user = resolve_user(request, _demo_user())
    try:
        _history.soft_delete_conversation(user, conversation_id, retention_days=30)
    except (NotFoundError, PersistenceValidationError) as exc:
        raise _persistence_error(exc) from exc


@app.post("/conversations/{conversation_id}/restore", response_model=ConversationModel)
def restore_conversation(conversation_id: str, request: Request) -> ConversationModel:
    user = resolve_user(request, _demo_user())
    try:
        return ConversationModel(**_history.restore_conversation(user, conversation_id))
    except (NotFoundError, PersistenceValidationError) as exc:
        raise _persistence_error(exc) from exc


# ---------------------------------------------------------------------------
# Long-term memory management
# ---------------------------------------------------------------------------


@app.get("/memories", response_model=MemoriesResponse)
def memories(request: Request) -> MemoriesResponse:
    user = resolve_user(request, _demo_user())
    return MemoriesResponse(
        memories=[MemoryModel(**item) for item in _history.list_memories(user)]
    )


@app.post("/memories", response_model=MemoryModel, status_code=201)
def create_memory(req: MemoryCreate, request: Request) -> MemoryModel:
    user = resolve_user(request, req.user or _demo_user())
    try:
        return MemoryModel(**_history.create_memory(user, req.content, kind=req.kind))
    except (NotFoundError, PersistenceValidationError) as exc:
        raise _persistence_error(exc) from exc


@app.patch("/memories/{memory_id}", response_model=MemoryModel)
def update_memory(memory_id: str, req: MemoryUpdate, request: Request) -> MemoryModel:
    user = resolve_user(request, req.user or _demo_user())
    try:
        return MemoryModel(
            **_history.update_memory(user, memory_id, content=req.content, kind=req.kind)
        )
    except (NotFoundError, PersistenceValidationError) as exc:
        raise _persistence_error(exc) from exc


@app.delete("/memories/{memory_id}", status_code=204)
def delete_memory(memory_id: str, request: Request) -> None:
    user = resolve_user(request, _demo_user())
    try:
        _history.delete_memory(user, memory_id)
    except (NotFoundError, PersistenceValidationError) as exc:
        raise _persistence_error(exc) from exc


@app.get("/metrics")
def metrics(request: Request) -> dict:
    """Operational SLIs: request counts by decision, latency percentiles, and
    derived rates. Admin-only when RAG_ADMIN_PRINCIPALS is set (open in dev)."""
    _require_admin_if_gated(request)
    return get_metrics().snapshot()


@app.exception_handler(Exception)
async def _on_unhandled_error(request: Request, exc: Exception) -> JSONResponse:
    """Record unexpected failures as an error SLI and return a clean 500."""
    get_metrics().record("error")
    return JSONResponse(status_code=500, content={"detail": "Internal server error."})


@app.get("/")
def root() -> dict:
    return {
        "service": "FPT RAG API",
        "version": __version__,
        "endpoints": ["/query", "/query/stream", "/conversations", "/memories",
                      "/ingest", "/feedback", "/health", "/metrics", "/docs"],
    }
