# FPT RAG — Bilingual (VI/EN) Hybrid-Retrieval RAG

A production-shaped Retrieval-Augmented Generation backend: hybrid dense + BM25
retrieval, three ACL security checkpoints, contextual chunking, reranking,
grounded + cited generation, and a FastAPI service. Bilingual Vietnamese /
English throughout.

> **Scope:** backend only (the React/TypeScript frontend is maintained
> separately). Every heavy dependency is **lazy-loaded** with an offline
> fallback, so the whole system imports, runs, and tests **without** the ~4 GB
> models or an LLM key. Short-term follow-up context remains serverless
> (SQLite), while durable chat history and long-term user memory use PostgreSQL
> in production and SQLite for offline development/tests.

---

## Project structure

Self-contained backend — zip or push this whole folder to git / the VDI.

```
backend/
├── rag/                     # the RAG application layer (importable package)
│   ├── config.py            #   all tunable knobs (env-overridable)
│   ├── schemas.py           #   typed data contracts (Chunk, RagAnswer, UserContext…)
│   ├── language.py          #   VI/EN detection (confidence-gated)
│   ├── tokenization.py      #   real-tokenizer token counting
│   ├── chunking.py          #   recursive chunker + contextual augmentation
│   ├── embedder.py          #   BGE-M3 (local) or OpenAI-compatible API embeddings
│   ├── vectorstore.py       #   ChromaDB dense store + BM25 store  [Checkpoint 1]
│   ├── cache.py             #   in-process TTL+LRU cache, ACL-scoped keys (Redis optional)
│   ├── audit.py             #   append-only JSONL compliance log
│   ├── memory.py            #   session memory: follow-up → standalone query
│   ├── persistence.py       #   conversations, messages, citations, user memories
│   ├── security.py          #   [Checkpoint 2 / 3] + injection scan + PII redaction
│   ├── retriever.py         #   dense + BM25 candidate generation (VI/EN analyzers)
│   ├── fusion.py            #   Reciprocal Rank Fusion (k=60)
│   ├── reranker.py          #   bge-reranker-v2-m3 cross-encoder
│   ├── context.py           #   score filter → dedup → [CP3] → sandwich → budget → tags
│   ├── generator.py         #   pluggable LLM (Claude / OpenAI / extractive) + citations
│   ├── ingester.py          #   13-step atomic write path + tombstone delete
│   ├── pipeline.py          #   single query entry point (+ streaming)
│   ├── observability.py     #   latency SLIs + decision/error rates
│   └── feedback.py          #   thumbs up/down → golden-set candidates
├── api/                     # FastAPI service (the only thing the frontend calls)
│   ├── main.py              #   query + conversation/memory CRUD + admin/ops APIs
│   ├── auth.py              #   identity: SSO headers + proxy-secret, admin gate
│   └── schemas.py           #   HTTP request/response models
├── scripts/                 # command-line tools
│   ├── ingest.py            #   bulk-load text, Office, PDF, and image files
│   └── query.py             #   ask one question
├── tests/                   # unit, smoke, API, and history/ownership integration tests
├── migrations/              # Alembic schema migrations for PostgreSQL/SQLite
├── alembic.ini              # migration configuration
├── eval/                    # golden set, metrics, regression gate, security pass
│   ├── golden/              #   corpus/ + queries.jsonl (EN/VI + unanswerable)
│   ├── metrics.py           #   Recall@k, MRR, nDCG@k
│   ├── evaluate.py          #   score + nDCG@5 regression gate (blocks on >3% drop)
│   ├── security_pass.py     #   red-team: injection, PII, ACL, cross-tenant, abuse
│   └── baseline.json        #   saved primary-metric baseline
├── Dockerfile               # full or --build-arg REQUIREMENTS=requirements-vdi-minimal.txt
├── docker-compose.yml       # nginx → api → PostgreSQL, one command
├── nginx/nginx.conf         # edge proxy: TLS stub, rate limit, SSE, SSO headers
├── requirements.txt         # full backend deps (+ VDI playbook at the top)
├── requirements-vdi-minimal.txt  # slim: no local models (aiportalapi path)
├── .env.example             # configuration reference
└── README.md

Design docs (RAG_System_Overview.md, FPT_RAG_Architecture_Flow.md,
FPT_RAG_Stepbystep_Checklist.md, The Complete RAG Engineering Handbook.md) live
in the parent FPT_RAG/ folder.
```

