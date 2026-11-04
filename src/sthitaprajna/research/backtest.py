"""Backtest runner: drives the SAME Engine (same strategies, funnel, Aggression Layer, Risk Guardian,
staging and paper broker) in deterministic fast-forward -- no separate "backtest version" of anything.

On the built-in simulator every result is labelled SIMULATED (D-006): it validates plumbing, costs and
risk mechanics, never edge. Real-edge research (Phase 4) needs historical Dhan/Upstox data behind the
same adapters, walk-forward splits and the A12 red-team -- listed as open work in docs/progress.md.
"""
from __future__ import annotations

import copy
import datetime as dt
from typing import Any

from sthitaprajna.config import MarketRules, OperatorConfig, operator_from_dict
from sthitaprajna.engine.runtime import Engine
from sthitaprajna.market.calendar import MarketCalendar
from sthitaprajna.research.metrics import report


def single_entry_variant(cfg: OperatorConfig) -> OperatorConfig:
    """Research variant: the probe IS the full position (no confirm-add). Never used live."""
    raw = copy.deepcopy(cfg.raw)
    raw["staging"]["probe_fraction"] = 1.0
    return operator_from_dict(raw)


def run(cfg: OperatorConfig, rules: MarketRules, cal: MarketCalendar, *, days: int = 5, seed: int = 7,
        start: dt.date | None = None, step_seconds: float = 30.0, staging: bool = True) -> dict[str, Any]:
    c = cfg if staging else single_entry_variant(cfg)
    start = start or dt.date(2026, 9, 28)
    e = Engine(c, rules, cal, seed=seed, start=start, warmup_days=2)
    end_day = e.market.day
    for _ in range(max(1, days) - 1):
        end_day = cal.next_trading_day(end_day)
    e.run_until(cal.close_dt(end_day) + dt.timedelta(minutes=1), step_seconds=step_seconds)
    rep = report(e.ledger.closed_trades, e.ledger.daily, e.perf_label.value, cfg.starting_capital)
    by_strategy: dict[str, dict[str, Any]] = {}
    for t in e.ledger.closed_trades:
        s = by_strategy.setdefault(t["strategy"], {"n": 0, "net": 0.0, "r": []})
        s["n"] += 1
        s["net"] += t["net"]
        s["r"].append(t["r"])
    for s in by_strategy.values():
        s["expectancy_r"] = sum(s["r"]) / len(s["r"]) if s["r"] else None
        s["net"] = round(s["net"], 2)
        del s["r"]
    return {"variant": "staged" if staging else "single-entry", "seed": seed, "days": days, "report": rep,
            "by_strategy": by_strategy, "kpis": e.kpis.to_dict(), "daily": e.ledger.daily,
            "equity_curve": e.ledger.equity_curve[::5], "gear_events": [ev.to_dict() for ev in e.gears.events]}


def compare_staging(cfg: OperatorConfig, rules: MarketRules, cal: MarketCalendar, *, days: int = 5,
                    seeds: tuple[int, ...] = (7,)) -> dict[str, Any]:
    """AGG-2.2: 'Staging is itself a hypothesis' -- staged vs single entry on identical markets."""
    out: dict[str, Any] = {"label": "SIMULATED", "rows": []}
    for sd in seeds:
        a = run(cfg, rules, cal, days=days, seed=sd, staging=True)
        b = run(cfg, rules, cal, days=days, seed=sd, staging=False)
        out["rows"].append({"seed": sd, "staged": a["report"], "single": b["report"]})
    return out
