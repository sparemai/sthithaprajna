"""Aggression KPIs (AGG-1, A19): measured, reported next to risk metrics, never used to raise risk."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from sthitaprajna.domain import Grade, PositionStage
from sthitaprajna.portfolio.position import Position


@dataclass
class AggressionKPIs:
    idle_seconds: float = 0.0
    session_seconds: float = 0.0
    missed_by_stage: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    missed_detail: list[dict[str, Any]] = field(default_factory=list)
    closed: list[dict[str, Any]] = field(default_factory=list)
    probes_full_staging: int = 0
    probes_confirmed: int = 0

    def on_step(self, seconds: float, open_risk: float, limit: float, queued_a: int) -> None:
        self.session_seconds += seconds
        if queued_a > 0 and open_risk < limit * 0.999:
            self.idle_seconds += seconds

    def on_missed(self, grade: Grade | None, stage: str, reason: str, symbol: str) -> None:
        if grade in (Grade.A_PLUS, Grade.A):
            self.missed_by_stage[stage] += 1
            self.missed_detail.append({"grade": grade.value, "stage": stage, "reason": reason, "symbol": symbol})
            self.missed_detail = self.missed_detail[-50:]

    def on_close(self, p: Position) -> None:
        reached = p.stage.value
        stages = {h.get("to") for h in p.stage_history}
        if p.staging_mode == "full":
            self.probes_full_staging += 1
            if PositionStage.CONFIRM_ADD.value in stages:
                self.probes_confirmed += 1
        mfe_rupees = abs(p.mfe_price - p.avg_entry) * max(p.opened_qty, 1) if p.mfe_price else 0.0
        fill_lag = ((p.legs[0].ts - p.trigger_ts).total_seconds() if p.trigger_ts and p.legs else None)
        self.closed.append({
            "id": p.id, "grade": p.grade.value, "r": p.r_multiple(), "net": p.net_pnl(),
            "pressed": p.presses > 0, "harvested": p.harvested, "stage_reached": reached,
            "stages": sorted(s for s in stages if s), "mfe_rupees": mfe_rupees,
            "gross": p.realized, "fill_lag_s": fill_lag, "strategy": p.strategy,
        })

    def to_dict(self) -> dict[str, Any]:
        wins = [c for c in self.closed if c["r"] > 0]
        losses = [c for c in self.closed if c["r"] <= 0]
        cap = [min(1.0, c["gross"] / c["mfe_rupees"]) for c in wins if c["mfe_rupees"] > 0]
        by_grade: dict[str, list[float]] = defaultdict(list)
        by_stage: dict[str, list[float]] = defaultdict(list)
        for c in self.closed:
            by_grade[c["grade"]].append(c["r"])
            for s in c["stages"] or [c["stage_reached"]]:
                by_stage[s].append(c["r"])
        lags = [c["fill_lag_s"] for c in self.closed if c["fill_lag_s"] is not None]
        mean = lambda xs: sum(xs) / len(xs) if xs else None  # noqa: E731
        return {
            "idle_risk_budget_pct": (self.idle_seconds / self.session_seconds) if self.session_seconds else 0.0,
            "idle_seconds": round(self.idle_seconds, 1),
            "missed_a_by_stage": dict(self.missed_by_stage), "missed_recent": self.missed_detail[-10:],
            "capture_ratio": mean(cap), "avg_winner_r": mean([c["r"] for c in wins]),
            "avg_loser_r": mean([c["r"] for c in losses]),
            "pct_winners_pressed": (sum(c["pressed"] for c in wins) / len(wins)) if wins else None,
            "pct_winners_harvested": (sum(c["harvested"] for c in wins) / len(wins)) if wins else None,
            "trigger_to_fill_s": mean(lags),
            "probe_success_rate": (self.probes_confirmed / self.probes_full_staging) if self.probes_full_staging else None,
            "expectancy_by_grade": {g: {"n": len(v), "r": mean(v)} for g, v in by_grade.items()},
            "expectancy_by_stage": {s: {"n": len(v), "r": mean(v)} for s, v in by_stage.items()},
            "trades": len(self.closed),
        }
