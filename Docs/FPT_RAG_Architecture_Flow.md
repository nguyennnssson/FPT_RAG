# FPT RAG SYSTEM

**Ingestion: Source doc → hash/lock/chunk → contextual augment → embed (BGE-M3) + BM25 index → ChromaDB/BM25 store**



**Query: session memory (condense) → query embed → CP1 → retrieve → RRF fuse → CP2 → rerank → CP3 → Claude/ChatGPT generate → cited answer**

\---

## INGESTION — WRITE PATH

*Orchestrated end-to-end by `Ingester` (`rag/ingester.py`).*

```
+----------------------------------------------------+
| 1. Source Document                                 |  ---- Raw input file enters the pipeline, tagged with source metadata
| .txt / .pdf / .md                                  |
+----------------------------------------------------+
                     |
                     v
+----------------------------------------------------+
| 2. Content Hash                                    |  ---- Computes a SHA-256 hash of the normalized content to check if this exact version was already indexed
| SHA-256, idempotency check                         |
+----------------------------------------------------+
                     |
                     v
+----------------------------------------------------+
| 3. Per-doc\_id Lock                                |  ---- Acquires an exclusive lock on the doc\_id for the duration of ingestion
| concurrency guard                                  |
+----------------------------------------------------+
                     |
                     v
+----------------------------------------------------+
| 4. Chunker                                         |  ---- Recursively splits the document into 400-512 token segments along headings/paragraphs/sentences, with 10-15% overlap
| recursive, 400-512 tok, 10-15% overlap             |
+----------------------------------------------------+
                     |
                     v
+----------------------------------------------------+
| 5. Contextual Augmentation                         |  ---- Prepends a heading/section reference to each chunk, producing a separate contextual\_text field alongside the raw chunk
| heading-prefix -> contextual\_text (separate field)|
+----------------------------------------------------+
                     |
                     v
+----------------------------------------------------+
| 6. Language Detection                              |  ---- Runs language identification on each chunk and stores the result (EN/VI) as metadata
| per-chunk, metadata only (EN/VI)                   |
+----------------------------------------------------+
                     |
                     v
+----------------------------------------------------+
| 7. Injection Heuristic Scan                        |  ---- Scans chunk text for patterns resembling embedded instructions and quarantines matches instead of indexing them
| quarantine on match                                |
+----------------------------------------------------+
                     |
                     v
+----------------------------------------------------+
| 8. PII Detection \& Redaction                      |  ---- Applies classification-based rules to detect personal-data fields and redacts them before embedding
| classification-based policy                        |
+----------------------------------------------------+
                     |
                     v
+----------------------------------------------------+
| 9. Embedder (BGE-M3)                               |  ---- Encodes contextual\_text into a 1024-dim dense vector using BGE-M3 in passage mode, no instruction prefix
| passage mode, no prefix, 1024-dim dense            |
+----------------------------------------------------+
                     |
                     v
+----------------------------------------------------+
| 10. BM25 Indexer                                   |  ---- Tokenizes chunk text (pyvi for VI, Porter stemmer for EN) and updates the BM25 term-frequency index
| pyvi (VI) / Porter (EN) tokenization               |
+----------------------------------------------------+
                     |
                     v
+----------------------------------------------------+
| 11. Version Stamp                                  |  ---- Records which embedder, chunker, and parser versions produced this chunk
| embedder / chunker / parser versions               |
+----------------------------------------------------+
                     |
                     v
+----------------------------------------------------+
| 12. Atomic Upsert                                  |  ---- Writes the chunk, vector, and BM25 entry through a stage-validate-swap-verify sequence
| stage -> validate -> swap -> verify                |
+----------------------------------------------------+
                     |
                     v
+----------------------------------------------------+
| 13. Tombstone on Delete                            |  ---- Marks a deleted document's chunks as tombstoned in the index instead of physically removing them
| never hard-delete                                  |
+----------------------------------------------------+
```

\---

## STORAGE LAYER

*Shared by both paths.*

```
+----------------------------------------------------+
| ChromaDB Collection                                |  ---- Stores one collection of dense vectors, queried by cosine similarity
| 1 collection, cosine similarity, dense vectors     |
+----------------------------------------------------+

+----------------------------------------------------+
| BM25 Index                                         |  ---- Stores the term-frequency postings as a pickled rank-bm25 index on disk
| rank-bm25, pickle cache                            |
+----------------------------------------------------+
```

