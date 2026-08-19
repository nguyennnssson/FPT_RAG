"""Cache for the expensive paths (embed / retrieve / generate).

Keys are **ACL-scoped**: the key includes the requesting user's permission
fingerprint, not just the query text. Otherwise User A's cached answer — built
from chunks only A can see — could be served to User B (handbook M.8; step 30).

**Default backend is serverless in-process** (TTL + LRU), because this enterprise
internal-docs system deliberately avoids standing up Redis: a second store of
ACL-gated content plus a networked service in a guardrailed VDI is not worth the
security/ops surface for diverse, low-repeat internal traffic. Select a backend
with ``RAG_CACHE_BACKEND``:

- ``memory`` (default) — per-process TTL+LRU dict. Fast repeats within a worker.
- ``redis``  — opt-in, only if the enterprise approves it (needs the redis pkg).
- ``off``    — disable caching entirely.

Cross-process invalidation: answer caches are stamped with a per-tenant
*generation* counter that is bumped on ingest/delete. The counter is
**file-backed** (``data_dir/cache_generations.json``), so a CLI ingest in one
process invalidates the API process's cached answers even though each process
has its own in-memory cache.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from collections import OrderedDict
from typing import Any

from .config import CacheConfig, RAGConfig, RedisConfig, get_config
from .schemas import ConfigurationError, UserContext

log = logging.getLogger("rag.cache")


def acl_fingerprint(user: UserContext) -> str:
    """Stable hash of the identity that determines what a user may retrieve.
    Two users with the same tenant + principals + ceiling share cache entries;
    anyone else does not."""
    principals = sorted(user.all_principals())
    basis = json.dumps(
        {"t": user.tenant_id, "p": principals, "c": user.max_classification},
        sort_keys=True,
    )
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# Backends
# --------------------------------------------------------------------------- #


class _MemoryCacheBackend:
    """Per-process TTL + LRU cache. Serverless default."""

    def __init__(self, max_entries: int) -> None:
        self._max = max(1, max_entries)
        self._data: "OrderedDict[str, tuple[float, Any]]" = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> Any | None:
        now = time.time()
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            expiry, value = item
            if expiry < now:
                self._data.pop(key, None)
                return None
            self._data.move_to_end(key)  # LRU touch
            return value

    def set(self, key: str, value: Any, ttl: int) -> None:
        now = time.time()
        with self._lock:
            self._data[key] = (now + ttl, value)
            self._data.move_to_end(key)
            # LRU eviction until under the cap (oldest = least recently used).
            while len(self._data) > self._max:
                self._data.popitem(last=False)


class _RedisCacheBackend:
    """Opt-in Redis backend (RAG_CACHE_BACKEND=redis)."""

    def __init__(self, url: str) -> None:
        import redis  # type: ignore

        self._r = redis.Redis.from_url(url, decode_responses=True)
        self._r.ping()

    def get(self, key: str) -> Any | None:
        raw = self._r.get(key)
        return json.loads(raw) if raw is not None else None

    def set(self, key: str, value: Any, ttl: int) -> None:
        self._r.setex(key, ttl, json.dumps(value))


# --------------------------------------------------------------------------- #
# File-backed per-tenant generation counters (cross-process invalidation)
# --------------------------------------------------------------------------- #


class _GenerationStore:
    def __init__(self, path, redis_backend: _RedisCacheBackend | None, prefix: str) -> None:
        self._path = path
        self._redis = redis_backend  # if redis is the cache backend, keep it there
        self._prefix = prefix
        self._lock = threading.Lock()

    def get(self, tenant_id: str) -> str:
        if self._redis is not None:
            return str(self._redis._r.get(f"{self._prefix}:gen:{tenant_id}") or "0")
        with self._lock:
            return str(self._read().get(tenant_id, 0))

    def bump(self, tenant_id: str) -> None:
        if self._redis is not None:
            self._redis._r.incr(f"{self._prefix}:gen:{tenant_id}")
            return
        with self._lock:
            data = self._read()
            data[tenant_id] = int(data.get(tenant_id, 0)) + 1
            self._write(data)

    def _read(self) -> dict:
        try:
            return json.loads(self._path.read_text("utf-8"))
        except Exception:
            return {}

    def _write(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), "utf-8")
        os.replace(tmp, self._path)


# --------------------------------------------------------------------------- #
# Cache facade
# --------------------------------------------------------------------------- #


class Cache:
    """ACL-scoped JSON cache with per-layer TTLs over a selectable backend."""

    def __init__(
        self,
        redis_config: RedisConfig | None = None,
        cache_config: CacheConfig | None = None,
        config: RAGConfig | None = None,
    ) -> None:
        cfg = config or get_config()
        self._full_cfg = cfg
        self._redis_cfg = redis_config or cfg.redis
        self._cfg = cache_config or cfg.cache
        self._backend = None            # None until first use
        self._gen: _GenerationStore | None = None
        self._state: str | None = None  # None | "ready" | "disabled"

    # ------------------------------------------------------------ lifecycle  #
    def _ensure(self) -> None:
        if self._state is not None:
            return
        kind = self._cfg.backend.lower()
        try:
            if kind == "off":
                self._backend = None
                self._state = "disabled"
            elif kind == "redis":
                self._backend = _RedisCacheBackend(self._redis_cfg.url)
                self._state = "ready"
            else:  # "memory" (default)
                self._backend = _MemoryCacheBackend(self._cfg.max_entries)
                self._state = "ready"
        except Exception as exc:
            if self._redis_cfg.required:
                raise ConfigurationError(f"Cache backend unavailable: {exc}") from exc
            log.warning("Cache disabled (%s backend unavailable: %s)", kind, exc)
            self._backend = None
            self._state = "disabled"
        # Generation counters: co-located with a redis cache, else file-backed.
        redis_be = self._backend if isinstance(self._backend, _RedisCacheBackend) else None
        self._gen = _GenerationStore(
            self._full_cfg.paths.cache_generations_path, redis_be, self._cfg.key_prefix
        )

    @property
    def enabled(self) -> bool:
        self._ensure()
        return self._state == "ready"

    @property
    def backend_name(self) -> str:
        self._ensure()
        return "disabled" if self._state != "ready" else self._cfg.backend.lower()

    # ------------------------------------------------------------------ keys #
    def _key(self, layer: str, user: UserContext | None, *parts: str) -> str:
        scope = acl_fingerprint(user) if user is not None else "global"
        digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:24]
        return f"{self._cfg.key_prefix}:{layer}:{scope}:{digest}"

    def _ttl(self, layer: str) -> int:
        return {
            "embed": self._cfg.embedding_ttl,
            "retrieval": self._cfg.retrieval_ttl,
            "llm": self._cfg.llm_ttl,
        }.get(layer, self._cfg.retrieval_ttl)

    # ------------------------------------------------------------ operations #
    def get(self, layer: str, user: UserContext | None, *parts: str) -> Any | None:
        self._ensure()
        if self._state != "ready":
            return None
        try:
            return self._backend.get(self._key(layer, user, *parts))  # type: ignore[union-attr]
        except Exception as exc:  # pragma: no cover - backend dependent
            log.warning("Cache get failed: %s", exc)
            return None

    def set(self, layer: str, user: UserContext | None, value: Any, *parts: str) -> None:
        self._ensure()
        if self._state != "ready":
            return
        try:
            self._backend.set(self._key(layer, user, *parts), value, self._ttl(layer))  # type: ignore[union-attr]
        except Exception as exc:  # pragma: no cover - backend dependent
            log.warning("Cache set failed: %s", exc)

    # Tenant generation (cross-process invalidation) ------------------------- #
    def tenant_generation(self, tenant_id: str) -> str:
        self._ensure()
        try:
            return self._gen.get(tenant_id)  # type: ignore[union-attr]
        except Exception:
            return "0"

    def bump_tenant_generation(self, tenant_id: str) -> None:
        self._ensure()
        try:
            self._gen.bump(tenant_id)  # type: ignore[union-attr]
        except Exception as exc:
            log.warning("Cache generation bump failed: %s", exc)

    # Layer-specific sugar ---------------------------------------------------- #
    def get_embedding(self, signature: str, text: str) -> list[float] | None:
        # signature scopes the key to a specific embedder (backend/model/dim), so
        # switching embedders never serves vectors from a different vector space.
        return self.get("embed", None, signature, text)

    def set_embedding(self, signature: str, text: str, vector: list[float]) -> None:
        self.set("embed", None, vector, signature, text)

    def get_retrieval(self, user: UserContext, query: str) -> Any | None:
        return self.get("retrieval", user, query)

    def set_retrieval(self, user: UserContext, query: str, payload: Any) -> None:
        self.set("retrieval", user, payload, query)

    def get_answer(self, user: UserContext, query: str) -> Any | None:
        return self.get("llm", user, query)

    def set_answer(self, user: UserContext, query: str, payload: Any) -> None:
        self.set("llm", user, payload, query)


_shared: Cache | None = None


def get_cache() -> Cache:
    global _shared
    if _shared is None:
        _shared = Cache()
    return _shared
