"""Feedback loop — thumbs up/down → golden-set candidates (arch flow step 33).

Turns real user corrections into the next eval set instead of losing that
signal. Every rating is appended to a feedback log; down-votes are ALSO written
to a golden-candidates file so a human can review and promote them into
eval/golden/queries.jsonl.

Text only, append-only, no external deps.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, field
from typing import Literal

from .config import RAGConfig, get_config
from .schemas import utcnow

Rating = Literal["up", "down"]


@dataclass(frozen=True)
class Feedback:
    trace_id: str | None
    tenant_id: str
    user_id: str
    query: str
    rating: Rating
    answer: str | None = None
    comment: str | None = None
    doc_ids: list[str] = field(default_factory=list)
    created_at: str = ""


class FeedbackStore:
    def __init__(self, config: RAGConfig | None = None) -> None:
        self._cfg = config or get_config()
        self._dir = self._cfg.paths.data_dir / "feedback"
        self._lock = threading.Lock()

    def _append(self, path_name: str, record: dict) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        with self._lock:
            with open(self._dir / path_name, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def record(self, fb: Feedback) -> None:
        now = fb.created_at or utcnow().isoformat()
        # The answer is stored as a hash + short excerpt (not raw), matching the
        # audit log's posture. The QUERY is kept raw on purpose: golden-set
        # curation needs the actual question to build the next eval case.
        self._append("feedback.jsonl", {
            "trace_id": fb.trace_id,
            "tenant_id": fb.tenant_id,
            "user_id": fb.user_id,
            "query": fb.query,
            "rating": fb.rating,
            "answer_excerpt": (fb.answer[:500] if fb.answer else None),
            "answer_sha256": (
                hashlib.sha256(fb.answer.encode("utf-8")).hexdigest() if fb.answer else None
            ),
            "comment": fb.comment,
            "doc_ids": fb.doc_ids,
            "created_at": now,
        })
        # A down-vote is a candidate for the golden set: the retrieval or answer
        # was wrong, so this query is worth adding to the regression suite.
        if fb.rating == "down":
            self._append("golden_candidates.jsonl", {
                "query": fb.query,
                "tenant_id": fb.tenant_id,
                "user_principals": [],
                "relevant_doc_ids": fb.doc_ids,
                "note": fb.comment or "user down-voted",
                "trace_id": fb.trace_id,
                "created_at": now,
            })


_shared: FeedbackStore | None = None


def get_feedback_store() -> FeedbackStore:
    global _shared
    if _shared is None:
        _shared = FeedbackStore()
    return _shared
