"""Confidence calibration tracking (A9.4): "a 70% confidence must win about 70% of the time".

Tracks predicted P(win) vs outcome in bins; reports expected calibration error (ECE). A calibration
map is only offered once there is enough data; until then probabilities are labelled UNCALIBRATED.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class CalibrationTracker:
    bins: int = 10
    min_samples: int = 200
    ece_band: float = 0.08
    records: list[tuple[float, int]] = field(default_factory=list)

    def add(self, p: float, won: bool) -> None:
        self.records.append((min(max(p, 0.0), 1.0), 1 if won else 0))

    def table(self) -> list[dict[str, Any]]:
        out = []
        for i in range(self.bins):
            lo, hi = i / self.bins, (i + 1) / self.bins
            sel = [(p, y) for p, y in self.records if lo <= p < hi or (i == self.bins - 1 and p == 1.0)]
            if sel:
                out.append({"lo": lo, "hi": hi, "n": len(sel), "predicted": sum(p for p, _ in sel) / len(sel),
                            "observed": sum(y for _, y in sel) / len(sel)})
        return out

    def ece(self) -> float | None:
        n = len(self.records)
        if n == 0:
            return None
        return sum(row["n"] / n * abs(row["predicted"] - row["observed"]) for row in self.table())

    @property
    def calibrated(self) -> bool:
        e = self.ece()
        return len(self.records) >= self.min_samples and e is not None and e <= self.ece_band

    @property
    def out_of_band(self) -> bool:
        """Only meaningful with enough samples; triggers a one-gear drop (D12.4)."""
        e = self.ece()
        return len(self.records) >= self.min_samples and e is not None and e > self.ece_band

    def to_dict(self) -> dict[str, Any]:
        e = self.ece()
        return {"n": len(self.records), "ece": round(e, 4) if e is not None else None,
                "calibrated": self.calibrated, "out_of_band": self.out_of_band, "table": self.table(),
                "status": "CALIBRATED" if self.calibrated else "UNCALIBRATED"}
