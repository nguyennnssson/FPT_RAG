"""Context assembly — turn reranked candidates into a prompt-ready evidence set.

Full sequence (arch flow step 26, overview NOTE):
  score filter (abstain if nothing clears the reranker threshold, step 25)
    -> dedup + contradiction check
    -> CP3 secondary ACL recheck
    -> sandwich ordering (best chunk first, next-best last)
    -> token budget
    -> source tagging (<source id="Sn">)

Key invariants:
- Abstain rather than force an answer from weak evidence; a necessary-but-missing
  answer is a corpus gap to fix upstream, not a reason to lower the bar (F.3).
- Sources display the RAW chunk text, never contextual_text.
- Retrieved text is fenced as untrusted so a poisoned chunk cannot hijack the
  generator (the query-time injection-content guard).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Sequence

from .config import RAGConfig, get_config
from .schemas import RetrievalResult, Source, UserContext
from .security import cp3_recheck, guard_context
from .vectorstore import _cosine

_NUMBER_RE = re.compile(r"\b\d[\d,.]*\b")
_SOURCE_CLOSE_RE = re.compile(r"</\s*source\b", re.IGNORECASE)


@dataclass
class AssemblyResult:
    sources: list[Source] = field(default_factory=list)
    context_block: str = ""
    abstain: bool = False
    abstain_reason: str | None = None
    contradictions: list[str] = field(default_factory=list)
    flagged_injection: bool = False

    @property
    def source_ids(self) -> set[str]:
        return {s.label for s in self.sources}


class ContextAssembler:
    def __init__(self, config: RAGConfig | None = None) -> None:
        self._cfg = config or get_config()

    def assemble(
        self,
        query: str,
        reranked: Sequence[RetrievalResult],
        user: UserContext,
        risk: str = "medium",
    ) -> AssemblyResult:
        rc = self._cfg.retrieval

        # 1) Score filter — abstain if nothing clears the risk-graded threshold.
        threshold = rc.rerank_threshold(risk)
        passing = [r for r in reranked if (r.rerank_score or 0.0) >= threshold]
        if not passing:
            return AssemblyResult(
                abstain=True,
                abstain_reason=(
                    f"No evidence cleared the reranker threshold ({threshold:.2f}) "
                    f"for risk level '{risk}'."
                ),
            )

        # 2) CP3 — re-verify authorization (defense in depth). Fails closed.
        passing = cp3_recheck(passing, user)
        if not passing:
            return AssemblyResult(
                abstain=True,
                abstain_reason="No authorized evidence remained after Checkpoint 3.",
            )

        # 3) Dedup by parent + near-duplicate text/embedding.
        deduped = self._dedupe(passing)

        # 4) Token-budget selection (greedy by rerank score).
        selected = self._pack(deduped)
        if not selected:
            return AssemblyResult(
                abstain=True,
                abstain_reason="Evidence did not fit within the context token budget.",
            )

        # 5) Contradiction check (report, do not hide).
        contradictions = self._detect_contradictions(selected)

        # 6) Sandwich ordering — best first, next-best last.
        ordered = _sandwich_order(selected)

        # 7) Source tagging + untrusted-content fencing.
        sources, context_block, flagged = self._tag_sources(ordered)

        return AssemblyResult(
            sources=sources,
            context_block=context_block,
            contradictions=contradictions,
            flagged_injection=flagged,
        )

    # ------------------------------------------------------------ internals  #
    def _dedupe(self, results: Sequence[RetrievalResult]) -> list[RetrievalResult]:
        rc = self._cfg.retrieval
        selected: list[RetrievalResult] = []
        parent_counts: dict[str, int] = {}
        for r in sorted(results, key=lambda x: x.rerank_score or 0.0, reverse=True):
            parent = r.chunk.metadata.get("parent_id", r.doc_id)
            if parent_counts.get(parent, 0) >= rc.max_chunks_per_parent:
                continue
            if self._too_similar(r, selected, rc.dedup_similarity_threshold):
                continue
            selected.append(r)
            parent_counts[parent] = parent_counts.get(parent, 0) + 1
        return selected

    def _too_similar(
        self, cand: RetrievalResult, chosen: list[RetrievalResult], threshold: float
    ) -> bool:
        for prev in chosen:
            if cand.embedding is not None and prev.embedding is not None:
                if _cosine(cand.embedding, prev.embedding) >= threshold:
                    return True
            elif _text_jaccard(cand.chunk.text, prev.chunk.text) >= threshold:
                return True
        return False

    def _pack(self, results: Sequence[RetrievalResult]) -> list[RetrievalResult]:
        budget = self._cfg.retrieval.context_token_budget
        packed: list[RetrievalResult] = []
        used = 0
        for r in results:
            tokens = r.chunk.token_count or len(r.chunk.text.split())
            if used + tokens > budget and packed:
                continue
            packed.append(r)
            used += tokens
        return packed

    def _detect_contradictions(self, results: Sequence[RetrievalResult]) -> list[str]:
        """Heuristic: two chunks about the same topic (high lexical overlap) that
        carry different numeric values likely disagree. Reported, not hidden."""
        notes: list[str] = []
        items = list(results)
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                a, b = items[i].chunk, items[j].chunk
                if _text_jaccard(a.text, b.text) < 0.25:
                    continue
                nums_a = set(_NUMBER_RE.findall(a.text))
                nums_b = set(_NUMBER_RE.findall(b.text))
                if nums_a and nums_b and nums_a != nums_b:
                    notes.append(
                        f"Sources from '{a.doc_id}' and '{b.doc_id}' cover similar "
                        f"content but report different figures."
                    )
        return notes

    def _tag_sources(
        self, ordered: Sequence[RetrievalResult]
    ) -> tuple[list[Source], str, bool]:
        sources: list[Source] = []
        blocks: list[str] = []
        any_flagged = False
        for idx, r in enumerate(ordered, start=1):
            label = f"S{idx}"
            chunk = r.chunk
            # Fence suspicious retrieved text as untrusted before the LLM sees it.
            safe_text, flagged = guard_context(chunk.text)
            any_flagged = any_flagged or flagged
            # Neutralize closing tags inside the evidence so a document
            # containing a literal "</source>" cannot break out of its fence.
            safe_text = _SOURCE_CLOSE_RE.sub("<\\\\/source", safe_text)
            sources.append(
                Source(
                    label=label,
                    chunk_id=chunk.chunk_id,
                    doc_id=chunk.doc_id,
                    title=chunk.title,
                    source_uri=chunk.source_uri,
                    text=chunk.text,  # RAW text for citation display
                    section_path=list(chunk.section_path),
                    page_number=chunk.page_number,
                    modified_at=chunk.metadata.get("modified_at"),
                )
            )
            section = " > ".join(chunk.section_path) if chunk.section_path else ""
            blocks.append(
                f'<source id="{label}">\n'
                f"title: {chunk.title}\n"
                f"source_uri: {chunk.source_uri}\n"
                + (f"section: {section}\n" if section else "")
                + (f"page: {chunk.page_number}\n" if chunk.page_number else "")
                + "text:\n"
                f"{safe_text}\n"
                f"</source>"
            )
        return sources, "\n".join(blocks), any_flagged


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _sandwich_order(items: list[RetrievalResult]) -> list[RetrievalResult]:
    """Best first, next-best last, weaker in the middle — attention drops in the
    middle of a long context, so the strongest evidence brackets the ends."""
    ordered = sorted(items, key=lambda x: x.rerank_score or 0.0, reverse=True)
    front: list[RetrievalResult] = []
    back: list[RetrievalResult] = []
    for i, item in enumerate(ordered):
        (front if i % 2 == 0 else back).append(item)
    return front + list(reversed(back))


def _text_jaccard(a: str, b: str) -> float:
    sa, sb = set(a.lower().split()), set(b.lower().split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)
