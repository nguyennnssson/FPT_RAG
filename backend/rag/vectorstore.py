"""Storage interfaces: ChromaDB dense store (with Checkpoint 1) and the BM25
keyword store.

VectorStore isolates ChromaDB-specific syntax behind one module and runs
**Checkpoint 1** — the cheapest ACL cut: a scalar pre-filter on
``tenant_id + classification`` applied *before* any similarity computation
(arch flow step 20). CP1 is coarse on purpose: Chroma can filter scalars
natively but NOT ``acl_principals`` (a list), which is exactly why Checkpoint 2
exists downstream in Python.

Both stores load their heavy backend (chromadb / rank-bm25) lazily and fall
back to an in-memory / pure-python implementation when the dependency or a
persisted index is unavailable, so a document can go in and an answer come out
on this box without extra installs. Set ``RAG_VECTORSTORE_BACKEND=memory`` or
``RAG_VECTORSTORE_BACKEND=chroma`` to pin one.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import asdict
from typing import Any, Iterable

from .config import RAGConfig, get_config
from .schemas import (
    Chunk,
    RetrievalResult,
    UserContext,
    classification_rank,
    CLASSIFICATION_ORDER,
)

_LIST_SEP = "\x1f"

# The in-memory backend is a process-wide store keyed by (path, collection), so
# a VectorStore built by the Ingester and one built by the Pipeline share state
# exactly the way separate handles to one ChromaDB collection would. Without
# this, offline (memory-backend) ingest+query would silently see empty results.
_MEMORY_REGISTRY: dict[str, dict[str, Any]] = {}


def _memory_store(key: str) -> dict[str, Any]:
    return _MEMORY_REGISTRY.setdefault(key, {})


# Process-wide BM25 registry keyed by index path. "mtime" is the on-disk
# file's mtime at last load/save; "dirty" marks unsaved in-process writes.
_BM25_REGISTRY: dict[str, dict[str, Any]] = {}


def _bm25_registry(path: str) -> dict[str, Any]:
    return _BM25_REGISTRY.setdefault(
        path, {"docs": None, "version": 0, "mtime": None, "dirty": False}
    )


# --------------------------------------------------------------------------- #
# CP1 helper
# --------------------------------------------------------------------------- #


def cp1_allowed_classifications(user_max: str) -> list[str]:
    """Classifications a user may read: everything at or below their ceiling."""
    ceiling = classification_rank(user_max)
    return [c for c in CLASSIFICATION_ORDER if classification_rank(c) <= ceiling]


# --------------------------------------------------------------------------- #
# Chunk <-> flat-metadata serialization (Chroma stores only scalars)
# --------------------------------------------------------------------------- #


def _chunk_to_metadata(chunk: Chunk) -> dict[str, Any]:
    return {
        "tenant_id": chunk.tenant_id,
        "doc_id": chunk.doc_id,
        "source_system": chunk.source_system,
        "source_uri": chunk.source_uri,
        "title": chunk.title,
        # Raw text kept for citation display / hydration.
        "text": chunk.text,
        "contextual_text": chunk.contextual_text,
        # acl_principals is a LIST — stored as a delimited scalar because Chroma
        # cannot filter lists. CP2 parses it back and checks in Python.
        "acl": _LIST_SEP.join(chunk.acl_principals),
        "classification": chunk.sensitivity,
        "section_path": json.dumps(chunk.section_path),
        "page_number": -1 if chunk.page_number is None else chunk.page_number,
        "language": chunk.language or "",
        "chunk_type": chunk.chunk_type,
        "token_count": chunk.token_count,
        "char_start": -1 if chunk.char_start is None else chunk.char_start,
        "char_end": -1 if chunk.char_end is None else chunk.char_end,
        "version_id": chunk.version_id or "",
        "chunker_version": chunk.chunker_version or "",
        "parser_version": chunk.parser_version or "",
        "embedding_model": chunk.embedding_model or "",
        "embedding_version": chunk.embedding_version or "",
        "meta_json": json.dumps(chunk.metadata),
        "deleted": False,
    }


def _metadata_to_chunk(meta: dict[str, Any]) -> Chunk:
    def _opt_int(v: Any) -> int | None:
        return None if v in (-1, "", None) else int(v)

    return Chunk(
        tenant_id=meta["tenant_id"],
        doc_id=meta["doc_id"],
        chunk_id=meta["chunk_id"],
        source_system=meta["source_system"],
        source_uri=meta["source_uri"],
        title=meta["title"],
        text=meta["text"],
        contextual_text=meta["contextual_text"],
        acl_principals=[p for p in meta.get("acl", "").split(_LIST_SEP) if p],
        sensitivity=meta.get("classification", "internal"),
        section_path=json.loads(meta.get("section_path", "[]")),
        page_number=_opt_int(meta.get("page_number", -1)),
        language=meta.get("language") or None,
        chunk_type=meta.get("chunk_type", "text"),
        metadata=json.loads(meta.get("meta_json", "{}")),
        char_start=_opt_int(meta.get("char_start", -1)),
        char_end=_opt_int(meta.get("char_end", -1)),
        token_count=int(meta.get("token_count", 0)),
        version_id=meta.get("version_id") or None,
        chunker_version=meta.get("chunker_version") or None,
        parser_version=meta.get("parser_version") or None,
        embedding_model=meta.get("embedding_model") or None,
        embedding_version=meta.get("embedding_version") or None,
    )


# --------------------------------------------------------------------------- #
# VectorStore (dense, ChromaDB + in-memory fallback, runs CP1)
# --------------------------------------------------------------------------- #


class VectorStore:
    def __init__(self, config: RAGConfig | None = None) -> None:
        self._cfg = config or get_config()
        self._backend: str | None = None
        self._collection = None       # chroma collection
        # chunk_id -> {meta, embedding}; shared per (dir, collection) — see above.
        mem_key = f"{self._cfg.paths.chroma_dir}::{self._cfg.vectorstore.collection_name}"
        self._mem: dict[str, dict[str, Any]] = _memory_store(mem_key)
        self._requested = os.environ.get("RAG_VECTORSTORE_BACKEND", "auto")

    # ------------------------------------------------------------ lifecycle  #
    def _ensure(self) -> None:
        if self._backend is not None:
            return
        if self._requested in ("auto", "chroma"):
            try:
                self._init_chroma()
                self._backend = "chroma"
                return
            except Exception:
                if self._requested == "chroma":
                    raise
        self._backend = "memory"

    def _init_chroma(self) -> None:
        import chromadb  # type: ignore
        from chromadb.config import Settings  # type: ignore

        self._cfg.paths.ensure()
        client = chromadb.PersistentClient(
            path=str(self._cfg.paths.chroma_dir),
            settings=Settings(anonymized_telemetry=False, allow_reset=False),
        )
        self._collection = client.get_or_create_collection(
            name=self._cfg.vectorstore.collection_name,
            metadata={"hnsw:space": self._cfg.vectorstore.distance},
        )

    @property
    def backend(self) -> str:
        self._ensure()
        return self._backend or "unknown"

    # ---------------------------------------------------------------- write  #
    def upsert(self, chunks: Iterable[Chunk], embeddings: list[list[float]]) -> int:
        chunks = list(chunks)
        if not chunks:
            return 0
        if len(chunks) != len(embeddings):
            raise ValueError("chunks and embeddings length mismatch")
        self._ensure()
        metas = []
        for chunk in chunks:
            m = _chunk_to_metadata(chunk)
            m["chunk_id"] = chunk.chunk_id
            metas.append(m)

        if self._backend == "chroma":
            self._collection.upsert(  # type: ignore[union-attr]
                ids=[c.chunk_id for c in chunks],
                embeddings=embeddings,
                metadatas=metas,
                documents=[c.text for c in chunks],
            )
        else:
            for chunk, meta, emb in zip(chunks, metas, embeddings):
                self._mem[chunk.chunk_id] = {"meta": meta, "embedding": emb}
        return len(chunks)

    def tombstone_doc(
        self, tenant_id: str, doc_id: str, keep_ids: set[str] | None = None
    ) -> int:
        """Mark all chunks of a doc deleted (never hard-delete; step 13).
        ``keep_ids`` spares the given chunk ids — used by re-ingest to retire
        only the previous version's chunks after the new ones are live."""
        self._ensure()
        keep = keep_ids or set()
        if self._backend == "chroma":
            got = self._collection.get(  # type: ignore[union-attr]
                where={"$and": [{"tenant_id": tenant_id}, {"doc_id": doc_id}]},
                include=["metadatas"],
            )
            pairs = [
                (cid, m)
                for cid, m in zip(got.get("ids", []), got.get("metadatas", []))
                if cid not in keep
            ]
            if not pairs:
                return 0
            for _, m in pairs:
                m["deleted"] = True
            self._collection.update(  # type: ignore[union-attr]
                ids=[cid for cid, _ in pairs], metadatas=[m for _, m in pairs]
            )
            return len(pairs)
        count = 0
        for cid, rec in self._mem.items():
            m = rec["meta"]
            if m["tenant_id"] == tenant_id and m["doc_id"] == doc_id and cid not in keep:
                m["deleted"] = True
                count += 1
        return count

    def tombstone_chunks(self, chunk_ids: list[str]) -> int:
        """Mark specific chunks deleted (rollback path for a failed ingest)."""
        self._ensure()
        if not chunk_ids:
            return 0
        if self._backend == "chroma":
            got = self._collection.get(ids=chunk_ids, include=["metadatas"])  # type: ignore[union-attr]
            ids = got.get("ids", [])
            if not ids:
                return 0
            metas = got.get("metadatas", [])
            for m in metas:
                m["deleted"] = True
            self._collection.update(ids=ids, metadatas=metas)  # type: ignore[union-attr]
            return len(ids)
        count = 0
        for cid in chunk_ids:
            rec = self._mem.get(cid)
            if rec is not None:
                rec["meta"]["deleted"] = True
                count += 1
        return count

    # --------------------------------------------------------------- search  #
    def search(
        self,
        query_embedding: list[float],
        user: UserContext,
        top_k: int,
        doc_ids: set[str] | None = None,
    ) -> list[RetrievalResult]:
        """Dense cosine search with **Checkpoint 1** applied as a native scalar
        pre-filter (tenant_id + allowed classifications + not deleted)."""
        self._ensure()
        allowed = cp1_allowed_classifications(user.max_classification)

        if self._backend == "chroma":
            filters: list[dict[str, Any]] = [
                    {"tenant_id": user.tenant_id},
                    {"classification": {"$in": allowed}},
                    {"deleted": False},
            ]
            if doc_ids:
                filters.append({"doc_id": {"$in": sorted(doc_ids)}})
            where = {"$and": filters}
            res = self._collection.query(  # type: ignore[union-attr]
                query_embeddings=[query_embedding],
                n_results=max(top_k, 1),
                where=where,
                include=["metadatas", "documents", "distances", "embeddings"],
            )
            return self._chroma_results(res)

        # in-memory: apply CP1 in Python, then brute-force cosine.
        scored: list[tuple[float, dict[str, Any], list[float]]] = []
        for cid, rec in self._mem.items():
            m = rec["meta"]
            if m["tenant_id"] != user.tenant_id or m.get("deleted"):
                continue
            if m["classification"] not in allowed:
                continue
            if doc_ids and m["doc_id"] not in doc_ids:
                continue
            sim = _cosine(query_embedding, rec["embedding"])
            scored.append((sim, {**m, "chunk_id": cid}, rec["embedding"]))
        scored.sort(key=lambda t: t[0], reverse=True)
        results = []
        for rank, (sim, meta, emb) in enumerate(scored[:top_k], start=1):
            results.append(
                RetrievalResult(
                    chunk=_metadata_to_chunk(meta),
                    retriever="dense",
                    rank=rank,
                    score=sim,
                    normalized_score=sim,
                    embedding=emb,
                )
            )
        return results

    def _chroma_results(self, res: dict[str, Any]) -> list[RetrievalResult]:
        ids = (res.get("ids") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        embs = (res.get("embeddings") or [[None] * len(ids)])[0]
        results = []
        for rank, (cid, meta, dist) in enumerate(zip(ids, metas, dists), start=1):
            meta = {**meta, "chunk_id": cid}
            sim = 1.0 - float(dist)  # cosine distance -> similarity
            emb = embs[rank - 1] if rank - 1 < len(embs) else None
            results.append(
                RetrievalResult(
                    chunk=_metadata_to_chunk(meta),
                    retriever="dense",
                    rank=rank,
                    score=sim,
                    normalized_score=sim,
                    embedding=list(emb) if emb is not None else None,
                )
            )
        return results

    def get_all_active(self, tenant_id: str | None = None) -> list[Chunk]:
        """Return every non-deleted chunk (used to (re)build the BM25 index)."""
        self._ensure()
        if self._backend == "chroma":
            got = self._collection.get(include=["metadatas"])  # type: ignore[union-attr]
            out = []
            for cid, m in zip(got.get("ids", []), got.get("metadatas", [])):
                if m.get("deleted"):
                    continue
                if tenant_id and m["tenant_id"] != tenant_id:
                    continue
                out.append(_metadata_to_chunk({**m, "chunk_id": cid}))
            return out
        out = []
        for cid, rec in self._mem.items():
            m = rec["meta"]
            if m.get("deleted"):
                continue
            if tenant_id and m["tenant_id"] != tenant_id:
                continue
            out.append(_metadata_to_chunk({**m, "chunk_id": cid}))
        return out


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


# --------------------------------------------------------------------------- #
# BM25 store (keyword; rank-bm25 + pickle, pure-python fallback)
# --------------------------------------------------------------------------- #


class BM25Store:
    """Persistent keyword index. Tokenization is language-aware (pyvi for VI,
    Porter/simple for EN) and is provided by the caller (retriever) so the same
    analyzer is used at index and query time."""

    def __init__(self, config: RAGConfig | None = None) -> None:
        self._cfg = config or get_config()
        self._path = self._cfg.paths.bm25_path
        # Docs live in a process-wide registry keyed by path, and carry a
        # version counter. All BM25Store handles to the same index share the
        # docs and see each other's writes/deletes — so an ingest and a query in
        # the same process, and a tombstone from either, stay consistent
        # (mirrors the way ChromaDB handles share one collection).
        self._reg = _bm25_registry(str(self._path))
        self._bm25 = None
        self._order: list[str] = []
        self._built_version = -1

    @property
    def _docs(self) -> dict[str, dict[str, Any]]:
        return self._reg["docs"]

    def _bump(self) -> None:
        self._reg["version"] += 1

    # ------------------------------------------------------------ persistence #
    # The index is persisted as JSON (not pickle): pickle.load on a tamperable
    # file is arbitrary code execution. Chunks round-trip through the same
    # metadata serialization the vector store uses.
    def _file_mtime(self) -> float | None:
        try:
            return self._path.stat().st_mtime
        except OSError:
            return None

    def _read_file(self) -> dict[str, dict[str, Any]]:
        docs: dict[str, dict[str, Any]] = {}
        if self._path.exists():
            try:
                payload = json.loads(self._path.read_text("utf-8"))
                for cid, rec in payload.get("docs", {}).items():
                    docs[cid] = {
                        "tokens": rec["tokens"],
                        "chunk": _metadata_to_chunk({**rec["meta"], "chunk_id": cid}),
                    }
            except Exception:
                # Unreadable/legacy (pickle-era) index: start empty; the next
                # ingest rebuilds and saves in the JSON format.
                docs = {}
        return docs

    def _ensure_loaded(self) -> None:
        if self._reg["docs"] is None:
            self._reg["docs"] = self._read_file()
            self._reg["mtime"] = self._file_mtime()
            return
        # Cross-process refresh: another process saved a newer index. Only
        # reload when we have no unsaved local writes to lose.
        if not self._reg["dirty"] and self._file_mtime() != self._reg["mtime"]:
            self._reg["docs"] = self._read_file()
            self._reg["mtime"] = self._file_mtime()
            self._bump()

    def save(self) -> None:
        self._ensure_loaded()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "docs": {
                cid: {
                    "tokens": rec["tokens"],
                    "meta": _chunk_to_metadata(rec["chunk"]),
                }
                for cid, rec in self._docs.items()
            }
        }
        self._path.write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
        self._reg["mtime"] = self._file_mtime()
        self._reg["dirty"] = False

    # ------------------------------------------------------------------ write #
    def add(self, chunk: Chunk, tokens: list[str]) -> None:
        self._ensure_loaded()
        self._docs[chunk.chunk_id] = {"tokens": tokens, "chunk": chunk}
        self._reg["dirty"] = True
        self._bump()

    def tombstone_doc(
        self, tenant_id: str, doc_id: str, keep_ids: set[str] | None = None
    ) -> int:
        self._ensure_loaded()
        keep = keep_ids or set()
        remove = [
            cid
            for cid, rec in self._docs.items()
            if rec["chunk"].tenant_id == tenant_id
            and rec["chunk"].doc_id == doc_id
            and cid not in keep
        ]
        for cid in remove:
            del self._docs[cid]
        if remove:
            self._reg["dirty"] = True
            self._bump()
        return len(remove)

    def remove_chunks(self, chunk_ids: list[str]) -> int:
        """Drop specific chunks (rollback path for a failed ingest)."""
        self._ensure_loaded()
        removed = 0
        for cid in chunk_ids:
            if self._docs.pop(cid, None) is not None:
                removed += 1
        if removed:
            self._reg["dirty"] = True
            self._bump()
        return removed

    # ----------------------------------------------------------------- build  #
    def _build(self) -> None:
        self._ensure_loaded()
        self._order = list(self._docs.keys())
        self._built_version = self._reg["version"]
        corpus = [self._docs[cid]["tokens"] for cid in self._order]
        if not corpus:
            self._bm25 = _EmptyBM25()
            return
        try:
            from rank_bm25 import BM25Okapi  # type: ignore

            self._bm25 = BM25Okapi(corpus)
        except Exception:
            self._bm25 = _PurePythonBM25(corpus)

    # ---------------------------------------------------------------- search  #
    def search(
        self,
        query_tokens: list[str],
        user: UserContext,
        top_k: int,
        doc_ids: set[str] | None = None,
    ) -> list[RetrievalResult]:
        self._ensure_loaded()
        # Rebuild if never built or if another handle mutated the shared index.
        if self._bm25 is None or self._built_version != self._reg["version"]:
            self._build()

        allowed = set(cp1_allowed_classifications(user.max_classification))
        scores = self._bm25.get_scores(query_tokens)  # type: ignore[union-attr]
        ranked = sorted(
            range(len(self._order)), key=lambda i: scores[i], reverse=True
        )
        results: list[RetrievalResult] = []
        rank = 0
        for idx in ranked:
            if scores[idx] <= 0:
                break
            chunk: Chunk = self._docs[self._order[idx]]["chunk"]
            # Apply the CP1-equivalent scalar cut here too, so BM25 candidates
            # obey tenant + classification before fusion.
            if chunk.tenant_id != user.tenant_id or chunk.sensitivity not in allowed:
                continue
            if doc_ids and chunk.doc_id not in doc_ids:
                continue
            rank += 1
            results.append(
                RetrievalResult(
                    chunk=chunk,
                    retriever="bm25",
                    rank=rank,
                    score=float(scores[idx]),
                )
            )
            if rank >= top_k:
                break
        return results

    @property
    def size(self) -> int:
        self._ensure_loaded()
        return len(self._docs)


