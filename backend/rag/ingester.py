"""Write-path orchestrator — the full 13-step ingestion sequence, atomic per doc.

Runs as one lockable unit (arch flow steps 1-13):

    content hash (2) -> per-doc_id lock (3) -> recursive chunk (4)
      -> contextual augmentation (5) -> language detection (6)
      -> injection heuristic scan (7) -> PII detection & redaction (8)
      -> BGE-M3 embed, passage mode (9) -> BM25 index (10)
      -> version stamp (11) -> atomic upsert (12) -> tombstone-on-delete (13)

Idempotency: a content hash over normalized text + ACL lets an unchanged
re-ingest short-circuit (step 2), so reprocessing the same version does not
duplicate chunks or churn the index.

Deletion is an ingestion-pipeline capability only, triggered by an authenticated
admin/document-management process. The query/generation path — including the LLM
— has no write or delete access to the index at any point.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import re
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import unquote, urlparse

from .audit import AuditLogger, get_audit_logger
from .chunking import Chunker, augment_contextual
from .config import RAGConfig, get_config
from .embedder import Embedder, get_embedder
from .language import LanguageDetector, get_detector
from .originals import OriginalStore, StoredOriginal
from .retriever import analyze
from .schemas import (
    Chunk,
    IngestionResult,
    SourceDocument,
    Tombstone,
    UserContext,
    utcnow,
)
from .security import SecurityGuard, get_security_guard, scan_injection, user_may_read
from .vectorstore import BM25Store, VectorStore

log = logging.getLogger("rag.ingester")

_WS_RE = re.compile(r"\s+")
_MAX_PREVIEW_CHARS = 200_000
_CONTENT_TYPE_FILE_TYPES = {
    "application/pdf": "PDF",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "DOCX",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "PPTX",
    "text/csv": "CSV",
    "text/html": "HTML",
    "text/markdown": "MD",
    "text/plain": "TXT",
    "text/x-rst": "RST",
}

# Per-doc locks (in-process concurrency guard, step 3).
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _doc_lock(key: str) -> threading.Lock:
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
        return lock


class _FileLock:
    """Best-effort cross-process advisory lock via an O_EXCL lockfile.

    Complements the in-process threading lock so two *processes* (e.g. a CLI
    ingest while the API serves, or uvicorn --workers 2) don't interleave writes
    to the same doc_id. Steals a lock older than ``stale`` seconds (crashed
    holder); on timeout it proceeds with a warning rather than deadlocking
    ingestion — the in-process lock still serializes same-process writers.
    """

    def __init__(self, path: Path, timeout: float = 30.0, stale: float = 300.0) -> None:
        self._path = path
        self._timeout = timeout
        self._stale = stale
        self._fd: int | None = None

    def acquire(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + self._timeout
        while True:
            try:
                self._fd = os.open(str(self._path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self._fd, str(os.getpid()).encode())
                return
            except FileExistsError:
                try:
                    if time.time() - self._path.stat().st_mtime > self._stale:
                        os.unlink(self._path)
                        continue
                except FileNotFoundError:
                    continue
                if time.time() >= deadline:
                    log.warning("File lock timeout on %s; proceeding.", self._path.name)
                    return
                time.sleep(0.05)

    def release(self) -> None:
        try:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None
            self._path.unlink(missing_ok=True)
        except Exception:  # pragma: no cover - best effort
            pass


def normalize_text(text: str) -> str:
    return _WS_RE.sub(" ", (text or "").strip())


def content_hash(
    text: str,
    acl_principals: list[str],
    sensitivity: str,
    *,
    parser_version: str = "",
    chunker_version: str = "",
    title: str = "",
) -> str:
    """SHA-256 over normalized content + answer-relevant metadata (ACL,
    classification) and transformation versions.

    Parser/chunker upgrades must rebuild unchanged source files; otherwise a
    corrected extractor can leave old corrupt chunks active indefinitely.
    """
    h = hashlib.sha256()
    h.update(normalize_text(text).encode("utf-8"))
    h.update(b"\x1f")
    h.update("|".join(sorted(acl_principals)).encode("utf-8"))
    h.update(b"\x1f")
    h.update(sensitivity.encode("utf-8"))
    h.update(b"\x1f")
    h.update(parser_version.encode("utf-8"))
    h.update(b"\x1f")
    h.update(chunker_version.encode("utf-8"))
    h.update(b"\x1f")
    h.update(normalize_text(title).encode("utf-8"))
    return h.hexdigest()


def _source_path(source_uri: str) -> str:
    parsed = urlparse(source_uri or "")
    if parsed.scheme:
        return unquote(f"{parsed.netloc}{parsed.path}")
    return unquote(source_uri or "")


def _assigned_folder(source_system: str, source_uri: str) -> str:
    """Assign a stable display folder from ingestion provenance."""
    parsed = urlparse(source_uri or "")
    if parsed.scheme == "upload" or source_system.lower() == "upload":
        return "Uploads"

    source_path = _source_path(source_uri).replace("\\", "/").rstrip("/")
    parent = Path(source_path).parent.name if source_path else ""
    if parent and parent not in (".", "/"):
        return parent

    label = (source_system or parsed.scheme or "Other").strip()
    return label.upper() if label.lower() == "api" else label.replace("_", " ").title()


def _assigned_file_type(content_type: str, source_uri: str, title: str = "") -> str:
    """Prefer the original extension, then fall back to the extracted MIME type."""
    for value in (source_uri, title):
        suffix = Path(_source_path(value)).suffix.lstrip(".")
        if suffix:
            return suffix.upper()
    if content_type in _CONTENT_TYPE_FILE_TYPES:
        return _CONTENT_TYPE_FILE_TYPES[content_type]
    subtype = (content_type or "").split("/")[-1].split(";")[0].strip()
    return subtype.upper() if subtype else "OTHER"


class Ingester:
    def __init__(
        self,
        config: RAGConfig | None = None,
        vector_store: VectorStore | None = None,
        bm25_store: BM25Store | None = None,
        embedder: Embedder | None = None,
        chunker: Chunker | None = None,
        language: LanguageDetector | None = None,
        security: SecurityGuard | None = None,
        audit: AuditLogger | None = None,
    ) -> None:
        self._cfg = config or get_config()
        self._vs = vector_store or VectorStore(self._cfg)
        self._bm25 = bm25_store or BM25Store(self._cfg)
        self._embedder = embedder or get_embedder()
        self._chunker = chunker or Chunker()
        self._lang = language or get_detector()
        self._security = security or get_security_guard()
        self._audit = audit or get_audit_logger()
        self._manifest_path = self._cfg.paths.data_dir / "ingest_manifest.json"
        self._originals = OriginalStore(self._cfg)

    # ------------------------------------------------------------ manifest   #
    def _load_manifest(self) -> dict[str, dict[str, str]]:
        if self._manifest_path.exists():
            try:
                return json.loads(self._manifest_path.read_text("utf-8"))
            except Exception:
                return {}
        return {}

    def _save_manifest(self, manifest: dict[str, dict[str, str]]) -> None:
        # Atomic write: a crash mid-write must never truncate the manifest.
        self._manifest_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._manifest_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(manifest, indent=2), "utf-8")
        os.replace(tmp, self._manifest_path)

    @staticmethod
    def _manifest_key(tenant_id: str, doc_id: str) -> str:
        return f"{tenant_id}::{doc_id}"

    @contextmanager
    def _doc_guard(self, key: str):
        """Serialize a doc_id across BOTH threads (in-process) and processes
        (lockfile), so step-3's concurrency guarantee holds under uvicorn
        workers and concurrent CLI ingests."""
        tlock = _doc_lock(key)
        tlock.acquire()
        lock_name = hashlib.sha256(key.encode()).hexdigest()[:32] + ".lock"
        flock = _FileLock(self._cfg.paths.data_dir / "locks" / lock_name)
        try:
            flock.acquire()
            yield
        finally:
            flock.release()
            tlock.release()

    # ---------------------------------------------------------------- ingest #
    def ingest(self, doc: SourceDocument) -> IngestionResult:
        key = self._manifest_key(doc.tenant_id, doc.doc_id)
        trace_id = hashlib.sha256(key.encode()).hexdigest()[:16]

        # (2) content hash + idempotency check.
        chash = content_hash(
            doc.text,
            doc.acl_principals,
            doc.sensitivity,
            parser_version=self._cfg.versions.parser_version,
            chunker_version=self._cfg.versions.chunker_version,
            title=doc.title,
        )
        version_id = doc.version_id or chash[:12]
        doc = dataclasses.replace(doc, content_hash=chash, version_id=version_id)

        manifest = self._load_manifest()
        existing = manifest.get(key)
        if existing and existing.get("content_hash") == chash:
            self._audit.log_event(
                trace_id=trace_id, tenant_id=doc.tenant_id, user_id=doc.extractor_name,
                event="ingest", decision="skipped_unchanged", doc_ids=[doc.doc_id],
            )
            return IngestionResult(
                doc_id=doc.doc_id, status="skipped_unchanged",
                content_hash=chash, version_id=version_id,
                message="Content unchanged since last ingest.",
            )

        # (3) per-doc lock — serialize concurrent ingests of the same doc_id.
        with self._doc_guard(key):
            return self._ingest_locked(doc, chash, version_id, trace_id, manifest, key)

    def _ingest_locked(
        self,
        doc: SourceDocument,
        chash: str,
        version_id: str,
        trace_id: str,
        manifest: dict[str, dict[str, str]],
        key: str,
    ) -> IngestionResult:
        # (4-5) recursive chunk + contextual augmentation.
        chunks = self._chunker.chunk_document(doc)
        if not chunks:
            return IngestionResult(
                doc_id=doc.doc_id, status="failed",
                message="No chunks produced (empty or too-short document).",
            )

        indexable: list[Chunk] = []
        quarantined = 0
        for chunk in chunks:
            # (6) per-chunk language detection (metadata only).
            language = doc.language or self._lang.detect(chunk.text).language

            # (7) injection heuristic scan -> quarantine on match.
            scan = scan_injection(chunk.text)
            if self._security.is_quarantine(scan):
                self._quarantine(doc, chunk, scan.matches)
                quarantined += 1
                continue

            # (8) PII detection & redaction (policy by classification).
            redaction = self._security.redact_pii(chunk.text, doc.sensitivity)
            text = redaction.text
            contextual = (
                augment_contextual(chunk.title, chunk.section_path, text)
                if redaction.redacted
                else chunk.contextual_text
            )

            # (11) version stamp (embed model/version stamped just below).
            indexable.append(
                dataclasses.replace(
                    chunk,
                    text=text,
                    contextual_text=contextual,
                    language=language,
                    embedding_model=self._cfg.models.embedding_model,
                    embedding_version=self._cfg.versions.embedding_version,
                    metadata={**chunk.metadata, "pii_redacted": redaction.redacted},
                )
            )

        if not indexable:
            self._audit.log_event(
                trace_id=trace_id, tenant_id=doc.tenant_id, user_id=doc.extractor_name,
                event="ingest", decision="quarantined", doc_ids=[doc.doc_id],
                detail={"quarantined": quarantined},
            )
            return IngestionResult(
                doc_id=doc.doc_id, status="quarantined",
                chunks_quarantined=quarantined, content_hash=chash, version_id=version_id,
                message="All chunks quarantined by the injection scan.",
            )

        # (9) BGE-M3 embed, passage mode (no prefix).
        embeddings = self._embedder.embed_passages([c.contextual_text for c in indexable])

        # (12) atomic upsert: write the NEW version first, then retire the old
        # one (chunk ids include the version, so old and new never collide). A
        # failure mid-way leaves the previous version live and searchable —
        # tombstone-first would lose it with no way back.
        new_ids = {c.chunk_id for c in indexable}
        try:
            self._vs.upsert(indexable, embeddings)
            # (10) BM25 index — same language-aware analyzer as query time.
            for chunk in indexable:
                self._bm25.add(chunk, analyze(chunk.contextual_text, chunk.language))
            # Retire the prior version's chunks (everything not in the new set).
            self._vs.tombstone_doc(doc.tenant_id, doc.doc_id, keep_ids=new_ids)
            self._bm25.tombstone_doc(doc.tenant_id, doc.doc_id, keep_ids=new_ids)
            self._bm25.save()
        except Exception as exc:
            # Roll back only the new chunks; the previous version stays intact.
            self._vs.tombstone_chunks(sorted(new_ids))
            self._bm25.remove_chunks(sorted(new_ids))
            self._bm25.save()
            log.exception("Ingest failed for %s", doc.doc_id)
            self._audit.log_event(
                trace_id=trace_id, tenant_id=doc.tenant_id, user_id=doc.extractor_name,
                event="ingest", decision="error", doc_ids=[doc.doc_id],
                detail={"error": str(exc)},
            )
            return IngestionResult(
                doc_id=doc.doc_id, status="failed", message=f"Upsert failed: {exc}"
            )

        # Update manifest (idempotency record). Re-read under the lock so a
        # concurrent writer's entries for other docs aren't lost.
        manifest = self._load_manifest()
        manifest[key] = {
            "content_hash": chash,
            "version_id": version_id,
            "indexed_at": utcnow().isoformat(),
            "chunks": str(len(indexable)),
            "doc_id": doc.doc_id,
            "title": doc.title or doc.doc_id,
            "source_uri": doc.source_uri or "",
            "folder": _assigned_folder(doc.source_system, doc.source_uri),
            "file_type": _assigned_file_type(
                doc.content_type, doc.source_uri, doc.title
            ),
            "content_type": doc.content_type,
            "source_system": doc.source_system,
            "parser_version": self._cfg.versions.parser_version,
            "chunker_version": self._cfg.versions.chunker_version,
            "status": "active",
        }
        self._save_manifest(manifest)

        # Invalidate ACL-scoped answer caches for this tenant (best-effort;
        # no-op when Redis is down, in which case there is no cache to stale).
        from .cache import get_cache

        get_cache().bump_tenant_generation(doc.tenant_id)

        self._audit.log_event(
            trace_id=trace_id, tenant_id=doc.tenant_id, user_id=doc.extractor_name,
            event="ingest", decision="indexed", doc_ids=[doc.doc_id],
            detail={"chunks": len(indexable), "quarantined": quarantined},
        )
        return IngestionResult(
            doc_id=doc.doc_id, status="indexed", chunks_indexed=len(indexable),
            chunks_quarantined=quarantined, content_hash=chash, version_id=version_id,
        )

    # ---------------------------------------------------------------- delete #
    def delete(self, tenant_id: str, doc_id: str, reason: str = "admin_delete") -> IngestionResult:
        """(13) Tombstone on delete — never hard-delete. A hard delete can be
        silently resurrected by a retry or backfill; a tombstone cannot."""
        key = self._manifest_key(tenant_id, doc_id)
        trace_id = hashlib.sha256((key + reason).encode()).hexdigest()[:16]
        with self._doc_guard(key):
            n_vec = self._vs.tombstone_doc(tenant_id, doc_id)
            self._bm25.tombstone_doc(tenant_id, doc_id)
            self._bm25.save()
            self._write_tombstone(
                Tombstone(
                    tenant_id=tenant_id, doc_id=doc_id,
                    deleted_at=utcnow(), reason=reason,
                )
            )
            manifest = self._load_manifest()
            manifest.pop(key, None)
            self._save_manifest(manifest)
            self._originals.delete(tenant_id, doc_id)
        from .cache import get_cache

        get_cache().bump_tenant_generation(tenant_id)
        self._audit.log_event(
            trace_id=trace_id, tenant_id=tenant_id, user_id="admin",
            event="delete", decision="deleted", doc_ids=[doc_id],
            detail={"reason": reason, "chunks_tombstoned": n_vec},
        )
        return IngestionResult(
            doc_id=doc_id, status="deleted", message=f"Tombstoned {n_vec} chunks."
        )

    # ------------------------------------------------------------- listing   #
    def list_documents(self, tenant_id: str) -> list[dict]:
        """Return the indexed documents for a tenant (from the manifest), newest
        first. Used by the API's GET /documents so the UI reflects the real
        index instead of a hard-coded list."""
        prefix = f"{tenant_id}::"
        docs: list[dict] = []
        for key, meta in self._load_manifest().items():
            if not key.startswith(prefix):
                continue
            docs.append({
                "doc_id": meta.get("doc_id") or key[len(prefix):],
                "title": meta.get("title") or key[len(prefix):],
                "source_uri": meta.get("source_uri") or "",
                "folder": meta.get("folder") or _assigned_folder(
                    meta.get("source_system") or "manual",
                    meta.get("source_uri") or "",
                ),
                "file_type": meta.get("file_type") or _assigned_file_type(
                    meta.get("content_type") or "",
                    meta.get("source_uri") or "",
                    meta.get("title") or "",
                ),
                "status": meta.get("status") or "active",
                "chunks": int(meta.get("chunks", 0) or 0),
                "indexed_at": meta.get("indexed_at") or "",
            })
        docs.sort(key=lambda d: d["indexed_at"], reverse=True)
        return docs

    def document_preview(self, user: UserContext, doc_id: str) -> dict | None:
        """Return authorized, active chunk text for a document preview."""
        key = self._manifest_key(user.tenant_id, doc_id)
        meta = self._load_manifest().get(key)
        if not meta:
            return None

        chunks = [
            chunk
            for chunk in self._vs.get_all_active(user.tenant_id)
            if chunk.doc_id == doc_id and user_may_read(chunk, user)
        ]
        if not chunks:
            # Do not disclose whether the document exists when access is denied.
            return None

        chunks.sort(
            key=lambda chunk: (
                chunk.char_start is None,
                chunk.char_start or 0,
                chunk.page_number is None,
                chunk.page_number or 0,
                chunk.chunk_id,
            )
        )
        parts: list[str] = []
        seen: set[tuple[int | None, int | None, str]] = set()
        for chunk in chunks:
            marker = (chunk.char_start, chunk.char_end, chunk.text)
            if marker in seen:
                continue
            seen.add(marker)
            parts.append(chunk.text.strip())

        full_content = "\n\n".join(part for part in parts if part)
        truncated = len(full_content) > _MAX_PREVIEW_CHARS
        return {
            "doc_id": meta.get("doc_id") or doc_id,
            "title": meta.get("title") or doc_id,
            "folder": meta.get("folder") or _assigned_folder(
                meta.get("source_system") or "manual",
                meta.get("source_uri") or "",
            ),
            "file_type": meta.get("file_type") or _assigned_file_type(
                meta.get("content_type") or "",
                meta.get("source_uri") or "",
                meta.get("title") or "",
            ),
            "indexed_at": meta.get("indexed_at") or "",
            "content": full_content[:_MAX_PREVIEW_CHARS],
            "truncated": truncated,
            "has_original": bool(meta.get("original_file_name")),
            "content_type": meta.get("original_content_type") or meta.get("content_type") or "",
            "file_name": meta.get("original_file_name") or meta.get("title") or doc_id,
        }

    def cited_texts(self, user: UserContext, chunk_ids: list[str]) -> dict[str, str]:
        """Hydrate legacy chat citations from the current authorized index.

        New turns persist their evidence excerpts with the message. This path is
        only for older turns and intentionally returns nothing for deleted,
        cross-tenant, over-classified, or otherwise unauthorized chunks.
        """
        wanted = set(chunk_ids)
        if not wanted:
            return {}
        return {
            chunk.chunk_id: chunk.text
            for chunk in self._vs.get_all_active(user.tenant_id)
            if chunk.chunk_id in wanted and user_may_read(chunk, user)
        }

    def store_original(
        self,
        tenant_id: str,
        doc_id: str,
        filename: str,
        content_type: str,
        data: bytes,
    ) -> StoredOriginal | None:
        """Persist upload bytes and attach their metadata to an active manifest.

        The manifest check prevents failed/quarantined ingests from creating a
        downloadable document that has no authorized active chunks.
        """
        key = self._manifest_key(tenant_id, doc_id)
        with self._doc_guard(key):
            manifest = self._load_manifest()
            meta = manifest.get(key)
            if not meta or meta.get("status") != "active":
                return None
            stored = self._originals.save(
                tenant_id, doc_id, filename, content_type, data
            )
            meta.update({
                "original_file_name": stored.filename,
                "original_content_type": stored.content_type,
                "original_size": str(stored.size),
            })
            manifest[key] = meta
            self._save_manifest(manifest)
            return stored

    def document_original(self, user: UserContext, doc_id: str) -> StoredOriginal | None:
        """Return original-file metadata only when the caller can read a chunk."""
        key = self._manifest_key(user.tenant_id, doc_id)
        meta = self._load_manifest().get(key)
        if not meta or not meta.get("original_file_name"):
            return None
        authorized = any(
            chunk.doc_id == doc_id and user_may_read(chunk, user)
            for chunk in self._vs.get_all_active(user.tenant_id)
        )
        if not authorized:
            return None
        try:
            size = int(meta.get("original_size") or 0)
        except (TypeError, ValueError):
            size = 0
        return self._originals.get(
            user.tenant_id,
            doc_id,
            meta.get("original_file_name") or doc_id,
            meta.get("original_content_type") or meta.get("content_type") or "",
            size,
        )

    # ------------------------------------------------------------ internals  #
    def _quarantine(self, doc: SourceDocument, chunk: Chunk, matches: list[str]) -> None:
        qdir: Path = self._cfg.paths.quarantine_dir
        qdir.mkdir(parents=True, exist_ok=True)
        # The quarantine file sits outside every ACL, so PII must be masked even
        # for confidential/restricted docs (force_redact ignores policy). The
        # hash preserves forensic linkage to the original without storing it.
        redaction = self._security.force_redact(chunk.text)
        record = {
            "tenant_id": doc.tenant_id,
            "doc_id": doc.doc_id,
            "chunk_id": chunk.chunk_id,
            "matches": matches,
            "text": redaction.text,
            "text_sha256": hashlib.sha256(chunk.text.encode("utf-8")).hexdigest(),
            "pii_findings": [f.kind for f in redaction.findings],
            "quarantined_at": utcnow().isoformat(),
        }
        path = qdir / f"{doc.tenant_id}_{doc.doc_id}_{chunk.chunk_id[:12]}.json"
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2), "utf-8")
        log.warning("Quarantined chunk %s of doc %s", chunk.chunk_id[:12], doc.doc_id)

    def _write_tombstone(self, tombstone: Tombstone) -> None:
        path = self._cfg.paths.data_dir / "tombstones.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "tenant_id": tombstone.tenant_id,
                        "doc_id": tombstone.doc_id,
                        "deleted_at": tombstone.deleted_at.isoformat(),
                        "reason": tombstone.reason,
                    }
                )
                + "\n"
            )
