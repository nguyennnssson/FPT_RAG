# FPT RAG System — Overview
---

## System architecture

```
+----------------------------------------------------+
| User / employee                                    |   ROLE  Requester
| browser or CLI                                     |   WHY   Origin of every query and admin action -- nothing else in this system runs without it
+----------------------------------------------------+
                           |
                           v
+----------------------------------------------------+
| nginx -- edge, port 443                            |   ROLE  Edge / perimeter
| TLS, rate limit, SSO headers                       |   WHY   TLS, rate-limiting and auth headers should not be reimplemented inside the app itself
+----------------------------------------------------+
                           |
                           v

-------- PRESENTATION LAYER -- React + TypeScript SPA (separate Docker container) 

+----------------------------------------------------+
| Chat input                                         |   ROLE  Query intake
| query + language tag                               |   WHY   Captures the raw question and language tag before anything else happens
+----------------------------------------------------+

+----------------------------------------------------+
| Answer view                                        |   ROLE  Result display
| answer + [S#] citations                            |   WHY   A "grounded answer" claim is unverifiable unless citations are actually visible and clickable
+----------------------------------------------------+

+----------------------------------------------------+
| Sidebar / admin                                    |   ROLE  Operational control
| doc upload, health status                          |   WHY   Someone needs to upload docs and see index health without touching the database directly
+----------------------------------------------------+
                           |
                           v   HTTPS fetch / SSE stream

-------- API LAYER -- FastAPI (separate Docker container, :8000) 

+----------------------------------------------------+
| API                                                 |   ROLE  HTTP/SSE boundary  [NEW]
| POST /query, POST /ingest, GET /health, SSE stream |   WHY   The React frontend has no Python runtime and cannot import rag/* directly -- a thin HTTP/SSE service is the only thing it is allowed to call
+----------------------------------------------------+
                           |
                           v   query / admin action

-------- APPLICATION LAYER -- rag/*  (same Docker container as the API layer, not the presentation) 

-------- INGESTION -- WRITE PATH --------

+----------------------------------------------------+
| Ingester                                           |   ROLE  Write-path orchestrator  [BROKEN]
| hash to chunk to embed to upsert                   |   WHY   Runs the 13-step ingestion sequence as one atomic, lockable unit
+----------------------------------------------------+

NOTE  Full 13-step sequence this orchestrates: content hash (2) -> per-doc_id
      lock (3) -> recursive chunk (4) -> contextual augmentation (5) ->
      language detection (6) -> injection heuristic scan (7) -> PII
      detection & redaction (8) -> BGE-M3 embed (9) -> BM25 index (10) ->
      version stamp (11) -> atomic upsert (12) -> tombstone-on-delete (13).
      Injection scanning and PII redaction happen here, at ingestion --
      neither is part of Security below, which only ever runs at query time.

+----------------------------------------------------+
| Chunker                                            |   ROLE  Size normalizer + context restorer
| recursive, 400-512 tok -> contextual_text          |   WHY   Embedding models cap input length and oversized chunks hurt retrieval precision; a bare chunk is meaningless without knowing its source, so a heading-prefixed contextual_text is generated and stored separately -- citations still display the original raw chunk
+----------------------------------------------------+

-------- SHARED -- used by both paths --------

+----------------------------------------------------+
| Language                                           |   ROLE  Language tagger (both paths)
| detect + confidence gate                           |   WHY   Ingestion tags each chunk's language (metadata only) for citation display; query time detects the question's language, confidence-gated, to choose the response language
+----------------------------------------------------+

+----------------------------------------------------+
| Embedder                                           |   ROLE  Meaning encoder  [BROKEN]
| BGE-M3 dense, 1024-dim                             |   WHY   Converts text to vectors so semantically similar content is findable without matching keywords
+----------------------------------------------------+

NOTE  Asymmetric by design: passage mode adds no instruction prefix at
      ingest (step 9); query mode adds a query instruction prefix at read
      time (step 18), so the query is meant to land in the same vector
      space as the unprefixed passage vectors.

+----------------------------------------------------+
| VectorStore                                        |   ROLE  Storage interface + Checkpoint 1
| Chroma upsert (ingest) + cosine search & CP1       |   WHY   Isolates ChromaDB-specific query syntax behind one module; CP1 is the cheapest place to cut candidates -- a scalar pre-filter on tenant_id + classification, before any similarity computation runs
+----------------------------------------------------+

-------- QUERY -- READ PATH --------

+----------------------------------------------------+
| RAGPipeline                                        |   ROLE  Single entry point (query path)
| orchestrator, single entry point                   |   WHY   One orchestrator means one place to add auth/logging, instead of every caller reimplementing the flow
+----------------------------------------------------+

+----------------------------------------------------+
| Session Memory                                     |   ROLE  History-aware query rewriter
| condense turns -> standalone query (Redis)         |   WHY   A follow-up like "what about Vietnam?" is unretrievable on its own -- resolves it into a self-contained question before Language/Embedder run
+----------------------------------------------------+

NOTE  Runs first in the read path, before Query Language Detection and the
      Query Embedder above -- everything downstream (CP1/CP2/CP3, retrieval,
      reranking, generation) is unchanged and still runs fresh on every
      turn. Redis-backed, keyed by session_id + tenant_id like the caches
      below; stores question/answer text only, never retrieved chunks.

+----------------------------------------------------+
| Retriever                                          |   ROLE  Candidate generator
| dense top-20 + BM25 top-20                         |   WHY   Two notions of relevant (meaning vs. keyword) catch different misses if run together
+----------------------------------------------------+

+----------------------------------------------------+
| Fusion                                             |   ROLE  Result merger
| RRF fuse, k=60                                     |   WHY   Dense and BM25 scores are not comparable numbers -- rank fusion combines them anyway
+----------------------------------------------------+

+----------------------------------------------------+
| Security                                           |   ROLE  2nd ACL gate
| CP2 filter on acl_principals, runs before reranker |   WHY   acl_principals is a list Chroma cannot filter natively at CP1 -- this is the only place that check can run, and it must happen before evidence reaches the reranker or the model
+----------------------------------------------------+

+----------------------------------------------------+
| Reranker                                           |   ROLE  Precision pass
| bge-reranker-v2-m3                                 |   WHY   Retrieval is a coarse funnel; only a query-aware reranker tells which top-20 are truly on-topic
+----------------------------------------------------+

+----------------------------------------------------+
| Context                                            |   ROLE  Prompt builder + Checkpoint 3
| score filter + dedup + order + budget + CP3 recheck |   WHY   A reranker score below the tuned threshold means abstaining, never forcing an answer; raw chunks are not a prompt, so ordering, budget, dedup and a third permissions recheck all happen here before the LLM sees anything
+----------------------------------------------------+

NOTE  Full sequence: score filter (abstain if nothing clears the reranker
      threshold, step 25) -> dedup + contradiction check -> CP3 secondary
      ACL recheck -> sandwich ordering (best chunk first, next-best last)
      -> token budget -> source tagging (<source id="Sn">).

+----------------------------------------------------+
| Generator                                          |   ROLE  Answer writer  [BROKEN]
| Claude LLM, grounded + cited                       |   WHY   The only component that actually produces the natural-language answer
+----------------------------------------------------+

-------- CROSS-CUTTING SERVICES --------

+----------------------------------------------------+
| Cache                                              |   ROLE  Latency / cost reducer
| Redis, ACL-scoped keys                             |   WHY   Re-running embed/retrieve/generate for a repeat question wastes time and money
+----------------------------------------------------+

+----------------------------------------------------+
| Audit                                              |   ROLE  Compliance record
| append-only JSON lines                             |   WHY   Every access needs to be reconstructable after the fact, including rejected/failed requests
+----------------------------------------------------+

+----------------------------------------------------+
| Config + schemas                                   |   ROLE  Shared contracts
| RAGConfig + typed contracts                        |   WHY   One place for tunable knobs and typed data shapes so every module agrees on what a Chunk looks like
+----------------------------------------------------+

                           |
                           v

-------- DATA LAYER ----------------------------------------------------

+----------------------------------------------------+
| ChromaDB                                           |   ROLE  Vector store
| 1 collection, cosine, dense                        |   WHY   Persists dense embeddings so they survive a restart and can be searched by cosine similarity
+----------------------------------------------------+

+----------------------------------------------------+
| BM25 index                                         |   ROLE  Keyword store
| pyvi (VI) / Porter (EN)                            |   WHY   Persists the term-frequency index the keyword retriever needs
+----------------------------------------------------+

+----------------------------------------------------+
| Redis                                              |   ROLE  Cache store
| TTL cache, ACL-scoped                              |   WHY   Sits in front of the expensive paths (embed/retrieve/generate) to serve repeat queries fast
+----------------------------------------------------+

+----------------------------------------------------+
| Model weights                                      |   ROLE  Model artifact store
| BGE-M3 ~2.5GB + reranker ~1.5GB                    |   WHY   BGE-M3 and the reranker load from here, pinned to a specific revision
+----------------------------------------------------+

+----------------------------------------------------+
| Audit logs                                         |   ROLE  Compliance store
| query_hash, chunk_ids, latency                     |   WHY   The audit logger's write-once record needs a durable, separately access-controlled home
+----------------------------------------------------+

                           |
                           v
+----------------------------------------------------+
| Claude API                                         |   ROLE  External generation service
| api.anthropic.com, outbound only                   |   WHY   Keeps the heaviest compute (LLM inference) out of the app's own infra, at the cost of a network boundary that must be secured
+----------------------------------------------------+
```