class _EmptyBM25:
    def get_scores(self, _tokens: list[str]) -> list[float]:
        return []


class _PurePythonBM25:
    """Minimal BM25Okapi fallback (k1=1.5, b=0.75) so keyword search works
    without rank-bm25 installed."""

    def __init__(self, corpus: list[list[str]], k1: float = 1.5, b: float = 0.75) -> None:
        self.corpus = corpus
        self.k1 = k1
        self.b = b
        self.N = len(corpus)
        self.doc_len = [len(d) for d in corpus]
        self.avgdl = sum(self.doc_len) / self.N if self.N else 0.0
        self.freqs: list[dict[str, int]] = []
        df: dict[str, int] = {}
        for doc in corpus:
            f: dict[str, int] = {}
            for term in doc:
                f[term] = f.get(term, 0) + 1
            self.freqs.append(f)
            for term in f:
                df[term] = df.get(term, 0) + 1
        self.idf = {
            term: math.log(1 + (self.N - n + 0.5) / (n + 0.5)) for term, n in df.items()
        }

    def get_scores(self, query_tokens: list[str]) -> list[float]:
        scores = [0.0] * self.N
        for term in query_tokens:
            idf = self.idf.get(term)
            if idf is None:
                continue
            for i, f in enumerate(self.freqs):
                tf = f.get(term, 0)
                if tf == 0:
                    continue
                denom = tf + self.k1 * (1 - self.b + self.b * self.doc_len[i] / self.avgdl)
                scores[i] += idf * (tf * (self.k1 + 1)) / denom
        return scores
