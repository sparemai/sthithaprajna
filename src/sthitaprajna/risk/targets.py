"""Dynamic targets, Ratchet Mode and /lock (D5).

The most important rule in the system: BEHIND TARGET -> NOTHING CHANGES. A target never changes gear,
size, thresholds or trade frequency. Once hit, it becomes a level to PROTECT:
* profit floor = 50% of the period's peak P&L (rises with P&L); P&L back at the floor -> no new
  entries for that period;
* only A+ / A grades trade (B probes off); open positions trail to lock >= 50% of open profit.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

PERIODS = ("day", "week", "month")


@dataclass
class Target:
    period: str
    value: float
    unit: str              # "RUPEES" | "PCT"
    set_at: dt.datetime

    def rupees(self, base_equity: float) -> float:
        return self.value if self.unit == "RUPEES" else self.value / 100.0 * base_equity


@dataclass
class Ratchet:
    active: bool = False
    peak_pnl: float = 0.0
    floor: float | None = None
    floor_hit: bool = False


@dataclass
class TargetBook:
    floor_fraction: float = 0.5
    targets: dict[str, Target] = field(default_factory=dict)
    ratchet: dict[str, Ratchet] = field(default_factory=lambda: {p: Ratchet() for p in PERIODS})
    locks: set[str] = field(default_factory=set)

    def set_target(self, period: str, value: float, unit: str, now: dt.datetime) -> Target:
        if period not in PERIODS:
            raise ValueError(f"period must be one of {PERIODS}")
        if value <= 0:
            raise ValueError("target must be positive")
        t = Target(period, float(value), unit.upper(), now)
        self.targets[period] = t          # replaces the old target of the same period
        return t

    def lock(self, period: str) -> None:
        if period not in PERIODS:
            raise ValueError(f"period must be one of {PERIODS}")
        self.locks.add(period)

    def reset_period(self, period: str) -> None:
        self.ratchet[period] = Ratchet()
        self.locks.discard(period)

    def update(self, pnl: dict[str, float], base_equity: dict[str, float]) -> None:
        for period, target in self.targets.items():
            r = self.ratchet[period]
            p = pnl.get(period, 0.0)
            goal = target.rupees(base_equity.get(period, 0.0))
            if not r.active and goal > 0 and p >= goal:
                r.active = True
            if r.active:
                r.peak_pnl = max(r.peak_pnl, p)
                r.floor = self.floor_fraction * r.peak_pnl
                if p <= r.floor:
                    r.floor_hit = True

    # --------------------------------------------------------------- queries used by the guardian
    def entry_blocks(self) -> list[str]:
        out = [f"/lock {p}: no new entries this {p}" for p in PERIODS if p in self.locks]
        out += [f"ratchet floor hit ({p}): no new entries this {p}" for p in PERIODS if self.ratchet[p].floor_hit]
        return out

    @property
    def b_grade_allowed(self) -> bool:
        return not any(self.ratchet[p].active for p in PERIODS)

    @property
    def trail_required(self) -> bool:
        return any(self.ratchet[p].active for p in PERIODS)

    def to_dict(self, pnl: dict[str, float], base_equity: dict[str, float]) -> dict[str, Any]:
        out = {}
        for p in PERIODS:
            t = self.targets.get(p)
            r = self.ratchet[p]
            goal = t.rupees(base_equity.get(p, 0.0)) if t else None
            out[p] = {
                "target": {"value": t.value, "unit": t.unit, "rupees": round(goal, 2)} if t else None,
                "pnl": round(pnl.get(p, 0.0), 2),
                "progress": (pnl.get(p, 0.0) / goal) if goal else None,
                "ratchet": {"active": r.active, "peak": round(r.peak_pnl, 2),
                            "floor": round(r.floor, 2) if r.floor is not None else None, "floor_hit": r.floor_hit},
                "locked": p in self.locks,
            }
        return out
