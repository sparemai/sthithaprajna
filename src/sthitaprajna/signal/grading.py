"""Uncertainty Engine (A9.5), Trade Quality Score (A9.7) and grade bands (D12.5).

* Effective confidence is capped by the weakest critical component -- never an average.
* TQS weights are PLACEHOLDER (equal) until Phase 5 calibrates them statistically. The UI shows the
  placeholder status; nothing here claims calibrated quality.
* Quality percentile is the candidate's rank within a rolling window of recent scores.
"""
from __future__ import annotations

import bisect
from collections import deque
from dataclasses import asdict, dataclass
from typing import Any

from sthitaprajna.config import OperatorConfig
from sthitaprajna.domain import Grade


@dataclass(frozen=True)
class Confidence:
    model: float
    data: float
    regime: float
    execution: float

    @property
    def effective(self) -> float:
        return min(self.model, self.data, self.regime, self.execution)

    @property
    def weakest(self) -> str:
        d = {"model": self.model, "data": self.data, "regime": self.regime, "execution": self.execution}
        return min(d, key=d.get)  # type: ignore[arg-type]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["effective"] = self.effective
        d["weakest"] = self.weakest
        return d


TQS_FEATURES = ("edge", "regime", "trend", "volume", "volatility", "breadth", "participant", "liquidity",
                "execution", "reward_risk", "event")
TQS_WEIGHTS_STATUS = "PLACEHOLDER (equal weights) -- calibrate in Phase 5"


def quality_score(features: dict[str, float]) -> float:
    """0-100. Each feature is expected in [0, 1]; missing features count as neutral 0.5."""
    vals = [min(1.0, max(0.0, float(features.get(f, 0.5)))) for f in TQS_FEATURES]
    return 100.0 * sum(vals) / len(vals)


class QualityDistribution:
    """Rolling distribution of recent quality scores -> percentile of a new score."""

    def __init__(self, maxlen: int = 500, min_samples: int = 30) -> None:
        self._dq: deque[float] = deque(maxlen=maxlen)
        self._sorted: list[float] = []
        self.min_samples = min_samples

    def add(self, score: float) -> None:
        if len(self._dq) == self._dq.maxlen:
            old = self._dq[0]
            idx = bisect.bisect_left(self._sorted, old)
            if idx < len(self._sorted):
                self._sorted.pop(idx)
        self._dq.append(score)
        bisect.insort(self._sorted, score)

    def percentile(self, score: float) -> float:
        n = len(self._sorted)
        if n < self.min_samples:
            # prior until enough history: map score linearly, conservatively shrunk toward the middle
            return 0.5 + (score / 100.0 - 0.5) * 0.8
        return bisect.bisect_right(self._sorted, score) / n

    def __len__(self) -> int:
        return len(self._sorted)


def grade(cfg: OperatorConfig, *, net_ev_r: float, margin_ok: bool, quality_pct: float,
          effective_confidence: float) -> Grade:
    if not margin_ok:
        return Grade.C
    ap = cfg.grades[Grade.A_PLUS]
    if (quality_pct >= ap.min_quality_pct and net_ev_r >= ap.min_ev_r
            and effective_confidence >= ap.min_confidence):
        return Grade.A_PLUS
    a = cfg.grades[Grade.A]
    if quality_pct >= a.min_quality_pct and net_ev_r >= a.min_ev_r and effective_confidence >= a.min_confidence:
        return Grade.A
    b = cfg.grades[Grade.B]
    if net_ev_r >= b.min_ev_r:
        return Grade.B
    return Grade.C
