# FPT RAG — Step-by-Step Build Checklist

> **Owners:** **Son** — backend pipeline (lead) · **Quang Anh** — frontend (owner) + backend support · **Both** — shared / integration.

---

## Phase 1 — Build in parallel

INGESTION	
- [ ] Language detection (EN/VI) — **Son**
- [ ] Chunker — splits documents into pieces — **Son**
- [ ] Embedder — turns text into vectors (fix: broken) — **Son**
- [ ] VectorStore — saves vectors to Chroma + BM25 — **Son**
- [ ] Ingester — ties ingestion together (fix: broken) — **Son**
- [ ] Ingest CLI — command to bulk-load documents — **Son**

QUERY
- [ ] Session memory — rewrites follow-up questions — **Son**
- [ ] Retriever — searches by meaning + keyword — **Son**
- [ ] Fusion — merges the two search results — **Son**
- [ ] Security — checks permissions, blocks bad content — **Son**
- [ ] Reranker — re-scores results for real relevance — **Son**
- [ ] Context assembly — packages evidence for the model — **Son**
- [ ] Generator — writes the answer (fix: broken) — **Son**
- [ ] Pipeline — ties the whole query side together — **Son**
- [ ] Query CLI — command to test one question at a time — **Son**


---

## Phase 2 — Plug it together
*Together*

- [ ] Point Query Pipeline at the real index Ingester fills — **Both**
- [ ] Run one real document through ingestion, then ask a real question about it — **Both**

> **Milestone M2:** one real document in, one real cited answer out — no manual seeding involved.

---

## Phase 3 — API and frontend

- [ ] FastAPI service: `/query`, `/ingest`, `/health`, plus a streaming route for token-by-token answers — **Quang Anh**
- [ ] React + TypeScript chat screen with a question box — **Quang Anh**
- [ ] Answer view with clickable citation tags — **Quang Anh**
- [ ] Admin panel: upload a document, check system health — **Quang Anh**
- [ ] Connect everything to the API — **Both**

> **Milestone M3:** the chat app works end to end in a browser.

---

## Phase 4 — Make it fast and accountable

- [ ] Caching for repeat questions — **Son**
- [ ] Audit log — records every request — **Son**
- [ ] Basic monitoring — latency and error rates — **Son**
- [ ] Feedback button — thumbs up/down — **Quang Anh** 

> **Milestone M4:** repeat questions are noticeably faster; every request shows up in the log.

---

## Phase 5 — Prove it works


- [ ] Build a small test set of questions with known right answers — **Son**
- [ ] Run the evaluator, get a baseline score — **Son**
- [ ] Set the rule: don't ship if the score drops more than 3% — **Son**
- [ ] Run a security pass — try to break your own system with bad input — **Son**

> **Milestone M5:** your test set passes the quality bar.

---

## Phase 6 — Ship it


- [ ] Docker Compose file — starts everything with one command — **Son**
- [ ] Configure nginx — traffic and security at the edge — **Son**
- [ ] Final walkthrough demo, together — **Both**

> **Milestone M6 (final):** everything marked broken now works, and the demo runs start to finish.
