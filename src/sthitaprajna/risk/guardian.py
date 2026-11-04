"""Risk Guardian -- "The Monk" (A5.1 #8, A10.12, D4, D12.2). Absolute veto; exits are never blocked.

Every check is named and recorded, so each decision object carries the full checklist (A18) and the
UI can show exactly which rule allowed or blocked a trade. Nothing in this module can raise risk:
it only answers yes/no on a proposal that was already sized by the Aggression Layer.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from sthitaprajna.aggression.sizer import SizingDecision, gear_r, grade_multiplier
from sthitaprajna.aggression.staging import AddPlan
from sthitaprajna.config import OperatorConfig, TierSpec
from sthitaprajna.domain import Candidate, Direction, Gear, Grade, Mode, Segment
from sthitaprajna.portfolio.position import Position
from sthitaprajna.risk import ceilings
from sthitaprajna.signal.ev import EVResult

EPS = 1e-6


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


@dataclass
class Verdict:
    approved: bool
    checks: list[Check]

    @property
    def reason(self) -> str:
        bad = [c for c in self.checks if not c.ok]
        return bad[0].name + (f": {bad[0].detail}" if bad[0].detail else "") if bad else "APPROVED"

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]

    def to_dict(self) -> dict[str, Any]:
        return {"approved": self.approved, "reason": self.reason, "checks": [c.to_dict() for c in self.checks]}


@dataclass
class PositionView:
    id: str
    underlying_key: str
    exposure: Direction
    segment: Segment
    grade: Grade
    open_risk: float
    notional: float
    margin: float
    beta: float
    sleeve: bool
    index_linked: bool

    @classmethod
    def of(cls, p: Position, margin: float) -> "PositionView":
        return cls(p.id, p.underlying_key, p.exposure, p.segment, p.grade, p.open_risk, p.notional(), margin,
                   p.beta, p.sleeve, p.segment in (Segment.INDEX_OPTION, Segment.INDEX_FUTURE))


@dataclass
class RiskContext:
    mode: Mode
    now: dt.datetime
    equity_sod: float                  # equity at previous close: the sizing basis (D1)
    equity_mtm: float
    all_time_peak: float
    gear: Gear                         # effective gear
    drawdown: float                    # ladder drawdown
    tier: TierSpec
    day_pnl: float = 0.0
    day_peak_pnl: float = 0.0
    week_pnl: float = 0.0
    month_pnl: float = 0.0
    week_start_equity: float = 0.0
    month_start_equity: float = 0.0
    positions: list[PositionView] = field(default_factory=list)
    trades_today: int = 0
    losing_streak: int = 0
    cooldown_until: dt.datetime | None = None
    kill_switch: bool = False
    operational_halts: list[str] = field(default_factory=list)
    blocked_instruments: dict[str, str] = field(default_factory=dict)
    event_blackout: str | None = None
    target_blocks: list[str] = field(default_factory=list)
    b_allowed: bool = True
    reentries_today: dict[str, int] = field(default_factory=dict)
    stopped_signal_bars: dict[str, set[str]] = field(default_factory=dict)
    sleeve_used: dict[str, float] = field(default_factory=dict)
    sleeve_cumulative_pnl: float = 0.0   # net realized P&L of the sleeve since the last operator review (D8)
    edge_ceiling: float | None = None
    entry_window_open: bool = True
    free_capital: float = float("inf")   # unbounded mode: capital not tied up in open trades


@dataclass
class EntryProposal:
    candidate: Candidate
    grade: Grade
    sizing: SizingDecision
    qty: int                      # qty to enter now (the probe)
    risk_now: float               # Rs lost if the probe is stopped (incl. costs)
    notional_now: float
    margin_now: float
    ev: EVResult


class RiskGuardian:
    def __init__(self, cfg: OperatorConfig) -> None:
        self.cfg = cfg

    # ------------------------------------------------------------------ helpers
    def open_risk_limit(self, ctx: RiskContext) -> float:
        frac = self.cfg.gears[ctx.gear].open_risk * self.cfg.tier_factor(ctx.tier)
        return ceilings.clamp_open_risk_limit(frac) * ctx.equity_sod

    def bucket_cap(self, ctx: RiskContext) -> float:
        """Nifty + Sensex same direction (and aligned high-beta stocks) = ONE bet (A7.3, D4)."""
        top = grade_multiplier(self.cfg, ctx.gear, Grade.A_PLUS, ctx.drawdown)
        frac = ceilings.clamp_risk_fraction(gear_r(self.cfg, ctx.tier, ctx.gear, ctx.edge_ceiling) * top)
        return frac * ctx.equity_sod

    def _in_index_bucket(self, index_linked: bool, beta: float, segment: Segment) -> bool:
        return index_linked or (segment in (Segment.EQ_INTRADAY, Segment.EQ_DELIVERY, Segment.STOCK_OPTION,
                                            Segment.STOCK_FUTURE)
                                and beta >= float(self.cfg.limits["high_beta_threshold"]))

    def _common(self, ctx: RiskContext) -> list[Check]:
        floor = ceilings.ruin_floor(ctx.all_time_peak, self.cfg.ruin_floor_abs, self.cfg.ruin_floor_pct)
        lim = self.cfg.limits
        day_limit = float(lim["daily_loss"]) * ctx.equity_sod
        wk_base = ctx.week_start_equity or ctx.equity_sod
        mo_base = ctx.month_start_equity or ctx.equity_sod
        return [
            Check("kill_switch_off", not ctx.kill_switch, "kill switch engaged" if ctx.kill_switch else ""),
            Check("above_ruin_floor", ctx.equity_mtm > floor, f"equity {ctx.equity_mtm:,.0f} vs floor {floor:,.0f}"),
            Check("not_halted", ctx.gear is not Gear.HALTED and ctx.drawdown < ceilings.HALT_DRAWDOWN,
                  f"gear {ctx.gear.value}, drawdown {ctx.drawdown:.2%}"),
            Check("no_operational_halt", not ctx.operational_halts, ", ".join(ctx.operational_halts)),
            Check("daily_loss_ok", ctx.day_pnl > -day_limit + EPS, f"day {ctx.day_pnl:,.0f} vs -{day_limit:,.0f}"),
            Check("weekly_loss_ok", ctx.week_pnl > -float(lim["weekly_loss"]) * wk_base + EPS,
                  f"week {ctx.week_pnl:,.0f}; breach -> rest of week PAPER"),
            Check("monthly_loss_ok", ctx.month_pnl > -float(lim["monthly_loss"]) * mo_base + EPS,
                  f"month {ctx.month_pnl:,.0f}; breach -> rest of month PAPER + review"),
            Check("targets_not_locked", not ctx.target_blocks, "; ".join(ctx.target_blocks)),
        ]

    # ------------------------------------------------------------------ entries
    MANDATORY = (   # unbounded mode: the only things that can stop an entry (name, what it means)
        ("kill_switch_off", "Your kill switch is off"),
        ("no_operational_halt", "No operational halt (order-reject storm, stale data)"),
        ("entry_window_open", "Market open, before the intraday exit time"),
        ("instrument_data_ok", "Fresh, sane market data for the instrument"),
        ("stop_defined", "A stop-loss on the losing side of the entry"),
        ("quantity_positive", "At least one share or one lot fits the capital"),
        ("funds_available", "Enough free capital for the trade"),
        ("no_naked_option_selling", "Options are bought, never sold naked"),
        ("dhan_instrument_id", "LIVE only: Dhan knows the contract"),
    )

    def check_entry_unbounded(self, ctx: RiskContext, prop: EntryProposal) -> Verdict:
        """Operator decision D-031: no ceilings. Only what the market, broker or law enforces, the operator's
        own switch, and the checks that stop a broken trade (bad data, no stop, no funds)."""
        c = prop.candidate
        is_short_option = c.instrument.is_option and c.direction is Direction.SHORT
        checks = [
            Check("kill_switch_off", not ctx.kill_switch, "kill switch engaged" if ctx.kill_switch else ""),
            Check("no_operational_halt", not ctx.operational_halts, ", ".join(ctx.operational_halts)),
            Check("entry_window_open", ctx.entry_window_open, "market closed or too close to the intraday exit"),
            Check("instrument_data_ok", c.instrument.key not in ctx.blocked_instruments
                  and c.underlying_key not in ctx.blocked_instruments,
                  ctx.blocked_instruments.get(c.instrument.key) or ctx.blocked_instruments.get(c.underlying_key, "")),
            Check("stop_defined", c.unit_risk > 0 and c.direction.sign * (c.entry - c.stop) > 0,
                  f"entry {c.entry} stop {c.stop}"),
            Check("quantity_positive", prop.qty > 0, "sized to zero"),
            Check("funds_available", prop.margin_now <= ctx.free_capital + EPS,
                  f"needs Rs {prop.margin_now:,.0f}, free Rs {ctx.free_capital:,.0f}"),
            Check("no_naked_option_selling", not (is_short_option and not self.cfg.limits.get("naked_option_selling")),
                  "selling options needs far more margin than the account holds"),
        ]
        if ctx.mode in (Mode.LIVE_PILOT, Mode.LIVE_FULL):     # a real order needs Dhan's id for the contract
            checks.append(Check("dhan_instrument_id", bool(c.instrument.dhan_security_id),
                                f"no Dhan security id for {c.instrument.symbol}"))
        return Verdict(all(ch.ok for ch in checks), checks)

    def check_entry(self, ctx: RiskContext, prop: EntryProposal) -> Verdict:
        if self.cfg.is_unbounded:
            return self.check_entry_unbounded(ctx, prop)
        cfg, lim, c = self.cfg, self.cfg.limits, prop.candidate
        gspec = cfg.gears[ctx.gear]
        checks = self._common(ctx)

        gear_r_rupees = gear_r(cfg, ctx.tier, ctx.gear, ctx.edge_ceiling) * ctx.equity_sod
        giveback = (ctx.day_peak_pnl >= float(lim["giveback_trigger_gear_r_multiple"]) * gear_r_rupees > 0
                    and ctx.day_pnl <= (1 - float(lim["giveback_fraction"])) * ctx.day_peak_pnl)
        checks.append(Check("profit_giveback_ok", not giveback,
                            f"day peak {ctx.day_peak_pnl:,.0f} gave back to {ctx.day_pnl:,.0f}" if giveback else ""))

        cooling = ctx.cooldown_until is not None and ctx.now < ctx.cooldown_until
        done = ctx.losing_streak >= int(lim["streak_stop_losses"])
        checks.append(Check("losing_streak_ok", not (cooling or done),
                            "done for the day" if done else (f"cooldown until {ctx.cooldown_until:%H:%M}" if cooling else "")))
        checks.append(Check("entry_window_open", ctx.entry_window_open, "outside the entry window"))
        checks.append(Check("trade_count_ok", ctx.trades_today < ctx.tier.max_trades_per_day,
                            f"{ctx.trades_today}/{ctx.tier.max_trades_per_day} trades today"))
        checks.append(Check("position_count_ok", len(ctx.positions) < ctx.tier.max_positions,
                            f"{len(ctx.positions)}/{ctx.tier.max_positions} open"))
        checks.append(Check("instrument_data_ok", c.instrument.key not in ctx.blocked_instruments
                            and c.underlying_key not in ctx.blocked_instruments,
                            ctx.blocked_instruments.get(c.instrument.key) or ctx.blocked_instruments.get(c.underlying_key, "")))
        checks.append(Check("no_event_blackout", ctx.event_blackout is None, ctx.event_blackout or ""))

        # grade permissions
        checks.append(Check("grade_allowed_in_gear", prop.grade in gspec.grades,
                            f"{prop.grade.value} not allowed in {ctx.gear.value}"))
        checks.append(Check("b_grade_allowed", prop.grade is not Grade.B or ctx.b_allowed,
                            "ratchet mode: B probes off"))
        a_plus_open = sum(1 for p in ctx.positions if p.grade is Grade.A_PLUS)
        checks.append(Check("a_plus_limit_ok", prop.grade is not Grade.A_PLUS
                            or a_plus_open < cfg.grades[Grade.A_PLUS].max_open, "max one A+ position open"))

        # re-entry rules (AGG-1 #6)
        if c.is_reentry:
            base = c.setup_id.rsplit(":re", 1)[0]
            n = ctx.reentries_today.get(base, 0)
            fresh = c.signal_bar not in ctx.stopped_signal_bars.get(base, set())
            max_re = int(cfg.staging["reentry_max_per_setup_per_day"])
            probe_sized = prop.sizing.staging_mode == "probe_only" and prop.qty <= prop.sizing.probe_qty
            checks.append(Check("reentry_allowed", gspec.reentry and n < max_re and fresh and probe_sized,
                                f"gear={ctx.gear.value} used={n}/{max_re} fresh_trigger={fresh} probe_sized={probe_sized}"))

        # per-trade risk ceilings
        pr_frac = prop.sizing.planned_risk / ctx.equity_sod if ctx.equity_sod > 0 else 1.0
        checks.append(Check("per_trade_ceiling", pr_frac <= ceilings.MAX_RISK_PER_TRADE + EPS,
                            f"planned {pr_frac:.2%} vs ceiling {ceilings.MAX_RISK_PER_TRADE:.1%}"))
        checks.append(Check("sized_within_plan", prop.risk_now <= prop.sizing.planned_risk + EPS and prop.qty > 0,
                            f"risk now {prop.risk_now:,.0f} vs planned {prop.sizing.planned_risk:,.0f}"))

        # portfolio open risk
        open_risk = sum(p.open_risk for p in ctx.positions)
        limit = self.open_risk_limit(ctx)
        checks.append(Check("open_risk_ok", open_risk + prop.risk_now <= limit + EPS,
                            f"{open_risk + prop.risk_now:,.0f} vs limit {limit:,.0f} ({ctx.gear.value})"))

        # correlation bucket
        if self._in_index_bucket(c.segment in (Segment.INDEX_OPTION, Segment.INDEX_FUTURE), c.beta, c.segment):
            bucket = sum(p.open_risk for p in ctx.positions
                         if p.exposure is c.exposure and self._in_index_bucket(p.index_linked, p.beta, p.segment))
            cap = self.bucket_cap(ctx)
            checks.append(Check("correlation_bucket_ok", bucket + prop.risk_now <= cap + EPS,
                                f"{c.exposure.value}-index bucket {bucket + prop.risk_now:,.0f} vs one-bet cap {cap:,.0f}"))

        # margin and leverage
        margin_used = sum(p.margin for p in ctx.positions)
        checks.append(Check("margin_ok", margin_used + prop.margin_now <= float(lim["margin_utilisation"]) * ctx.equity_sod + EPS,
                            f"{margin_used + prop.margin_now:,.0f} vs {float(lim['margin_utilisation']) * ctx.equity_sod:,.0f}"))
        if c.segment is Segment.EQ_INTRADAY:
            gross = sum(p.notional for p in ctx.positions if p.segment is Segment.EQ_INTRADAY)
            checks.append(Check("cash_leverage_ok",
                                prop.notional_now <= float(lim["cash_intraday_single_notional_x"]) * ctx.equity_sod + EPS
                                and gross + prop.notional_now <= float(lim["cash_intraday_gross_notional_x"]) * ctx.equity_sod + EPS,
                                f"single {prop.notional_now:,.0f}, gross {gross + prop.notional_now:,.0f}"))
        if c.segment is Segment.EQ_DELIVERY:
            tot = sum(p.notional for p in ctx.positions if p.segment is Segment.EQ_DELIVERY)
            checks.append(Check("delivery_caps_ok",
                                prop.notional_now <= float(lim["cash_delivery_single"]) * ctx.equity_sod + EPS
                                and tot + prop.notional_now <= float(lim["cash_delivery_total"]) * ctx.equity_sod + EPS,
                                f"single {prop.notional_now:,.0f}, total {tot + prop.notional_now:,.0f}"))

        # structure permissions
        checks.append(Check("defined_risk_if_required", not gspec.defined_risk_only or c.defined_risk,
                            f"{ctx.gear.value} allows defined-risk structures only"))
        is_short_option = c.instrument.is_option and c.direction is Direction.SHORT
        checks.append(Check("no_naked_option_selling", not (is_short_option and not lim.get("naked_option_selling")),
                            "naked option selling disabled (D0)"))
        checks.append(Check("stop_defined", c.unit_risk > 0 and c.direction.sign * (c.entry - c.stop) > 0,
                            f"entry {c.entry} stop {c.stop}"))
        checks.append(Check("ev_margin_of_safety", prop.ev.margin_ok, prop.ev.reason))
        if ctx.mode in (Mode.LIVE_PILOT, Mode.LIVE_FULL):
            checks.append(Check("calibrated_for_live", c.p_win_calibrated,
                                "live trading requires calibrated probabilities (A9.4, Phase 5)"))

        if c.sleeve:
            sl = cfg.sleeve
            used = ctx.sleeve_used
            eq = ctx.equity_sod
            ok = (gspec.sleeve and used.get("expiry", 0) + prop.risk_now <= float(sl["per_expiry"]) * eq + EPS
                  and used.get("week", 0) + prop.risk_now <= float(sl["per_week"]) * eq + EPS
                  and used.get("month", 0) + prop.risk_now <= float(sl["per_month"]) * eq + EPS
                  and (ctx.mode in (Mode.SIM, Mode.PAPER) or bool(sl["enabled_live"])))
            checks.append(Check("sleeve_budget_ok", ok, f"sleeve used {used}"))
            kill_at = float(sl["kill_cumulative_loss"]) * eq
            killed = -ctx.sleeve_cumulative_pnl >= kill_at - EPS
            checks.append(Check("sleeve_not_killed", not killed,
                                f"cumulative sleeve P&L Rs {ctx.sleeve_cumulative_pnl:,.0f} <= -Rs {kill_at:,.0f} "
                                "(D8): sleeve OFF until operator review" if killed else ""))

        approved = all(ch.ok for ch in checks)
        return Verdict(approved, checks)

    # ------------------------------------------------------------------ stage adds
    def check_add(self, ctx: RiskContext, pos: Position, plan: AddPlan, price: float, is_press: bool,
                  add_margin: float = 0.0, add_notional: float = 0.0) -> Verdict:
        checks = self._common(ctx)
        gspec = self.cfg.gears[ctx.gear]
        checks.append(Check("add_plan_valid", plan.ok, plan.reason))
        checks.append(Check("never_add_to_loser", pos.unrealized(price) > 0,
                            f"unrealised {pos.unrealized(price):,.0f}"))
        if is_press:
            checks.append(Check("gear_allows_pressing", gspec.pressing, f"{ctx.gear.value} does not press"))
        if plan.ok:
            limit_ok = plan.worst_case_after <= pos.planned_risk + EPS
            checks.append(Check("whole_position_within_plan", limit_ok,
                                f"worst case {plan.worst_case_after:,.0f} vs planned {pos.planned_risk:,.0f}"))
            others = sum(p.open_risk for p in ctx.positions if p.id != pos.id)
            new_risk = max(0.0, plan.worst_case_after)
            limit = self.open_risk_limit(ctx)
            checks.append(Check("open_risk_ok", others + new_risk <= limit + EPS,
                                f"{others + new_risk:,.0f} vs {limit:,.0f}"))
            margin_used = sum(p.margin for p in ctx.positions)
            checks.append(Check("margin_ok", margin_used + add_margin
                                <= float(self.cfg.limits["margin_utilisation"]) * ctx.equity_sod + EPS, ""))
            if pos.segment is Segment.EQ_INTRADAY:
                checks.append(Check("cash_leverage_ok", pos.notional(price) + add_notional
                                    <= float(self.cfg.limits["cash_intraday_single_notional_x"]) * ctx.equity_sod + EPS, ""))
        return Verdict(all(ch.ok for ch in checks), checks)

    @staticmethod
    def check_exit() -> Verdict:
        """Exits, stops, harvests, time exits and the kill switch are never blocked (D9)."""
        return Verdict(True, [Check("exits_always_allowed", True)])
