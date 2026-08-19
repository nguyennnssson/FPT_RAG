# LinkedIn Experience — AI Engineer Intern, FPT Software

**Title:** AI Engineer Intern
**Company:** FPT Software
**Dates:** [Month Year] – [Month Year / Present] *(fill in)*

---

## Description (copy into the Experience "Description" field)

Contributed to the design and development of an enterprise-grade Retrieval-Augmented Generation (RAG) platform for secure, multilingual knowledge search — combining hybrid retrieval, multi-layer access control, and grounded LLM answer generation.

- Built a hybrid retrieval pipeline fusing BGE-M3 dense embeddings (1024-dim) with BM25 keyword search via Reciprocal Rank Fusion, then re-ranked candidates with a cross-encoder (bge-reranker-v2-m3) for precision.
- Designed a 3-checkpoint access-control architecture enforcing tenant, classification, and per-user permission checks at every stage of retrieval, from vector pre-filtering through final context assembly.
- Engineered a 13-step document ingestion pipeline — hashing, chunking, contextual augmentation, PII redaction, prompt-injection screening, and atomic versioned upserts — for safe, idempotent indexing at scale.
- Implemented confidence-gated abstention so the system declines to answer rather than generate an ungrounded response when retrieved evidence is weak, prioritizing trustworthiness over coverage.
- Built Redis-backed session memory to resolve follow-up questions into standalone queries, plus a multi-tier caching layer to cut latency and inference cost on repeat queries.
- Set up an evaluation harness with a golden test set and an nDCG@5 regression gate to block deployments that would degrade retrieval quality.
- Supported bilingual English/Vietnamese retrieval and generation through per-chunk language detection and confidence-gated response-language matching.
- Deployed the system as a containerized service (Docker, nginx, FastAPI, React/TypeScript) with append-only audit logging for compliance and traceability.

*LinkedIn caps descriptions at ~2,000 characters — if you need to shorten, keep the intro line plus the 4–5 bullets most relevant to the role you're targeting next.*

---

## Skills to add to this experience

**Core AI/ML**
Retrieval-Augmented Generation (RAG) · Large Language Models (LLM) · Natural Language Processing (NLP) · Machine Learning · Prompt Engineering · Artificial Intelligence (AI)

**Search & Retrieval**
Semantic Search · Vector Databases · ChromaDB · BM25 · Hybrid Search · Reranking · Information Retrieval

**Engineering & Infrastructure**
Python · TypeScript · React · FastAPI · Docker · Redis · Nginx · System Design · API Integration (Claude API)

**Security & Quality**
Access Control (RBAC/ACL) · Data Privacy / PII Redaction · Model Evaluation · MLOps

*On LinkedIn: open this Experience entry → "Add skills" → select from the list above so they're tagged to this role specifically.*
