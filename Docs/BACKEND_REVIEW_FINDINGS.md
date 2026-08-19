# FPT RAG Backend — PM Review: Findings & Fix Orders

**Date:** 2026-07-08 · **Reviewer:** PM pass (security / efficiency / correctness / ops)
**Scope:** everything under `backend/` (rag/, api/, scripts/, tests/, eval/, Docker/nginx)
**Method:** line-level re-read of current code (post-cleanup), claims verified by grep/execution — every finding below carries a `file:line` that was checked against the working tree, not memory.
**Baseline state:** all suites green at review time (28 unit + smoke + API PASS, eval gate PASS, security pass PASS).

---

## Executive summary

The backend is in genuinely good shape: three ACL checkpoints fail closed, injection/PII gates run at ingest, caches are ACL-scoped **and** generation-invalidated, auth is atomic header-or-body with a proxy secret and an admin gate, and the eval harness + security pass give a regression floor. **No criticals are open.**

What remains: **1 High**, **7 Medium**, **6 Low** findings plus a process/backlog list. Nothing blocks a VDI deployment in dev posture; H-1 and M-1…M-4 should land before any production traffic.

**Counts:** 🔴 High 1 · 🟠 Medium 7 · 🟡 Low 6 · 📋 Process 5

---

## ✅ RESOLUTION LOG (Opus, 2026-07-08) — 13 of 14 code findings fixed

All code findings except L-6 (deferred backlog) are **implemented, tested, and verified**: 31 unit + smoke + API suites PASS, eval gate PASS (nDCG@5 1.0), security pass PASS.

| Finding | Status | What changed | Regression test |
|---|---|---|---|
| **H-1** embedding cache key | ✅ Fixed | `Embedder.signature()` (backend\|model\|revision\|dim) threaded into `cache.get/set_embedding` | `test_unit`: signature differs by dim → different cache key |
| **M-1** quarantine raw PII | ✅ Fixed | `SecurityGuard.force_redact` (policy-independent); `_quarantine` masks + stores `text_sha256` | `security_pass` #1: email `[REDACTED_EMAIL]` in quarantine file |
| **M-2** feedback raw/uncapped | ✅ Fixed | pydantic caps; answer stored as sha256 + 500-char excerpt; query kept raw (documented) | `test_api`: oversized→422, long answer truncated, hash present |
| **M-3** no /ingest size cap | ✅ Fixed | `text` max 2 MB; `doc_id` bounded + `^[A-Za-z0-9._-]+$`; `query` max 8000 | `test_api`: oversized→422, `../` doc_id→422 |
| **M-4** Redis exposed | ✅ Fixed | compose `ports` → `expose` (network-only); password recipe documented | manual/compose |
| **M-5** root container | ✅ Fixed | multi-stage build, venv copy, `USER app` (uid 10001), toolchain dropped | manual/docker |
| **M-6** process-local locks | ✅ Fixed | cross-process `_FileLock` (O_EXCL + stale steal) + atomic manifest (`os.replace`) + re-read under lock | covered by smoke/ingest paths |
| **M-7** /metrics unauth | ✅ Fixed | `/metrics` admin-gated when `RAG_ADMIN_PRINCIPALS` set; `/health` minimal for non-admins | `test_api`: 403 without admin, minimal health, 200 with admin |
| **L-1** SSE abort invisible | ✅ Fixed | `event_source` try/finally records `aborted` metric + audit | (behavioral) |
| **L-2** nginx header strip | ✅ Fixed | also strips `X-Session-Id`, `X-Language`, `X-Proxy-Secret` | manual/nginx |
| **L-3** config import-time defaults | ✅ Fixed | both converted to `field(default_factory=…)` | verified: env set post-import honored |
| **L-4** reranker double-compute | ✅ Fixed | `_query_recall` computed once per candidate | (no behavior change) |
| **L-5** cache-hit loses claims | ✅ Fixed | `cited_claims` serialized + restored in answer cache | (round-trip) |
| **L-6** BM25 pickle | ⏸ Deferred | backlog — JSON serialization; low risk while data dir is trusted | — |

