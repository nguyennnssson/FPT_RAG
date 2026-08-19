"""Recursive, structure-aware chunker + contextual augmentation.

Role (arch flow steps 4-5): split a document into ~400-512 token pieces along
natural boundaries (headings -> paragraphs -> sentences -> words) with a small
overlap, without cutting mid-sentence where avoidable. Then produce a separate
``contextual_text`` for each chunk by prefixing its heading/section path, so a
bare chunk like "the limit is 30 days" carries the context needed for good
retrieval (handbook B.6-B.7).

Invariant: ``text`` stays the raw source span (what citations display);
``contextual_text`` is the augmented version we embed and BM25-index.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .config import ChunkConfig, VersionConfig, get_config
from .schemas import Chunk, SourceDocument
from .tokenization import Tokenizer, get_tokenizer

# Markdown/plaintext heading detection: "# ...", "## ...", or an ALL-CAPS or
# "Section N." style line. Kept deliberately simple and deterministic.
_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_NUM_HEADING_RE = re.compile(r"^\s*(\d+(?:\.\d+)*)[.)]\s+(\S.*)$")
# Sentence splitter that respects common abbreviations only loosely; good
# enough to avoid mid-sentence cuts for EN and VI prose.
_SENTENCE_RE = re.compile(r"(?<=[.!?。！？])\s+(?=[^\s])")
_PARA_RE = re.compile(r"\n\s*\n")
_PAGE_SECTION_RE = re.compile(r"^Page\s+(\d+)\b", re.IGNORECASE)
_PANEL_SECTION_RE = re.compile(r"^Panel\s+(\d+)\b", re.IGNORECASE)


@dataclass
class _Segment:
    """An intermediate piece of text with structure and source provenance.

    ``page_number`` is tracked independently from ``section_path``. OCR often
    emits short uppercase lines that the generic heading detector treats as a
    new level-one heading; those headings must not erase the physical PDF page.
    """

    text: str
    section_path: list[str]
    char_start: int
    char_end: int
    page_number: int | None = None
    panel_number: int | None = None


class Chunker:
    """Splits a :class:`SourceDocument` into :class:`Chunk` objects."""

    def __init__(
        self,
        config: ChunkConfig | None = None,
        versions: VersionConfig | None = None,
        tokenizer: Tokenizer | None = None,
    ) -> None:
        cfg = get_config()
        self._cfg = config or cfg.chunking
        self._versions = versions or cfg.versions
        self._tok = tokenizer or get_tokenizer()

    # ----------------------------------------------------------------- public #
    def chunk_document(self, doc: SourceDocument) -> list[Chunk]:
        segments = self._segment(doc.text)
        packed = self._pack(segments)
        chunks: list[Chunk] = []
        for seg in packed:
            token_count = self._tok.count(seg.text)
            if token_count < self._cfg.drop_below_tokens:
                continue  # orphan/noise fragment (safe to drop when others remain)
            chunks.append(self._make_chunk(doc, seg, token_count))

        # A short document whose every segment fell below the drop-floor would
        # otherwise be lost entirely. Index it as one chunk instead — losing a
        # small doc silently is worse than keeping a below-threshold one.
        if not chunks and packed:
            whole = _merge(packed)
            if whole.text.strip():
                chunks.append(self._make_chunk(doc, whole, self._tok.count(whole.text)))
        return chunks

    def _make_chunk(self, doc: SourceDocument, seg: "_Segment", token_count: int) -> Chunk:
        contextual = augment_contextual(doc.title, seg.section_path, seg.text)
        page_number = seg.page_number
        if page_number is None:
            page_number = next(
                (
                    int(match.group(1))
                    for section in seg.section_path
                    if (match := _PAGE_SECTION_RE.match(section))
                ),
                None,
            )
        chunk_id = Chunk.make_id(
            tenant_id=doc.tenant_id,
            source_system=doc.source_system,
            doc_id=doc.doc_id,
            version_id=doc.version_id,
            chunker_version=self._versions.chunker_version,
            char_start=seg.char_start,
            char_end=seg.char_end,
        )
        metadata = dict(doc.metadata)
        if seg.panel_number is not None:
            metadata["panel_number"] = seg.panel_number
        return Chunk(
            tenant_id=doc.tenant_id,
            doc_id=doc.doc_id,
            chunk_id=chunk_id,
            source_system=doc.source_system,
            source_uri=doc.source_uri,
            title=doc.title,
            text=seg.text,
            contextual_text=contextual,
            acl_principals=list(doc.acl_principals),
            sensitivity=doc.sensitivity,
            section_path=list(seg.section_path),
            page_number=page_number,
            language=doc.language,
            chunk_type="figure_caption" if seg.panel_number is not None else "text",
            metadata=metadata,
            char_start=seg.char_start,
            char_end=seg.char_end,
            token_count=token_count,
            version_id=doc.version_id,
            chunker_version=self._versions.chunker_version,
            parser_version=self._versions.parser_version,
        )

    # ------------------------------------------------------------- internals  #
    def _segment(self, text: str) -> list[_Segment]:
        """Walk the document, tracking the current heading trail, and emit one
        segment per paragraph/sentence unit with absolute char offsets."""
        segments: list[_Segment] = []
        section_path: list[str] = []
        page_number: int | None = None
        panel_number: int | None = None
        offset = 0

        for line_block in self._iter_blocks(text):
            block_text, block_start = line_block
            heading = self._as_heading(block_text)
            if heading is not None:
                level, title = heading
                if match := _PAGE_SECTION_RE.match(title):
                    page_number = int(match.group(1))
                    panel_number = None
                elif match := _PANEL_SECTION_RE.match(title):
                    panel_number = int(match.group(1))
                # Trim the trail to this heading's level, then push.
                section_path = section_path[: level - 1]
                section_path.append(title)
                continue

            # Body paragraph — split into sentences so we never have to cut one.
            for sent, s_start, s_end in self._iter_sentences(block_text, block_start):
                if sent.strip():
                    segments.append(
                        _Segment(
                            sent.strip(), list(section_path), s_start, s_end,
                            page_number=page_number,
                            panel_number=panel_number,
                        )
                    )
        return segments

    def _iter_blocks(self, text: str):
        """Yield (block_text, char_start) for each blank-line-separated block."""
        pos = 0
        for block in _PARA_RE.split(text):
            start = text.find(block, pos) if block else pos
            if start < 0:
                start = pos
            yield block, start
            pos = start + len(block)

    def _iter_sentences(self, block: str, base_offset: int):
        pos = 0
        for sent in _SENTENCE_RE.split(block):
            start = block.find(sent, pos)
            if start < 0:
                start = pos
            abs_start = base_offset + start
            abs_end = abs_start + len(sent)
            yield sent, abs_start, abs_end
            pos = start + len(sent)

    def _as_heading(self, block: str) -> tuple[int, str] | None:
        stripped = block.strip()
        if not stripped or "\n" in stripped:
            return None
        m = _MD_HEADING_RE.match(stripped)
        if m:
            return len(m.group(1)), m.group(2).strip()
        m = _NUM_HEADING_RE.match(stripped)
        if m:
            level = m.group(1).count(".") + 1
            return level, m.group(2).strip()
        # A short line with no terminal punctuation reads as a bare heading —
        # but only if it looks like a title, not a data row. Colons/semicolons
        # signal key:value data (e.g. "Row: Holiday: Tet; Days: 5"), so exclude
        # them to avoid swallowing table/CSV rows as section titles.
        if (
            len(stripped) <= 80
            and not stripped.endswith((".", "!", "?", ":", ";"))
            and ":" not in stripped
            and ";" not in stripped
        ):
            words = stripped.split()
            if 1 <= len(words) <= 12 and stripped[0].isupper():
                return 1, stripped
        return None

    def _pack(self, segments: list[_Segment]) -> list[_Segment]:
        """Greedily merge sentence segments up to ``max_tokens``, keeping a
        sentence-level overlap tail between consecutive chunks. Only sentences
        sharing the same section_path are merged, so headings stay coherent."""
        if not segments:
            return []

        packed: list[_Segment] = []
        cur: list[_Segment] = []
        cur_tokens = 0
        max_tokens = self._cfg.max_tokens

        def flush() -> list[_Segment]:
            if not cur:
                return []
            merged = _merge(cur)
            packed.append(merged)
            # Build the overlap tail from trailing sentences.
            return self._overlap_tail(cur)

        for seg in segments:
            seg_tokens = self._tok.count(seg.text)
            same_section = not cur or (
                cur[-1].section_path == seg.section_path
                and cur[-1].page_number == seg.page_number
                and cur[-1].panel_number == seg.panel_number
            )

            if cur and (cur_tokens + seg_tokens > max_tokens or not same_section):
                tail = flush()
                # Overlap only continues within a section — carrying the tail
                # across a heading would mix sections and mislabel the new
                # chunk with the previous section_path.
                cur = tail if same_section else []
                cur_tokens = sum(self._tok.count(s.text) for s in cur)

            # A single oversized sentence becomes its own chunk (can't split
            # further without cutting mid-sentence).
            cur.append(seg)
            cur_tokens += seg_tokens

        if cur:
            packed.append(_merge(cur))
        return packed

    def _overlap_tail(self, segs: list[_Segment]) -> list[_Segment]:
        target = self._cfg.overlap_tokens
        if target <= 0:
            return []
        tail: list[_Segment] = []
        total = 0
        for seg in reversed(segs):
            tail.insert(0, seg)
            total += self._tok.count(seg.text)
            if total >= target:
                break
        return tail


def _merge(segs: list[_Segment]) -> _Segment:
    text = " ".join(s.text for s in segs).strip()
    return _Segment(
        text=text,
        section_path=list(segs[0].section_path),
        char_start=segs[0].char_start,
        char_end=segs[-1].char_end,
        page_number=segs[0].page_number,
        panel_number=segs[0].panel_number,
    )


def augment_contextual(title: str, section_path: list[str], text: str) -> str:
    """Heading-prefix augmentation (arch flow step 5, handbook B.7).

    Deterministic and cheap — prepends the document title + section trail so the
    embedded/indexed text carries its referents. The raw ``text`` is untouched.
    """
    trail = " > ".join([t for t in [title, *section_path] if t]).strip()
    if not trail:
        return text
    return f"[{trail}]\n{text}"