---

## Quickstart (offline — no downloads, no keys)

Runs on the in-memory / hash / extractive fallbacks. Good for wiring, demos, CI.

```powershell
# 1) Ingest documents (persistent Chroma so a later query sees them)
python scripts/ingest.py ./docs --offline --tenant demo --acl "*"

# 2) Ask a question
python scripts/query.py "What is the refund policy?" --offline --tenant demo --principals "*"

# 3) Or run the API offline
$env:RAG_EMBEDDER_BACKEND="hash"; $env:RAG_VECTORSTORE_BACKEND="chroma"
$env:RAG_RERANKER_BACKEND="lexical"; $env:RAG_LLM_PROVIDER="extractive"
uvicorn api.main:app --port 8000
```

`--offline` pins the hash embedder + lexical reranker + extractive generator but
keeps a **persistent** ChromaDB index, so `ingest` and `query` (separate
processes) share state.

## Quickstart (real models)

```powershell
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env          # then edit
# BGE-M3 + reranker (~4 GB) download from Hugging Face on first use.
python scripts/ingest.py ./docs --tenant demo --acl "group:support"
python scripts/query.py "refund policy" --tenant demo --principals "group:support"
```

## Company VDI (guardrailed, may block model downloads)

Drive embeddings **and** generation through the OpenAI-compatible `aiportalapi`
gateway — no local models needed. In `.env`:

```
RAG_EMBEDDER_BACKEND=openai
RAG_EMBEDDING_API_BASE_URL=https://<aiportalapi-host>/v1
RAG_EMBEDDING_DIM=1536
RAG_LLM_PROVIDER=openai
RAG_LLM_BASE_URL=https://<aiportalapi-host>/v1
RAG_LLM_MODEL=gpt-4o-mini
OPENAI_API_KEY=<key>
```

> Note: switching the embedder changes the vector dimension, so re-ingest into a
> fresh index (`RAG_INDEX_VERSION` / `RAG_CHROMA_COLLECTION`).

---

## API

| Method | Path            | Purpose                                    |
|--------|-----------------|--------------------------------------------|
| POST   | `/query`        | grounded, cited answer (blocking)          |
| POST   | `/query/stream` | Server-Sent Events, token-by-token + final |
| GET/POST | `/conversations` | list/create owned conversations          |
| GET/PATCH/DELETE | `/conversations/{id}` | transcript, rename/pin, soft-delete |
| POST   | `/conversations/{id}/restore` | restore during the 30-day window  |
| GET/POST | `/memories`   | list/create long-term user memories       |
| PATCH/DELETE | `/memories/{id}` | edit/delete a saved memory           |
| POST   | `/ingest`       | ingest one document (admin-gated)          |
| POST   | `/upload`       | upload + OCR/extract + retain original     |
| GET    | `/documents/{id}/preview` | ACL-checked extracted text preview |
| GET    | `/documents/{id}/file` | ACL-checked original file preview/download |
| DELETE | `/ingest/{id}`  | tombstone a document (admin-gated)         |
| POST   | `/feedback`     | thumbs up/down → golden-set candidates     |
| GET    | `/metrics`      | operational SLIs (latency, decisions, rates) |
| GET    | `/health`       | liveness + backend status                  |
| GET    | `/docs`         | interactive OpenAPI docs                   |

