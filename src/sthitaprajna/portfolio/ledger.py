"""Capital ledger (D1): equity = starting capital + realised net P&L after charges.

* equity_sod (previous close) is the sizing basis -- never recalculated intraday.
* MTM equity (realised + unrealised) drives drawdown, the gear ladder and the ruin floor (D-007).
* The all-time peak is measured on MTM and never resets (ruin floor, D12.3).
* Deposits/withdrawals are capital flows, never profit.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Ledger:
    starting: float
    realized_net: float = 0.0
    flows: float = 0.0
    equity_sod: float = 0.0
    all_time_peak: float = 0.0
    day_start: float = 0.0
    week_start: float = 0.0
    month_start: float = 0.0
    day_realized: float = 0.0
    day_peak_pnl: float = 0.0
    trades_today: int = 0
    losing_streak: int = 0
    cooldown_until: dt.datetime | None = None
    r_outcomes: list[float] = field(default_factory=list)      # net R of closed trades (edge brake)
    closed_trades: list[dict[str, Any]] = field(default_factory=list)
    equity_curve: list[dict[str, Any]] = field(default_factory=list)
    daily: list[dict[str, Any]] = field(default_factory=list)
    week_key: str = ""
    month_key: str = ""
    daily_loss_breached: bool = False

    def __post_init__(self) -> None:
        eq = self.equity
        self.equity_sod = self.equity_sod or eq
        self.all_time_peak = self.all_time_peak or eq
        self.day_start = self.day_start or eq
        self.week_start = self.week_start or eq
        self.month_start = self.month_start or eq

    @property
    def equity(self) -> float:
        return self.starting + self.flows + self.realized_net

    def mtm(self, unrealized: float, open_charges: float = 0.0) -> float:
        return self.equity + unrealized - open_charges

    def mark(self, unrealized: float, open_charges: float = 0.0) -> float:
        eq = self.mtm(unrealized, open_charges)
        self.all_time_peak = max(self.all_time_peak, eq)
        pnl = eq - self.day_start
        self.day_peak_pnl = max(self.day_peak_pnl, pnl)
        return eq

    def period_pnl(self, unrealized: float, open_charges: float = 0.0) -> dict[str, float]:
        eq = self.mtm(unrealized, open_charges)
        return {"day": eq - self.day_start, "week": eq - self.week_start, "month": eq - self.month_start}

    def restore_from_history(self, trades: list[dict[str, Any]]) -> int:
        """D-047: replay every closed trade from a prior session (oldest first, from the store) to rebuild
        realized P&L and the all-time peak, instead of resetting equity to `starting` on every process
        restart -- PAPER/SIM equity previously had no continuity across a restart at all. Only realized
        P&L carries forward this way: any position that was still open at the moment of the previous
        shutdown is not restored and is not reflected in the result. Call before __post_init__-dependent
        state (day/week/month starts) is used, and before anything reads `equity` or `all_time_peak`.
        Returns the number of trades replayed, for a one-line log message at startup."""
        eq = self.starting
        peak = eq
        for t in trades:
            eq += float(t.get("net_pnl", 0.0))
            peak = max(peak, eq)
        self.realized_net = eq - self.starting
        self.all_time_peak = peak
        # __post_init__ already ran at construction time (before this restore) and seeded these from the
        # pre-restore equity; without resetting them too, equity_sod (the sizing basis, D1) would stay at
        # `starting` even though `equity` now correctly reflects the restored realized P&L.
        self.equity_sod = eq
        self.day_start = eq
        self.week_start = eq
        self.month_start = eq
        return len(trades)

    def record_close(self, net: float, r: float, trade: dict[str, Any], now: dt.datetime,
                     cooldown_losses: int, cooldown_minutes: int) -> None:
        self.realized_net += net
        self.r_outcomes.append(r)
        self.closed_trades.append(trade)
        if net < 0:
            self.losing_streak += 1
            if self.losing_streak == cooldown_losses:
                self.cooldown_until = now + dt.timedelta(minutes=cooldown_minutes)
        else:
            self.losing_streak = 0

    def add_realized_charges(self, amount: float) -> None:
        """Charges on partial exits/adds are booked when the whole position closes (net P&L)."""

    def start_day(self, day: dt.date) -> None:
        eq = self.equity
        self.equity_sod = eq
        self.day_start = eq
        self.day_peak_pnl = 0.0
        self.trades_today = 0
        self.losing_streak = 0
        self.cooldown_until = None
        self.daily_loss_breached = False
        wk = f"{day.isocalendar().year}-W{day.isocalendar().week}"
        mo = f"{day.year}-{day.month:02d}"
        if wk != self.week_key:
            self.week_key, self.week_start = wk, eq
        if mo != self.month_key:
            self.month_key, self.month_start = mo, eq

    def end_day(self, day: dt.date, gear: str, process_score: float, trades: int) -> dict[str, Any]:
        eq = self.equity
        row = {"date": day.isoformat(), "equity": round(eq, 2), "pnl": round(eq - self.day_start, 2),
               "pnl_pct": (eq - self.day_start) / self.day_start if self.day_start else 0.0, "gear": gear,
               "process_score": process_score, "trades": trades,
               "drawdown_from_peak": 1 - eq / self.all_time_peak if self.all_time_peak else 0.0}
        self.daily.append(row)
        return row

    def snapshot(self, now: dt.datetime, mtm: float, drawdown: float, gear: str) -> None:
        self.equity_curve.append({"t": int(now.timestamp()), "equity": round(mtm, 2), "dd": round(drawdown, 5),
                                  "gear": gear})
        if len(self.equity_curve) > 20000:
            self.equity_curve = self.equity_curve[-20000:]
