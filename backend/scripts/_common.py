"""Shared helpers for the FPT RAG command-line tools.

Keeps the two CLIs (``ingest.py`` / ``query.py``) consistent: how they put the
``rag`` package on the path, how ``--offline`` mode is configured, and how source
files are read.

``--offline`` mode is the one the team uses on a laptop without the 4 GB models:
it pins the hash embedder + lexical reranker + extractive generator, but keeps
the PERSISTENT ChromaDB backend so state written by ``ingest`` is visible to a
later ``query`` process (an in-memory store would not survive the process exit).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Put the project root on sys.path at import time so the shared extractor
# (rag.extract) is importable here — the CLIs import _common before calling
# bootstrap(). Importing rag is cheap (heavy deps stay lazy) and does NOT read
# config env, so bootstrap() setting RAG_* afterwards still takes effect.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from rag.extract import (  # noqa: E402
    CONTENT_TYPES,
    SUPPORTED_EXTS,
    TEXT_EXTS,
    ExtractionError,
    derive_title,
)
from rag.extract import read_document as _read_document  # noqa: E402

__all__ = [
    "bootstrap",
    "collect_files",
    "read_document",
    "derive_title",
    "parse_principals",
    "CONTENT_TYPES",
    "SUPPORTED_EXTS",
    "TEXT_EXTS",
]


def bootstrap(offline: bool) -> None:
    """Set env for the chosen mode, THEN put the project root on sys.path.

    Env must be set before ``rag`` is imported because backends read it at
    construction time.
    """
    if not offline:
        # API startup loads this file explicitly, but the CLIs historically did
        # not. That made documented ``copy .env.example .env`` configurations
        # silently fall back to unavailable local model backends.
        from rag.config import load_env_file

        load_env_file(PROJECT_ROOT / ".env")
    if offline:
        # No model download; real (persistent) Chroma so ingest<->query share state.
        os.environ.setdefault("RAG_EMBEDDER_BACKEND", "hash")
        os.environ.setdefault("RAG_RERANKER_BACKEND", "lexical")
        os.environ.setdefault("RAG_VECTORSTORE_BACKEND", "chroma")
        os.environ.setdefault("RAG_LLM_PROVIDER", "extractive")
    # Force UTF-8 stdout/stderr so Vietnamese diacritics render on Windows
    # consoles (which default to cp1252 and would mojibake VI answers).
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))


def collect_files(path: str, recursive: bool) -> list[Path]:
    p = Path(path)
    if p.is_file():
        return [p]
    if not p.is_dir():
        raise SystemExit(f"Path not found: {path}")
    globber = p.rglob("*") if recursive else p.glob("*")
    return sorted(f for f in globber if f.is_file() and f.suffix.lower() in SUPPORTED_EXTS)


def read_document(path: Path) -> tuple[str, str]:
    """Return (text, content_type) for any supported format. Extraction lives in
    ``rag.extract``; here we just turn a missing-parser error into a clean CLI
    exit instead of a traceback."""
    try:
        return _read_document(path)
    except ExtractionError as exc:
        raise SystemExit(str(exc)) from exc


def parse_principals(raw: str) -> list[str]:
    return [p.strip() for p in raw.split(",") if p.strip()]
