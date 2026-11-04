"""Gear Controller (AGG-1 component 1; D12.3 ladder rules).

* Downshift: immediate, the moment drawdown from the ladder peak crosses a threshold. No approval,
  no delay, no override. There is deliberately no method that raises the gear except `on_day_close`
  (the recovery rule) and `resume_after_halt` (operator review -> restarts in GUARD).
* Upshift: one gear at a time, only after drawdown has improved past the threshold AND
  `upshift_clean_days` consecutive trading days without a daily-loss breach since the last gear change.
* Never driven by targets, being behind target, or a hot streak -- nothing here reads P&L streaks.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from sthitaprajna.config import OperatorConfig
from sthitaprajna.domain import GEAR_ORDER, Gear
from sthitaprajna.risk import ceilings


@dataclass
class GearEvent:
    ts: dt.datetime
    frm: Gear
    to: Gear
    reason: str
    drawdown: float

    def to_dict(self) -> dict[str, Any]:
        return {"ts": self.ts.isoformat(), "from": self.frm.value, "to": self.to.value, "reason": self.reason,
                "drawdown": round(self.drawdown, 5)}


@dataclass
class GearController:
    cfg: OperatorConfig
    ref_peak: float
    gear: Gear = Gear.ATTACK
    clean_days: int = 0
    halted_review_pending: bool = False
    day_drop: bool = False          # pre-market state check answered "no" (A15) -- today only
    evidence_drop: bool = False     # calibration out of band / divergence / change point (D12.4)
    expectancy_cap: bool = False    # edge brake: expectancy <= 0 after 100 trades -> cap at GUARD
    events: list[GearEvent] = field(default_factory=list)
    time_in_gear: dict[str, float] = field(default_factory=lambda: {g.value: 0.0 for g in GEAR_ORDER})

    # ------------------------------------------------------------------ ladder
    def ladder_gear(self, dd: float) -> Gear:
        if dd >= ceilings.HALT_DRAWDOWN:
            return Gear.HALTED
        for g in (Gear.ATTACK, Gear.PRESS, Gear.GUARD, Gear.PRESERVE):
            if dd < self.cfg.gears[g].dd_below:
                return g
        return Gear.HALTED

    def drawdown(self, equity: float) -> float:
        if self.ref_peak <= 0:
            return 1.0
        # round away float noise so an exact 10% drawdown (90,000 / 100,000) counts as 10%, not 9.999..%
        return max(0.0, round(1.0 - equity / self.ref_peak, 9))

    def distance_to_next_downshift(self, equity: float) -> float | None:
        """Fraction of the ladder peak still available before the next downshift."""
        if self.gear is Gear.HALTED:
            return None
        return max(0.0, self.cfg.gears[self.gear].dd_below - self.drawdown(equity))

    # ------------------------------------------------------------------ transitions
    def on_equity(self, equity: float, ts: dt.datetime) -> GearEvent | None:
        """Call on every mark-to-market update. Can only move the gear DOWN."""
        self.ref_peak = max(self.ref_peak, equity)
        dd = self.drawdown(equity)
        target = self.ladder_gear(dd)
        if target.rank > self.gear.rank:
            ev = GearEvent(ts, self.gear, target, f"downshift: drawdown {dd:.2%}", dd)
            self.gear = target
            self.clean_days = 0
            if target is Gear.HALTED:
                self.halted_review_pending = True
            self.events.append(ev)
            return ev
        return None

    def on_day_close(self, equity: float, daily_loss_breached: bool, ts: dt.datetime,
                     day_pnl: float = 0.0) -> GearEvent | None:
        """End-of-day recovery rule: the ONLY automatic path to a higher gear.

        Decision D-004 (docs/decisions.md): an upshift is never taken at the close of a losing day,
        even a non-breaching one, so risk can never rise immediately after a loss (A10.7 / Part C).
        """
        self.day_drop = False
        self.clean_days = 0 if daily_loss_breached else self.clean_days + 1
        if self.gear is Gear.HALTED:
            return None  # only operator review can leave HALTED
        if day_pnl < 0:
            return None
        dd = self.drawdown(equity)
        target = self.ladder_gear(dd)
        if target.rank < self.gear.rank and self.clean_days >= self.cfg.upshift_clean_days:
            new = self.gear.up()
            ev = GearEvent(ts, self.gear, new, f"upshift: dd {dd:.2%}, {self.clean_days} clean days", dd)
            self.gear = new
            self.clean_days = 0
            self.events.append(ev)
            return ev
        return None

    def resume_after_halt(self, equity: float, ts: dt.datetime, reviewed: bool) -> GearEvent:
        """Operator review required. Ladder reference resets to current equity; restart in GUARD.
        The ruin floor (tracked in the ledger from the all-time peak) never resets."""
        if self.gear is not Gear.HALTED:
            raise ValueError("resume_after_halt is only valid from HALTED")
        if not reviewed:
            raise PermissionError("operator review is required before resuming from HALTED")
        self.ref_peak = equity
        ev = GearEvent(ts, Gear.HALTED, Gear.GUARD, "operator review: resume in GUARD", 0.0)
        self.gear = Gear.GUARD
        self.clean_days = 0
        self.halted_review_pending = False
        self.events.append(ev)
        return ev

    def apply_state_check(self, slept_well: bool, calm: bool, undistracted: bool) -> None:
        """A15: any 'no' -> one gear lower for the day. It can never raise one."""
        if not (slept_well and calm and undistracted):
            self.day_drop = True

    def effective_gear(self) -> Gear:
        g = self.gear
        if g is Gear.HALTED:
            return g
        if self.day_drop:
            g = g.down()
        if self.evidence_drop:
            g = g.down()
        if self.expectancy_cap:
            g = Gear.most_defensive(g, Gear.GUARD)
        return g

    def accumulate(self, seconds: float) -> None:
        self.time_in_gear[self.effective_gear().value] += seconds

    def to_dict(self, equity: float) -> dict[str, Any]:
        eff = self.effective_gear()
        return {
            "gear": self.gear.value, "effective": eff.value, "ref_peak": round(self.ref_peak, 2),
            "drawdown": round(self.drawdown(equity), 5), "clean_days": self.clean_days,
            "upshift_needs": self.cfg.upshift_clean_days, "halted_review_pending": self.halted_review_pending,
            "day_drop": self.day_drop, "evidence_drop": self.evidence_drop, "expectancy_cap": self.expectancy_cap,
            "next_downshift_in": self.distance_to_next_downshift(equity),
            "ladder": [{"gear": g.value, "dd_below": self.cfg.gears[g].dd_below, "r": self.cfg.gears[g].r,
                        "open_risk": self.cfg.gears[g].open_risk} for g in GEAR_ORDER],
            "events": [e.to_dict() for e in self.events[-20:]],
            "time_in_gear": {k: round(v, 1) for k, v in self.time_in_gear.items()},
        }