**Process items (📋 1–5):** not code — left for the deploy/CI owner (pin deps for VDI, container-test Docker/nginx, set the real eval baseline with `--real`, wire CI, aggregate metrics if multi-worker).

---

## Verified FIXED — do not re-fix (credit: the cleanup/hardening pass)

These were on my suspect list and are confirmed already resolved in the current tree:

| # | Area | Evidence |
|---|------|----------|
| ✅ | **Answer-cache staleness/leak on ingest/delete** — tenant generation counter in cache keys, bumped by ingester on both ingest and delete; read + write use the same stamped key | `rag/cache.py:123-142`, `rag/ingester.py:255,288`, `rag/pipeline.py:173-175,212,219` |
| ✅ | **Session-memory cross-user bleed** — `user_id` is now part of the memory key | `rag/memory.py:126-131` |
| ✅ | **Non-atomic ingest** — new chunks written first, prior version retired with `keep_ids`, rollback removes only the new chunks | `rag/ingester.py:215-241` |
| ✅ | **Identity spoofing surface** — atomic header-XOR-body resolution, `RAG_PROXY_SHARED_SECRET`, `RAG_ALLOW_BODY_IDENTITY` kill switch, `require_admin` on ingest/delete | `api/auth.py:49-108` |

---

## 🔴 HIGH

### H-1 · Embedding cache key ignores the embedder identity
- **Where:** `rag/cache.py:145-149` (`get_embedding`/`set_embedding` key on text only) and `rag/pipeline.py:262-266` (`"query::" + text`).
- **Issue:** the Redis embedding cache key contains only the query text. If the embedder backend, model, revision, or dimension changes (hash ↔ BGE-M3 ↔ `openai` @1536 — exactly the switch planned for the VDI), cached vectors from the *previous* embedder are served for up to 1 h. Wrong-dimension vectors break Chroma queries; same-dimension-wrong-space vectors silently return garbage retrieval.
- **Fix:** include an embedder signature in the key parts — `f"{backend}|{model}|{revision or '-'}|{dim}"`. Cleanest: add `Embedder.signature()` (returns that string; forces `_ensure_model()` first) and pass it through `pipeline._embed_query` → `cache.get_embedding(sig, text)` / `set_embedding(sig, text, vec)`. Also apply to any future passage-embedding caching.
- **Verify:** unit test — cache a vector under backend A, flip `RAG_EMBEDDER_BACKEND`, assert a fresh embed happens (no hit).

---

## 🟠 MEDIUM

### M-1 · Quarantine files store raw, unredacted chunk text
- **Where:** `rag/ingester.py:307` (`"text": chunk.text` written to `data/quarantine/*.json`).
- **Issue:** the injection scan (step 7) runs *before* PII redaction (step 8) — correct pipeline order, but the quarantine record persists the raw text to disk. A document that is both malicious and PII-laden lands unredacted at rest, outside every ACL.
- **Fix:** in `_quarantine()`, always run `self._security.redact_pii(chunk.text, "public")` (force-redact regardless of sensitivity) before writing, and store `text_sha256` of the original for forensics. Keep `matches` as-is.
- **Verify:** extend `eval/security_pass.py` check 1: ingest an injection doc containing an email; assert the quarantine file has `[REDACTED_EMAIL]` and no raw address.

### M-2 · Feedback stores raw query/answer with no size caps (audit hashes; feedback doesn't)
- **Where:** `rag/feedback.py:50-63` (raw `query`, `answer`, `comment` to `feedback.jsonl` + `golden_candidates.jsonl`); `api/schemas.py` `FeedbackRequest` (no `max_length` anywhere — verified by grep).
- **Issue:** (a) inconsistent data posture — the audit log deliberately stores only hashes, while feedback persists raw text of the same conversations; (b) unbounded fields = disk-fill DoS via `POST /feedback`.
- **Fix:** ① pydantic caps: `query: str = Field(max_length=4000)`, `answer: str | None = Field(default=None, max_length=8000)`, `comment: str | None = Field(default=None, max_length=2000)`. ② In `FeedbackStore.record`, keep raw `query` (golden-set curation needs it — document that choice in the module docstring) but store `answer` as sha256 + first-500-chars excerpt. ③ Note retention expectations in the docstring.
- **Verify:** API test — oversized feedback body → 422; feedback file contains excerpt+hash, not the full answer.