`[BROKEN]` = corrupted today, blocks end-to-end pipeline run (`README.md`, 2026-07-03). `CP1`/`CP2`/`CP3` = the three ACL security checkpoints (`RAG_System_Plan.md` §13.3).

---

## Data flow

**Quick summary:** Ingestion (document → chunks → vectors) → Query (question → retrieve → rerank) → Generation (context → Claude → cited answer) → Display (answer → chat UI).

**Full pipeline, step by step** — every stage in `RAG_System_Plan.md`, in execution order, each with its **ROLE** and **WHY** alongside it:

```
-------- INGESTION -- WRITE PATH ---------------------------------------

+----------------------------------------------------+
| 1  Source document                                 |   ROLE  Entry point
|    .txt / .pdf / .md                               |   WHY   Nothing downstream exists without a document to process
+----------------------------------------------------+
                           |
                           v
+----------------------------------------------------+
| 2  Content hash                                    |   ROLE  Idempotency gate
|    SHA-256, idempotency check                      |   WHY   Skips re-embedding an unchanged file -- saves compute, avoids index churn
+----------------------------------------------------+
                           |
                           v
+----------------------------------------------------+
| 3  Per-doc_id lock                                 |   ROLE  Concurrency guard
|    concurrency guard                               |   WHY   Two concurrent ingests of the same doc_id could interleave writes and corrupt the index
+----------------------------------------------------+
                           |
                           v
+----------------------------------------------------+
| 4  Recursive chunker                               |   ROLE  Size normalizer
|    400-512 tok, 10-15% overlap                     |   WHY   Embedding models cap input length; oversized chunks also hurt retrieval precision
+----------------------------------------------------+
                           |
                           v
+----------------------------------------------------+
| 5  Contextual augmentation                         |   ROLE  Referent restorer
|    heading-prefix -> contextual_text               |   WHY   A bare chunk like "the limit is 30 days" is meaningless without knowing which policy it is from
+----------------------------------------------------+

NOTE  Stored as a SEPARATE field (contextual_text) from the raw chunk
      (text). Embedding/BM25 index the contextual version; citations always
      display the raw one.

+----------------------------------------------------+
| 6  Language detection                              |   ROLE  Metadata tagger
|    per-chunk, metadata only                        |   WHY   Citation display and query-time routing both need each chunk's language on record
+----------------------------------------------------+
                           |
                           v
+----------------------------------------------------+
| 7  Injection heuristic scan                        |   ROLE  First-line content filter
|    quarantine on match                             |   WHY   Malicious instructions hidden in a document must be flagged before they can reach the LLM
+----------------------------------------------------+
                           |
                           v
+----------------------------------------------------+
| 8  PII detection & redaction                       |   ROLE  Compliance gate
|    classification-based policy                     |   WHY   Sensitive personal data must never be embedded or indexed unmasked
+----------------------------------------------------+
                           |
                           v
+----------------------------------------------------+
| 9  BGE-M3 embedder                                 |   ROLE  Meaning encoder  [BROKEN]
|    passage mode, no prefix                         |   WHY   Converts text to vectors so semantically similar content is findable without matching keywords
+----------------------------------------------------+
                           |
                           v
+----------------------------------------------------+
| 10 BM25 indexer                                    |   ROLE  Keyword encoder
|    pyvi (VI) / Porter (EN)                         |   WHY   Dense vectors miss exact terms (IDs, names, codes) that keyword search catches reliably
+----------------------------------------------------+
                           |
                           v
+----------------------------------------------------+
| 11 Version stamp                                   |   ROLE  Provenance tag
|    embedder / chunker / parser versions            |   WHY   Tracks which chunks must be re-embedded when a model, chunker or parser changes later
+----------------------------------------------------+
                           |
                           v
+----------------------------------------------------+
| 12 Atomic upsert                                   |   ROLE  Consistency enforcer
|    stage -> validate -> swap -> verify             |   WHY   A crash mid-write must never leave the index half-updated or a doc_id missing
+----------------------------------------------------+
                           |
                           v
+----------------------------------------------------+
| 13 Tombstone on delete                             |   ROLE  Safe-deletion marker
|    never hard-delete                               |   WHY   A hard delete can be silently resurrected by a retry or backfill; a tombstone cannot
+----------------------------------------------------+

NOTE  Deletion is an INGESTION-PIPELINE capability only, triggered by an
      authenticated admin/document-management process. The query/generation
      path, including the LLM itself, has no write or delete access to the
      index at any point.

NOTE  Steps 1-13 above are all orchestrated by rag/ingester.py as one atomic
      unit (hash -> lock -> chunk -> embed -> upsert -> verify -> release).
      That module is currently corrupted -- so even though each step is
      individually well-specified, the write path cannot run end to end
      today. See "Ingester" in the System architecture above.


-------- STORAGE LAYER -------------------------------------------------

+---------------------------+   +---------------------------+
| 14 ChromaDB collection    |   | 14 BM25 index             |
| dense vectors + cosine    |   | rank-bm25, pickle cache   |
+---------------------------+   +---------------------------+
             |                             |             
             +-----------------------------+             
                            |                            
                            v                            

NOTE  ROLE Persistent dual index. WHY One store answers "what means the same
      thing", the other answers "what has this exact word" -- hybrid
      retrieval needs both.

NOTE  doc_id MUST be a stable, human-legible identifier (filename/slug) --
      never a content hash. Golden-set labels and citation display key off
      this exact value; a hashed doc_id breaks Recall@k silently (retrieval
      can be correct and still score 0).


-------- QUERY -- READ PATH --------------------------------------------

+----------------------------------------------------+
| 15 User query                                      |   ROLE  Entry point
|    chat UI or CLI                                  |   WHY   The entire read path exists to answer this one thing
+----------------------------------------------------+
                           |
                           v
+----------------------------------------------------+
| 16 Input validation                                |   ROLE  Abuse / error guard
|    length cap, charset check                       |   WHY   Bounds size and charset before the query touches any model or index
+----------------------------------------------------+

NOTE  Length cap must be counted with the embedding model's real tokenizer,
      not query.split(). A whitespace word-count silently loosens the cap
      the name "MAX_QUERY_TOKENS" promises.

+----------------------------------------------------+
| 17 Session Memory                                  |   ROLE  History-aware query rewriter
|    condense turns -> standalone query (Redis)      |   WHY   A follow-up like "what about Vietnam?" is unretrievable on its own -- this resolves it into a self-contained question before anything downstream runs
+----------------------------------------------------+

NOTE  Redis-backed, keyed by session_id + tenant_id -- same ACL-scoping
      convention as the caches below, own TTL. Stores only question/answer
      text, never retrieved chunks: replaying old evidence would bypass
      CP1/CP2/CP3 and could leak content under stale permissions. Passes
      the query through unchanged if it is already self-contained, so it
      does not contaminate genuinely new questions with old context.

+----------------------------------------------------+
| 18 Query language detection                        |   ROLE  Response-language selector
|    confidence-gated                                |   WHY   The answer must be written in the language the user actually asked in
+----------------------------------------------------+
                           |
                           v
+----------------------------------------------------+
| 19 BGE-M3 query embedder                           |   ROLE  Query-to-vector encoder
|    + query instruction prefix                      |   WHY   Must land in the same vector space as passage embeddings -- the prefix is what makes that true
+----------------------------------------------------+
                           |
                           v
+====================================================+
| 20 [LOCK] Checkpoint 1: scalar pre-filter          |   ROLE  1st access-control gate
|    tenant_id + classification                      |   WHY   Cheapest place to cut candidates -- never spend a similarity computation on ineligible docs
+====================================================+

NOTE  Coarse only: a native Chroma filter, so it can check
      tenant_id/classification (scalars) but NOT acl_principals (a list) --
      that gap is exactly why Checkpoint 2 has to exist.

+---------------------------+   +---------------------------+
| 21 Dense retrieval        |   | 21 BM25 retrieval         |
| Chroma cosine, top-20     |   | top-20                    |
+---------------------------+   +---------------------------+
             |                             |             
             +-----------------------------+             
                            |                            
                            v                            

NOTE  ROLE Candidate generators. WHY Two different notions of "relevant"
      (meaning vs. keyword) catch different misses -- running only one loses
      recall the other would have caught.

+----------------------------------------------------+
| 22 RRF fusion                                      |   ROLE  Result merger
|    k=60, dedup by chunk_id                         |   WHY   Dense and BM25 scores are not comparable numbers -- rank-based fusion combines them without needing them to be
+----------------------------------------------------+
                           |
                           v
+====================================================+
| 23 [LOCK] Checkpoint 2: ACL filter                 |   ROLE  2nd access-control gate
|    Python, runs before reranker                    |   WHY   acl_principals is a list Chroma cannot filter natively -- this is the only place that check can run
+====================================================+

NOTE  If a user's permissions are narrower than the corpus average, this can
      starve their top-20 down below 5 usable chunks even though nothing
      unauthorized ever leaks. Mitigation: over-fetch top-40 instead of
      top-20 for restrictive principals.

+----------------------------------------------------+
| 24 Cross-encoder reranker                          |   ROLE  Precision pass
|    bge-reranker-v2-m3, top-5                       |   WHY   Retrieval is a coarse funnel; only a query-aware reranker can tell which top-20 are truly on-topic
+----------------------------------------------------+
                           |
                           v
+----------------------------------------------------+
| 25 Score filter                                    |   ROLE  Confidence gate
|    risk-graded reranker threshold                  |   WHY   Forcing an answer from weak evidence produces a confident-sounding wrong answer
+----------------------------------------------------+

NOTE  "Not good enough" = below the tuned reranker score. Zero chunks
      clearing it = abstain ("no relevant documents found"), never force an
      answer. A necessary-but-missing answer is a corpus gap to fix
      upstream, not a reason to lower the bar.

+----------------------------------------------------+
| 26 Context assembly                                |   ROLE  Prompt builder
|    dedup, ordering, budget, CP3, tags              |   WHY   Raw chunks are not a prompt -- ordering, budget, dedup and Checkpoint 3 all happen before the LLM sees anything
+----------------------------------------------------+

NOTE  Ordering is a SANDWICH, not a scale-down: best chunk first, next-best
      last, weaker ones in the middle -- because attention drops in the
      middle of a long context.

+----------------------------------------------------+
| 27 LLM generator                                   |   ROLE  Answer writer  [BROKEN]
|    Claude / Qwen / Llama, lang-matched             |   WHY   The only component that actually produces the natural-language answer
+----------------------------------------------------+

NOTE  Language match is confidence-gated (0.85) with a default-language
      fallback -- not a simple "query looks English -> answer English" rule.
      Short/ambiguous queries fall back to the default language regardless
      of how they read.

+----------------------------------------------------+
| 28 Citation-ID validation                          |   ROLE  Grounding check
|    every [Sn] checked vs context                   |   WHY   The model can hallucinate a citation tag that was never in context -- this catches that mechanically
+----------------------------------------------------+
                           |
                           v
+----------------------------------------------------+
| 29 Structured response                             |   ROLE  Contract enforcer
|    answer + sources + metadata JSON                |   WHY   Downstream UI and logging need a stable shape, not free-form text
+----------------------------------------------------+

-------- CROSS-CUTTING SERVICES (attach throughout, not in main chain) -

+---------------------------+   +---------------------------+   +---------------------------+
| 30 Embedding cache        |   | 30 Retrieval cache        |   | 30 LLM cache              |
| 1h TTL                    |   | 5-15m, ACL-scoped         |   | 15-60m, ACL-scoped        |
+---------------------------+   +---------------------------+   +---------------------------+
             |                             |                             |             
             +-----------------------------------------------------------+             
                                           |                                           
                                           v                                           

NOTE  ROLE Latency/cost reducer. WHY Re-running embed/retrieve/generate for
      a repeat question wastes time and money for an answer you already
      computed.

NOTE  ACL-scoped means the cache key includes the user's permissions, not
      just the query text -- otherwise User A's cached answer (built from
      chunks only A can see) could be served to User B.

+----------------------------------------------------+
| 31 Audit logger                                    |   ROLE  Compliance record
|    write-once, append-only                         |   WHY   Every access needs to be reconstructable after the fact, including rejected/failed requests
+----------------------------------------------------+

NOTE  Not a performance cache -- a permanent compliance record (hashed
      query/answer, never raw text). Never reused to answer anything; exists
      purely for accountability.

+----------------------------------------------------+
| 32 Observability                                   |   ROLE  Health signal
|    latency SLIs, error rates                       |   WHY   You cannot fix degradation you cannot see -- SLIs are the early-warning system
+----------------------------------------------------+

+----------------------------------------------------+
| 33 Feedback loop                                   |   ROLE  Quality flywheel
|    thumbs up/down -> golden-set candidates         |   WHY   Turns real user corrections into the next eval set instead of losing that signal
+----------------------------------------------------+

+----------------------------------------------------+
| 34 Evaluation harness                              |   ROLE  Regression gate
|    golden set, regression gate (offline)           |   WHY   Without it, a small tweak can silently make retrieval worse and nobody finds out until users complain
+----------------------------------------------------+

NOTE  Regression gate blocks deployment if nDCG@5 drops more than 3% vs. the
      last approved baseline -- stops a fix that quietly makes retrieval
      worse from shipping.
```

