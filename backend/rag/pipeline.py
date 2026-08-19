"""RAGPipeline — the single entry point for the query read path.

One orchestrator means one place to add auth, caching, logging and abstention,
instead of every caller reimplementing the flow. Execution order (arch flow
steps 15-29):

  input validation (16) -> session memory rewrite (17)
    -> query language detection (18) -> query embed, +prefix (19)
    -> [CP1] dense + BM25 retrieval (20-21) -> RRF fusion (22)
    -> [CP2] ACL filter (23) -> rerank (24) -> score filter (25)
    -> context assembly [CP3] (26) -> generate (27)
    -> citation validation (28) -> structured response (29)

Cross-cutting: the query embedding and the final answer are cached (ACL-scoped
for the answer), every request is audited (including abstains and rejects), and
completed turns are written to session memory (text only).
"""

from __future__ import annotations

import logging
import hashlib
import time

from .audit import AuditLogger, get_audit_logger
from .cache import Cache, get_cache
from .config import RAGConfig, get_config
from .context import ContextAssembler
from .embedder import Embedder, get_embedder
from .fusion import RRFFuser
from .generator import Generator, cited_source_ids
from .language import LanguageDetector, get_detector
from .memory import SessionMemory
from .observability import MetricsCollector, get_metrics
from .reranker import Reranker
from .retriever import Retriever
from .schemas import CitedClaim, RagAnswer, Source, UserContext, utcnow
from .security import cp2_acl_filter

log = logging.getLogger("rag.pipeline")


