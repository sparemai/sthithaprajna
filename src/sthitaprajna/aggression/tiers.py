"""Capital tiers (D3) as explicit state.

* Promotion is NEVER automatic: equity above the next tier's floor for 20 consecutive trading days
  + >= 100 live trades with positive expectancy + no limit breach in 20 days + operator approval.
* Demotion is automatic when equity closes below 90% of the current tier's floor.
* Decision D-017: a demotion that happens on a losing day keeps the higher tier's (smaller) ATTACK R
  until the next non-losing close, because D3's lower-tier R% is larger and would otherwise raise risk
  right after a loss (A10.7 / Part C).
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from sthitaprajna.config import OperatorConfig, TierSpec


@dataclass
class TierState:
    cfg: OperatorConfig
    index: int = 0
    attack_r_cap: float | None = None
    days_above_next: int = 0
    breach_free_days: int = 0
    events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def tier(self) -> TierSpec:
        return self.cfg.tiers[self.index]

    def effective(self) -> TierSpec:
        t = self.tier
        if self.attack_r_cap is not None and self.attack_r_cap < t.attack_r:
            return replace(t, attack_r=self.attack_r_cap)
        return t

    def on_day_close(self, equity: float, day_pnl: float, limit_breached: bool) -> dict[str, Any] | None:
        self.breach_free_days = 0 if limit_breached else self.breach_free_days + 1
        nxt = self.cfg.tiers[self.index + 1] if self.index + 1 < len(self.cfg.tiers) else None
        self.days_above_next = self.days_above_next + 1 if nxt and equity >= nxt.min_equity else 0
        ev = None
        if self.index > 0 and equity < 0.9 * self.tier.min_equity:
            old = self.effective()
            while self.index > 0 and equity < 0.9 * self.cfg.tiers[self.index].min_equity:
                self.index -= 1
            ev = {"type": "demotion", "from": old.name, "to": self.tier.name, "equity": round(equity, 2)}
            if day_pnl < 0 and self.tier.attack_r > old.attack_r:
                self.attack_r_cap = old.attack_r
                ev["note"] = f"losing day: ATTACK R held at {old.attack_r:.2%} until a non-losing close"
            self.days_above_next = 0
        elif day_pnl >= 0:
            self.attack_r_cap = None
        if ev:
            self.events.append(ev)
        return ev

    def promotion_check(self, live_trades: int, live_expectancy_r: float | None) -> dict[str, Any]:
        nxt = self.cfg.tiers[self.index + 1] if self.index + 1 < len(self.cfg.tiers) else None
        reasons = []
        if nxt is None:
            reasons.append("already at the top tier")
        if self.days_above_next < 20:
            reasons.append(f"equity above next floor {self.days_above_next}/20 consecutive days")
        if live_trades < 100:
            reasons.append(f"{live_trades}/100 live trades")
        if not live_expectancy_r or live_expectancy_r <= 0:
            reasons.append("live expectancy not positive")
        if self.breach_free_days < 20:
            reasons.append(f"{self.breach_free_days}/20 days without a limit breach")
        return {"eligible": not reasons, "reasons": reasons, "next": nxt.name if nxt else None}

    def promote(self, approved_by_operator: bool, live_trades: int, live_expectancy_r: float | None) -> dict[str, Any]:
        chk = self.promotion_check(live_trades, live_expectancy_r)
        if not approved_by_operator:
            return {"ok": False, "error": "operator approval required (D9)"}
        if not chk["eligible"]:
            return {"ok": False, "error": "; ".join(chk["reasons"])}
        self.index += 1
        self.attack_r_cap = None
        self.days_above_next = 0
        ev = {"type": "promotion", "to": self.tier.name}
        self.events.append(ev)
        return {"ok": True, **ev}

    def to_dict(self) -> dict[str, Any]:
        e = self.effective()
        return {"tier": self.tier.name, "attack_r": e.attack_r, "attack_r_cap": self.attack_r_cap,
                "max_positions": e.max_positions, "max_trades_per_day": e.max_trades_per_day,
                "days_above_next": self.days_above_next, "breach_free_days": self.breach_free_days,
                "events": self.events[-10:]}