\---

## QUERY — READ PATH

*Orchestrated by `RAGPipeline` (`rag/pipeline.py`).*

```
+----------------------------------------------------+
| 1. User Query                                      |  ---- Receives the raw question text from the chat UI or CLI, with a language tag
| chat UI or CLI, tagged with language               |
+----------------------------------------------------+
                     |
                     v
+----------------------------------------------------+
| 2. Input Validation                                |  ---- Checks the query's token length (via the embedding model's real tokenizer) and character set
| length cap (real tokenizer), charset check         |
+----------------------------------------------------+
                     |
                     v
+----------------------------------------------------+
| 3. Session Memory                                  |  ---- Rewrites the question into a self-contained one using recent turns from this session, e.g. "what about Vietnam?" -> "What is the remote-work policy                           
| condense turns -> standalone query (Redis)         |       for Vietnam?"	
+----------------------------------------------------+
                     |
                     v
+----------------------------------------------------+
| 4. Query Language Detection                        |  ---- Runs confidence-gated language detection on the question to pick the response language
| confidence-gated                                   |
+----------------------------------------------------+
                     |
                     v
+----------------------------------------------------+
| 5. Query Embedder (BGE-M3)                         |  ---- Encodes the question into a 1024-dim vector using BGE-M3, adding a query instruction prefix
| + query instruction prefix                         |
+----------------------------------------------------+
                     |
                     v
+----------------------------------------------------+
| 6. Checkpoint 1 (CP1)                              |  ---- Applies a native ChromaDB where-filter on tenant\_id + classification before similarity search runs
| scalar pre-filter: tenant\_id + classification      |
+----------------------------------------------------+
                     |
                     v
+----------------------------------------------------+
| 7. Retriever                                       |  ---- Runs a dense cosine-similarity search (top-20) and a BM25 keyword search (top-20) in parallel
| dense (Chroma cosine, top-20) + BM25 (top-20)      |
+----------------------------------------------------+
                     |
                     v
+----------------------------------------------------+
| 8. Fusion (RRF)                                    |  ---- Combines the dense and BM25 ranked lists via Reciprocal Rank Fusion (k=60), deduped by chunk\_id
| k=60, dedup by chunk\_id                            |
+----------------------------------------------------+
                     |
                     v
+----------------------------------------------------+
| 9. Checkpoint 2 (CP2)                              |  ---- Checks the user's principals against each chunk's acl\_principals list in Python, removing unauthorized chunks
| ACL filter on acl\_principals, before reranker      |
+----------------------------------------------------+
                     |
                     v
+----------------------------------------------------+
| 10. Reranker                                       |  ---- Scores each fused candidate against the query with the bge-reranker-v2-m3 cross-encoder and keeps the top 5
| bge-reranker-v2-m3, top-5                          |
+----------------------------------------------------+
                     |
                     v
+----------------------------------------------------+
| 11. Score Filter                                   |  ---- Compares the top reranker score to a risk-graded threshold; abstains if nothing clears it
| risk-graded threshold; abstain if none pass        |
+----------------------------------------------------+
                     |
                     v
+----------------------------------------------------+
| 12. Context Assembly                               |  ---- Dedupes chunks, flags contradictions, re-applies CP3, orders evidence sandwich-style, enforces token budget, tags sources
| dedup + contradiction check, CP3 recheck, sandwich |
| order, token budget, source tags                   |
+----------------------------------------------------+
                     |
                     v
+----------------------------------------------------+
| 13. Generator (ChatGpt/Claude)                     |  ---- Generates the answer from the assembled context only, in the matched language, attaching \[Sn] citation tags
| grounded, cited answer                             |
+----------------------------------------------------+
                     |
                     v
+----------------------------------------------------+
| 14. Citation-ID Validation                         |  ---- Parses every \[Sn] tag in the generated answer and checks it against source IDs present in context
| every \[Sn] checked vs context                      |
+----------------------------------------------------+
                     |
                     v
+----------------------------------------------------+
| 15. Structured Response                            |  ---- Packages the answer, citations, and metadata into a fixed JSON schema for the UI and logs
| answer + sources + metadata JSON                   |
+----------------------------------------------------+
```

\---