class RAGPipeline:
    def __init__(
        self,
        config: RAGConfig | None = None,
        *,
        embedder: Embedder | None = None,
        retriever: Retriever | None = None,
        reranker: Reranker | None = None,
        generator: Generator | None = None,
        memory: SessionMemory | None = None,
        cache: Cache | None = None,
        audit: AuditLogger | None = None,
        language: LanguageDetector | None = None,
        metrics: MetricsCollector | None = None,
    ) -> None:
        self._cfg = config or get_config()
        self._embedder = embedder or get_embedder()
        self._retriever = retriever or Retriever(config=self._cfg)
        self._fuser = RRFFuser(self._cfg.retrieval)
        self._reranker = reranker or Reranker()
        self._context = ContextAssembler(self._cfg)
        self._generator = generator or Generator(self._cfg.generator)
        self._memory = memory or SessionMemory()
        self._cache = cache or get_cache()
        self._audit = audit or get_audit_logger()
        self._lang = language or get_detector()
        self._metrics = metrics or get_metrics()

        # Wire the LLM-backed follow-up condenser into session memory.
        self._memory.set_condenser(self._generator.condense)

    # ---------------------------------------------------------------- query  #
    def query(
        self,
        query: str,
        user: UserContext,
        *,
        risk: str = "medium",
        user_memory: list[str] | None = None,
        bypass_cache: bool = False,
        attachment_doc_ids: list[str] | None = None,
    ) -> RagAnswer:
        """Blocking query — returns the full grounded answer."""
        start = time.perf_counter()
        trace_id = _trace_id(user, query, start)

        early, ctx = self._prepare(
            query, user, risk, start, trace_id, user_memory or [],
            bypass_cache=bypass_cache, attachment_doc_ids=attachment_doc_ids or [],
        )
        if early is not None:
            return early

        # (27-29) Generate grounded, cited, validated, structured answer.
        answer = self._generator.generate(
            ctx["rewritten"], ctx["assembly"], user,
            language=ctx["language"], trace_id=trace_id,
            user_memory=ctx["user_memory"],
        )
        self._finalize(user, query, answer, ctx, start, trace_id)
        return answer

    def query_stream(
        self,
        query: str,
        user: UserContext,
        *,
        risk: str = "medium",
        user_memory: list[str] | None = None,
        bypass_cache: bool = False,
        attachment_doc_ids: list[str] | None = None,
    ):
        """Streaming query with observable preparation and generation stages.

        Progress events are deliberately operational rather than chain-of-thought:
        they expose what subsystem is running, safe command summaries, candidate
        counts and elapsed time without exposing prompts, credentials or hidden
        model reasoning.
        """
        start = time.perf_counter()
        trace_id = _trace_id(user, query, start)

        preparation = self._prepare_events(
            query, user, risk, start, trace_id, user_memory or [],
            bypass_cache=bypass_cache, attachment_doc_ids=attachment_doc_ids or [],
        )
        while True:
            try:
                yield next(preparation)
            except StopIteration as done:
                early, ctx = done.value
                break
        if early is not None:
            yield {"type": "delta", "text": early.answer}
            yield {"type": "final", **_answer_event(early)}
            return

        detail, detail_type = _generation_activity(self._cfg)
        yield _progress_event(
            "generation", "running", "Running language model", detail,
            start, trace_id, detail_type=detail_type,
        )
        final: RagAnswer | None = None
        for kind, payload in self._generator.generate_stream(
            ctx["rewritten"], ctx["assembly"], user,
            language=ctx["language"], trace_id=trace_id,
            user_memory=ctx["user_memory"],
        ):
            if kind == "delta":
                yield {"type": "delta", "text": payload}
            else:
                final = payload
        if final is None:  # pragma: no cover - defensive
            final = RagAnswer.abstain("Generation produced no output.",
                                      language=ctx["language"], trace_id=trace_id)
        yield _progress_event(
            "generation", "complete", "Language model finished",
            f"Generated {len(final.answer)} characters.", start, trace_id,
        )
        self._finalize(user, query, final, ctx, start, trace_id)
        yield {"type": "final", **_answer_event(final)}

    # ------------------------------------------------------------ evaluation #
    def rank_candidates(
        self, query, user, *, top_k: int = 10,
        attachment_doc_ids: list[str] | None = None,
    ):
        """Return the reranked candidate list (post-CP2, pre-abstain) for a
        query. Used by the evaluation harness to measure retrieval quality
        (Recall@k / nDCG@k / MRR) independently of the abstention decision.
        Runs the same path as query() up to the reranker, but never generates."""
        rewritten = self._memory.rewrite(query, user)
        language = self._detect_language(query, user)
        query_embedding = self._embed_query(rewritten)
        bundle = self._retriever.retrieve(
            rewritten, query_embedding, user, language,
            doc_ids=set(attachment_doc_ids or []) or None,
        )
        fused = self._fuser.fuse(bundle)
        authorized = cp2_acl_filter(fused, user)
        return self._reranker.rerank(rewritten, authorized, top_n=top_k)

    def summarize_conversation(
        self, messages: list[dict[str, str]], user: UserContext
    ) -> str:
        """Summarize an existing transcript without invoking the RAG read path."""
        last_user_text = next(
            (
                item.get("content", "")
                for item in reversed(messages)
                if item.get("role") == "user" and item.get("content", "").strip()
            ),
            "",
        )
        language = self._detect_language(last_user_text, user) if last_user_text else "en"
        return self._generator.summarize_conversation(messages, language)

    # ---------------------------------------------------- shared prep/finalize #
    def _prepare(
        self, query, user, risk, start, trace_id, user_memory, *, bypass_cache=False,
        attachment_doc_ids=None,
    ):
        """Run steps 16-26. Returns (early_answer, None) for a terminal outcome
        (reject / cache hit / abstain, already audited) or (None, ctx) to
        proceed to generation."""
        preparation = self._prepare_events(
            query, user, risk, start, trace_id, user_memory,
            bypass_cache=bypass_cache, attachment_doc_ids=attachment_doc_ids or [],
        )
        while True:
            try:
                next(preparation)
            except StopIteration as done:
                return done.value

    def _prepare_events(
        self, query, user, risk, start, trace_id, user_memory, *, bypass_cache=False,
        attachment_doc_ids=None,
    ):
        """Run steps 16-26 and yield safe, user-visible progress events."""
        # (16) Input validation.
        yield _progress_event(
            "validation", "running", "Validating question", None,
            start, trace_id,
        )
        errors = self._validate(query)
        if errors:
            yield _progress_event(
                "validation", "error", "Question could not be processed",
                "; ".join(errors), start, trace_id,
            )
            self._audit.log_query(
                trace_id=trace_id, user=user, query=query, decision="rejected",
                detail={"errors": errors},
            )
            self._metrics.record("rejected", _elapsed_ms(start),
                                 user.language or self._cfg.language.default_language)
            return RagAnswer.abstain(
                "; ".join(errors),
                language=user.language or self._cfg.language.default_language,
                trace_id=trace_id,
            ), None
        yield _progress_event(
            "validation", "complete", "Question validated", None,
            start, trace_id,
        )

        # (17) Session memory — rewrite follow-ups into standalone queries.
        yield _progress_event(
            "memory", "running", "Checking conversation context", None,
            start, trace_id,
        )
        rewritten = self._memory.rewrite(query, user)
        # (18) Query language detection (confidence-gated + default fallback).
        language = self._detect_language(query, user)
        memory_detail = (
            "Follow-up rewritten as a standalone search query."
            if rewritten != query else "No query rewrite was needed."
        )
        yield _progress_event(
            "memory", "complete", "Conversation context checked",
            f"{memory_detail} Language: {language}.", start, trace_id,
        )

        # Answer cache (ACL-scoped, generation-stamped so ingest/delete for the
        # tenant invalidates it) — repeat questions skip embed/retrieve/gen.
        yield _progress_event(
            "cache", "running", "Checking answer cache", None,
            start, trace_id,
        )
        gen = self._cache.tenant_generation(user.tenant_id)
        memory_stamp = hashlib.sha256(
            "\n".join(user_memory).encode("utf-8")
        ).hexdigest()[:16]
        attachment_ids = sorted(set(attachment_doc_ids or []))
        attachment_stamp = ",".join(attachment_ids) or "-"
        cache_key = (
            f"{rewritten}|{risk}|{language}|g{gen}|m{memory_stamp}"
            f"|attachments:{attachment_stamp}"
        )
        cached = None if bypass_cache else self._cache.get_answer(user, cache_key)
        if cached is not None:
            yield _progress_event(
                "cache", "complete", "Cached answer found",
                "Skipped embedding, retrieval, reranking and model generation.",
                start, trace_id,
            )
            answer = _answer_from_cache(cached, trace_id)
            self._audit.log_query(
                trace_id=trace_id, user=user, query=query, decision="cache_hit",
                answer=answer.answer, language=language, latency_ms=_elapsed_ms(start),
            )
            self._metrics.record("cache_hit", _elapsed_ms(start), language)
            return answer, None
        if bypass_cache:
            yield _progress_event(
                "cache", "complete", "Generating a fresh answer",
                "Skipped the previous cached answer for regeneration.",
                start, trace_id,
            )
        else:
            yield _progress_event(
                "cache", "complete", "No cached answer", "Running a fresh search.",
                start, trace_id,
            )

        # (19) Query embed (asymmetric prefix applied inside the embedder).
        yield _progress_event(
            "embedding", "running", "Encoding the question",
            "Creating a query vector compatible with the existing index.",
            start, trace_id,
        )
        query_embedding = self._embed_query(rewritten)
        yield _progress_event(
            "embedding", "complete", "Question encoded",
            f"Vector dimensions: {len(query_embedding)}.", start, trace_id,
        )
        # (20-21) CP1 scalar pre-filter + dense/BM25 retrieval.
        yield _progress_event(
            "retrieval", "running", "Searching indexed documents",
            (
                f"Searching only the {len(attachment_ids)} attached document(s)."
                if attachment_ids
                else "Running semantic and keyword retrieval over the existing index."
            ),
            start, trace_id,
        )
        bundle = self._retriever.retrieve(
            rewritten, query_embedding, user, language,
            doc_ids=set(attachment_ids) or None,
        )
        yield _progress_event(
            "retrieval", "complete", "Indexed search complete",
            f"Found {len(bundle.dense)} semantic and {len(bundle.bm25)} keyword candidates.",
            start, trace_id,
        )
        # (22) RRF fusion.
        fused = self._fuser.fuse(bundle)
        # (23) CP2 — ACL filter on acl_principals, before the reranker.
        yield _progress_event(
            "authorization", "running", "Applying document permissions",
            f"Checking {len(fused)} fused candidates against access controls.",
            start, trace_id,
        )
        authorized = cp2_acl_filter(fused, user)
        yield _progress_event(
            "authorization", "complete", "Document permissions applied",
            f"{len(authorized)} authorized candidates remain.", start, trace_id,
        )
        # (24) Rerank precision pass.
        yield _progress_event(
            "reranking", "running", "Ranking evidence",
            "Scoring authorized candidates for relevance.", start, trace_id,
        )
        reranked = self._reranker.rerank(rewritten, authorized)
        yield _progress_event(
            "reranking", "complete", "Evidence ranked",
            f"Kept {len(reranked)} top candidates.", start, trace_id,
        )
        # (25-26) Score filter + context assembly (CP3, dedup, order, budget, tags).
        yield _progress_event(
            "context", "running", "Building grounded context", None,
            start, trace_id,
        )
        assembly = self._context.assemble(rewritten, reranked, user, risk=risk)
        if assembly.abstain:
            yield _progress_event(
                "context", "complete", "Insufficient grounded evidence",
                assembly.abstain_reason or "No eligible evidence passed the threshold.",
                start, trace_id,
            )
            answer = RagAnswer.abstain(
                assembly.abstain_reason or "Insufficient evidence.",
                language=language, trace_id=trace_id,
            )
            self._audit.log_query(
                trace_id=trace_id, user=user, query=query, decision="abstained",
                answer=answer.answer, language=language, latency_ms=_elapsed_ms(start),
                detail={"reason": assembly.abstain_reason},
            )
            self._metrics.record("abstained", _elapsed_ms(start), language)
            return answer, None
        yield _progress_event(
            "context", "complete", "Grounded context ready",
            f"Selected {len(assembly.sources)} cited sources for the answer.",
            start, trace_id,
        )

        return None, {
            "assembly": assembly, "language": language,
            "rewritten": rewritten, "risk": risk, "cache_key": cache_key,
            "user_memory": user_memory,
        }

    def _finalize(self, user, query, answer, ctx, start, trace_id):
        """Persist turn (text only) + cache + audit for a generated answer."""
        if not answer.abstained:
            self._memory.record(user, query, answer.answer)
            self._cache.set_answer(user, ctx["cache_key"], _answer_to_cache(answer))
        decision = "abstained" if answer.abstained else "answered"
        self._audit.log_query(
            trace_id=trace_id, user=user, query=query, decision=decision,
            answer=answer.answer,
            chunk_ids=[s.chunk_id for s in answer.sources],
            doc_ids=sorted({s.doc_id for s in answer.sources}),
            latency_ms=_elapsed_ms(start), language=ctx["language"], model=answer.model,
        )
        self._metrics.record(
            decision, _elapsed_ms(start), ctx["language"],
            citation_valid=None if answer.abstained else answer.metadata.get("citations_valid", True),
        )

    # ------------------------------------------------------------ internals  #
    def _validate(self, query: str) -> list[str]:
        errors: list[str] = []
        qc = self._cfg.query
        stripped = (query or "").strip()
        if len(stripped) < qc.min_query_chars:
            errors.append("Query is empty.")
            return errors
        if len(stripped) > qc.max_query_chars:
            errors.append(f"Query exceeds {qc.max_query_chars} characters.")
        # Length cap counted with the REAL embedding tokenizer, not split().
        from .tokenization import count_tokens

        if count_tokens(stripped) > qc.max_query_tokens:
            errors.append(f"Query exceeds {qc.max_query_tokens} tokens.")
        # Charset: reject control characters (basic abuse guard).
        if any(ord(ch) < 9 or (13 < ord(ch) < 32) for ch in stripped):
            errors.append("Query contains disallowed control characters.")
        return errors

    def _detect_language(self, query: str, user: UserContext) -> str:
        if user.language in self._cfg.language.supported:
            return user.language  # explicit UI tag wins
        result = self._lang.detect(query)
        return result.gated(
            self._cfg.language.confidence_gate, self._cfg.language.default_language
        )

    def _embed_query(self, text: str) -> list[float]:
        sig = self._embedder.signature()
        cached = self._cache.get_embedding(sig, "query::" + text)
        if cached is not None:
            return cached
        vec = self._embedder.embed_query(text)
        self._cache.set_embedding(sig, "query::" + text, vec)
        return vec


