"""Append-only compliance audit log.

Every request — including rejected, abstained, and failed ones — is recorded so
access can be reconstructed after the fact (arch flow step 31). This is NOT a
performance cache and is never read back to answer anything; it exists purely
for accountability.

Records store *hashed* query/answer text plus structural fields (chunk_ids,
latency, decision), never raw sensitive content, matching the handbook's
"permanent compliance record, hashed query/answer, never raw text" (M.7/J.4).
Writes are newline-delimited JSON (JSON Lines), append-only.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .config import RAGConfig, get_config
from .schemas import UserContext, utcnow

log = logging.getLogger("rag.audit")


def _hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AuditRecord:
    trace_id: str
    timestamp: str
    tenant_id: str
    user_id: str
    event: str                       # "query" | "ingest" | "delete" | "rejected"
    decision: str                    # "answered" | "abstained" | "error" | "indexed" | ...
    query_hash: str | None = None
    answer_hash: str | None = None
    chunk_ids: list[str] = field(default_factory=list)
    doc_ids: list[str] = field(default_factory=list)
    latency_ms: float | None = None
    language: str | None = None
    index_version: str | None = None
    model: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


class AuditLogger:
    """Thread-safe append-only writer for JSON Lines audit records."""

    def __init__(self, config: RAGConfig | None = None) -> None:
        self._cfg = config or get_config()
        self._path: Path = self._cfg.paths.audit_log_path
        self._lock = threading.Lock()

    def _write(self, record: AuditRecord) -> None:
        line = json.dumps(asdict(record), ensure_ascii=False)
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                # Open in append mode per write so an external log-rotator can
                # move the file without us holding a stale handle.
                with open(self._path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except Exception as exc:  # pragma: no cover
            log.error("Failed to write audit record %s: %s", record.trace_id, exc)

    # ------------------------------------------------------------------ API  #
    def log_query(
        self,
        *,
        trace_id: str,
        user: UserContext,
        query: str,
        decision: str,
        answer: str | None = None,
        chunk_ids: list[str] | None = None,
        doc_ids: list[str] | None = None,
        latency_ms: float | None = None,
        language: str | None = None,
        model: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self._write(
            AuditRecord(
                trace_id=trace_id,
                timestamp=utcnow().isoformat(),
                tenant_id=user.tenant_id,
                user_id=user.user_id,
                event="query",
                decision=decision,
                query_hash=_hash(query),
                answer_hash=_hash(answer) if answer is not None else None,
                chunk_ids=chunk_ids or [],
                doc_ids=doc_ids or [],
                latency_ms=latency_ms,
                language=language,
                index_version=self._cfg.versions.index_version,
                model=model,
                detail=detail or {},
            )
        )

    def log_event(
        self,
        *,
        trace_id: str,
        tenant_id: str,
        user_id: str,
        event: str,
        decision: str,
        doc_ids: list[str] | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self._write(
            AuditRecord(
                trace_id=trace_id,
                timestamp=utcnow().isoformat(),
                tenant_id=tenant_id,
                user_id=user_id,
                event=event,
                decision=decision,
                doc_ids=doc_ids or [],
                index_version=self._cfg.versions.index_version,
                detail=detail or {},
            )
        )


_shared: AuditLogger | None = None


def get_audit_logger() -> AuditLogger:
    global _shared
    if _shared is None:
        _shared = AuditLogger()
    return _shared
