# PM Review — Redis Removal: Gap Analysis & Fix Specification

**Date:** 2026-07-08 · **Reviewer:** PM pass · **Executor:** Opus
**Decision under review:** Redis is **not an option** for this enterprise internal-documents
search system (new networked service in a guardrailed VDI; second plaintext store of
ACL-gated content; low cache-hit value on diverse internal traffic).

---

## 1. Review verdict: the removal is currently DOC-ONLY — the system does not yet honor it

What changed so far: the word "Redis" was removed from the mentor checklist. **No code changed.**
The backend still treats Redis as the primary backend for two subsystems, with fallbacks that
were designed as *degraded modes*, not as the plan of record:

| Subsystem | Today without Redis | Consequence if we ship as-is |
|---|---|---|
| `rag/cache.py` (embed / retrieval / answer caches) | silently **no-ops** (`_state="disabled"`) | **Milestone M4 fails** — "repeat questions noticeably faster" is untrue; every repeat re-runs embed→retrieve→generate |
| `rag/memory.py` (session memory) | falls back to **in-process dict** | follow-up rewriting lost on every restart and broken under >1 uvicorn worker |
| `docker-compose.yml` | ships a `redis:` service + `RAG_REDIS_URL` | deploys the very service the decision forbids |
| `requirements.txt` / `requirements-vdi-minimal.txt` | `redis>=5.0.0` mandatory-looking | VDI installs a client for a server that won't exist |
| `/health` (api/main.py) | reports `redis: unavailable` | permanent false-alarm signal on the VDI |
| `.env.example`, `README.md`, `requirements.txt` playbook | document Redis as the cache/memory backend | docs contradict the architecture decision |

**Conclusion:** accept the decision, then make the code match it — serverless backends must be
**first-class defaults**, not degraded fallbacks. Redis code paths may remain as an optional
backend (config-selected) for a future where the enterprise approves it, but nothing may
require or default to it.

---

## 2. Recommended fix (design)

Both replacement backends use only what the VDI already allows: **process memory + the local
data directory** (same trust boundary as the ChromaDB index — no new store of gated content).

### F-1 · Cache: in-process TTL+LRU backend (new default)
- `rag/cache.py`: introduce a backend seam. `RAG_CACHE_BACKEND = memory | redis | off`,
  **default `memory`**.
- `_MemoryCacheBackend`: dict + per-entry expiry (TTLs from existing `CacheConfig`), LRU cap
  (`RAG_CACHE_MAX_ENTRIES`, default 2048) so an unbounded key space can't grow memory forever.
  Eviction lazily on get/set. Thread-safe (single lock is fine at this scale).
- **Public interface unchanged** (`get/set_embedding`, `get/set_retrieval`, `get/set_answer`,
  `enabled`) so `pipeline.py` needs zero changes for lookups.

### F-2 · Tenant-generation invalidation must survive processes (critical correctness detail)
Today `tenant_generation`/`bump_tenant_generation` live in Redis. With an in-process cache,
a **CLI ingest in one process would not invalidate the API process's cached answers** —
re-introducing the stale/deleted-answer leak the generation mechanism exists to prevent.
- Persist generation counters to `data_dir/cache_generations.json`, written atomically
  (`os.replace`, same pattern as the ingest manifest) and **read on every answer-cache lookup**
  (a tiny file; stat+read is negligible next to an LLM call).
- This keeps the ingest→invalidate guarantee across CLI/API processes with no server.

### F-3 · Session memory: SQLite turn store (new default)
- `rag/memory.py`: `RAG_MEMORY_BACKEND = sqlite | redis | inprocess`, **default `sqlite`**.
- `_SqliteTurnStore` on stdlib `sqlite3`, file `data_dir/session_memory.db`, WAL mode,
  table `(key TEXT, ts REAL, question TEXT, answer TEXT)`; load = last N by ts; append =
  insert + trim + delete rows older than TTL. Key stays `prefix:tenant:user_id:session`.
- Why not in-process: durable across restarts and correct under multi-worker — removes both
  caveats instead of documenting them. Stdlib only; no new dependency.
- Note in the module docstring: this file holds conversation text (like Redis did) — it lives
  under the same protected `data_dir` as the index, which is the accepted trust boundary.

### F-4 · Deployment & docs sweep
- `docker-compose.yml`: delete the `redis:` service, `RAG_REDIS_URL` env, `depends_on`.
- `requirements.txt` + `requirements-vdi-minimal.txt`: remove `redis>=5.0.0` (leave a
  commented line: optional, only if `RAG_CACHE_BACKEND=redis` is ever approved). Update the
  VDI playbook step asking "Is Redis available?" → remove.
- `.env.example`: replace the Redis section with `RAG_CACHE_BACKEND` / `RAG_MEMORY_BACKEND`
  / `RAG_CACHE_MAX_ENTRIES`; keep `RAG_REDIS_URL` only under an "optional redis backend" comment.