# --------------------------------------------------------------------------- #
# User-visible streaming activity (safe operational detail, never prompts)
# --------------------------------------------------------------------------- #


def _progress_event(
    stage: str,
    status: str,
    label: str,
    detail: str | None,
    start: float,
    trace_id: str,
    *,
    detail_type: str = "text",
) -> dict:
    event = {
        "type": "progress",
        "stage": stage,
        "status": status,
        "label": label,
        "elapsed_ms": round(_elapsed_ms(start)),
        "trace_id": trace_id,
    }
    if detail:
        event["detail"] = detail
        event["detail_type"] = detail_type
    return event


def _generation_activity(config: RAGConfig) -> tuple[str, str]:
    """Return a credential-free summary of the generation operation."""
    provider = config.generator.provider.lower().replace("-", "_")
    model = config.generator.model
    if provider == "codex_cli":
        effort = config.generator.reasoning_effort
        command = f"codex exec --sandbox read-only --ephemeral --model {model}"
        if effort.strip().lower() != "auto":
            command += f" --reasoning-effort {effort}"
        return command, "command"
    if provider == "claude_cli":
        return f"claude -p --model {model} --output-format json", "command"
    if provider in {"anthropic", "openai"}:
        return f"Streaming request to {provider} model {model}.", "text"
    return "Building an answer directly from the selected evidence.", "text"


