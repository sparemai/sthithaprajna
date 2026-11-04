"""One read-only view of engine state for the web terminal, Discord and reports."""
from __future__ import annotations

from typing import Any

from sthitaprajna.aggression.sizer import gear_r, planned_risk_fraction
from sthitaprajna.domain import IST, Grade, Kind
from sthitaprajna.intelligence import scanner as scan_mod
from sthitaprajna.risk import ceilings
from sthitaprajna.signal.grading import TQS_WEIGHTS_STATUS


def _r(x: float | None, n: int = 2) -> float | None:
    return None if x is None else round(float(x), n)


def watchlist(e) -> list[dict[str, Any]]:
    out = []
    session_open = e.cal.is_open(e.now)      # outside the session a last price is expected, not "stale"
    for key in e.market.index_keys + e.market.stock_keys:
        st = e.market.states[key]
        q = e.quote(key)
        ltp = q.ltp if q else st.price
        pc = st.prev_close or ltp
        inst = e.market.instruments[key]
        out.append({"key": key, "symbol": inst.symbol, "name": inst.name, "kind": inst.kind.value,
                    "exchange": inst.exchange.value, "sector": inst.sector, "ltp": _r(ltp), "prev_close": _r(pc),
                    "change": _r(ltp - pc), "change_pct": _r((ltp / pc - 1) * 100 if pc else 0, 3),
                    "open": _r(st.open), "high": _r(st.high), "low": _r(st.low), "volume": int(st.day_volume),
                    "bid": _r(q.bid) if q else None, "ask": _r(q.ask) if q else None,
                    "stale": q is None and session_open,
                    "lot": inst.lot_size if inst.kind is Kind.INDEX else 1})
    return out


def _broker(e) -> dict[str, Any]:
    b = e.broker
    out: dict[str, Any] = {"live": e.live_orders, "name": "Dhan" if e.live_orders else "paper",
                           "resting_stops": len(b.resting), "working": len(b.pending)}
    if e.live_orders:
        out.update(available_funds=_r(b.available_funds) if b.available_funds is not None else None,
                   health=dict(b.health), rejects_1m=b.reject_count(e.now))
    return out


def _sleeve(e, ctx, eq_sod: float) -> dict[str, Any]:
    """Zero-to-Hero sleeve status (D8): budgets used this expiry/week/month and the cumulative-loss
    kill switch (D-039) -- a stopped sleeve needs an explicit operator /resume, same as a gear HALT."""
    sl = e.cfg.sleeve
    kill_at = float(sl["kill_cumulative_loss"]) * eq_sod
    return {
        "enabled_live": bool(sl["enabled_live"]), "used": {k: _r(v) for k, v in e.sleeve_used.items()},
        "budget": {"per_expiry": _r(float(sl["per_expiry"]) * eq_sod), "per_week": _r(float(sl["per_week"]) * eq_sod),
                  "per_month": _r(float(sl["per_month"]) * eq_sod)},
        "cumulative_pnl": _r(e.sleeve_cumulative_pnl), "kill_at_loss": _r(kill_at),
        "killed": -e.sleeve_cumulative_pnl >= kill_at - 1e-6,
    }


def expiry_calendar(e, count: int = 4) -> list[dict[str, Any]]:
    """D-054: upcoming expiries for every sleeve-eligible underlying, nearest first -- the planning
    surface the operator asked for ("an expiry calendar")."""
    today = e.now.date()
    out = []
    for und in ("NIFTY", "SENSEX", "BANKNIFTY"):
        for exp in e.cal.weekly_expiries(und, today, count):
            out.append({"underlying": und, "expiry": exp.isoformat(), "days_to_expiry": (exp - today).days,
                        "is_today": exp == today})
    out.sort(key=lambda r: (r["expiry"], r["underlying"]))
    return out


