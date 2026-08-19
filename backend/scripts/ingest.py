#!/usr/bin/env python
"""Ingest CLI — bulk-load documents into the FPT RAG index.

Reads text, PDF, Office, HTML/CSV, and common image files from a file or directory and
runs each through the full 13-step ingestion pipeline (hash -> lock -> chunk ->
augment -> detect -> injection scan -> PII redact -> embed -> BM25 -> stamp ->
atomic upsert). Idempotent: re-ingesting an unchanged file is skipped.

Examples
--------
  # Ingest a folder, offline (no model download), world-readable, internal:
  python scripts/ingest.py ./docs --offline --acl "*"

  # Ingest one file for a tenant, restricted to a group:
  python scripts/ingest.py ./policy.md --tenant demo --acl "group:support" \
      --sensitivity confidential

  # Delete (tombstone) a previously ingested doc:
  python scripts/ingest.py --delete refund-policy --tenant demo
"""

from __future__ import annotations

import argparse
import re
import sys

from _common import (
    bootstrap,
    collect_files,
    derive_title,
    parse_principals,
    read_document,
)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Bulk-load documents into the RAG index.")
    ap.add_argument("path", nargs="?", help="File or directory to ingest.")
    ap.add_argument("--tenant", default="demo", help="Tenant id (default: demo).")
    ap.add_argument("--source-system", default="manual", help="Source system label.")
    ap.add_argument(
        "--acl", default="*",
        help='Comma-separated acl_principals (default "*" = world-readable).',
    )
    ap.add_argument(
        "--sensitivity", default="internal",
        choices=["public", "internal", "confidential", "restricted"],
    )
    ap.add_argument("--uri-base", default="file://", help="Prefix for source_uri.")
    ap.add_argument("--recursive", action="store_true", help="Recurse into subdirs.")
    ap.add_argument(
        "--offline", action="store_true",
        help="Hash embedder + persistent Chroma; no 4GB model download.",
    )
    ap.add_argument("--delete", metavar="DOC_ID", help="Tombstone a doc_id instead of ingesting.")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    bootstrap(args.offline)

    from rag import Ingester, RAGConfig, SourceDocument  # noqa: E402

    cfg = RAGConfig.load()
    ing = Ingester(cfg)

    # Delete mode ------------------------------------------------------------ #
    if args.delete:
        res = ing.delete(args.tenant, args.delete)
        print(f"[{res.status}] {args.delete} — {res.message}")
        return 0

    if not args.path:
        build_parser().error("a path is required (or use --delete DOC_ID)")

    files = collect_files(args.path, args.recursive)
    if not files:
        print("No supported text, PDF, Office, HTML/CSV, or image files found.")
        return 1

    acl = parse_principals(args.acl)
    totals = {"indexed": 0, "skipped_unchanged": 0, "quarantined": 0, "failed": 0, "deleted": 0}
    chunk_total = 0

    print(f"Ingesting {len(files)} file(s) into tenant '{args.tenant}' "
          f"(acl={acl}, sensitivity={args.sensitivity})\n")
    for f in files:
        # A Word lock file (~$foo.docx), a corrupt PDF, or a missing OCR runtime
        # must not abort the whole batch — report and move on.
        if f.name.startswith("~$"):
            totals["failed"] = totals.get("failed", 0) + 1
            print(f"  [x] {f.name:40} skipped (temp/lock file)")
            continue
        try:
            text, content_type = read_document(f)
        except SystemExit as exc:  # missing parser dependency
            totals["failed"] = totals.get("failed", 0) + 1
            print(f"  [x] {f.name:40} {exc}")
            continue
        except Exception as exc:
            totals["failed"] = totals.get("failed", 0) + 1
            print(f"  [x] {f.name:40} read error: {type(exc).__name__}: {exc}")
            continue
        if not text.strip():
            totals["failed"] = totals.get("failed", 0) + 1
            print(f"  [x] {f.name:40} no extractable text (image-only?)")
            continue
        # doc_id is interpolated into quarantine/tombstone filenames and the API
        # enforces ^[A-Za-z0-9._-]+$, so map a messy real filename (spaces,
        # Vietnamese, punctuation) to a path-safe id. The title keeps the
        # original text.
        doc_id = re.sub(r"[^A-Za-z0-9._-]+", "_", f.stem).strip("_") or "doc"
        doc = SourceDocument(
            tenant_id=args.tenant,
            doc_id=doc_id,                       # stable, human-legible id (NOT a hash)
            source_system=args.source_system,
            source_uri=f"{args.uri_base}{f.as_posix()}",
            title=derive_title(text, f.stem),
            content_type=content_type,
            text=text,
            acl_principals=acl,
            sensitivity=args.sensitivity,
        )
        try:
            res = ing.ingest(doc)
        except Exception as exc:
            totals["failed"] = totals.get("failed", 0) + 1
            print(f"  [x] {f.name:40} ingest error: {type(exc).__name__}: {exc}")
            continue
        totals[res.status] = totals.get(res.status, 0) + 1
        chunk_total += res.chunks_indexed
        marker = {
            "indexed": "+", "skipped_unchanged": "=", "quarantined": "!",
            "failed": "x",
        }.get(res.status, "?")
        detail = f"{res.chunks_indexed} chunks" if res.status == "indexed" else (res.message or "")
        print(f"  [{marker}] {f.name:40} {res.status:18} {detail}")

    print(
        f"\nDone. indexed={totals['indexed']} "
        f"skipped={totals['skipped_unchanged']} "
        f"quarantined={totals['quarantined']} failed={totals['failed']} "
        f"({chunk_total} chunks total)"
    )
    return 0 if totals["failed"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
