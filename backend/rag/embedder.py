"""Meaning encoder — BGE-M3 dense embeddings, 1024-dim.

Asymmetric by design (arch flow steps 9 & 18, handbook C-D):
- ``embed_passages`` (ingest): NO instruction prefix.
- ``embed_query`` (read): prepends the query instruction prefix so the query
  lands in the same vector space as the unprefixed passage vectors.

The real model (~2.5 GB) is loaded lazily on first embed call, never at import,
so ``import rag`` and unit tests stay fast. Backends, tried in order unless one
is pinned via ``RAG_EMBEDDER_BACKEND``:

- ``sentence_transformers`` — SentenceTransformer("BAAI/bge-m3")
- ``flag`` — FlagEmbedding.BGEM3FlagModel
- ``hash`` — a deterministic, dependency-free fallback for offline dev/tests
  (NOT semantically meaningful; enable explicitly).

If a real backend is requested but unavailable, we raise ConfigurationError
with a clear message rather than silently degrading retrieval quality.
"""

from __future__ import annotations

import hashlib
import math
import os
from typing import Sequence

from .config import ModelConfig, get_config
from .schemas import ConfigurationError


def _resolve_device(device: str) -> str:
    if device != "auto":
        return device
    try:
        import torch  # type: ignore

        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:
        return "cpu"