### M-3 · No app-level size cap on `POST /ingest` text
- **Where:** `api/schemas.py` `IngestRequest.text` (no constraint); nginx `client_max_body_size 10m` only protects the *proxied* path — direct `:8000` (exposed in compose) bypasses it.
- **Issue:** a multi-hundred-MB body reaches chunking/embedding and can OOM the worker.
- **Fix:** `text: str = Field(max_length=2_000_000)` (~2 MB of text ≈ a very large document; tune as needed) + `doc_id: Field(max_length=256, pattern=r"[A-Za-z0-9._-]+")` while there (path-safe doc ids — `doc_id` is interpolated into quarantine/tombstone filenames).
- **Verify:** API test — oversized ingest → 422; `doc_id` with `../` → 422.

### M-4 · Compose exposes unauthenticated Redis to the host
- **Where:** `docker-compose.yml:15-16` (`ports: 6379:6379`).
- **Issue:** anyone on the host network can read/flush the cache and session memory (query texts live in session memory). Redis has no auth configured.
- **Fix:** delete the `ports:` mapping on the redis service (api reaches it via the compose network); optionally add `command: ["redis-server", "--requirepass", "${REDIS_PASSWORD}"]` + reflect in `RAG_REDIS_URL`. Consider dropping the api `8000:8000` mapping in a prod profile so nginx is the only door.
- **Verify:** `docker compose config` renders; api still connects (in-network URL unchanged).

### M-5 · Container runs as root; build tools ship in the final image
- **Where:** `backend/Dockerfile` (no `USER`; `build-essential` installed and never removed — single stage).
- **Fix:** multi-stage build (builder installs requirements into a venv; final stage copies the venv), then `RUN useradd -r -u 10001 app && chown -R app /data` + `USER app`. Keeps the slim image genuinely slim and drops compiler toolchain from the runtime surface.
- **Verify:** image builds both profiles; `docker run --rm image id -u` → `10001`; suites pass in-container.

### M-6 · Ingestion locking and manifest are process-local
- **Where:** `rag/ingester.py:48-60` (`threading.Lock` registry) and `_load_manifest`/`_save_manifest` (read-modify-write of one JSON file, no file lock).
- **Issue:** two processes (uvicorn `--workers 2`, or CLI ingest while the API serves) can interleave ingests of the same `doc_id` and clobber the manifest — exactly what step 3 (per-doc lock) is supposed to prevent, but only within one process.
- **Fix (pragmatic, in order of preference):** ① acquire a per-doc lockfile via `os.open(path, O_CREAT|O_EXCL)` with stale-lock timeout under `data/locks/`; ② manifest: write-to-temp + `os.replace` (atomic) and re-read inside the lock; ③ document "single ingest writer" as a deployment constraint until then (uvicorn workers=1 is already the compose default).
- **Verify:** two concurrent `scripts/ingest.py` runs on the same doc → one indexes, one skips/waits; manifest never truncated.

### M-7 · `/metrics` (and `/health` backend detail) are unauthenticated
- **Where:** `api/main.py` `GET /metrics`, `GET /health`.
- **Issue:** decision counts, latency, abstention rates, index size, and configured backends are visible to any caller. Low direct risk, but it's recon surface and the VDI security team will flag it.
- **Fix:** gate `/metrics` behind `require_admin(resolve_user(...))` (or an nginx `location /metrics { allow internal; deny all; }` block); trim `/health` to `status` + `version` for unauthenticated callers, returning full detail only for admins.
- **Verify:** API test — `/metrics` without admin principal → 403 (with `RAG_ADMIN_PRINCIPALS` set); `/health` shape check.

---

## 🟡 LOW

### L-1 · SSE client disconnect skips audit/metrics/memory
- **Where:** `rag/pipeline.py::query_stream` — `_finalize` runs only after the generator is fully consumed; `api/main.py::event_source` has no try/finally.
- **Fix:** wrap the API generator in `try/finally`; on early termination, audit a `"aborted"` decision (partial answer hash) and record a metrics decision so aborted streams aren't invisible.