**Identity** for the ACL checks is header-first, body-fallback:
`X-Tenant-Id`, `X-User-Id`, `X-Principals` (comma-separated), `X-Classification`
— injected by the edge proxy after SSO, or supplied in the request body's
`user` object for local dev.

```bash
curl -X POST localhost:8000/query \
  -H "X-Tenant-Id: demo" -H "X-User-Id: alice" -H "X-Principals: group:support" \
  -H "Content-Type: application/json" \
  -d '{"query":"What is the refund window?","risk":"medium"}'
```

---

## How it works

**Ingestion (write path):** content hash → per-doc lock → recursive chunk →
contextual augmentation → language tag → injection scan → PII redaction →
BGE-M3 embed → BM25 index → version stamp → atomic upsert → tombstone-on-delete.

**Query (read path):** load owner-scoped user memory → input validation →
session-memory rewrite → language
detect → query embed → **CP1** (tenant+classification) → dense+BM25 retrieve →
RRF fuse → **CP2** (acl_principals) → rerank → score filter / abstain → context
assembly **CP3** (dedup, sandwich order, budget, source tags) → generate →
citation validation → structured response.

**Three ACL checkpoints:** CP1 is a cheap scalar pre-filter in the vector store;
CP2 filters the `acl_principals` list (which Chroma can't) before the reranker;
CP3 re-verifies inside context assembly. All fail **closed**.

See `RAG_System_Overview.md` and `FPT_RAG_Architecture_Flow.md` for the full
design, and `The Complete RAG Engineering Handbook.md` for the underlying
engineering rationale.

---

## Deploy (Docker)

```powershell
cp .env.example .env               # then edit
docker compose up --build          # nginx :8080 -> api :8000 -> PostgreSQL
```

Compose waits for PostgreSQL, runs `alembic upgrade head`, and then starts the
API. Conversations are soft-deleted and purged after 30 days. Direct local API
runs default to `data/chat_history.db`; set `RAG_DATABASE_URL` to use an
external PostgreSQL instance.

Slim image for a guardrailed VDI (no local models — embeddings + LLM via
aiportalapi):

```powershell
docker build --build-arg REQUIREMENTS=requirements-vdi-minimal.txt -t fpt-rag-api .
```

`nginx/nginx.conf` handles rate limiting, SSE-safe streaming, and the SSO
identity-header injection (`X-Tenant-Id`/`X-User-Id`/… + `X-Proxy-Secret`) the
API trusts. Production hardening flags are documented at the top of
`requirements.txt`.

## Configuration

All settings live in `rag/config.py` and are overridable via `RAG_*` environment
variables — see `.env.example` for the full annotated list.

## Testing

All tests run offline (no models, no keys, no external services):

```powershell
python tests/run_all.py         # unit (pure logic) + smoke (e2e) + api (TestClient)
```

The package imports and its pure logic run with zero heavy deps; the smoke and
API suites exercise ingest → query → delete end-to-end (EN + VI, ACL-enforced).

## Evaluation & regression gate

```powershell
python eval/evaluate.py                 # score the golden set + gate vs baseline (exit 2 on >3% nDCG@5 drop)
python eval/evaluate.py --update-baseline
python eval/evaluate.py --real          # use real BGE-M3 / reranker / LLM instead of offline fallbacks
python eval/security_pass.py            # red-team: injection, PII, ACL, cross-tenant, abuse (fails closed)
```

Metrics: Recall@{1,3,5}, MRR, **nDCG@5** (the gated primary metric), plus
abstention accuracy, citation validity, and ACL-leak count (must be 0). The
committed `baseline.json` was set on **offline** backends — re-establish it with
`--real --update-baseline` once the real models/LLM are available.

## Status

**All backend phases (1–6) complete** — core + CLIs + M2, FastAPI + SSE,
caching + audit + observability + feedback, eval harness with the nDCG@5
regression gate + security pass, and Docker/nginx. Open items are the teammate's
frontend and the final joint demo. See `../FPT_RAG_Stepbystep_Checklist.md`.
