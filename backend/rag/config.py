"""Central configuration for the FPT RAG system.

One place for every tunable knob — model names, thresholds, top-k values,
TTLs, file paths — so every module agrees on the same numbers instead of
hard-coding them independently. Values are sourced from environment
variables where it makes sense (secrets, hosts) and otherwise carry the
defaults fixed by the architecture docs.

Nothing heavy is imported here; this module is safe to import anywhere.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #

# rag/ package dir -> project root (FPT_RAG/)
_PACKAGE_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _PACKAGE_DIR.parent


def load_env_file(path: Path | None = None) -> None:
    """Load a simple ``.env`` file without requiring python-dotenv.

    Existing process variables always win. The helper is shared by the API and
    Alembic so both commands resolve the same database and backend settings.
    """
    env_path = path or (_PROJECT_ROOT / ".env")
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.lower().startswith("export "):
            line = line[7:].lstrip()
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not key:
            continue
        if value[:1] not in ("'", '"'):
            comment_index = value.find(" #")
            if comment_index != -1:
                value = value[:comment_index].rstrip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw not in (None, "") else default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw not in (None, "") else default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class PathConfig:
    """Filesystem locations for persisted state."""

    data_dir: Path = field(default_factory=lambda: Path(_env("RAG_DATA_DIR", str(_PROJECT_ROOT / "data"))))

    @property
    def chroma_dir(self) -> Path:
        return self.data_dir / "chroma"

    @property
    def bm25_path(self) -> Path:
        # Pickled rank-bm25 index (see vectorstore / retriever).
        return self.data_dir / "bm25" / "index.pkl"

    @property
    def audit_log_path(self) -> Path:
        return self.data_dir / "audit" / "audit.log.jsonl"

    @property
    def quarantine_dir(self) -> Path:
        # Where injection-flagged chunks are parked instead of indexed.
        return self.data_dir / "quarantine"

    @property
    def originals_dir(self) -> Path:
        """Private storage for original uploaded binaries.

        Files here are never served directly by the web server. The API resolves
        them through an ACL-checked endpoint instead.
        """
        return self.data_dir / "originals"

    @property
    def session_db_path(self) -> Path:
        # SQLite session-memory store (serverless replacement for Redis).
        return self.data_dir / "session_memory.db"

    @property
    def cache_generations_path(self) -> Path:
        # Per-tenant cache-generation counters — file-backed so an ingest/delete
        # in one process invalidates another process's answer cache.
        return self.data_dir / "cache_generations.json"

    def ensure(self) -> None:
        """Create every directory this config points at. Cheap and idempotent."""
        for p in (
            self.data_dir,
            self.chroma_dir,
            self.bm25_path.parent,
            self.audit_log_path.parent,
            self.quarantine_dir,
            self.originals_dir,
        ):
            p.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ModelConfig:
    """Embedding, reranking and generation model settings."""

    # Dense embedder — BGE-M3, 1024-dim, asymmetric passage/query modes.
    embedding_model: str = field(default_factory=lambda: _env("RAG_EMBEDDING_MODEL", "BAAI/bge-m3"))
    embedding_dim: int = field(default_factory=lambda: _env_int("RAG_EMBEDDING_DIM", 1024))
    embedding_revision: str | None = field(default_factory=lambda: os.environ.get("RAG_EMBEDDING_REVISION") or None)
    # BGE-M3 is asymmetric: passages are embedded bare; queries get a prefix so
    # they land in the same space (handbook C-D; arch flow steps 9 & 18).
    query_instruction_prefix: str = field(default_factory=lambda: _env(
        "RAG_QUERY_PREFIX",
        "Represent this sentence for searching relevant passages: ",
    ))

    # Cross-encoder reranker.
    reranker_model: str = field(default_factory=lambda: _env("RAG_RERANKER_MODEL", "BAAI/bge-reranker-v2-m3"))
    reranker_revision: str | None = field(default_factory=lambda: os.environ.get("RAG_RERANKER_REVISION") or None)

    # torch device: "auto" resolves to cuda if available else cpu (see embedder).
    device: str = field(default_factory=lambda: _env("RAG_DEVICE", "auto"))

    # Normalize embeddings so cosine == dot product (handbook D.3).
    normalize_embeddings: bool = field(default_factory=lambda: _env_bool("RAG_NORMALIZE_EMBEDDINGS", True))

    # --- OpenAI-compatible API embedder (for a VDI that can't download models) ---
    # Activate with RAG_EMBEDDER_BACKEND=openai. dim must match the API model
    # (e.g. text-embedding-3-small = 1536) via RAG_EMBEDDING_DIM.
    embedding_api_base_url: str | None = field(default_factory=lambda: os.environ.get("RAG_EMBEDDING_API_BASE_URL") or None)
    embedding_api_model: str = field(default_factory=lambda: _env("RAG_EMBEDDING_API_MODEL", "text-embedding-3-small"))
    embedding_api_key_env: str = field(default_factory=lambda: _env("RAG_EMBEDDING_API_KEY_ENV", "OPENAI_API_KEY"))

    @property
    def embedding_api_key(self) -> str | None:
        return os.environ.get(self.embedding_api_key_env)


# --------------------------------------------------------------------------- #
# Chunking
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ChunkConfig:
    """Recursive chunker sizing (arch flow step 4)."""

    min_tokens: int = field(default_factory=lambda: _env_int("RAG_CHUNK_MIN_TOKENS", 400))
    max_tokens: int = field(default_factory=lambda: _env_int("RAG_CHUNK_MAX_TOKENS", 512))
    # 10-15% overlap; expressed as a fraction of max_tokens.
    overlap_ratio: float = field(default_factory=lambda: _env_float("RAG_CHUNK_OVERLAP_RATIO", 0.12))
    # A chunk below this is treated as noise/orphan and dropped.
    drop_below_tokens: int = field(default_factory=lambda: _env_int("RAG_CHUNK_DROP_BELOW_TOKENS", 20))

    @property
    def overlap_tokens(self) -> int:
        return max(0, round(self.max_tokens * self.overlap_ratio))


# --------------------------------------------------------------------------- #
# Retrieval / reranking / context
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RetrievalConfig:
    """Candidate generation, fusion, reranking and context-packing knobs."""

    dense_top_k: int = field(default_factory=lambda: _env_int("RAG_DENSE_TOP_K", 20))
    bm25_top_k: int = field(default_factory=lambda: _env_int("RAG_BM25_TOP_K", 20))
    # CP2 mitigation: over-fetch for restrictive principals so an ACL filter
    # doesn't starve the candidate set below what the reranker needs.
    restrictive_top_k: int = field(default_factory=lambda: _env_int("RAG_RESTRICTIVE_TOP_K", 40))
    # A principal set at or below this size is treated as "restrictive".
    restrictive_principal_threshold: int = field(default_factory=lambda: _env_int("RAG_RESTRICTIVE_PRINCIPAL_THRESHOLD", 2))

    # Reciprocal Rank Fusion dampening constant (handbook D.4).
    rrf_k: int = field(default_factory=lambda: _env_int("RAG_RRF_K", 60))

    # Reranker keeps this many after the precision pass.
    rerank_top_n: int = field(default_factory=lambda: _env_int("RAG_RERANK_TOP_N", 5))

    # Risk-graded reranker score thresholds. Below the applicable threshold =>
    # abstain rather than force an answer (handbook F.3).
    rerank_threshold_low: float = field(default_factory=lambda: _env_float("RAG_RERANK_THRESHOLD_LOW", 0.35))
    rerank_threshold_medium: float = field(default_factory=lambda: _env_float("RAG_RERANK_THRESHOLD_MEDIUM", 0.50))
    rerank_threshold_high: float = field(default_factory=lambda: _env_float("RAG_RERANK_THRESHOLD_HIGH", 0.65))

    # Context assembly.
    context_token_budget: int = field(default_factory=lambda: _env_int("RAG_CONTEXT_TOKEN_BUDGET", 3000))
    max_chunks_per_parent: int = field(default_factory=lambda: _env_int("RAG_MAX_CHUNKS_PER_PARENT", 2))
    dedup_similarity_threshold: float = field(default_factory=lambda: _env_float("RAG_DEDUP_SIM_THRESHOLD", 0.95))

    def rerank_threshold(self, risk: str) -> float:
        return {
            "low": self.rerank_threshold_low,
            "medium": self.rerank_threshold_medium,
            "high": self.rerank_threshold_high,
        }.get(risk, self.rerank_threshold_medium)


# --------------------------------------------------------------------------- #
# Language
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class LanguageConfig:
    """Language detection + response-language routing (arch flow steps 6, 18)."""

    supported: tuple[str, ...] = ("en", "vi")
    default_language: str = field(default_factory=lambda: _env("RAG_DEFAULT_LANGUAGE", "en"))
    # Query-time routing is confidence-gated; below this we fall back to the
    # default language rather than trusting a shaky guess (overview step 27).
    confidence_gate: float = field(default_factory=lambda: _env_float("RAG_LANG_CONFIDENCE_GATE", 0.85))


# --------------------------------------------------------------------------- #
# Query validation
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class QueryConfig:
    """Input-validation bounds (arch flow step 16)."""

    # Counted with the embedding model's real tokenizer, NOT str.split()
    # (handbook/overview note on step 16).
    max_query_tokens: int = field(default_factory=lambda: _env_int("RAG_MAX_QUERY_TOKENS", 256))
    max_query_chars: int = field(default_factory=lambda: _env_int("RAG_MAX_QUERY_CHARS", 4000))
    min_query_chars: int = field(default_factory=lambda: _env_int("RAG_MIN_QUERY_CHARS", 1))


# --------------------------------------------------------------------------- #
# Redis / cache / session memory
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RedisConfig:
    """Redis connection — used ONLY when a redis backend is explicitly selected
    (RAG_CACHE_BACKEND=redis / RAG_MEMORY_BACKEND=redis). Redis is not the
    default: this enterprise internal-docs system runs serverless (in-process
    cache + SQLite session memory) to avoid a second store of ACL-gated content
    and a standing service in the VDI."""

    url: str = field(default_factory=lambda: _env("RAG_REDIS_URL", "redis://localhost:6379/0"))
    required: bool = field(default_factory=lambda: _env_bool("RAG_REDIS_REQUIRED", False))


@dataclass(frozen=True)
class CacheConfig:
    """Cache backend + TTLs for the three layers (arch flow step 30). Seconds."""

    # memory (default, serverless) | redis (opt-in) | off
    backend: str = field(default_factory=lambda: _env("RAG_CACHE_BACKEND", "memory"))
    # LRU cap for the in-process backend so a large key space can't grow forever.
    max_entries: int = field(default_factory=lambda: _env_int("RAG_CACHE_MAX_ENTRIES", 2048))
    embedding_ttl: int = field(default_factory=lambda: _env_int("RAG_CACHE_EMBED_TTL", 3600))  # 1h
    retrieval_ttl: int = field(default_factory=lambda: _env_int("RAG_CACHE_RETRIEVAL_TTL", 600))   # 10m (5-15m band)
    llm_ttl: int = field(default_factory=lambda: _env_int("RAG_CACHE_LLM_TTL", 1800))              # 30m (15-60m band)
    key_prefix: str = field(default_factory=lambda: _env("RAG_CACHE_PREFIX", "rag:cache"))


@dataclass(frozen=True)
class MemoryConfig:
    """Conversational session-memory settings (arch flow step 17)."""

    # sqlite (default, durable, serverless) | redis (opt-in) | inprocess
    backend: str = field(default_factory=lambda: _env("RAG_MEMORY_BACKEND", "sqlite"))
    key_prefix: str = field(default_factory=lambda: _env("RAG_MEMORY_PREFIX", "rag:mem"))
    # How many prior turns to condense a follow-up against.
    max_turns: int = field(default_factory=lambda: _env_int("RAG_MEMORY_MAX_TURNS", 6))
    ttl: int = field(default_factory=lambda: _env_int("RAG_MEMORY_TTL", 3600))


# --------------------------------------------------------------------------- #
# Generation / LLM provider
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class GeneratorConfig:
    """LLM provider settings. The API key is injected later (env); no key is
    required to import this package — only to actually generate.

    Providers: ``anthropic`` (Claude), ``openai`` (OpenAI or any OpenAI-compatible
    gateway such as the company ``aiportalapi`` — set ``base_url``), ``codex_cli``
    (trusted local demo using an existing Codex ChatGPT login), and ``extractive``
    (no-LLM fallback).
    """

    provider: str = field(default_factory=lambda: _env("RAG_LLM_PROVIDER", "anthropic"))
    model: str = field(default_factory=lambda: _env("RAG_LLM_MODEL", "claude-opus-4-8"))
    # base_url lets the openai provider point at aiportalapi (or Azure/other).
    base_url: str | None = field(default_factory=lambda: os.environ.get("RAG_LLM_BASE_URL") or None)
    # If unset, a per-provider default key env is used (see resolved_key_env).
    api_key_env: str = field(default_factory=lambda: _env("RAG_LLM_API_KEY_ENV", ""))
    max_tokens: int = field(default_factory=lambda: _env_int("RAG_LLM_MAX_TOKENS", 1024))
    temperature: float = field(default_factory=lambda: _env_float("RAG_LLM_TEMPERATURE", 0.0))
    # ``auto`` leaves effort selection to the provider/model. Set an explicit
    # value only when an operator deliberately wants to override it.
    reasoning_effort: str = field(
        default_factory=lambda: _env("RAG_LLM_REASONING_EFFORT", "auto")
    )
    prompt_version: str = field(default_factory=lambda: _env("RAG_PROMPT_VERSION", "grounded-v1"))
    timeout_seconds: float = field(default_factory=lambda: _env_float("RAG_LLM_TIMEOUT", 60.0))

    def resolved_key_env(self) -> str:
        if self.api_key_env:
            return self.api_key_env
        return {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}.get(
            self.provider, "LLM_API_KEY"
        )

    @property
    def api_key(self) -> str | None:
        return os.environ.get(self.resolved_key_env())


# --------------------------------------------------------------------------- #
# Security
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SecurityConfig:
    """Injection scanning + PII redaction policy (arch flow steps 7, 8)."""

    # Sensitivity levels at which PII must be redacted before indexing.
    redact_pii_for: tuple[str, ...] = ("public", "internal")
    # Chunk is quarantined (not indexed) if injection score >= this.
    injection_quarantine_threshold: int = field(default_factory=lambda: _env_int("RAG_INJECTION_THRESHOLD", 1))


# --------------------------------------------------------------------------- #
# Vector store
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class VectorStoreConfig:
    """ChromaDB collection settings."""

    collection_name: str = field(default_factory=lambda: _env("RAG_CHROMA_COLLECTION", "fpt_rag"))
    distance: str = field(default_factory=lambda: _env("RAG_CHROMA_DISTANCE", "cosine"))


# --------------------------------------------------------------------------- #
# Evaluation
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class EvalConfig:
    """Regression gate (arch flow step 34)."""

    primary_metric: str = field(default_factory=lambda: _env("RAG_EVAL_METRIC", "ndcg@5"))
    # Block deploy if the metric drops more than this vs. the approved baseline.
    max_regression: float = field(default_factory=lambda: _env_float("RAG_EVAL_MAX_REGRESSION", 0.03))


# --------------------------------------------------------------------------- #
# Version stamps (provenance — arch flow step 11)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class VersionConfig:
    """Component versions stamped onto every chunk so a model/parser/chunker
    change can be detected and re-embedded later."""

    chunker_version: str = "chunker-1.1.0"
    parser_version: str = "parser-1.1.0"
    embedding_version: str = "bge-m3-1.0.0"
    index_version: str = field(default_factory=lambda: _env("RAG_INDEX_VERSION", "index-1"))


# --------------------------------------------------------------------------- #
# Top-level config
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RAGConfig:
    """The single settings object every module reads from.

    Construct one with ``RAGConfig()`` (env-driven defaults) and pass it down,
    or call ``RAGConfig.load()`` which also ensures data directories exist.
    """

    paths: PathConfig = field(default_factory=PathConfig)
    models: ModelConfig = field(default_factory=ModelConfig)
    chunking: ChunkConfig = field(default_factory=ChunkConfig)
    retrieval: RetrievalConfig = field(default_factory=RetrievalConfig)
    language: LanguageConfig = field(default_factory=LanguageConfig)
    query: QueryConfig = field(default_factory=QueryConfig)
    redis: RedisConfig = field(default_factory=RedisConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    generator: GeneratorConfig = field(default_factory=GeneratorConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    vectorstore: VectorStoreConfig = field(default_factory=VectorStoreConfig)
    eval: EvalConfig = field(default_factory=EvalConfig)
    versions: VersionConfig = field(default_factory=VersionConfig)

    @classmethod
    def load(cls) -> "RAGConfig":
        cfg = cls()
        cfg.paths.ensure()
        return cfg


# A process-wide default, lazily built so importing rag.config never touches
# the filesystem or the environment more than reading a few vars.
_DEFAULT: RAGConfig | None = None


def get_config() -> RAGConfig:
    """Return the shared default config, building it once on first use."""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = RAGConfig()
    return _DEFAULT
