#!/usr/bin/env python
"""Query CLI — ask one question against the FPT RAG index.

Runs the full read path (validation -> memory -> language -> embed -> CP1 ->
retrieve -> fuse -> CP2 -> rerank -> score filter -> context/CP3 -> generate ->
citation check) and prints the grounded answer with its cited sources.

Examples
--------
  # Ask a question, offline (matches an offline ingest):
  python scripts/query.py "What is the refund window?" --offline

  # Ask as a specific principal / classification:
  python scripts/query.py "refund policy" --tenant demo \
      --principals "group:support" --classification internal

  # Machine-readable output:
  python scripts/query.py "refund policy" --offline --json
"""

from __future__ import annotations

import argparse
import json
import sys

from _common import bootstrap, parse_principals


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Ask one question against the RAG index.")
    ap.add_argument("query", help="The question to ask.")
    ap.add_argument("--tenant", default="demo")
    ap.add_argument("--user", default="cli-user", help="User id.")
    ap.add_argument(
        "--principals", default="group:support",
        help="Comma-separated principals for the ACL checks (CP2).",
    )
    ap.add_argument(
        "--classification", default="internal",
        choices=["public", "internal", "confidential", "restricted"],
        help="The user's max readable classification (CP1 ceiling).",
    )
    ap.add_argument("--session", default=None, help="Session id (enables follow-up memory).")
    ap.add_argument("--risk", default="medium", choices=["low", "medium", "high"])
    ap.add_argument("--offline", action="store_true", help="Hash embedder + persistent Chroma.")
    ap.add_argument("--json", action="store_true", help="Emit the structured answer as JSON.")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    bootstrap(args.offline)

    from rag import RAGConfig, RAGPipeline, UserContext  # noqa: E402

    cfg = RAGConfig.load()
    pipe = RAGPipeline(cfg)
    user = UserContext(
        tenant_id=args.tenant,
        user_id=args.user,
        principals=parse_principals(args.principals),
        max_classification=args.classification,
        session_id=args.session,
    )

    answer = pipe.query(args.query, user, risk=args.risk)

    if args.json:
        print(json.dumps(
            {
                "answer": answer.answer,
                "abstained": answer.abstained,
                "abstention_reason": answer.abstention_reason,
                "language": answer.language,
                "model": answer.model,
                "trace_id": answer.trace_id,
                "sources": [
                    {"label": s.label, "doc_id": s.doc_id, "title": s.title,
                     "source_uri": s.source_uri, "section_path": s.section_path,
                     "page_number": s.page_number}
                    for s in answer.sources
                ],
            },
            ensure_ascii=False, indent=2,
        ))
        return 0

    print("\n" + "=" * 70)
    print(answer.answer)
    print("=" * 70)
    if answer.abstained:
        print(f"[abstained: {answer.abstention_reason}]")
    if answer.sources:
        print("\nSources:")
        for s in answer.sources:
            # Drop section entries identical to the title to avoid "X - X".
            section = [p for p in s.section_path if p != s.title]
            loc = " > ".join(section)
            page = f" p.{s.page_number}" if s.page_number else ""
            print(f"  [{s.label}] {s.title}{(' - ' + loc) if loc else ''}{page}")
            print(f"        {s.source_uri}")
    print(f"\n(lang={answer.language}, model={answer.model}, trace={answer.trace_id})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
