"""FPT RAG — bilingual (VI/EN) hybrid-retrieval RAG system.

Public API:

    from rag import RAGPipeline, Ingester, RAGConfig
    from rag import SourceDocument, UserContext, RagAnswer

    cfg = RAGConfig.load()
    Ingester(cfg).ingest(source_document)
    answer = RAGPipeline(cfg).query("What is the refund window?", user)

Every heavy dependency (BGE-M3, the reranker, ChromaDB, the LLM SDK) is
loaded lazily, so importing ``rag`` is cheap and the package works offline with
in-memory / fallback backends until the real services are configured.
"""

from __future__ import annotations

from .config import RAGConfig, get_config
from .schemas import (
    Chunk,
    CitedClaim,
    ConfigurationError,
    IngestionResult,
    RagAnswer,
    RAGError,
    RetrievalResult,
    SecurityViolation,
    Source,
    SourceDocument,
    Tombstone,
    UserContext,
    ValidationError,
)
from .ingester import Ingester
from .pipeline import RAGPipeline

__all__ = [
    "RAGConfig",
    "get_config",
    "RAGPipeline",
    "Ingester",
    "SourceDocument",
    "UserContext",
    "RagAnswer",
    "Source",
    "Chunk",
    "CitedClaim",
    "RetrievalResult",
    "IngestionResult",
    "Tombstone",
    "RAGError",
    "ConfigurationError",
    "ValidationError",
    "SecurityViolation",
]

__version__ = "1.0.0"
