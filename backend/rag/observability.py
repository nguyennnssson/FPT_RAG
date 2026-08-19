"""Observability — latency SLIs and error/decision rates (arch flow step 32).

You cannot fix degradation you cannot see. This is a lightweight, in-process
metrics collector: per-request decision counts, latency percentiles, and derived
rates (abstention, cache-hit, error, citation-valid). No external deps; the API
exposes a snapshot at GET /metrics.

For a multi-process deployment, point the API at a real metrics backend
(Prometheus/StatsD) — this collector is per-process and resets on restart, which
is fine for a single-worker service and for local/demo use.
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Any

_DECISIONS = ("answered", "abstained", "rejected", "cache_hit", "error", "aborted")


def _percentile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * pct
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = k - lo
    return round(sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac, 2)


class MetricsCollector:
    def __init__(self, latency_window: int = 1000) -> None:
        self._lock = threading.Lock()
        self._decisions: dict[str, int] = {d: 0 for d in _DECISIONS}
        self._languages: dict[str, int] = {}
        self._latencies: deque[float] = deque(maxlen=latency_window)
        self._citation_valid = 0
        self._citation_checked = 0
        self._total = 0

    def record(
        self,
        decision: str,
        latency_ms: float | None = None,
        language: str | None = None,
        *,
        citation_valid: bool | None = None,
    ) -> None:
        with self._lock:
            self._total += 1
            self._decisions[decision] = self._decisions.get(decision, 0) + 1
            if language:
                self._languages[language] = self._languages.get(language, 0) + 1
            if latency_ms is not None:
                self._latencies.append(latency_ms)
            if citation_valid is not None:
                self._citation_checked += 1
                if citation_valid:
                    self._citation_valid += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            lat = sorted(self._latencies)
            total = self._total or 1
            answered = self._decisions.get("answered", 0)
            return {
                "total_requests": self._total,
                "decisions": dict(self._decisions),
                "languages": dict(self._languages),
                "latency_ms": {
                    "p50": _percentile(lat, 0.50),
                    "p95": _percentile(lat, 0.95),
                    "p99": _percentile(lat, 0.99),
                    "max": round(max(lat), 2) if lat else 0.0,
                    "count": len(lat),
                },
                "rates": {
                    "abstention": round(self._decisions.get("abstained", 0) / total, 4),
                    "cache_hit": round(self._decisions.get("cache_hit", 0) / total, 4),
                    "error": round(self._decisions.get("error", 0) / total, 4),
                    "citation_valid": round(
                        self._citation_valid / self._citation_checked, 4
                    ) if self._citation_checked else 1.0,
                },
            }

    def reset(self) -> None:
        with self._lock:
            self.__init__(self._latencies.maxlen or 1000)


_shared: MetricsCollector | None = None


def get_metrics() -> MetricsCollector:
    global _shared
    if _shared is None:
        _shared = MetricsCollector()
    return _shared
