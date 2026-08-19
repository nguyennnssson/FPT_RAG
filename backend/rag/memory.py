"""Conversational session memory — rewrites follow-ups into standalone queries.

Runs first on the read path (arch flow step 17), before language detection and
the query embedder. A follow-up like "what about Vietnam?" is unretrievable on
its own; this resolves it into a self-contained question using recent turns.

Design constraints from the architecture docs:
- Keyed by ``prefix:tenant_id:user_id:session_id`` (ACL-scoped), with its own TTL.
- Stores ONLY question/answer text, never retrieved chunks — replaying old
  evidence would bypass CP1/CP2/CP3 and could leak content under stale
  permissions.
- Passes a query through UNCHANGED if it is already self-contained, so genuinely
  new questions are not contaminated with old context.

**Default backend is SQLite** (``data_dir/session_memory.db``) — serverless,
durable across restarts, and correct under multiple workers, so we don't depend
on Redis. It holds conversation text (as Redis would) but lives under the same
protected ``data_dir`` as the index — the accepted trust boundary. Select with
``RAG_MEMORY_BACKEND``: ``sqlite`` (default) | ``redis`` (opt-in) | ``inprocess``.

Condensation uses an injected LLM callable when available (wired by the
pipeline); otherwise a deterministic heuristic keeps the system functional.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Callable, Protocol

from .config import MemoryConfig, RAGConfig, RedisConfig, get_config
from .schemas import UserContext

log = logging.getLogger("rag.memory")

# A condenser turns (query, history) into a standalone query string.
Condenser = Callable[[str, list["Turn"]], str]


@dataclass(frozen=True)
class Turn:
    question: str
    answer: str


# Signals that a query depends on prior context (EN + VI).
_FOLLOWUP_MARKERS = re.compile(
    r"\b(it|its|that|those|these|they|them|this|there|he|she|him|her|"
    r"what about|how about|and|also|nó|đó|họ|còn|vậy|thế|kia)\b",
    re.IGNORECASE,
)
_PRONOUN_START = re.compile(
    r"^\s*(and|but|what about|how about|còn|vậy|thế)\b", re.IGNORECASE
)


class _TurnStore(Protocol):
    def load(self, key: str, limit: int) -> list[Turn]: ...
    def append(self, key: str, turn: Turn, ttl: int, cap: int) -> None: ...


class _RedisTurnStore:
    def __init__(self, url: str) -> None:
        import redis  # type: ignore

        self._r = redis.Redis.from_url(url, decode_responses=True)
        self._r.ping()

    def load(self, key: str, limit: int) -> list[Turn]:
        raw = self._r.lrange(key, -limit, -1)
        turns = []
        for item in raw:
            d = json.loads(item)
            turns.append(Turn(question=d["q"], answer=d["a"]))
        return turns

    def append(self, key: str, turn: Turn, ttl: int, cap: int) -> None:
        pipe = self._r.pipeline()
        pipe.rpush(key, json.dumps({"q": turn.question, "a": turn.answer}))
        pipe.ltrim(key, -cap, -1)
        pipe.expire(key, ttl)
        pipe.execute()


class _MemoryTurnStore:
    """In-process store (per-process, non-durable). RAG_MEMORY_BACKEND=inprocess."""

    def __init__(self) -> None:
        self._data: dict[str, list[Turn]] = {}

    def load(self, key: str, limit: int) -> list[Turn]:
        return self._data.get(key, [])[-limit:]

    def append(self, key: str, turn: Turn, ttl: int, cap: int) -> None:
        self._data.setdefault(key, []).append(turn)
        self._data[key] = self._data[key][-cap:]


class _SqliteTurnStore:
    """Durable, serverless, multi-worker-safe turn store (default backend)."""

    def __init__(self, path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = str(path)
        self._lock = threading.Lock()
        with self._connect() as con:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute(
                "CREATE TABLE IF NOT EXISTS turns "
                "(key TEXT, ts REAL, question TEXT, answer TEXT)"
            )
            con.execute("CREATE INDEX IF NOT EXISTS idx_turns_key ON turns(key, ts)")

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path, timeout=5.0)

    def load(self, key: str, limit: int) -> list[Turn]:
        # Order by rowid (monotonic, unique) so ties in ts can't scramble order.
        with self._lock, self._connect() as con:
            rows = con.execute(
                "SELECT question, answer FROM turns WHERE key=? ORDER BY rowid DESC LIMIT ?",
                (key, limit),
            ).fetchall()
        return [Turn(question=q, answer=a) for q, a in reversed(rows)]

    def append(self, key: str, turn: Turn, ttl: int, cap: int) -> None:
        now = time.time()
        with self._lock, self._connect() as con:
            con.execute(
                "INSERT INTO turns (key, ts, question, answer) VALUES (?,?,?,?)",
                (key, now, turn.question, turn.answer),
            )
            # Purge expired turns (by ts) and trim this key to the cap (by rowid,
            # unique so identical timestamps can't over- or under-delete).
            con.execute("DELETE FROM turns WHERE ts < ?", (now - ttl,))
            con.execute(
                "DELETE FROM turns WHERE key=? AND rowid NOT IN "
                "(SELECT rowid FROM turns WHERE key=? ORDER BY rowid DESC LIMIT ?)",
                (key, key, cap),
            )


class SessionMemory:
    def __init__(
        self,
        memory_config: MemoryConfig | None = None,
        redis_config: RedisConfig | None = None,
        condenser: Condenser | None = None,
        config: RAGConfig | None = None,
    ) -> None:
        cfg = config or get_config()
        self._full_cfg = cfg
        self._cfg = memory_config or cfg.memory
        self._redis_cfg = redis_config or cfg.redis
        self._condenser = condenser
        self._store: _TurnStore | None = None

    def set_condenser(self, condenser: Condenser) -> None:
        """Wire an LLM-backed condenser (called by the pipeline)."""
        self._condenser = condenser

    # ------------------------------------------------------------ store init #
    def _ensure_store(self) -> _TurnStore:
        if self._store is not None:
            return self._store
        backend = self._cfg.backend.lower()
        try:
            if backend == "redis":
                self._store = _RedisTurnStore(self._redis_cfg.url)
            elif backend == "inprocess":
                self._store = _MemoryTurnStore()
            else:  # "sqlite" (default)
                self._store = _SqliteTurnStore(self._full_cfg.paths.session_db_path)
        except Exception as exc:
            if self._redis_cfg.required:
                raise
            log.warning("Session memory '%s' unavailable (%s); using in-process store.",
                        backend, exc)
            self._store = _MemoryTurnStore()
        return self._store

    def _key(self, user: UserContext) -> str:
        # user_id is part of the key: session ids are client-supplied, so
        # without it any user in a tenant could read (via the condenser) the
        # conversation of anyone whose session id they share or guess.
        session = user.session_id or "default"
        return f"{self._cfg.key_prefix}:{user.tenant_id}:{user.user_id}:{session}"

    # ------------------------------------------------------------------ API  #
    def is_followup(self, query: str) -> bool:
        words = query.split()
        if _PRONOUN_START.search(query):
            return True
        if len(words) <= 5 and _FOLLOWUP_MARKERS.search(query):
            return True
        return False

    def rewrite(self, query: str, user: UserContext) -> str:
        """Return a standalone query. Pass-through if already self-contained or
        there is no history to condense against."""
        history = self._ensure_store().load(self._key(user), self._cfg.max_turns)
        if not history or not self.is_followup(query):
            return query
        if self._condenser is not None:
            try:
                rewritten = self._condenser(query, history)
                if rewritten and rewritten.strip():
                    return rewritten.strip()
            except Exception as exc:
                log.warning("LLM condenser failed, using heuristic: %s", exc)
        return self._heuristic_rewrite(query, history)

    def record(self, user: UserContext, question: str, answer: str) -> None:
        """Persist a completed turn (text only, never chunks)."""
        self._ensure_store().append(
            self._key(user),
            Turn(question=question, answer=answer),
            ttl=self._cfg.ttl,
            cap=self._cfg.max_turns * 2,
        )

    # ------------------------------------------------------------ heuristics #
    def _heuristic_rewrite(self, query: str, history: list[Turn]) -> str:
        """Deterministic fallback: anchor the follow-up to the most recent
        question's topic. Not as good as an LLM rewrite, but keeps a follow-up
        retrievable without inventing facts."""
        last_q = history[-1].question.strip().rstrip("?.")
        return f"{query.strip()} (in the context of: {last_q})"


def default_condenser_prompt(query: str, history: list[Turn]) -> str:
    """Build the standard condensation prompt for an LLM condenser."""
    convo = "\n".join(f"User: {t.question}\nAssistant: {t.answer}" for t in history)
    return (
        "Given the conversation so far and a follow-up question, rewrite the "
        "follow-up as a standalone question that can be understood without the "
        "conversation. Do not answer it. Preserve all constraints.\n\n"
        f"Conversation:\n{convo}\n\nFollow-up: {query}\n\nStandalone question:"
    )