- `api/main.py` `/health`: report `cache: <backend>` and `memory: <backend>` instead of
  `redis: connected|unavailable`.
- `README.md`: quickstart/deploy sections lose the redis container; add one line of rationale
  ("no standing cache service — serverless cache/memory inside the app's data boundary").
- `rag/config.py`: add the two backend fields + max-entries; keep `RedisConfig` (used only
  when a redis backend is explicitly selected); `RAG_REDIS_REQUIRED` becomes meaningful only
  in that mode.

### F-5 · Tests (definition of done)
1. Unit: memory-cache TTL expiry + LRU eviction; generation bump in a *fresh* Cache instance
   (same data_dir) invalidates an answer cached by another instance — the cross-process proof.
2. Unit: SQLite turn store — append/load round-trip, trim to max_turns, TTL purge, and
   persistence across two store instances.
3. Existing gates all green: `tests/run_all.py`, `eval/evaluate.py` (gate PASS),
   `eval/security_pass.py`.
4. `test_api.py` health check updated for the new fields.
5. Repeat-query behavior: same question twice → second hit is a `cache_hit` decision in
   `/metrics` **without any Redis running** (this is M4's "noticeably faster", restored).

---

## 3. Explicitly out of scope
- Deleting the Redis code paths (they stay as optional backends; they're small and isolated).
- Cross-worker *shared* caching (per-worker cache duplication is accepted; workers=1 is the
  current deployment shape anyway).
- The BM25 pickle→JSON backlog item (unchanged, tracked in BACKEND_REVIEW_FINDINGS.md L-6).

## 4. Risks & mitigations
| Risk | Mitigation |
|---|---|
| SQLite write contention under concurrent requests | WAL mode + short transactions; session-memory writes are tiny and per-turn |
| Memory growth in the in-process cache | LRU cap + per-entry TTL (F-1) |
| Stale answers across processes | file-backed generation counters read on lookup (F-2) — this is the one non-negotiable detail |
| Losing Redis knowledge for the future | backends stay selectable via `RAG_CACHE_BACKEND=redis` / `RAG_MEMORY_BACKEND=redis` |

## ✅ EXECUTION LOG (Opus, 2026-07-08)

All five items **implemented AND VERIFIED GREEN** (confirming run 2026-07-13):
`tests/run_all.py` → 37 unit + smoke (incl. `cache_hit: True` M4 proof, no Redis)
+ API all pass; `eval/evaluate.py` → **Gate PASS** (nDCG@5 1.0→1.0, 0 leaks);
`eval/security_pass.py` → **all 7 checks pass (fails closed)**.

The confirming run caught **two real bugs** (now fixed):
1. `rag/pipeline.py` `set_answer(user, query, payload)` was called with `query`
   and `payload` **swapped** → the answer-cache `set` threw on `"|".join(parts)`
   (a dict in the key slot) and the `try/except` swallowed it, so answers were
   **never cached** — M4 was silently broken. Fixed the call site (arg order).
2. `eval/evaluate.py` offline branch defaulted `RAG_VECTORSTORE_BACKEND=chroma`,
   but chromadb isn't installed offline → the gate crashed on import. Changed the
   offline default to `memory` (matches the smoke/security suites; offline eval is
   now fully dependency-free).

| Item | Status | Notes |
|---|---|---|
| F-1 in-process TTL+LRU cache (default) | ✅ | `rag/cache.py`: `_MemoryCacheBackend`, `RAG_CACHE_BACKEND=memory|redis|off`, `RAG_CACHE_MAX_ENTRIES`; public interface unchanged |
| F-2 file-backed generation counters | ✅ | `_GenerationStore` → `data_dir/cache_generations.json` (atomic `os.replace`), read on every answer lookup; redis backend keeps them in redis |
| F-3 SQLite session memory (default) | ✅ | `rag/memory.py`: `_SqliteTurnStore` (WAL, trims by unique `rowid`), `RAG_MEMORY_BACKEND=sqlite|redis|inprocess` |
| F-4 deploy/doc sweep | ✅ | compose (redis service removed), `requirements*.txt` (redis → opt-in comment), `.env.example` (backend selectors), `/health` (`cache_backend`/`memory_backend`), README, `rag/__init__.py` |
| F-5 tests | ✅ | unit: LRU+TTL, cross-instance generation bump, SQLite round-trip/persist/trim; smoke: repeat query = `cache_hit` with no Redis (M4 proof) |

Redis code paths retained as opt-in backends. `RedisConfig` still wired; `redis`
pip package is now optional (commented) in both requirements files.

## 5. Suggested execution order (single batch, ~1 session)
1. F-1 + F-2 (cache backend + file-backed generations) → unit tests
2. F-3 (SQLite memory) → unit tests
3. F-4 (compose/env/requirements/health/README sweep)
4. F-5 full verification: run_all + eval gate + security pass, all green with **no Redis anywhere**
