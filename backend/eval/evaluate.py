#!/usr/bin/env python
"""Evaluation harness — the offline regression gate (arch flow step 34).

Ingests the golden corpus into an isolated index, runs every golden query, and
scores retrieval + answer quality:

  Retrieval: Recall@{1,3,5}, MRR, nDCG@5 (the primary metric)
  Answer:    abstention accuracy, citation validity, must-not-return leaks

Then compares the primary metric against a saved baseline and BLOCKS (exit 2) if
it dropped by more than the allowed regression (default 3%, RAG_EVAL_MAX_REGRESSION).

By default it runs on the OFFLINE backends (hash embedder + lexical reranker +
extractive generator) so it works with no downloads — establish the real baseline
later with `--real` once BGE-M3 / the reranker / the LLM are available.

Usage
-----
  python eval/evaluate.py                 # score + gate against baseline
  python eval/evaluate.py --update-baseline
  python eval/evaluate.py --real          # use the configured (real) backends
  python eval/evaluate.py --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = EVAL_DIR.parent
BASELINE_PATH = EVAL_DIR / "baseline.json"
CORPUS_DIR = EVAL_DIR / "golden" / "corpus"
QUERIES_PATH = EVAL_DIR / "golden" / "queries.jsonl"


def _bootstrap(real: bool) -> None:
    if not real:
        os.environ.setdefault("RAG_EMBEDDER_BACKEND", "hash")
        os.environ.setdefault("RAG_VECTORSTORE_BACKEND", "memory")
        os.environ.setdefault("RAG_RERANKER_BACKEND", "lexical")
        os.environ.setdefault("RAG_LLM_PROVIDER", "extractive")
    # Isolate the eval index so it never touches prod data and is reproducible.
    os.environ["RAG_DATA_DIR"] = tempfile.mkdtemp(prefix="rag_eval_")
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))


def _title(text: str, fallback: str) -> str:
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("#"):
            return s.lstrip("#").strip() or fallback
        if s:
            break
    return fallback


def _load_queries() -> list[dict]:
    rows = []
    for line in QUERIES_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def run_eval(real: bool) -> dict:
    from rag import Ingester, RAGConfig, RAGPipeline, SourceDocument, UserContext
    from eval.metrics import dedupe_preserve_order, mrr, ndcg_at_k, recall_at_k

    cfg = RAGConfig.load()
    ing = Ingester(cfg)
    pipe = RAGPipeline(cfg)

    # --- Ingest the golden corpus (all world-readable, tenant 'demo'). ---
    for path in sorted(CORPUS_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        ing.ingest(SourceDocument(
            tenant_id="demo", doc_id=path.stem, source_system="golden",
            source_uri=f"golden://{path.name}", title=_title(text, path.stem),
            content_type="text/markdown", text=text, acl_principals=["*"],
            sensitivity="internal",
        ))

    queries = _load_queries()
    per_query = []
    recalls_5, recalls_3, recalls_1, mrrs, ndcgs = [], [], [], [], []
    abstain_correct = 0
    citation_valid = 0
    answered = 0
    leaks = 0

    for q in queries:
        user = UserContext(
            tenant_id=q["tenant_id"], user_id="eval",
            principals=q.get("user_principals", []), max_classification="internal",
        )
        relevant = set(q.get("relevant_doc_ids", []))
        forbidden = set(q.get("must_not_return_doc_ids", []))
        graded = {d: 1.0 for d in relevant}

        # Retrieval metrics (skip for genuinely unanswerable queries).
        row: dict = {"query_id": q["query_id"]}
        if relevant and not q.get("expect_abstain"):
            ranked = pipe.rank_candidates(q["query"], user, top_k=10)
            ranked_docs = dedupe_preserve_order([r.doc_id for r in ranked])
            r5 = recall_at_k(ranked_docs, relevant, 5)
            r3 = recall_at_k(ranked_docs, relevant, 3)
            r1 = recall_at_k(ranked_docs, relevant, 1)
            m = mrr(ranked_docs, relevant)
            nd = ndcg_at_k(ranked_docs, graded, 5)
            recalls_5.append(r5); recalls_3.append(r3); recalls_1.append(r1)
            mrrs.append(m); ndcgs.append(nd)
            row.update({"recall@5": r5, "mrr": round(m, 3), "ndcg@5": round(nd, 3),
                        "top": ranked_docs[:3]})

        # Answer-level checks (full pipeline).
        ans = pipe.query(q["query"], user, risk=q.get("risk", "medium"))
        expect_abstain = bool(q.get("expect_abstain"))
        if ans.abstained == expect_abstain:
            abstain_correct += 1
        row["abstain"] = ans.abstained
        row["abstain_ok"] = ans.abstained == expect_abstain

        if not ans.abstained:
            answered += 1
            returned_docs = {s.doc_id for s in ans.sources}
            if forbidden & returned_docs:
                leaks += 1
                row["leak"] = sorted(forbidden & returned_docs)
            # Every cited [S#] must exist in the returned sources.
            from rag.generator import cited_source_ids
            labels = {s.label for s in ans.sources}
            if not (cited_source_ids(ans.answer) - labels):
                citation_valid += 1

        per_query.append(row)

    metrics = {
        "recall@1": round(_mean(recalls_1), 4),
        "recall@3": round(_mean(recalls_3), 4),
        "recall@5": round(_mean(recalls_5), 4),
        "mrr": round(_mean(mrrs), 4),
        "ndcg@5": round(_mean(ndcgs), 4),
        "abstention_accuracy": round(abstain_correct / len(queries), 4),
        "citation_valid_rate": round(citation_valid / answered, 4) if answered else 1.0,
        "leaks": leaks,
        "n_queries": len(queries),
    }
    return {"metrics": metrics, "per_query": per_query,
            "mode": "real" if real else "offline"}


def gate(metrics: dict, cfg_metric: str, max_regression: float,
         update: bool) -> tuple[bool, str]:
    current = metrics.get(cfg_metric, 0.0)
    if update or not BASELINE_PATH.exists():
        BASELINE_PATH.write_text(json.dumps(
            {"primary_metric": cfg_metric, "metrics": metrics}, indent=2), "utf-8")
        return True, f"Baseline written ({cfg_metric}={current})."
    baseline = json.loads(BASELINE_PATH.read_text("utf-8"))
    base_val = baseline["metrics"].get(cfg_metric, 0.0)
    drop = base_val - current
    if drop > max_regression:
        return False, (f"REGRESSION: {cfg_metric} {base_val} -> {current} "
                       f"(drop {drop:.4f} > {max_regression}). Blocking.")
    return True, (f"OK: {cfg_metric} {base_val} -> {current} "
                  f"(delta {(-drop):+.4f}, limit -{max_regression}).")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Golden-set evaluation + regression gate.")
    ap.add_argument("--real", action="store_true", help="Use configured (real) backends.")
    ap.add_argument("--update-baseline", action="store_true", help="Overwrite the baseline.")
    ap.add_argument("--json", action="store_true", help="Emit JSON.")
    args = ap.parse_args(argv)

    _bootstrap(args.real)
    from rag import RAGConfig
    cfg = RAGConfig.load()

    result = run_eval(args.real)
    metrics = result["metrics"]
    passed, message = gate(
        metrics, cfg.eval.primary_metric, cfg.eval.max_regression, args.update_baseline
    )

    if args.json:
        print(json.dumps({**result, "gate": {"passed": passed, "message": message}},
                         ensure_ascii=False, indent=2))
        return 0 if passed else 2

    print(f"\n=== Golden-set evaluation ({result['mode']} backends) ===")
    for row in result["per_query"]:
        rd = f"nDCG@5={row.get('ndcg@5', '-')}, top={row.get('top', '-')}" if "ndcg@5" in row else "(unanswerable)"
        flag = "" if row["abstain_ok"] else "  <-- ABSTENTION MISMATCH"
        leak = f"  LEAK={row['leak']}" if row.get("leak") else ""
        print(f"  {row['query_id']}: abstain={row['abstain']} {rd}{flag}{leak}")
    print("\nAggregate metrics:")
    for k, v in metrics.items():
        print(f"  {k:22} {v}")
    print(f"\nGate: {'PASS' if passed else 'FAIL'} — {message}")
    if metrics["leaks"]:
        print("  WARNING: ACL leaks detected — this must be zero.")
    return 0 if passed else 2


if __name__ == "__main__":
    sys.exit(main())