## CROSS-CUTTING SERVICES

*Attach throughout both paths — not a sequential flow.*

```
+----------------------------------------------------+
| Embedding Cache                                    |  ---- Caches computed embeddings for 1 hour, keyed on normalized text + model version
| Redis, 1h TTL                                      |
+----------------------------------------------------+

+----------------------------------------------------+
| Retrieval Cache                                    |  ---- Caches retrieved candidate sets for 5-15 minutes, keyed with the user's permission scope
| Redis, 5-15m TTL, ACL-scoped                       |
+----------------------------------------------------+

+----------------------------------------------------+
| LLM Cache                                          |  ---- Caches generated answers for 15-60 minutes, keyed with the user's permission scope
| Redis, 15-60m TTL, ACL-scoped                      |
+----------------------------------------------------+

+----------------------------------------------------+
| Audit Logger                                       |  ---- Appends a write-once record of every request (hashed query/answer, chunk IDs, latency) to a permanent log
| write-once, append-only (query\_hash, chunk\_ids,    |
| latency)                                           |
+----------------------------------------------------+

+----------------------------------------------------+
| Observability                                      |  ---- Tracks latency percentiles and error rates for each pipeline stage
| latency SLIs, error rates                          |
+----------------------------------------------------+

+----------------------------------------------------+
| Feedback Loop                                      |  ---- Captures thumbs up/down signals and routes corrections into golden-set candidates
| thumbs up/down -> golden-set candidates            |
+----------------------------------------------------+

+----------------------------------------------------+
| Evaluation Harness                                 |  ---- Runs the golden set offline and blocks deployment if nDCG@5 drops more than 3% vs. the last baseline
| golden set, regression gate (blocks if nDCG@5      |
| drops >3%)                                         |
+----------------------------------------------------+

+----------------------------------------------------+
| Config + Schemas                                   |  ---- Defines the typed data contracts (Chunk, RagAnswer, etc.) and tunable settings every module reads from
| RAGConfig + typed contracts, shared by all modules |
+----------------------------------------------------+
```

\---

## Module responsibilities

|Module|File|What it does|
|-|-|-|
|Config|`rag/config.py`|Stores all settings — models, thresholds, file paths, TTLs|
|Schemas|`rag/schemas.py`|Defines the data shapes used everywhere (chunks, answers, users)|
|Language|`rag/language.py`|Detects Vietnamese vs. English, per chunk and per query|
|Chunker|`rag/chunking.py`|Splits documents into \~400–512 token pieces, no mid‑sentence cuts|
|Embedder|`rag/embedder.py`|Converts text into BGE‑M3 search vectors|
|Vector Store|`rag/vectorstore.py`|Stores and searches vectors in ChromaDB; runs Checkpoint 1|
|Ingester|`rag/ingester.py`|Runs the full document intake process end to end|
|Memory|`rag/memory.py`|Rewrites follow-up questions into standalone ones using recent session turns (Redis-backed)|
|Retriever|`rag/retriever.py`|Searches by meaning (dense) and by keyword (BM25)|
|Fusion|`rag/fusion.py`|Merges the two search results into one ranked list (RRF)|
|Reranker|`rag/reranker.py`|Re‑scores the top candidates for real relevance|
|Security|`rag/security.py`|Runs Checkpoint 2 (ACL filter) and the injection‑content guard|
|Context|`rag/context.py`|Assembles the final text sent to the LLM; runs Checkpoint 3|
|Generator|`rag/generator.py`|Asks Claude to write the grounded, cited answer|
|Pipeline|`rag/pipeline.py`|Orchestrates every step above, start to finish|
|Cache|`rag/cache.py`|Speeds up repeat queries via Redis (ACL‑scoped keys)|
|Audit|`rag/audit.py`|Logs every request, append‑only, for review/compliance|
|API|`api/main.py`|FastAPI service exposing Pipeline and Ingester over HTTP/SSE — the only thing the frontend is allowed to call|
|Frontend|`frontend/` (React + TypeScript)|The chat interface — calls the API only, no direct Python dependency|
|Ingest CLI|`scripts/ingest.py`|Command‑line tool to bulk‑load documents|
|Query CLI|`scripts/query.py`|Command‑line tool to test a single query|
|Evaluator|`eval/evaluate.py`|Scores retrieval and answer quality against a golden test set|



\---