def zero_to_hero_report(e) -> dict[str, Any]:
    """D-054: every Zero-to-Hero capture ever recorded (D8), grouped by the contract's own expiry, from
    the full closed-trade history in the store -- not just the current session's in-memory
    sleeve_used/cumulative_pnl, which reset every period (D-054's "Zero to Hero captures" ask)."""
    trades = [t for t in (e.store.closed_trade_history() if e.store else []) if t.get("sleeve")]
    by_expiry: dict[str, dict[str, Any]] = {}
    for t in trades:
        exp = (t.get("instrument") or {}).get("expiry") or "unknown"
        row = by_expiry.setdefault(exp, {"expiry": exp, "trades": 0, "wins": 0, "net_pnl": 0.0})
        row["trades"] += 1
        row["wins"] += 1 if t.get("net_pnl", 0.0) > 0 else 0
        row["net_pnl"] += t.get("net_pnl", 0.0)
    captures = sorted(by_expiry.values(), key=lambda r: r["expiry"])
    for r in captures:
        r["net_pnl"] = round(r["net_pnl"], 2)
        r["win_rate"] = round(r["wins"] / r["trades"], 3) if r["trades"] else None
    return {"captures_by_expiry": captures, "total_trades": len(trades),
           "total_net_pnl": round(sum(t.get("net_pnl", 0.0) for t in trades), 2),
           "current": {"used": {k: _r(v) for k, v in e.sleeve_used.items()},
                       "cumulative_pnl": _r(e.sleeve_cumulative_pnl)}}


def period_review(e) -> dict[str, Any]:
    """D-054: completed week/month plan-vs-actual history, plus the live/current period so the operator
    sees both at once -- "daily, weekly, monthly plans ... synchronizing, retrospection, reviewing"."""
    u, ch = e.unrealized()
    pnl = e.ledger.period_pnl(u, ch)
    base_equity = {"day": e.ledger.day_start, "week": e.ledger.week_start, "month": e.ledger.month_start}
    return {"completed": {pt: e.period_reviews[pt] for pt in ("week", "month")},
           "current": e.targets.to_dict(pnl, base_equity)}