# --------------------------------------------------------------------------- #
# Answer <-> cache serialization (small, ACL-scoped payload)
# --------------------------------------------------------------------------- #


def _answer_to_cache(answer: RagAnswer) -> dict:
    return {
        "answer": answer.answer,
        "language": answer.language,
        "model": answer.model,
        "abstained": answer.abstained,
        "sources": [
            {
                "label": s.label, "chunk_id": s.chunk_id, "doc_id": s.doc_id,
                "title": s.title, "source_uri": s.source_uri, "text": s.text,
                "section_path": s.section_path, "page_number": s.page_number,
            }
            for s in answer.sources
        ],
        "cited_claims": [
            {"claim": c.claim, "source_ids": c.source_ids, "support": c.support}
            for c in answer.cited_claims
        ],
    }


def _answer_from_cache(payload: dict, trace_id: str) -> RagAnswer:
    cited = cited_source_ids(payload.get("answer", ""))
    sources = [
        Source(
            label=s["label"], chunk_id=s["chunk_id"], doc_id=s["doc_id"],
            title=s["title"], source_uri=s["source_uri"], text=s["text"],
            section_path=s.get("section_path", []), page_number=s.get("page_number"),
        )
        for s in payload.get("sources", [])
        if s.get("label") in cited
    ]
    claims = [
        CitedClaim(claim=c["claim"], source_ids=c.get("source_ids", []),
                   support=c.get("support", "direct"))
        for c in payload.get("cited_claims", [])
    ]
    return RagAnswer(
        answer=payload["answer"], sources=sources, cited_claims=claims,
        language=payload.get("language", "en"),
        model=payload.get("model"), abstained=payload.get("abstained", False),
        trace_id=trace_id, metadata={"cache_hit": True},
    )


def _answer_event(answer: RagAnswer) -> dict:
    """Serialize a RagAnswer into the SSE 'final' event payload."""
    return {
        "answer": answer.answer,
        "abstained": answer.abstained,
        "abstention_reason": answer.abstention_reason,
        "language": answer.language,
        "model": answer.model,
        "trace_id": answer.trace_id,
        "sources": [
            {
                "label": s.label, "chunk_id": s.chunk_id, "doc_id": s.doc_id,
                "title": s.title, "source_uri": s.source_uri,
                "text": s.text,
                "section_path": s.section_path, "page_number": s.page_number,
            }
            for s in answer.sources
        ],
    }


def _trace_id(user: UserContext, query: str, start: float) -> str:
    import uuid

    # Random, not derived: perf_counter is process-relative, so a hash over it
    # collides across processes/restarts for repeated queries.
    return uuid.uuid4().hex[:16]


def _elapsed_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 2)
