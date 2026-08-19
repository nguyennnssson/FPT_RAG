"""Language detection for Vietnamese vs. English.

Used on both paths (arch flow steps 6 and 18):
- Ingestion tags each chunk's language as metadata (citation display + routing).
- Query time detects the question's language, confidence-gated, to choose the
  response language — with a default-language fallback for short/ambiguous input.

Detection uses ``langdetect`` when installed (lazy import), and otherwise falls
back to a dependency-free Vietnamese-diacritic + stopword heuristic so the
package works out of the box on this box. Both return a calibrated confidence
so the same confidence gate applies regardless of backend.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .config import LanguageConfig, get_config

# Vietnamese-specific characters (diacritics + đ). Their presence is a very
# strong signal for VI even in a short string.
_VI_CHARS = set(
    "ăâêôơưđ"
    "áàảãạ ắằẳẵặ ấầẩẫậ éèẻẽẹ ếềểễệ íìỉĩị óòỏõọ ốồổỗộ ớờởỡợ úùủũụ ứừửữự ýỳỷỹỵ".replace(" ", "")
)
_VI_STOPWORDS = {
    "và", "là", "của", "có", "cho", "không", "được", "trong", "một", "này",
    "với", "các", "để", "những", "người", "khi", "đã", "về", "như", "thì",
}
_EN_STOPWORDS = {
    "the", "is", "are", "of", "and", "to", "in", "a", "for", "that", "on",
    "with", "as", "this", "it", "be", "by", "an", "or", "was", "what", "how",
}

_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


@dataclass(frozen=True)
class LanguageResult:
    language: str          # "en" | "vi" (or config default if unknown)
    confidence: float      # 0.0 - 1.0

    def gated(self, gate: float, default_language: str) -> str:
        """Return the detected language only if we're confident enough,
        otherwise fall back to the default (query-side routing rule)."""
        return self.language if self.confidence >= gate else default_language


class LanguageDetector:
    """Detects VI/EN. Backend is chosen lazily and cached."""

    def __init__(self, config: LanguageConfig | None = None) -> None:
        self._cfg = config or get_config().language
        self._backend = None            # None until first use
        self._backend_kind: str | None = None

    # -- backend selection --------------------------------------------------- #
    def _ensure_backend(self) -> None:
        if self._backend_kind is not None:
            return
        try:
            from langdetect import DetectorFactory, detect_langs  # type: ignore

            DetectorFactory.seed = 0    # deterministic
            self._backend = detect_langs
            self._backend_kind = "langdetect"
        except Exception:
            self._backend = None
            self._backend_kind = "heuristic"

    # -- public API ---------------------------------------------------------- #
    def detect(self, text: str) -> LanguageResult:
        text = (text or "").strip()
        if not text:
            return LanguageResult(self._cfg.default_language, 0.0)

        # The VI-diacritic signal is decisive and cheap; check it first so a
        # single accented word isn't misread as English by a statistical model.
        heuristic = self._heuristic(text)
        if heuristic.language == "vi" and heuristic.confidence >= 0.9:
            return heuristic

        self._ensure_backend()
        if self._backend_kind == "langdetect":
            try:
                langs = self._backend(text)          # type: ignore[misc]
                best = langs[0]
                lang = "vi" if best.lang == "vi" else ("en" if best.lang == "en" else best.lang)
                if lang not in self._cfg.supported:
                    # Unsupported language detected — keep the confidence but
                    # map onto the default so downstream stays bilingual.
                    return LanguageResult(self._cfg.default_language, float(best.prob) * 0.5)
                return LanguageResult(lang, float(best.prob))
            except Exception:
                pass

        return heuristic

    def detect_language(self, text: str) -> str:
        """Just the label, applying the config's confidence gate + fallback."""
        result = self.detect(text)
        return result.gated(self._cfg.confidence_gate, self._cfg.default_language)

    # -- dependency-free fallback ------------------------------------------- #
    def _heuristic(self, text: str) -> LanguageResult:
        lowered = text.lower()
        vi_char_hits = sum(1 for ch in lowered if ch in _VI_CHARS)

        words = _WORD_RE.findall(lowered)
        if not words:
            # No alphabetic content (e.g. an error code) — undecidable.
            return LanguageResult(self._cfg.default_language, 0.0)

        vi_sw = sum(1 for w in words if w in _VI_STOPWORDS)
        en_sw = sum(1 for w in words if w in _EN_STOPWORDS)

        # Any Vietnamese diacritic is a near-certain VI signal.
        if vi_char_hits > 0:
            confidence = min(1.0, 0.9 + vi_char_hits / max(len(lowered), 1))
            return LanguageResult("vi", confidence)

        total_sw = vi_sw + en_sw
        if total_sw == 0:
            # No stopword evidence either way; weak default guess.
            return LanguageResult(self._cfg.default_language, 0.3)

        if vi_sw > en_sw:
            return LanguageResult("vi", 0.5 + 0.5 * vi_sw / total_sw)
        return LanguageResult("en", 0.5 + 0.5 * en_sw / total_sw)


_shared: LanguageDetector | None = None


def get_detector() -> LanguageDetector:
    global _shared
    if _shared is None:
        _shared = LanguageDetector()
    return _shared


def detect_language(text: str) -> str:
    """Module-level convenience: gated label using the default config."""
    return get_detector().detect_language(text)