def snapshot(e, *, include_watch: bool = True) -> dict[str, Any]:
    u, ch = e.unrealized()
    mtm = e.ledger.mtm(u, ch)
    pnl = e.ledger.period_pnl(u, ch)
    ctx = e._risk_ctx()
    tier = ctx.tier
    gear = ctx.gear
    eq_sod = e.ledger.equity_sod
    open_risk = sum(p.open_risk for p in e.positions.values())
    limit = e.guardian.open_risk_limit(ctx)
    planned = {g.value: round(planned_risk_fraction(e.cfg, tier, gear, g, ctx.drawdown, e.edge.ceiling_r) * eq_sod, 2)
               for g in (Grade.A_PLUS, Grade.A, Grade.B)}
    lim = e.cfg.limits
    floor = ceilings.ruin_floor(e.ledger.all_time_peak, e.cfg.ruin_floor_abs, e.cfg.ruin_floor_pct)
    now = e.now.astimezone(IST)
    session = ("OPEN" if e.cal.is_open(now) else "CLOSED")
    recs = sorted(e.funnel.records.values(), key=lambda r: (r.terminal, -(r.updated or r.first_seen).timestamp()))
    wall = e.wall()
    margin_used = sum(e._margin_of(p.segment, p.avg_entry, p.qty) for p in e.positions.values())
    snap: dict[str, Any] = {
        "type": "snapshot",
        "clock": {"now": now.isoformat(), "day": e.market.day.isoformat(), "session": session,
                  "minutes_into": _r(e.cal.minutes_into_session(now), 1), "speed": e.speed, "paused": e.paused,
                  "weekday": now.strftime("%A")},
        "mode": e.mode.value, "label": e.perf_label.value, "gates_missing": e.mode_gates_missing,
        "sim_regime": e.market.regime, "market": e.market.describe(),
        "risk_mode": e.cfg.risk_mode,
        "scanner": {"summary": {**(e.market.describe().get("scanner") or {}), "ranked": len(e.scanner_rows),
                                "booming": sum(1 for r in e.scanner_rows if r.tradable and r.score >= float(e.scanner_cfg["promote_min_score"])),
                                "weights": scan_mod.WEIGHTS_STATUS},
                    "top": [r.to_dict() for r in e.scanner_rows[:30]]},
        "capital": {"equity": _r(e.ledger.equity), "deployed": _r(e.capital_in_use()), "free": _r(e.free_capital()),
                    "per_trade_fraction": float(e.cfg.unbounded["capital_per_trade"]),
                    "per_trade": _r(float(e.cfg.unbounded["capital_per_trade"]) * eq_sod),
                    "intraday_leverage": float(e.cfg.unbounded["intraday_leverage"]),
                    "breakeven_at_r": float(e.cfg.unbounded["trail_to_breakeven_at_r"]),
                    "deployed_pct": _r(e.capital_in_use() / e.ledger.equity * 100 if e.ledger.equity > 0 else 0, 2)},
        "broker": _broker(e),
        "mandatory_checks": [{"name": n, "what": w} for n, w in e.guardian.MANDATORY
                             if n != "dhan_instrument_id" or e.live_orders],
        "account": {"equity": _r(e.ledger.equity), "mtm": _r(mtm), "equity_sod": _r(eq_sod), "unrealized": _r(u),
                    "open_charges": _r(ch), "day_pnl": _r(pnl["day"]), "week_pnl": _r(pnl["week"]),
                    "month_pnl": _r(pnl["month"]), "day_pnl_pct": _r(pnl["day"] / eq_sod * 100 if eq_sod else 0, 3),
                    "peak": _r(e.ledger.all_time_peak), "ruin_floor": _r(floor), "starting": e.cfg.starting_capital,
                    "total_return_pct": _r((mtm / e.cfg.starting_capital - 1) * 100, 3)},
        "gear": {**e.gears.to_dict(mtm), "effective_r": gear_r(e.cfg, tier, gear, e.edge.ceiling_r),
                 "headroom_to_halt": max(0.0, ceilings.HALT_DRAWDOWN - ctx.drawdown)},
        "tier": e.tiers.to_dict(),
        "risk": {"open_risk": _r(open_risk), "open_risk_limit": _r(limit), "planned_by_grade": planned,
                 "per_trade_ceiling": _r(ceilings.MAX_RISK_PER_TRADE * eq_sod),
                 "day_loss_limit": _r(float(lim["daily_loss"]) * eq_sod),
                 "week_loss_limit": _r(float(lim["weekly_loss"]) * (e.ledger.week_start or eq_sod)),
                 "month_loss_limit": _r(float(lim["monthly_loss"]) * (e.ledger.month_start or eq_sod)),
                 "trades_today": e.ledger.trades_today, "max_trades": tier.max_trades_per_day,
                 "positions": len(e.positions), "max_positions": tier.max_positions,
                 "losing_streak": e.ledger.losing_streak,
                 "cooldown_until": e.ledger.cooldown_until.isoformat() if e.ledger.cooldown_until else None,
                 "margin_used": _r(margin_used), "margin_limit": _r(float(lim["margin_utilisation"]) * eq_sod),
                 "kill_switch": e.kill_switch, "ruined": e.ruined, "halts": ctx.operational_halts,
                 "blocked_instruments": len(ctx.blocked_instruments), "day_peak_pnl": _r(e.ledger.day_peak_pnl),
                 "bucket_cap": _r(e.guardian.bucket_cap(ctx)), "entry_window_open": ctx.entry_window_open,
                 "target_blocks": ctx.target_blocks, "b_allowed": ctx.b_allowed},
        "sleeve": _sleeve(e, ctx, eq_sod),
        "ceilings": {"max_risk_per_trade": ceilings.MAX_RISK_PER_TRADE, "max_open_risk": ceilings.MAX_OPEN_RISK,
                     "halt_drawdown": ceilings.HALT_DRAWDOWN, "ruin_floor_abs": ceilings.RUIN_FLOOR_ABS_MIN,
                     "ruin_floor_pct": ceilings.RUIN_FLOOR_PCT_MIN, "ops_cap": int(e.cfg.execution["max_orders_per_second"])},
        "edge_brake": e.edge.to_dict(), "calibration": {k: v for k, v in e.calibration.to_dict().items() if k != "table"},
        "tqs_status": TQS_WEIGHTS_STATUS,
        "positions": [p.to_dict() for p in e.positions.values()],
        "closed": [{**t} for t in e.ledger.closed_trades[-40:]][::-1],
        "orders": [o.to_dict() for o in list(e.orders.values())[-80:]][::-1],
        "approvals": [a.to_dict(wall) for a in e.approvals.values() if a.status == "PENDING"],
        "funnel": {"counts": e.funnel.funnel_counts(), "drop_reasons": dict(e.funnel.drop_reasons),
                   "records": [r.to_dict() for r in recs[:48]]},
        "kpis": e.kpis.to_dict(), "targets": e.targets.to_dict(pnl, {"day": e.ledger.day_start, "week": e.ledger.week_start,
                                                                     "month": e.ledger.month_start}),
        "regime": e.regime.to_dict(), "feeds": e.arbiter.to_dict(), "quality": e.quality.to_dict(),
        "process": e._process_score(), "events": list(e.events)[-60:][::-1],
        "state_check": e.state_check, "pilot_approvals": e.pilot_approvals,
        "manual_exits": e.manual_exits[-10:], "journal_latest": e.journal[-1] if e.journal else None,
        "faults": e.faults.log[-10:], "ops_peak": e.ops.peak,
    }
    if include_watch:
        snap["watch"] = watchlist(e)
    return snap