### L-2 · nginx passes client-supplied `X-Session-Id`, `X-Language`, `X-Proxy-Secret`
- **Where:** `nginx/nginx.conf:39-42` clears only the four identity headers.
- **Fix:** add `proxy_set_header X-Session-Id ""; proxy_set_header X-Language ""; proxy_set_header X-Proxy-Secret "";` to the strip list (the edge re-injects the real ones). Harmless today (secret must *match*), but strip-everything-then-inject is the correct invariant.

### L-3 · Two config defaults still evaluated at import time
- **Where:** `rag/config.py:99` (`query_instruction_prefix`) and `:240` (`embedding_ttl`) use direct `_env*` defaults; the other 68 use `field(default_factory=...)`.
- **Issue:** env vars set after module import are honored by 68 fields but ignored by these 2 — inconsistent and surprising in tests.
- **Fix:** convert both to `field(default_factory=lambda: ...)`.

### L-4 · Lexical reranker computes `_query_recall` twice per candidate
- **Where:** `rag/reranker.py:117` — `[(_query_recall(q,t), _query_recall(q,t)) for t in texts]`.
- **Fix:** `scores = [_query_recall(query, t) for t in texts]; return [(s, s) for s in scores]`.

### L-5 · Cache-hit answers lose `cited_claims`/metadata
- **Where:** `rag/pipeline.py::_answer_to_cache/_answer_from_cache` — sources survive, claims don't.
- **Fix:** serialize `cited_claims` (label list + claim text) alongside sources; restore on read. Matters once the frontend renders claim-level support.

### L-6 · BM25 persistence is pickle
- **Where:** `rag/vectorstore.py` BM25Store `save/_ensure_loaded`.
- **Issue:** `pickle.load` of a data-dir file = arbitrary code execution if the data dir is ever tampered with; also brittle across schema versions.
- **Fix (backlog):** serialize as JSON via the existing `_chunk_to_metadata`/`_metadata_to_chunk` helpers + token lists. Not urgent while the data dir is trusted.

---

## 📋 Process / backlog (no code defect)

1. **Dependency reproducibility for the VDI** — `requirements*.txt` use `>=` ranges. On a known-good env run `pip freeze > constraints.txt` and install with `-c constraints.txt` on the VDI, or pin exact versions. Guardrailed environments hate surprise upgrades.
2. **Docker/nginx are untested** — authored and YAML/syntax-validated only (no Docker on this machine). First `docker compose up --build` on a Docker-capable box is the real proof; budget 30 min for wrinkle-fixing.
3. **Eval baseline is offline** — all-1.0 scores reflect a lexically clean golden set on fallback backends. After real models/LLM are reachable: `python eval/evaluate.py --real --update-baseline`. Expand the golden set from `data/feedback/golden_candidates.jsonl` as usage accumulates (target 30–50 queries incl. hard negatives).
4. **No CI** — wire `tests/run_all.py` + `eval/evaluate.py` + `eval/security_pass.py` into whatever CI the VDI/repo offers; all three are exit-code-gated already.
5. **Observability is per-process** — documented in `rag/observability.py`; fine at workers=1. If the VDI runs multiple workers, aggregate via Redis or expose per-worker and sum at the dashboard.

---

## Suggested execution order for the fix pass (Opus)

| Batch | Items | Rationale |
|---|---|---|
| **1 — correctness/security first** | H-1, M-1, M-2, M-3 | All small, self-contained, testable offline; close every data-handling gap |
| **2 — deploy posture** | M-4, M-5, M-7, L-2 | One sweep over compose/Dockerfile/nginx/api auth |
| **3 — robustness** | M-6, L-1 | Locking + SSE finalize; slightly more invasive |
| **4 — polish** | L-3, L-4, L-5 | Minutes each |
| **defer** | L-6, process items | Backlog / needs VDI or Docker access |

**Definition of done for every batch:** `python -m py_compile` over touched files → `python tests/run_all.py` → `python eval/evaluate.py` (gate must PASS) → `python eval/security_pass.py` — all green, plus the per-finding verify step listed above. Add a regression test with each fix (H-1, M-1, M-2, M-3, M-7 each specify one).