class Embedder:
    """Lazy BGE-M3 embedder with passage/query asymmetry."""

    def __init__(self, config: ModelConfig | None = None) -> None:
        self._cfg = config or get_config().models
        self._model = None
        self._backend: str | None = None
        self._requested = os.environ.get("RAG_EMBEDDER_BACKEND", "auto")

    # ------------------------------------------------------------- lifecycle #
    def _ensure_model(self) -> None:
        if self._backend is not None:
            return
        # "auto" prefers local BGE-M3 backends; the API embedder and hash
        # fallback are only used when explicitly requested.
        order = (
            [self._requested]
            if self._requested != "auto"
            else ["sentence_transformers", "flag"]
        )
        errors: list[str] = []
        for backend in order:
            try:
                self._load(backend)
                self._backend = backend
                return
            except ConfigurationError:
                raise
            except Exception as exc:  # pragma: no cover - depends on env
                errors.append(f"{backend}: {exc}")
        raise ConfigurationError(
            "No embedding backend available. Tried: "
            + "; ".join(errors)
            + ". Install `sentence-transformers` or `FlagEmbedding`, or set "
            "RAG_EMBEDDER_BACKEND=hash for a non-semantic offline fallback."
        )

    def _load(self, backend: str) -> None:
        if backend == "sentence_transformers":
            from sentence_transformers import SentenceTransformer  # type: ignore

            self._model = SentenceTransformer(
                self._cfg.embedding_model,
                device=_resolve_device(self._cfg.device),
                revision=self._cfg.embedding_revision,
            )
        elif backend == "flag":
            from FlagEmbedding import BGEM3FlagModel  # type: ignore

            self._model = BGEM3FlagModel(
                self._cfg.embedding_model,
                use_fp16=_resolve_device(self._cfg.device) != "cpu",
            )
        elif backend == "openai":
            self._model = _OpenAIEmbeddingModel(self._cfg)
        elif backend == "hash":
            self._model = _HashModel(self._cfg.embedding_dim)
        else:
            raise ConfigurationError(f"Unknown embedder backend: {backend!r}")

    # ---------------------------------------------------------------- public #
    @property
    def dim(self) -> int:
        return self._cfg.embedding_dim

    @property
    def backend(self) -> str:
        self._ensure_model()
        return self._backend or "unknown"

    def signature(self) -> str:
        """Stable id of the vector space this embedder produces — for cache keys.

        Two embedders with the same signature yield comparable vectors; a
        different backend / model / revision / dimension MUST produce a different
        signature so cached vectors from a previous embedder are never reused
        (e.g. when the VDI switches hash -> BGE-M3 -> openai@1536)."""
        self._ensure_model()
        if self._backend == "openai":
            model = self._cfg.embedding_api_model
            extra = self._cfg.embedding_api_base_url or "openai"
        else:
            model = self._cfg.embedding_model
            extra = self._cfg.embedding_revision or "-"
        return f"{self._backend}|{model}|{extra}|{self._cfg.embedding_dim}"

    def _uses_query_prefix(self) -> bool:
        # The query instruction prefix is a BGE-M3 asymmetry; API/hash
        # embeddings are symmetric and must NOT receive it.
        self._ensure_model()
        return self._backend in ("sentence_transformers", "flag")

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        """Ingest-side: embed contextual_text with NO prefix."""
        return self._encode(list(texts))

    def embed_query(self, text: str) -> list[float]:
        """Read-side: embed a single query, WITH the BGE instruction prefix
        (only when the backend is a BGE-M3 model)."""
        if self._uses_query_prefix():
            text = self._cfg.query_instruction_prefix + text
        return self._encode([text])[0]

    def embed_queries(self, texts: Sequence[str]) -> list[list[float]]:
        if self._uses_query_prefix():
            texts = [self._cfg.query_instruction_prefix + t for t in texts]
        return self._encode(list(texts))

    # ------------------------------------------------------------- internals #
    def _encode(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        self._ensure_model()
        if self._backend == "sentence_transformers":
            vecs = self._model.encode(  # type: ignore[union-attr]
                texts,
                normalize_embeddings=self._cfg.normalize_embeddings,
                convert_to_numpy=True,
            )
            return [v.tolist() for v in vecs]
        if self._backend == "flag":
            out = self._model.encode(texts)["dense_vecs"]  # type: ignore[union-attr]
            vecs = [list(map(float, v)) for v in out]
            return [_normalize(v) if self._cfg.normalize_embeddings else v for v in vecs]
        if self._backend == "openai":
            vecs = self._model.encode(texts)  # type: ignore[union-attr]
            return [_normalize(v) if self._cfg.normalize_embeddings else v for v in vecs]
        # hash fallback
        vecs = [self._model.encode(t) for t in texts]  # type: ignore[union-attr]
        return [_normalize(v) if self._cfg.normalize_embeddings else v for v in vecs]


class _OpenAIEmbeddingModel:
    """OpenAI-compatible embeddings (OpenAI, Azure, or the company aiportalapi
    gateway). Lazily builds the client; batches all texts in one request."""

    def __init__(self, cfg: ModelConfig) -> None:
        self._cfg = cfg
        api_key = cfg.embedding_api_key
        if not api_key:
            raise ConfigurationError(
                f"No embedding API key in ${cfg.embedding_api_key_env}. Set it to "
                "use RAG_EMBEDDER_BACKEND=openai."
            )
        try:
            from openai import OpenAI  # type: ignore
        except Exception as exc:  # pragma: no cover
            raise ConfigurationError("The `openai` package is not installed.") from exc
        self._client = OpenAI(api_key=api_key, base_url=cfg.embedding_api_base_url)

    def encode(self, texts: list[str]) -> list[list[float]]:
        resp = self._client.embeddings.create(
            model=self._cfg.embedding_api_model, input=texts
        )
        return [list(d.embedding) for d in resp.data]


class _HashModel:
    """Deterministic bag-of-hashed-tokens vector. Offline dev/test only.

    Produces stable, non-random vectors where lexical overlap yields cosine
    similarity — enough to exercise the pipeline end to end without a GPU or a
    2.5 GB download, but NOT a substitute for real embeddings in production.
    """

    def __init__(self, dim: int) -> None:
        self.dim = dim

    def encode(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        tokens = text.lower().split()
        for tok in tokens:
            h = int(hashlib.md5(tok.encode("utf-8")).hexdigest(), 16)
            vec[h % self.dim] += 1.0
        return vec


def _normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0.0:
        return vec
    return [x / norm for x in vec]


_shared: Embedder | None = None


def get_embedder() -> Embedder:
    global _shared
    if _shared is None:
        _shared = Embedder()
    return _shared
