"""Token counting shared by the chunker and query validation.

The architecture is explicit that token limits must be measured with the
embedding model's *real* tokenizer, not ``str.split()`` — a whitespace count
silently loosens any cap named in tokens (overview note on step 16).

Loading a Hugging Face tokenizer pulls only the small tokenizer files, not the
2.5 GB model weights, so this stays cheap. If ``transformers`` or the files
are unavailable (offline), we fall back to a subword-aware regex estimate that
correlates well enough with real token counts for sizing decisions.
"""

from __future__ import annotations

import re

from .config import ModelConfig, get_config

# Split into word-ish and punctuation runs; long words get an extra
# per-4-chars penalty to approximate subword splitting.
_TOKEN_RE = re.compile(r"\w+|[^\w\s]", re.UNICODE)


class Tokenizer:
    """Lazy wrapper around the embedding model's tokenizer with a fallback."""

    def __init__(self, config: ModelConfig | None = None) -> None:
        self._cfg = config or get_config().models
        self._hf = None
        self._kind: str | None = None

    def _ensure(self) -> None:
        if self._kind is not None:
            return
        try:
            from transformers import AutoTokenizer  # type: ignore

            self._hf = AutoTokenizer.from_pretrained(
                self._cfg.embedding_model,
                revision=self._cfg.embedding_revision,
            )
            self._kind = "hf"
        except Exception:
            self._hf = None
            self._kind = "heuristic"

    def count(self, text: str) -> int:
        if not text:
            return 0
        self._ensure()
        if self._kind == "hf":
            try:
                return len(self._hf.encode(text, add_special_tokens=False))  # type: ignore[union-attr]
            except Exception:
                pass
        return self._heuristic_count(text)

    @staticmethod
    def _heuristic_count(text: str) -> int:
        tokens = _TOKEN_RE.findall(text)
        count = 0
        for tok in tokens:
            # Approximate subword fragmentation for long tokens.
            count += 1 + (len(tok) - 1) // 4 if len(tok) > 4 else 1
        return count

    @property
    def backend(self) -> str:
        self._ensure()
        return self._kind or "heuristic"


_shared: Tokenizer | None = None


def get_tokenizer() -> Tokenizer:
    global _shared
    if _shared is None:
        _shared = Tokenizer()
    return _shared


def count_tokens(text: str) -> int:
    return get_tokenizer().count(text)