---

## Module responsibilities

| Module | File | What it does |
|---|---|---|
| Config | `rag/config.py` | Stores all settings — models, thresholds, file paths, TTLs |
| Schemas | `rag/schemas.py` | Defines the data shapes used everywhere (chunks, answers, users) |
| Language | `rag/language.py` | Detects Vietnamese vs. English, per chunk and per query |
| Chunker | `rag/chunking.py` | Splits documents into ~400–512 token pieces, no mid‑sentence cuts |
| Embedder | `rag/embedder.py` | Converts text into BGE‑M3 search vectors |
| Vector Store | `rag/vectorstore.py` | Stores and searches vectors in ChromaDB; runs Checkpoint 1 |
| Ingester | `rag/ingester.py` | Runs the full document intake process end to end |
| Memory | `rag/memory.py` | Rewrites follow-up questions into standalone ones using recent session turns (Redis-backed) |
| Retriever | `rag/retriever.py` | Searches by meaning (dense) and by keyword (BM25) |
| Fusion | `rag/fusion.py` | Merges the two search results into one ranked list (RRF) |
| Reranker | `rag/reranker.py` | Re‑scores the top candidates for real relevance |
| Security | `rag/security.py` | Runs Checkpoint 2 (ACL filter) and the injection‑content guard |
| Context | `rag/context.py` | Assembles the final text sent to the LLM; runs Checkpoint 3 |
| Generator | `rag/generator.py` | Asks Claude to write the grounded, cited answer |
| Pipeline | `rag/pipeline.py` | Orchestrates every step above, start to finish |
| Cache | `rag/cache.py` | Speeds up repeat queries via Redis (ACL‑scoped keys) |
| Audit | `rag/audit.py` | Logs every request, append‑only, for review/compliance |
| API | `api/main.py` | FastAPI service exposing Pipeline and Ingester over HTTP/SSE — the only thing the frontend is allowed to call |
| Frontend | `frontend/` (React + TypeScript) | The chat interface — calls the API only, no direct Python dependency |
| Ingest CLI | `scripts/ingest.py` | Command‑line tool to bulk‑load documents |
| Query CLI | `scripts/query.py` | Command‑line tool to test a single query |
| Evaluator | `eval/evaluate.py` | Scores retrieval and answer quality against a golden test set |


---
