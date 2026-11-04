"""The Sthitaprajna runtime: one deterministic loop implementing the A5.2 decision hierarchy.

MARKET DATA (primary + secondary) -> FEED ARBITRATION + DATA QUALITY -> REGIME -> STRATEGIES
-> PICKUP FUNNEL (S1-S5) -> OPPORTUNITY QUEUE -> AGGRESSION LAYER (size/stage) -> RISK GUARDIAN (S6)
-> OPERATOR APPROVAL (when required) -> EXECUTION (S7, Algo-ID tagged, broker-side stop) -> POSITION STAGE
MANAGER (probe -> confirm-add -> press -> harvest -> exit; every add re-runs the guardian) -> LEDGER
-> GEARS / EDGE BRAKE -> KPIs, journal, decision objects.

The same class drives SIM, PAPER and (after gate (c)) LIVE -- only the broker/feeds differ (A5.4).
No LLM is anywhere in this path (A5.3).
"""
from __future__ import annotations

import copy
import datetime as dt
import os
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from sthitaprajna.aggression import edge_brake, staging
from sthitaprajna.aggression.gears import GearController
from sthitaprajna.aggression.kpis import AggressionKPIs
from sthitaprajna.aggression.sizer import gear_r
from sthitaprajna.aggression.tiers import TierState
from sthitaprajna.config import MarketRules, OperatorConfig
from sthitaprajna.data.arbitration import FeedArbiter
from sthitaprajna.data.feeds import FaultInjector, SimFeed
from sthitaprajna.data.live import NullFaults
from sthitaprajna.data.quality import DataQualityGuardian
from sthitaprajna.data.sim import SimMarket
from sthitaprajna.domain import (
    IST, Candidate, Direction, Gear, Grade, Kind, Leg, Mode, PerfLabel,
    PositionStage, Quote, Segment, Side, Source, new_id,
)
from sthitaprajna.engine.funnel import FunnelRecord, PickupFunnel, decision_object
from sthitaprajna.execution.live import LIVE_MODES, exchange_segment, resolve_mode, round_tick
from sthitaprajna.execution.orders import Order, OrderStatus, OrderType, OpsLimiter, new_order, slice_for_freeze
from sthitaprajna.execution.paper import Fill, PaperBroker
from sthitaprajna.intelligence import regime as rg
from sthitaprajna.intelligence import scanner as scan_mod
from sthitaprajna.market import indicators as ind
from sthitaprajna.market.calendar import MarketCalendar
from sthitaprajna.market.charges import ChargesModel
from sthitaprajna.market.options import summary as chain_summary
from sthitaprajna.portfolio.ledger import Ledger
from sthitaprajna.portfolio.position import ClosedLeg, Position
from sthitaprajna.risk import ceilings
from sthitaprajna.risk.guardian import EntryProposal, PositionView, RiskContext, RiskGuardian
from sthitaprajna.risk.targets import TargetBook
from sthitaprajna.signal.calibration import CalibrationTracker
from sthitaprajna.strategies.options import all_strategies

RULE_EXITS = {"STOP", "BROKER_STOP", "TRAIL", "HARVEST", "TIME_STOP", "EOD", "CONFIRM_TIMEOUT_SCRATCH", "KILL",
              "FLATTEN", "FILL_OUTSIDE_PLAN", "TARGET"}
MIS_MARGIN_FRACTION = 0.20   # [VERIFY with Dhan] intraday cash margin as a fraction of notional
REVERSAL_WATCH_MINUTES = 60  # D-045: how long a stop-out keeps the opposite direction "watched" on that underlying


@dataclass
class Approval:
    id: str
    kind: str                         # ENTRY | CONFIRM_ADD | PRESS | REENTRY | SLEEVE
    created_wall: float
    expires_wall: float
    summary: dict[str, Any]
    setup_id: str | None = None
    position_id: str | None = None
    add_plan: Any = None
    status: str = "PENDING"

    def to_dict(self, now_wall: float) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "summary": self.summary, "status": self.status,
                "seconds_left": max(0.0, round(self.expires_wall - now_wall, 1)), "setup_id": self.setup_id,
                "position_id": self.position_id}


@dataclass
class PendingEntry:
    rec: FunnelRecord
    prop: EntryProposal
    order_id: str
    submitted: dt.datetime


@dataclass
class Shadow:
    """Counterfactual for a manual exit (D6): what the system's own exit would have produced."""
    pos: Position
    manual_pnl: float
    manual_ts: dt.datetime
    fraction: float
    done: bool = False
    system_pnl: float | None = None


class _FeatureCacheCtx:
    """StrategyContext with per-scan memoised features (strategies share bar-close features)."""

    def __init__(self, eng: "Engine") -> None:
        self.e = eng
        self.now = eng.now
        self.cal = eng.cal
        self.regime = eng.regime
        self.equity_sod = eng.ledger.equity_sod
        self._feat_cache: dict[str, Any] = {}
        self._chain_cache: dict[tuple, Any] = {}

    def candles(self, key):
        return self.e.market.candles(key)

    def quote(self, key):
        return self.e.quote(key)

    def instrument(self, key):
        return self.e.market.instruments[key]

    def stock_keys(self):
        if self.e.cash_only:
            active = set(self.e.market.stock_keys)
            return [r.key for r in self.e.scanner_rows if r.tradable and r.key in active
                    and r.score >= float(self.e.scanner_cfg["promote_min_score"])][
                        :int(self.e.scanner_cfg["promote_max"])]
        return self.e.market.stock_keys

    def index_keys(self):
        return self.e.market.index_keys

    def breadth(self):
        return self.e.market.breadth()

    def sector_change(self, sector):
        return self.e.sector_change(sector)

    def chain(self, underlying, expiry=None):
        k = (underlying, expiry)
        if k not in self._chain_cache:
            self._chain_cache[k] = self.e.market.chain(underlying, expiry)
        return self._chain_cache[k]

    def expiries(self, underlying):
        return self.e.market.expiries(underlying)

    def option_instrument(self, underlying, expiry, strike, ot):
        return self.e.market.option_instrument(underlying, expiry, strike, ot)

    def log(self, msg: str) -> None:
        """D-058: lets a strategy explain a near-miss (eligible regime, no candidate) at scan time,
        rather than leaving the operator to infer it from silence."""
        self.e._log("scan", msg)

    def option_underlyings(self):
        return self.e.market.option_underlyings()

    def sleeve_cfg(self):
        return self.e.cfg.sleeve

    def reversal_watch(self, key):
        return self.e.reversal_watch(key)

    def iv_history(self, underlying: str) -> list[float]:
        """D-055: EOD ATM-IV points for an underlying (D-039's _snapshot_iv_history, index-only). Empty
        with no store or fewer than 5 sessions recorded -- iv_rank()/iv_percentile() already treat that
        as "no data", never a guess."""
        return self.e.store.iv_history_for(underlying) if self.e.store else []


class _Env:
    """FunnelEnv view over the engine."""

    def __init__(self, e: "Engine", ctx: RiskContext) -> None:
        self.e = e
        self.mode = e.mode
        self.now = e.now
        self.equity_sod = ctx.equity_sod
        self.tier = ctx.tier
        self.gear = ctx.gear
        self.drawdown = ctx.drawdown
        self.edge_ceiling = ctx.edge_ceiling
        self.event_blackout = ctx.event_blackout
        self.entry_window_open = ctx.entry_window_open
        self._ctx = ctx

    def quote(self, key):
        return self.e.quote(key)

    def data_confidence(self, key):
        if key in self.e.quality.blocked:
            return 0.0
        return self.e.arbiter.data_confidence(key)

    def execution_confidence(self, key):
        rej = self.e.broker.reject_count(self.e.now)
        return 1.0 if rej == 0 else max(0.3, 1 - 0.15 * rej)

    def bucket_room(self, c: Candidate) -> float:
        g = self.e.guardian
        if not g._in_index_bucket(c.segment in (Segment.INDEX_OPTION, Segment.INDEX_FUTURE), c.beta, c.segment):
            return float("inf")
        used = sum(p.open_risk for p in self._ctx.positions
                   if p.exposure is c.exposure and g._in_index_bucket(p.index_linked, p.beta, p.segment))
        return g.bucket_cap(self._ctx) - used

    def is_banned(self, key):
        return self.e.banned.get(key)

    def free_capital(self) -> float:
        return self._ctx.free_capital if self._ctx.free_capital != float("inf") else self.e.free_capital()


class Engine:
    def __init__(self, cfg: OperatorConfig, rules: MarketRules, cal: MarketCalendar, *, mode: Mode = Mode.SIM,
                 seed: int = 7, start: dt.date | None = None, speed: float = 30.0, warmup_days: int = 5,
                 store: Any = None, wall_clock=time.monotonic, market: Any = None,
                 feeds: tuple[Any, Any] | None = None, broker: Any = None,
                 strategies=None, stock_specs=None, cash_only: bool = False) -> None:
        self.cfg, self.rules, self.cal = cfg, rules, cal
        self.cash_only = cash_only
        if market is not None and mode is Mode.SIM:
            mode = Mode.PAPER                          # real market data is never labelled SIMULATED
        resolved, missing = resolve_mode(mode, broker_live=bool(getattr(broker, "live", False)) and market is not None)
        self.mode = resolved
        self.mode_gates_missing = missing
        self.live_orders = self.mode in LIVE_MODES     # real orders on Dhan (D-034); otherwise the paper broker
        self.wall = wall_clock
        if market is None:
            self.market = SimMarket(cal, seed=seed, start=start, warmup_days=warmup_days,
                                    risk_free=float(rules.get("risk_free_rate", 0.065)), stock_specs=stock_specs)
            self.primary = SimFeed(self.market, "DHAN", Source.DHAN, 70, 15, seed + 1)
            self.secondary = SimFeed(self.market, "UPSTOX", Source.UPSTOX, 120, 30, seed + 2)
            self.faults = FaultInjector(self.market, self.primary, self.secondary)
        else:                                          # PAPER on live data (data/live.py): real clock, 1x
            self.market = market
            self.primary, self.secondary = feeds
            self.faults = NullFaults()
            speed = 1.0
        self.arbiter = FeedArbiter(cfg.data)
        self.live_data = self.market.source is not Source.SIM
        if self.live_data and not getattr(self.market, "secondary_configured", False):
            self.arbiter.situation = "SECONDARY_DOWN"  # A5.5 single-feed row: Dhan only, Upstox not configured
            self.arbiter.secondary.was_healthy = False
        self.quality = DataQualityGuardian(stale_after_s=float(cfg.data["stale_after_seconds"]))
        self.charges = ChargesModel(rules)
        self.guardian = RiskGuardian(cfg)
        self.funnel = PickupFunnel(cfg, self.charges, rules)
        self.funnel.on_terminal = self._on_funnel_terminal
        self.strategies = all_strategies() if strategies is None else list(strategies)
        self.broker = broker if self.live_orders else PaperBroker(self.charges, api_down=self.faults.order_api_down,
                                                                  reject_storm=self.faults.reject_storm)
        self.ops = OpsLimiter(int(cfg.execution["max_orders_per_second"]))
        self.store = store
        self.ledger = Ledger(cfg.starting_capital)
        self._restored_trades = self.ledger.restore_from_history(
            self.store.closed_trade_history()) if self.store else 0
        self.gears = GearController(cfg, ref_peak=self.ledger.all_time_peak)
        self.tiers = TierState(cfg)
        self.targets = TargetBook(float(cfg.targets["ratchet_floor_fraction"]))
        self.kpis = AggressionKPIs()
        self.calibration = CalibrationTracker()
        self.positions: dict[str, Position] = {}
        self.closed: list[Position] = []
        self.orders: dict[str, Order] = {}
        self.order_queue: deque[Order] = deque()
        self.pending_entries: dict[str, PendingEntry] = {}
        self.pending_adds: dict[str, tuple[str, staging.AddPlan]] = {}
        self.exit_orders: dict[str, str] = {}          # position_id -> open exit order id
        self.approvals: dict[str, Approval] = {}
        self.decisions: deque[dict[str, Any]] = deque(maxlen=600)
        self.events: deque[dict[str, Any]] = deque(maxlen=400)
        self.event_seq = 0                             # monotonic id per event; followers never stall on a full deque
        self.shadows: list[Shadow] = []
        self.manual_exits: list[dict[str, Any]] = []
        self.kill_switch = False
        self.ruined = False
        self.speed = speed
        self.paused = False
        self.regime = rg.RegimeState()
        self.state_check: dict[str, Any] | None = None
        # PAPER/SIM never read DHAN_EXCHANGE_ALGO_ID: a paper decision log must never carry what looks
        # like a real exchange Algo-ID, even if the operator's real one already sits in .env for later.
        if self.live_orders:
            self.algo_id = os.environ.get("DHAN_EXCHANGE_ALGO_ID") or "SELF"
        else:
            self.algo_id = "PAPER-ALGO-0000" if self.mode is Mode.PAPER else "SIM-ALGO-0000"
        # D9: every entry and stage-add needs operator approval the first time LIVE is enabled, exactly
        # as it would in SIM/PAPER if sim_require_approval is set. Relax later with the pilot_approvals action.
        self.pilot_approvals = bool(cfg.approvals.get("sim_require_approval", False)) or (
            self.live_orders and bool(cfg.approvals.get("live_pilot_every_entry_and_add", True)))
        self.banned: dict[str, str] = {}
        self.event_blackouts: list[dict[str, Any]] = []
        self.stopped_setups: dict[str, set[str]] = {}
        self.recent_stop_exits: dict[str, tuple[dt.datetime, Direction]] = {}  # D-045: underlying_key -> (ts, exposure)
        self.reentries_today: dict[str, int] = {}
        self.sleeve_used = {"expiry": 0.0, "week": 0.0, "month": 0.0}
        self.sleeve_cumulative_pnl = 0.0  # net realized P&L of the sleeve since the last operator review (D8)
        self.edge = edge_brake.evaluate([], cfg.edge_brake)
        self.process = self._new_process()
        self.journal: list[dict[str, Any]] = []
        self.period_reviews: dict[str, list[dict[str, Any]]] = {         # D-054
            pt: (self.store.period_reviews(pt) if self.store else []) for pt in ("week", "month")}
        self.vix_prev_close = self._vix_state().prev_close if self._vix_state() else 0.0
        self.straddle_open: dict[str, float] = {}
        self.last_snapshot_minute: dt.datetime | None = None
        self.sim_seconds = 0.0
        self._reject_log: dict[str, dt.datetime] = {}
        self._last_change_point = False
        self._last_expiry_regime: str | None = None  # D-056: log regime.expiry transitions, not just changes
        self._last_regime_tuple: tuple[str, str, str | None] | None = None
        self.regime_history: list[dict[str, Any]] = []  # D-058: (trend, volatility, expiry) over the session,
                                                         # for the web UI's regime timeline -- not persisted,
                                                         # same lifetime as the rest of in-memory engine state
        self.alert_channel_ok = True     # set by the Discord bot heartbeat when enabled (D10)
        self.alert_channel_required = False
        self.alert_heartbeat = self.wall()
        self._alert_email_sent = False
        self._unprotected_since: dict[str, dt.datetime] = {}
        self.scanner_rows: list[scan_mod.ScreenRow] = []
        self.scanner_cfg = {"interval_seconds": 15, "promote_max": 20, "promote_min_score": 0.55,
                            "demote_below_score": 0.35, "demote_after_minutes": 15, **(cfg.raw.get("scanner") or {})}
        self._last_scan_wall = -1e18
        self._cool_since: dict[str, dt.datetime] = {}
        self._fill_wall: dict[str, float] = {}         # LIVE: position id -> wall time of its last fill
        self._recon_seen: dict[str, float] = {}        # LIVE: position id -> Dhan net qty seen in the last snapshot
        self._recon_seq = 0
        self.ledger.start_day(self.market.day)
        self._log("system", f"Sthitaprajna started in {self.mode.value} mode"
                  + (f" (LIVE refused, missing gates: {', '.join(missing)})" if missing else ""))
        if self._restored_trades:
            self._log("system", f"D-047: restored equity Rs {self.ledger.equity:,.2f} from "
                      f"{self._restored_trades} closed trade(s) recorded in a prior session (realized P&L "
                      f"only -- any position still open at the previous shutdown was not resumed).")
        if cfg.is_unbounded:
            ub = cfg.unbounded
            self._log("system", f"Risk mode UNBOUNDED (operator decision D-031): {float(ub['capital_per_trade']):.0%} of "
                                f"equity per trade, full entry with stop-loss and target, no ceilings, gears, loss "
                                f"limits, staging or approvals")
        if self.live_orders:
            m = self.market.describe()
            self._log("system", f"LIVE on Dhan: {m['instruments']} instruments, session {m['session']} ({m['day']}). "
                                "Every entry, stop-loss and exit is a REAL order on your Dhan account (intraday). "
                                "The strategies are UNVALIDATED: there is no evidence of an edge yet.")
            self._log("system", "Stop everything: the KILL button, /kill on Discord, or close positions in the Dhan app "
                                "(Sthitaprajna reconciles with Dhan's positions)")
        elif self.live_data:
            m = self.market.describe()
            self._log("system", f"PAPER on live Dhan data: {m['instruments']} instruments, session {m['session']} "
                                f"({m['day']}); fills are simulated at bid/ask, no order reaches the exchange")
            if not m.get("secondary_configured"):
                self._log("system", "Upstox not configured: single-feed mode (A5.5 'Upstox down' row: entries "
                                    "allowed, data confidence capped at "
                                    f"{float(cfg.data['secondary_down_confidence_cap']):.1f})")
        else:
            self._log("system", f"SIM market: {self.market.day.isoformat()} regime={self.market.regime} -- synthetic data, not evidence")

    def _vix_state(self):
        return self.market.states.get("NSE:INDIAVIX")

    # ================================================================== basics
    @property
    def now(self) -> dt.datetime:
        return self.market.now

    @property
    def perf_label(self) -> PerfLabel:
        return {Mode.SIM: PerfLabel.SIMULATED, Mode.PAPER: PerfLabel.PAPER}.get(self.mode, PerfLabel.LIVE)

    def _log(self, kind: str, msg: str, **extra: Any) -> None:
        self.event_seq += 1
        e = {"ts": self.now.isoformat(), "seq": self.event_seq, "kind": kind, "msg": msg, **extra}
        self.events.append(e)
        if self.store:
            self.store.event(e)

    def _new_process(self) -> dict[str, Any]:
        return {"state_check_done": False, "approvals_answered": 0, "approvals_timed_out": 0,
                "stop_coverage_ticks": 0, "uncovered_ticks": 0, "rule_exits": 0, "other_exits": 0,
                "manual_exits": 0, "eod_flat": None}

    # ================================================================== opportunity scanner (S1 over the universe)
    def _run_scanner(self) -> None:
        self._last_scan_wall = self.wall()
        try:
            inputs = self.market.screen_inputs()
        except Exception as exc:   # the scanner must never stop the loop
            self._log("error", f"scanner inputs failed: {exc!r}")
            return
        open_ = self.cal.is_open(self.now)
        mins = self.cal.minutes_into_session(self.now) if open_ else scan_mod.SESSION_MINUTES
        self.scanner_rows = scan_mod.scan(inputs, mins)
        if open_ and self.live_data:
            self._rotate_universe()

    def _busy(self, key: str) -> bool:
        if any(p.instrument.key == key or p.underlying_key == key for p in self.positions.values()):
            return True
        if any(pe.prop.candidate.underlying_key == key for pe in self.pending_entries.values()):
            return True
        return any(not r.terminal and r.candidate.underlying_key == key for r in self.funnel.records.values())

    def _rotate_universe(self) -> None:
        """Booming stocks join the strategy universe; cooled-off ones leave it (never while in use)."""
        cfg, now = self.scanner_cfg, self.now
        rows = {r.key: r for r in self.scanner_rows}
        for key in sorted(self.market.promoted):
            r = rows.get(key)
            if r is not None and r.score >= float(cfg["demote_below_score"]):
                self._cool_since.pop(key, None)
                continue
            since = self._cool_since.setdefault(key, now)
            if (now - since).total_seconds() >= 60 * float(cfg["demote_after_minutes"]) and not self._busy(key):
                if self.market.demote(key):
                    self._cool_since.pop(key, None)
                    self._log("regime", f"Scanner: {key.split(':')[1]} cooled off, left the strategy universe")
        slots = int(cfg["promote_max"]) - len(self.market.promoted)
        for r in self.scanner_rows:
            if slots <= 0:
                break
            if r.tradable and not r.active and r.score >= float(cfg["promote_min_score"]):
                if self.market.promote(r.key):
                    slots -= 1
                    self._log("regime", f"Scanner: {r.symbol} joins the strategy universe ({r.direction}, score "
                                        f"{r.score:.2f}: {', '.join(r.flags[:4]) or 'momentum'})")

    def quote(self, key: str) -> Quote | None:
        """Best fresh quote per the arbitration table; never a stale one (A5.5)."""
        q = self.arbiter.best_quote(key)
        stale = float(self.cfg.data["stale_after_seconds"])
        if q is not None and (self.now - q.exchange_ts).total_seconds() <= stale:
            return q
        if self.arbiter.situation == "BOTH_DOWN":
            return None
        if key.startswith(("NFO:", "BFO:")):
            q = self.market.quote(key)       # option quotes on demand (chain / quote poll in live)
            if q is not None and self.live_data and (self.now - q.exchange_ts).total_seconds() > 3 * stale:
                return None
            return q
        return None

    def sector_change(self, sector: str) -> float:
        chs = [st.price / st.prev_close - 1 for st in self.market.states.values()
               if st.spec.sector == sector and st.spec.kind is Kind.EQUITY and st.prev_close > 0]
        return sum(chs) / len(chs) if chs else 0.0

    def unrealized(self) -> tuple[float, float]:
        u = sum(p.unrealized() for p in self.positions.values())
        ch = sum(p.charges for p in self.positions.values())
        return u, ch

    def mtm(self) -> float:
        u, ch = self.unrealized()
        return self.ledger.mtm(u, ch)

    # ================================================================== the loop
    def tick(self, real_dt: float = 0.25) -> None:
        if self.paused:
            return
        sim_dt = min(real_dt * self.speed, 120.0)
        events = self.market.step(sim_dt)
        if self.live_data:
            for kind, msg in self.market.drain_notes():
                self._log(kind, msg)
        if self.wall() - self._last_scan_wall >= float(self.scanner_cfg["interval_seconds"]):
            self._run_scanner()
        self.sim_seconds += sim_dt
        if "close" in events:
            self._end_of_day()
        if "open" in events:
            self._start_of_day()
            return
        self._process_market(sim_dt, "minute" in events)

    def run_until(self, until: dt.datetime, step_seconds: float = 15.0) -> None:
        """Deterministic fast-forward (backtests/tests): same code path, no wall clock needed."""
        saved = self.speed
        self.speed = step_seconds / 0.25
        while self.now < until:
            self.tick(0.25)
            self._expire_approvals()
        self.speed = saved

    def _watch_keys(self) -> list[str]:
        keys = list(self.market.index_keys) + list(self.market.stock_keys)
        for p in self.positions.values():
            keys.append(p.instrument.key)
        for pe in self.pending_entries.values():
            keys.append(pe.prop.candidate.instrument.key)
        for s in self.shadows:
            if not s.done:
                keys.append(s.pos.instrument.key)
        return list(dict.fromkeys(keys))

    def _process_market(self, sim_dt: float, minute: bool) -> None:
        now = self.now
        if self.live_data and not self.cal.is_open(now):
            if self.live_orders:                   # Dhan may still report late fills or its own square-off
                self._service_broker({}, now)
            self._expire_approvals()               # market closed: nothing trades, feeds are idle by design
            return
        keys = self._watch_keys()
        pq = self.primary.poll(keys)
        sq = self.secondary.poll(keys)
        self.arbiter.update(now, pq, sq)
        if self.arbiter.situation in ("BOTH_AGREE", "DISAGREE", "SECONDARY_DOWN"):
            self.quality.check(now, pq, [k for k in keys if not k.startswith(("NFO:", "BFO:"))],
                               set(self.market.index_keys))
        quotes = {k: q for k in keys if (q := self.quote(k)) is not None}

        self._flush_orders()          # submit anything queued since the last tick (operator actions)
        # the paper broker emulates the EXCHANGE: it fills and triggers resting stops on true prices,
        # whether or not OUR feeds are up (that is exactly why broker-side stops exist)
        order_keys = {o.instrument.key for o in list(self.broker.pending.values()) + list(self.broker.resting.values())}
        truth = {k: q for k in order_keys if (q := self.market.quote(k)) is not None}
        self._service_broker(truth, now)
        self._manage_positions(quotes)
        self._manage_shadows(quotes)
        self._operational_checks()
        if minute:
            self._on_minute()
        self._expire_approvals()

        u, ch = self.unrealized()
        mtm = self.ledger.mark(u, ch)
        bounded = not self.cfg.is_unbounded
        ev = self.gears.on_equity(mtm, now) if bounded else None
        if ev:
            self._log("gear", f"{ev.frm.value} -> {ev.to.value}: {ev.reason}")
            if ev.to is Gear.HALTED:
                self._log("risk", "HALTED at the 25% drawdown ceiling: no new live trades until operator review")
        floor = ceilings.ruin_floor(self.ledger.all_time_peak, self.cfg.ruin_floor_abs, self.cfg.ruin_floor_pct)
        if bounded and mtm <= floor and not self.ruined:
            self.ruined = True
            self._log("risk", f"RUIN FLOOR breached ({mtm:,.0f} <= {floor:,.0f}): full shutdown")
            self._flatten_all("FLATTEN")
        pnl = self.ledger.period_pnl(u, ch)
        self.targets.update(pnl, {"day": self.ledger.day_start, "week": self.ledger.week_start,
                                  "month": self.ledger.month_start})
        if bounded and pnl["day"] <= -float(self.cfg.limits["daily_loss"]) * self.ledger.equity_sod and not self.ledger.daily_loss_breached:
            self.ledger.daily_loss_breached = True
            self._log("risk", "daily loss limit reached: no new entries today")
        ctx_limit = self.guardian.open_risk_limit(self._risk_ctx())
        open_risk = sum(p.open_risk for p in self.positions.values())
        queued_a = sum(1 for r in self.funnel.records.values()
                       if r.status in ("READY", "ARMED", "PENDING_APPROVAL") and r.grade in (Grade.A_PLUS, Grade.A))
        if self.cal.is_open(now):
            self.kpis.on_step(sim_dt, open_risk, ctx_limit, queued_a)
            self.gears.accumulate(sim_dt)
            for p in self.positions.values():
                if p.broker_stop_id and p.broker_stop_id in self.broker.resting:
                    self.process["stop_coverage_ticks"] += 1
                elif p.qty > 0:
                    self.process["uncovered_ticks"] += 1
        m = now.replace(second=0, microsecond=0)
        if m != self.last_snapshot_minute:
            self.last_snapshot_minute = m
            self.ledger.snapshot(now, mtm, self.gears.drawdown(mtm), self.gears.effective_gear().value)
            if self.store and m.minute % 5 == 0:
                self.store.equity(now, mtm, self.gears.drawdown(mtm), self.gears.effective_gear().value)

    # ================================================================== minute: regime, scan, funnel
    def _on_minute(self) -> None:
        self._update_regime()
        mins = self.cal.minutes_into_session(self.now)
        if int(round(mins)) % 5 == 0:
            ctx = _FeatureCacheCtx(self)
            cands: list[Candidate] = []
            for s in self.strategies:
                try:
                    cands.extend(s.scan(ctx))
                except Exception as exc:  # a broken strategy must never crash the loop (A5.5 rule 7)
                    self._log("error", f"strategy {s.name} failed: {exc!r}")
            cands = self._apply_reentry(cands)
            self.funnel.ingest(cands, self.now)
        ctx = self._risk_ctx()
        ready = self.funnel.evaluate(_Env(self, ctx))
        if ready:
            self._deploy(ready)

    def _update_regime(self) -> None:
        nifty = self.market.candles("NSE:NIFTY")
        c5 = ind.resample(nifty[-375 * 2:], 5)
        trend, strength, conf = rg.trend_regime(c5)
        vst = self._vix_state()
        vix = vst.price if vst else 0.0
        b = self.market.breadth()
        r = self.regime
        r.trend, r.trend_strength = trend, strength
        if vix > 0:                                # no VIX print yet (live start): keep the last volatility read
            vol, tags = rg.vol_regime(vix, self.vix_prev_close)
            r.volatility, r.vol_tags = vol, tags
            r.vix, r.vix_change = vix, (vix / self.vix_prev_close - 1) if self.vix_prev_close else 0.0
        r.breadth_ad = b["ad_ratio"]
        r.change_point = rg.change_point(nifty)
        r.confidence = conf
        # expiry regime for the underlying expiring today (Nifty Tue / Sensex Thu)
        r.expiry = None
        for und in (() if self.cash_only else ("NIFTY", "SENSEX")):
            if self.cal.is_expiry_day(und, self.now.date()):
                spot, rows = self.market.chain(und)
                s = chain_summary(rows, spot)
                so = self.straddle_open.setdefault(und, s["straddle"]["straddle"])
                walls = s["walls"]
                broke = bool(walls["resistance"] and spot > walls["resistance"][0]["strike"]) or \
                    bool(walls["support"] and spot < walls["support"][0]["strike"])
                r.expiry = rg.expiry_regime(True, spot, s["max_pain"], s["straddle"]["straddle"], so, trend, broke,
                                            r.vix_change)
                break
        if r.expiry != self._last_expiry_regime:
            # D-056: previously silent -- the operator had no way to tell, after the fact, whether an
            # expiry afternoon with zero Zero-to-Hero trades meant "no GAMMA_ACCELERATION/BREAKOUT_EXPIRY/
            # TREND_EXPIRY ever classified" (expected: most tickets expire worthless, D8) or a real gap.
            self._log("regime", f"expiry regime -> {r.expiry or 'none (not an index expiry day, or too early)'}")
            self._last_expiry_regime = r.expiry
        if r.change_point and not self._last_change_point:
            self._log("regime", "change point detected in index volatility: confidence reduced, unusual-entry approvals on")
        self._last_change_point = r.change_point
        # D-058: a compact timeline of (trend, volatility, expiry) over the session, for the web UI's
        # regime strip -- recorded only on change, same discipline as the expiry-regime log above, so a
        # day's worth of ticks doesn't turn into thousands of identical points.
        now_tuple = (r.trend, r.volatility, r.expiry)
        if now_tuple != self._last_regime_tuple:
            self.regime_history.append({"ts": self.now.isoformat(), "trend": r.trend, "volatility": r.volatility,
                                        "expiry": r.expiry})
            self.regime_history = self.regime_history[-3000:]
            self._last_regime_tuple = now_tuple

    def _apply_reentry(self, cands: list[Candidate]) -> list[Candidate]:
        """AGG-1 #6: after a stop-out, one re-entry per setup per day, only on a FRESH trigger, probe size."""
        out = []
        for c in cands:
            rec = self.funnel.records.get(c.setup_id)
            if rec is not None and rec.terminal and c.setup_id in self.stopped_setups:
                used = self.reentries_today.get(c.setup_id, 0)
                fresh = c.signal_bar not in self.stopped_setups[c.setup_id]
                cap = 10**6 if self.cfg.is_unbounded else int(self.cfg.staging["reentry_max_per_setup_per_day"])
                if used < cap and fresh and c.trigger_fired:
                    re = copy.copy(c)
                    re.is_reentry = True
                    re.setup_id = f"{c.setup_id}:re{used + 1}"
                    re.explanation = "RE-ENTRY (fresh trigger, probe size): " + c.explanation
                    out.append(re)
                continue
            out.append(c)
        return out

    # ================================================================== risk context
    def _entry_window_open(self) -> bool:
        mins = self.cal.minutes_into_session(self.now)
        exit_min = (dt.datetime.combine(self.now.date(), self.cal.intraday_exit) -
                    dt.datetime.combine(self.now.date(), self.cal.session_open)).total_seconds() / 60
        return self.cal.is_open(self.now) and 5 <= mins <= exit_min - 30

    def _position_views(self) -> list[PositionView]:
        views = [PositionView.of(p, self._margin_of(p.segment, p.avg_entry, p.qty)) for p in self.positions.values()]
        for pe in self.pending_entries.values():
            c = pe.prop.candidate
            views.append(PositionView(pe.order_id, c.underlying_key, c.exposure, c.segment, pe.prop.grade,
                                      pe.prop.risk_now, pe.prop.notional_now, pe.prop.margin_now, c.beta, c.sleeve,
                                      c.segment in (Segment.INDEX_OPTION, Segment.INDEX_FUTURE)))
        return views

    def _margin_of(self, segment: Segment, price: float, qty: int) -> float:
        notional = abs(price * qty)
        if segment is Segment.EQ_INTRADAY:
            if self.cfg.is_unbounded:
                return notional / max(1.0, float(self.cfg.unbounded["intraday_leverage"]))
            return notional * MIS_MARGIN_FRACTION
        return notional   # long premium / delivery: full outlay

    def capital_in_use(self) -> float:
        used = sum(self._margin_of(p.segment, p.avg_entry, p.qty) for p in self.positions.values())
        return used + sum(pe.prop.margin_now for pe in self.pending_entries.values())

    def free_capital(self) -> float:
        free = max(0.0, self.ledger.equity - self.capital_in_use())
        avail = self.broker.available_funds if self.live_orders else None
        return free if avail is None else max(0.0, min(free, avail))   # LIVE: never more than Dhan says is free

    def _risk_ctx(self) -> RiskContext:
        u, ch = self.unrealized()
        mtm = self.ledger.mtm(u, ch)
        pnl = self.ledger.period_pnl(u, ch)
        tier = self.tiers.effective()
        halts = list(self.quality.halts.values())
        if self.broker.reject_count(self.now) >= int(self.cfg.execution["reject_storm_threshold"]):
            halts.append("order-reject storm")
        if not self.arbiter.entries_allowed():
            halts.append(f"feeds: {self.arbiter.situation}")
        if self.ruined and not self.cfg.is_unbounded:
            halts.append("ruin floor shutdown")
        if not self.cfg.is_unbounded and self.alert_channel_required and self.wall() - self.alert_heartbeat > float(self.cfg.discord.get("unreachable_email_after_seconds", 120)):
            halts.append("alert channel (Discord) unreachable")
            if self.positions and not self._alert_email_sent:
                from sthitaprajna.notify import send_email
                self._alert_email_sent = send_email("Sthitaprajna: Discord unreachable",
                                                    f"No Discord heartbeat for > 2 min with {len(self.positions)} open positions. "
                                                    "New entries are paused; broker-side stops remain in place.")
        blocked = dict(self.quality.blocked)
        for k in self.arbiter.disagreeing:
            blocked[k] = "feeds disagree"
        blackout = next((b["name"] for b in self.event_blackouts
                         if b["start"] <= self.now <= b["end"]), None)
        return RiskContext(
            mode=self.mode, now=self.now, equity_sod=self.ledger.equity_sod, equity_mtm=mtm,
            all_time_peak=self.ledger.all_time_peak, gear=self.gears.effective_gear(),
            drawdown=self.gears.drawdown(mtm), tier=tier, day_pnl=pnl["day"], day_peak_pnl=self.ledger.day_peak_pnl,
            week_pnl=pnl["week"], month_pnl=pnl["month"], week_start_equity=self.ledger.week_start,
            month_start_equity=self.ledger.month_start, positions=self._position_views(),
            trades_today=self.ledger.trades_today, losing_streak=self.ledger.losing_streak,
            cooldown_until=self.ledger.cooldown_until, kill_switch=self.kill_switch, operational_halts=halts,
            blocked_instruments=blocked, event_blackout=blackout, target_blocks=self.targets.entry_blocks(),
            b_allowed=self.targets.b_grade_allowed, reentries_today=dict(self.reentries_today),
            stopped_signal_bars={k: set(v) for k, v in self.stopped_setups.items()},
            sleeve_used=dict(self.sleeve_used), sleeve_cumulative_pnl=self.sleeve_cumulative_pnl,
            edge_ceiling=self.edge.ceiling_r,
            entry_window_open=self._entry_window_open(), free_capital=self.free_capital(),
        )

    # ================================================================== S6 / S7: deploy
    def _proposal(self, rec: FunnelRecord) -> EntryProposal:
        c, s = rec.candidate, rec.sizing
        assert s is not None and rec.ev is not None and rec.grade is not None
        qty = s.probe_qty
        risk_now = s.per_unit_risk * qty
        return EntryProposal(c, rec.grade, s, qty, risk_now, c.entry * qty,
                             self._margin_of(c.segment, c.entry, qty), rec.ev)

    def _approval_required(self, rec: FunnelRecord) -> bool:
        if self.cfg.is_unbounded:
            return self.pilot_approvals          # the operator's own toggle; off by default
        if self.mode is Mode.LIVE_PILOT:
            return True
        c = rec.candidate
        unusual = self.regime.change_point or "VOL_EXPANSION" in self.regime.vol_tags
        if self.mode is Mode.LIVE_FULL:
            return c.sleeve or unusual
        return self.pilot_approvals or (self.mode is Mode.PAPER and c.sleeve)

    def _deploy(self, ready: list[FunnelRecord]) -> None:
        # D-046: unbounded mode has no correlation-bucket check (that's bounded-only, risk/guardian.py's
        # check_entry), so without this, several strategies agreeing on the same underlying+direction in
        # the same cycle (e.g. index_option_momentum and day_range_breakout both buying NIFTY PE at once)
        # would each independently commit a full capital_per_trade slice to what is really one correlated
        # bet. One bet per underlying+direction per deploy cycle in unbounded mode; bounded mode already
        # has its own, more complete correlation check and doesn't need this.
        committed_unbounded: set[tuple[str, Direction]] = set()
        for rec in ready:
            if rec.status != "READY":
                continue
            if self.cfg.is_unbounded:
                key = (rec.candidate.underlying_key, rec.candidate.exposure)
                if key in committed_unbounded:
                    self.funnel.reject(rec, f"unbounded: already committed to a {key[1].value} bet on "
                                            f"{key[0]} this cycle (correlation cap, D-046)")
                    continue
                # earlier entries this minute already took their capital
                s = self.funnel._sizing(rec.candidate, _Env(self, self._risk_ctx()), rec.grade or Grade.B, {})
                if not s.ok:
                    self.funnel.reject(rec, f"capital_fits: {s.skip_reason}")
                    continue
                rec.sizing = s
                if rec.plan is not None:           # the plan follows the size actually sent
                    rec.plan.probe_qty = rec.plan.full_qty = s.full_qty
            prop = self._proposal(rec)
            verdict = self.guardian.check_entry(self._risk_ctx(), prop)
            rec.verdict = verdict.to_dict()
            if not verdict.approved:
                self.funnel.reject(rec, verdict.reason, rec.verdict)
                continue
            if self.cfg.is_unbounded:
                committed_unbounded.add((rec.candidate.underlying_key, rec.candidate.exposure))
            if self._approval_required(rec):
                self._request_approval(rec, prop)
                continue
            self._enter(rec, prop, operator="NOT_REQUIRED")

    def _summary(self, rec: FunnelRecord, prop: EntryProposal, stage: str = "PROBE") -> dict[str, Any]:
        c = rec.candidate
        return {"strategy": c.strategy, "symbol": c.instrument.symbol, "instrument": c.instrument.key,
                "direction": c.direction.value, "exposure": c.exposure.value, "grade": prop.grade.value,
                "gear": self.gears.effective_gear().value, "stage": stage, "qty": prop.qty,
                "full_qty": prop.sizing.full_qty, "entry": c.entry, "stop": c.stop,
                "harvest": f"{self.cfg.staging['harvest_fraction']:.0%} at +{self.cfg.staging['harvest_at_r']:.0f}R, trail rest",
                "risk_rupees": round(prop.risk_now, 2), "planned_risk": round(prop.sizing.planned_risk, 2),
                "ev_r": round(prop.ev.net_r, 3), "confidence": round(rec.confidence.effective, 3) if rec.confidence else None,
                "quality": round(rec.quality or 0, 1), "reason": c.explanation, "is_reentry": c.is_reentry,
                "sleeve": c.sleeve, "p_calibrated": c.p_win_calibrated}

    def _request_approval(self, rec: FunnelRecord, prop: EntryProposal) -> None:
        w = self.wall()
        kind = "SLEEVE" if rec.candidate.sleeve else ("REENTRY" if rec.candidate.is_reentry else "ENTRY")
        a = Approval(new_id("apr"), kind, w, w + float(self.cfg.approvals["timeout_seconds"]),
                     self._summary(rec, prop), setup_id=rec.candidate.setup_id)
        self.approvals[a.id] = a
        rec.status = "PENDING_APPROVAL"
        rec.reason = "awaiting operator approval (60 s, timeout = skip)"
        self._log("approval", f"approval requested: {kind} {rec.candidate.instrument.symbol} ({prop.grade.value})",
                  approval_id=a.id)

    def _enter(self, rec: FunnelRecord, prop: EntryProposal, operator: str) -> None:
        c = rec.candidate
        q = self.quote(c.instrument.key)
        if q is None:
            self.funnel.reject(rec, "no quote at entry")
            return
        px = q.ask if c.direction is Direction.LONG else q.bid
        drift = c.direction.sign * (px - c.entry)
        if drift > 0.15 * c.unit_risk:
            self.funnel.reject(rec, f"price left the entry zone (+{drift / c.unit_risk:.2f}R): no chasing")
            return
        side = Side.BUY if c.direction is Direction.LONG else Side.SELL
        o = new_order("pending", c.instrument, c.segment, side, prop.qty, OrderType.MARKET, "PROBE", self.now,
                      self.algo_id, model_price=c.entry)
        self.orders[o.id] = o
        self.pending_entries[o.id] = PendingEntry(rec, prop, o.id, self.now)
        self.order_queue.append(o)
        self.funnel.probed(rec)
        d = decision_object(rec=rec, action="ENTER", mode=self.mode, gear=self.gears.effective_gear(),
                            edge_ceiling=self.edge.ceiling_r, tier=self.tiers.effective().name,
                            drawdown_state=self.gears.effective_gear().value, now=self.now,
                            rules_version=self.rules.version, data_sources=["DHAN(sim)", "UPSTOX(sim)"],
                            feed_agreement=self.arbiter.feed_agreement(),
                            extra={"risk_approval": "APPROVED", "operator_approval": operator,
                                   "position_stage": "PROBE", "algo_id": self.algo_id,
                                   "grade_multiplier": self.cfg.grades[prop.grade].multiplier,
                                   "capital_used": round(prop.notional_now, 2), "order_id": o.id})
        rec.decision_id = d["decision_id"]
        self._record_decision(d)
        if self.cfg.is_unbounded:
            tgt = c.entry + c.direction.sign * c.target_r * c.unit_risk
            self._log("entry", f"{side.value} {c.instrument.symbol} qty {prop.qty} @~{px:.2f}: stop-loss {c.stop:.2f}, "
                               f"target {tgt:.2f} (capital Rs {prop.margin_now:,.0f}, {c.strategy})")
        else:
            self._log("entry", f"PROBE {c.instrument.symbol} {prop.grade.value} qty {prop.qty} @~{px:.2f} stop {c.stop:.2f}"
                      f" (risk Rs {prop.risk_now:,.0f})")

    def _record_decision(self, d: dict[str, Any]) -> None:
        self.decisions.append(d)
        if self.store:
            self.store.decision(d)

    def _on_funnel_terminal(self, rec: FunnelRecord) -> None:
        if rec.status in ("DROPPED", "REJECTED"):
            self.kpis.on_missed(rec.grade, rec.stage.value, rec.reason, rec.candidate.instrument.symbol)
            # only log decision objects for candidates that reached grading (keeps the log meaningful)
            if rec.grade is not None or rec.status == "REJECTED":
                d = decision_object(rec=rec, action="NO_TRADE", mode=self.mode, gear=self.gears.effective_gear(),
                                    edge_ceiling=self.edge.ceiling_r, tier=self.tiers.effective().name,
                                    drawdown_state=self.gears.effective_gear().value, now=self.now,
                                    rules_version=self.rules.version, feed_agreement=self.arbiter.feed_agreement(),
                                    extra={"risk_approval": "REJECTED" if rec.status == "REJECTED" else ""})
                rec.decision_id = d["decision_id"]
                self._record_decision(d)

    # ================================================================== approvals
    def _expire_approvals(self) -> None:
        w = self.wall()
        for a in list(self.approvals.values()):
            if a.status == "PENDING" and w >= a.expires_wall:
                a.status = "TIMEOUT"
                self.process["approvals_timed_out"] += 1
                self._resolve_approval(a, approved=False, how="TIMEOUT")

    def approve(self, approval_id: str, approved: bool) -> dict[str, Any]:
        a = self.approvals.get(approval_id)
        if a is None or a.status != "PENDING":
            return {"ok": False, "error": "no pending approval with that id"}
        a.status = "APPROVED" if approved else "DENIED"
        self.process["approvals_answered"] += 1
        return self._resolve_approval(a, approved, "APPROVED" if approved else "DENIED")

    def _resolve_approval(self, a: Approval, approved: bool, how: str) -> dict[str, Any]:
        self._log("approval", f"{a.kind} {a.summary.get('symbol')} -> {how}")
        if a.kind in ("CONFIRM_ADD", "PRESS"):
            pos = self.positions.get(a.position_id or "")
            if approved and pos is not None:
                return self._execute_add(pos, a.add_plan, a.kind == "PRESS", operator=how)
            return {"ok": True, "result": how}
        rec = self.funnel.records.get(a.setup_id or "")
        if rec is None or rec.status != "PENDING_APPROVAL":
            return {"ok": False, "error": "candidate no longer pending"}
        if not approved:
            self.funnel.reject(rec, f"operator {how.lower()} (safe default: skip)")
            return {"ok": True, "result": how}
        prop = self._proposal(rec)
        verdict = self.guardian.check_entry(self._risk_ctx(), prop)   # state may have changed
        rec.verdict = verdict.to_dict()
        if not verdict.approved:
            self.funnel.reject(rec, verdict.reason, rec.verdict)
            return {"ok": False, "error": f"Risk Guardian now rejects: {verdict.reason}"}
        rec.status = "READY"
        self._enter(rec, prop, operator="APPROVED")
        return {"ok": True, "result": "APPROVED"}

    # ================================================================== orders & fills
    def _flush_orders(self) -> None:
        if not self.order_queue:
            return
        ordered = sorted(self.order_queue, key=lambda o: 0 if o.is_exit else 1)   # exits first
        self.order_queue.clear()
        t = self.wall() if self.speed <= 1 else self.sim_seconds
        for o in ordered:
            if not self.ops.allow(t):
                self.order_queue.append(o)
                continue
            slices = slice_for_freeze(o.qty, o.instrument.lot_size, o.instrument.freeze_qty)
            if len(slices) > 1:
                self._log("exec", f"order {o.id} sliced for freeze qty: {slices}")
            ok = self.broker.submit(o, self.now)
            if not ok:
                self._on_order_failed(o)

    def _on_order_failed(self, o: Order) -> None:
        """An order that will not fill (rejected, cancelled, never placed): undo whatever waited on it."""
        last = self._reject_log.get(o.position_id)
        if last is None or (self.now - last).total_seconds() > 30:
            self._reject_log[o.position_id] = self.now
            word = "rejected" if o.status is OrderStatus.REJECTED else "not filled"
            self._log("exec", f"order {o.id} {o.purpose} {word}: {o.reject_reason}")
        if o.id in self.pending_entries:
            pe = self.pending_entries.pop(o.id)
            self._log("exec", f"entry for {pe.rec.candidate.instrument.symbol} not filled ({o.reject_reason or 'reject'})")
        if o.position_id in self.exit_orders and self.exit_orders[o.position_id] == o.id:
            del self.exit_orders[o.position_id]     # exit will be retried next tick
        self.pending_adds.pop(o.id, None)

    def _service_broker(self, truth: dict[str, Quote], now: dt.datetime) -> None:
        for f in self.broker.process(truth, now):   # paper: fills only on a quote newer than the order
            self._on_fill(f)
        if self.live_orders:
            for o in self.broker.drain_failed():
                self._on_order_failed(o)
            while self.broker.notes:
                self._log("exec", self.broker.notes.pop(0))
            self._reconcile_broker()

    def _place_broker_stop(self, p: Position) -> None:
        side = Side.SELL if p.direction is Direction.LONG else Side.BUY
        o = new_order(p.id, p.instrument, p.segment, side, p.qty, OrderType.SL_M, "BROKER_STOP", self.now,
                      self.algo_id, trigger=p.broker_stop, model_price=p.broker_stop)
        self.orders[o.id] = o
        if self.broker.submit(o, self.now):
            p.broker_stop_id = o.id
        else:
            self._log("risk", f"broker-side stop for {p.instrument.symbol} could not be placed: {o.reject_reason}")

    def _sync_broker_stop(self, p: Position) -> None:
        if not p.broker_stop_id:
            self._place_broker_stop(p)
            return
        buf = self._broker_buffer(p.instrument.tick_size, p.unit_risk)
        # D-058: tick-aligned, not just 2-decimal -- a trigger off the instrument's real tick grid (0.05
        # for options) can be rejected at the exchange in LIVE mode. `down=p.sign > 0` rounds the way
        # `_type_price()` already does for the matching close side (SELL for a long, BUY for a short):
        # toward giving the position a touch more room, never tighter than the buffer computed.
        new_trigger = round_tick(p.stop - p.sign * buf, p.instrument.tick_size, down=p.sign > 0)
        if p.sign * (new_trigger - p.broker_stop) > 0:
            if self.broker.modify_stop(p.broker_stop_id, new_trigger, self.now):
                p.broker_stop = new_trigger
        self.broker.modify_qty(p.broker_stop_id, p.qty, self.now)

    def _broker_buffer(self, tick: float, unit_risk: float) -> float:
        """Broker-side stop rests beyond the system stop: max(N ticks, x R) so the system stop stays primary."""
        return max(float(self.cfg.execution["broker_stop_buffer_ticks"]) * tick,
                   float(self.cfg.execution.get("broker_stop_buffer_r", 0.15)) * unit_risk)

    def _on_fill(self, f: Fill) -> None:
        o = f.order
        if o.purpose == "PROBE":
            pe = self.pending_entries.pop(o.id, None)
            if pe is None:
                if self.live_orders:
                    self._adopt_orphan(f)
                return
            self._open_position(pe, f)
            return
        p = self.positions.get(o.position_id)
        if p is None:
            return
        p.charges += f.charges
        self._fill_wall[p.id] = self.wall()
        if o.purpose in ("CONFIRM_ADD", "PRESS"):
            _pid, plan = self.pending_adds.pop(o.id, (None, None))
            p.legs.append(Leg(f.qty, f.price, f.ts, plan.stage if plan else PositionStage.PRESS_1))
            if plan is not None:
                if p.stop_is_better(plan.new_stop):
                    p.stop = plan.new_stop
                p.stage = plan.stage
                if o.purpose == "PRESS":
                    p.presses += 1
                p.stage_history.append({"ts": f.ts.isoformat(), "to": plan.stage.value, "qty": f.qty,
                                        "price": round(f.price, 2), "rule": plan.rule})
            # if the add filled worse than planned, pull the whole position back inside its rule
            if p.open_profit_r(f.price) >= self.cfg.staging["press_lock_from_r"]:
                req = p.avg_entry + p.sign * self.cfg.staging["press_lock_fraction"] * max(0.0, p.unrealized(f.price)) / p.qty
            else:
                req = p.avg_entry - p.sign * p.planned_risk / p.qty
            if p.stop_is_better(req) and p.sign * (f.price - req) > 0:
                p.stop = round(req, 2)
            self._sync_broker_stop(p)
            self._log("stage", f"{p.instrument.symbol} {o.purpose} {f.qty} @ {f.price:.2f}; stop {p.stop:.2f}")
            return
        # exits
        pnl = p.sign * (f.price - p.avg_entry) * f.qty
        reason = o.purpose if o.purpose != "EXIT" else "EXIT"
        p.closed_legs.append(ClosedLeg(f.qty, f.price, f.ts, reason, pnl))
        if o.purpose == "BROKER_STOP":
            p.broker_stop_id = ""
            ex = self.exit_orders.pop(p.id, None)
            if ex:
                self.broker.cancel(ex, self.now)
        elif self.exit_orders.get(p.id) == o.id:
            del self.exit_orders[p.id]
        if o.purpose == "MANUAL_REDUCE" and p.qty > 0:
            p.stage_history.append({"ts": f.ts.isoformat(), "to": p.stage.value, "note": f"manual reduce {f.qty}"})
            self._sync_broker_stop(p)
        if o.purpose == "HARVEST":
            p.harvested = True
            p.stage = PositionStage.HARVEST
            p.stage_history.append({"ts": f.ts.isoformat(), "to": "HARVEST", "qty": f.qty, "price": round(f.price, 2)})
            self._sync_broker_stop(p)
            self._log("stage", f"{p.instrument.symbol} HARVEST {f.qty} @ {f.price:.2f} (+{pnl:,.0f})")
        if p.qty <= 0:
            self._close_position(p, reason)

    def _open_position(self, pe: PendingEntry, f: Fill) -> None:
        rec, prop, c = pe.rec, pe.prop, pe.rec.candidate
        plan = rec.plan
        assert plan is not None
        unit = abs(f.price - c.stop)
        buf = self._broker_buffer(c.instrument.tick_size, unit)
        p = Position(
            id=new_id("pos"), candidate_id=rec.id, strategy=c.strategy, instrument=c.instrument,
            underlying_key=c.underlying_key, direction=c.direction, segment=c.segment, grade=prop.grade,
            gear_at_entry=self.gears.effective_gear(), mode=self.mode.value, planned_risk=prop.sizing.planned_risk,
            unit_risk=unit, initial_stop=c.stop, stop=c.stop,
            broker_stop=round_tick(c.stop - c.direction.sign * buf, c.instrument.tick_size, down=c.direction.sign > 0),
            staging_mode=plan.staging_mode, full_qty_target=plan.full_qty,
            confirm_trigger=f.price + c.direction.sign * self.cfg.staging["confirm_add_min_r"] * unit,
            confirm_deadline=plan.confirm_deadline, opened_at=f.ts, legs=[Leg(f.qty, f.price, f.ts, PositionStage.PROBE)],
            charges=f.charges, setup_id=c.setup_id, signal_bar=c.signal_bar, is_reentry=c.is_reentry,
            sleeve=c.sleeve, defined_risk=c.defined_risk, trail_atr=c.atr, trigger_ts=pe.submitted,
            beta=c.beta, bias=c.bias,
        )
        if self.cfg.is_unbounded and c.target_r > 0:
            p.target = round(f.price + c.direction.sign * c.target_r * unit, 2)
        p.mark(f.price)
        p.stage_history.append({"ts": f.ts.isoformat(), "to": "PROBE", "qty": f.qty, "price": round(f.price, 2)})
        f.order.position_id = p.id
        self.positions[p.id] = p
        self.ledger.trades_today += 1
        if c.is_reentry:
            base = c.setup_id.rsplit(":re", 1)[0]
            self.reentries_today[base] = self.reentries_today.get(base, 0) + 1
        if c.sleeve:
            for k in self.sleeve_used:
                self.sleeve_used[k] += prop.risk_now
        # a fill outside the plan (gap on entry) is pulled back inside it, or exited at once (bounded mode)
        wc = p.worst_case_at_stop() + p.charges
        limit = prop.sizing.planned_risk
        if not self.cfg.is_unbounded and wc > limit + 1e-6:
            req = p.avg_entry - p.sign * (limit - p.charges) / p.qty
            if p.sign * (f.price - req) > staging.min_stop_gap(p) and p.stop_is_better(req):
                p.stop = round(req, 2)
                self._log("risk", f"{p.instrument.symbol}: fill outside plan, stop tightened to {p.stop:.2f}")
            else:
                self._place_broker_stop(p)
                self._exit(p, p.qty, "FILL_OUTSIDE_PLAN")
                return
        self._place_broker_stop(p)
        self._fill_wall[p.id] = self.wall()
        if self.cfg.is_unbounded:
            self._log("fill", f"{p.instrument.symbol} filled {f.qty} @ {f.price:.2f}; stop-loss {p.stop:.2f} "
                              f"(broker {p.broker_stop:.2f}), target {p.target:.2f}")
        else:
            self._log("fill", f"{p.instrument.symbol} probe filled {f.qty} @ {f.price:.2f}; broker stop {p.broker_stop:.2f}")
        if self.kill_switch:                   # an entry that filled while the kill switch was being thrown
            self._log("risk", f"{p.instrument.symbol}: filled after the kill switch -> closing it now")
            self._exit(p, p.qty, "KILL")

    def _exit(self, p: Position, qty: int, reason: str) -> None:
        if qty <= 0 or p.id in self.exit_orders:
            return
        if self.live_orders and self.broker.exit_wait(p.id) > 0:
            return                             # Dhan refused the last exit moments ago; the stop still protects
        side = Side.SELL if p.direction is Direction.LONG else Side.BUY
        purpose = reason
        o = new_order(p.id, p.instrument, p.segment, side, qty, OrderType.MARKET, purpose, self.now, self.algo_id,
                      model_price=p.stop if reason == "STOP" else p.last_price)
        self.orders[o.id] = o
        self.exit_orders[p.id] = o.id
        self.order_queue.appendleft(o)
        self._flush_orders()

    def _close_position(self, p: Position, reason: str) -> None:
        p.closed_at = self.now
        p.exit_reason = reason
        p.stage = PositionStage.EXIT
        p.stage_history.append({"ts": self.now.isoformat(), "to": "EXIT", "reason": reason})
        if p.broker_stop_id:
            self.broker.cancel(p.broker_stop_id, self.now)
            p.broker_stop_id = ""
        net = p.realized - p.charges
        r = net / p.planned_risk if p.planned_risk > 0 else 0.0
        if p.sleeve:
            self.sleeve_cumulative_pnl += net
        trade = {"id": p.id, "symbol": p.instrument.symbol, "strategy": p.strategy, "grade": p.grade.value,
                 "direction": p.direction.value, "exposure": p.exposure.value, "net": round(net, 2), "r": round(r, 3),
                 "reason": reason, "opened": p.opened_at.isoformat(), "closed": self.now.isoformat(),
                 "stages": [h.get("to") for h in p.stage_history], "charges": round(p.charges, 2),
                 "label": self.perf_label.value, "sleeve": p.sleeve, "segment": p.segment.value}
        self.ledger.record_close(net, r, trade, self.now, int(self.cfg.limits["streak_cooldown_losses"]),
                                 int(self.cfg.limits["streak_cooldown_minutes"]))
        if not self.cfg.is_unbounded and self.ledger.losing_streak == int(self.cfg.limits["streak_cooldown_losses"]):
            self._log("risk", f"{self.ledger.losing_streak} losses in a row: 30-minute cooldown")
        if not self.cfg.is_unbounded and self.ledger.losing_streak >= int(self.cfg.limits["streak_stop_losses"]):
            self._log("risk", "4 losses in a row: done for the day")
        self.kpis.on_close(p)
        self.calibration.add(self._p_of(p), net > 0)
        if reason in RULE_EXITS:
            self.process["rule_exits"] += 1
        elif reason == "MANUAL_CLOSE":
            self.process["manual_exits"] += 1
        else:
            self.process["other_exits"] += 1
        if reason in ("STOP", "BROKER_STOP"):
            base = p.setup_id.rsplit(":re", 1)[0]
            self.stopped_setups.setdefault(base, set()).add(p.signal_bar)
            self.recent_stop_exits[p.underlying_key] = (self.now, p.exposure)
        del self.positions[p.id]
        self.closed.append(p)
        self.closed = self.closed[-300:]
        self.edge = edge_brake.evaluate(self.ledger.r_outcomes, self.cfg.edge_brake)
        self.gears.expectancy_cap = self.edge.cap_at_guard
        self.gears.evidence_drop = self.calibration.out_of_band
        d = decision_object(rec=None, action="EXIT", mode=self.mode, gear=self.gears.effective_gear(),
                            edge_ceiling=self.edge.ceiling_r, tier=self.tiers.effective().name,
                            drawdown_state=self.gears.effective_gear().value, now=self.now,
                            rules_version=self.rules.version,
                            extra={"instrument": p.instrument.key, "symbol": p.instrument.symbol, "strategy": p.strategy,
                                   "direction": p.direction.value, "grade": p.grade.value, "position_stage": "EXIT",
                                   "exit_logic": reason, "explanation": f"exit {reason}: net Rs {net:,.2f} ({r:+.2f}R)",
                                   "entry": round(p.avg_entry, 2), "stop": round(p.stop, 2),
                                   "planned_risk": round(p.planned_risk, 2), "net_pnl": round(net, 2), "r": round(r, 3)})
        self._record_decision(d)
        if self.store:
            self.store.position(p.to_dict())
        self._log("exit", f"{p.instrument.symbol} closed ({reason}): net Rs {net:,.2f} ({r:+.2f}R) [{self.perf_label.value}]")

    def reversal_watch(self, key: str) -> Direction | None:
        """D-045: the direction a FRESH, independently-confirmed candidate on this underlying would need
        to be in to count as a reversal of a recent stop-out -- never a forced flip, just evidence a
        strategy's own confirmation stack may use. None outside REVERSAL_WATCH_MINUTES of that stop."""
        hit = self.recent_stop_exits.get(key)
        if hit is None:
            return None
        ts, exposure = hit
        if (self.now - ts).total_seconds() > REVERSAL_WATCH_MINUTES * 60:
            return None
        return Direction.SHORT if exposure is Direction.LONG else Direction.LONG

    def _p_of(self, p: Position) -> float:
        rec = next((r for r in self.funnel.records.values() if r.id == p.candidate_id), None)
        return rec.candidate.p_win if rec else 0.45

    # ================================================================== position stage manager
    def _follow_through(self, p: Position) -> bool:
        und = p.underlying_key
        c = self.market.candles(und, limit=2)
        if not c:
            return False
        last = c[-1]
        up = last.close > last.open
        return up if p.exposure is Direction.LONG else (last.close < last.open)

    def _trend_ok(self, p: Position) -> bool:
        c = self.market.candles(p.underlying_key, limit=375)
        c5 = ind.resample(c, 5)
        if len(c5) < 25:
            return False
        x = ind.closes(c5)
        e20 = ind.ema(x, 20)
        up = x[-1] > e20[-1] and e20[-1] > e20[-3]
        dn = x[-1] < e20[-1] and e20[-1] < e20[-3]
        return up if p.exposure is Direction.LONG else dn

    def _reversal_lock_stop(self, p: Position, price: float) -> float | None:
        """D-053: feeds the position's own recent candles and option chain to staging.reversal_lock().
        Best-effort -- any data hiccup (chain not available for this underlying/expiry yet, outage) means
        no evidence, never a crash in the position-management loop."""
        try:
            cds = self.market.candles(p.underlying_key)
            if len(cds) < 2:
                return None
            und = self.market.instruments[p.underlying_key].symbol
            spot, rows = (price, []) if self.cash_only else self.market.chain(und, p.instrument.expiry)
        except Exception:
            return None
        return staging.reversal_lock(p, price, cds[-2], cds[-1], rows, spot, self.cfg.staging)

    def _manage_positions(self, quotes: dict[str, Quote]) -> None:
        now = self.now
        exit_time = dt.datetime.combine(now.date(), self.cal.intraday_exit, tzinfo=IST)
        for p in list(self.positions.values()):
            q = quotes.get(p.instrument.key)
            if q is None:
                continue   # data outage: the broker-side stop protects the position
            price = q.ltp
            p.mark(price)
            if p.id in self.exit_orders:
                continue
            if not p.broker_stop_id or p.broker_stop_id not in self.broker.resting:
                # red-team fix: never leave a position without its exchange-side backstop
                p.broker_stop_id = ""
                self._place_broker_stop(p)
                if not p.broker_stop_id:
                    since = self._unprotected_since.setdefault(p.id, now)
                    if (now - since).total_seconds() > 60:
                        self._log("risk", f"{p.instrument.symbol}: broker-side stop could not be placed for 60 s -> exiting")
                        self._exit(p, p.qty, "FLATTEN")
                        continue
                else:
                    self._unprotected_since.pop(p.id, None)
            if p.sign * (price - p.stop) <= 0:
                self._exit(p, p.qty, "STOP")
                continue
            if now >= exit_time:
                self._exit(p, p.qty, "EOD")
                continue
            if self.cfg.is_unbounded:              # D-031: stop-loss, target, breakeven trail -- no probe/
                                                    # confirm-add/press/harvest staging (that stays bounded-only)
                if p.target and p.sign * (price - p.target) >= 0:
                    self._exit(p, p.qty, "TARGET")
                    continue
                be_r = float(self.cfg.unbounded["trail_to_breakeven_at_r"])
                if be_r > 0 and p.move_r(price) >= be_r and p.stop_is_better(p.avg_entry):
                    p.stop = round(p.avg_entry, 2)
                    self._sync_broker_stop(p)
                    # D-058: a structured stop-history record (ts/stop/reason), not just the free-text log
                    # line above -- lets the web UI draw the stop as a step line over the position's own
                    # chart, so "was the exit perfect" is something the operator can see, not just read.
                    # Kept in `stop_history`, separate from `stage_history` (whose entries are all stage
                    # transitions read as such elsewhere, e.g. _close_position's "stages" summary).
                    p.stop_history.append({"ts": now.isoformat(), "stop": p.stop, "reason": "breakeven"})
                    self._log("stage", f"{p.instrument.symbol}: +{be_r:g}R reached -> stop-loss moved to entry {p.stop:.2f}")
                # D-052: a single-entry unbounded position used to have NO profit protection at all until
                # the full trail_to_breakeven_at_r move (1.0R by default) -- a position that ran to +0.6R
                # and reversed gave back everything down to the original stop. Reuse the same breakeven/
                # early-lock/press-lock tiers bounded mode already has (D-042/D-045) -- they only ever
                # tighten the stop, never resize or add to the position, so they're safe to borrow here.
                is_expiry_day = p.instrument.expiry is not None and p.instrument.expiry == now.date()
                new = staging.trailing_stop(p, price, self.cfg.staging, is_expiry_day=is_expiry_day)
                rev, reason = self._reversal_lock_stop(p, price), ""
                if rev is not None and (new is None or p.sign * (rev - new) > 0):
                    new, reason = rev, " (reversal evidence)"
                if new is not None and p.stop_is_better(new) and p.sign * (price - new) > 0:
                    p.stop = round(new, 2)
                    self._sync_broker_stop(p)
                    p.stop_history.append({"ts": now.isoformat(), "stop": p.stop,
                                           "reason": ("reversal_evidence" if reason else "trailing")})
                    self._log("stage", f"{p.instrument.symbol}: profit-lock stop tightened to {p.stop:.2f}{reason}")
                continue
            if (p.stage is PositionStage.PROBE and p.staging_mode == "full" and now > p.confirm_deadline):
                p.staging_mode = "probe_kept"
                p.stage_history.append({"ts": now.isoformat(), "to": "PROBE", "note": "confirm window expired: probe kept"})
                self._log("stage", f"{p.instrument.symbol}: no confirmation in window -> probe kept (time stop rule)")
            adding = any(pid == p.id for pid, _ in self.pending_adds.values())
            # P2 confirm-add
            if not adding and p.stage is PositionStage.PROBE and p.staging_mode == "full":
                plan = staging.plan_confirm_add(p, price, self._follow_through(p), now, self.cfg.staging)
                if plan.ok:
                    self._propose_add(p, plan, price, is_press=False)
                    continue
            # P3 press
            gear = self.gears.effective_gear()
            if not adding and (p.stage in (PositionStage.CONFIRM_ADD, PositionStage.PRESS_1) or (
                    p.stage is PositionStage.PROBE and p.staging_mode == "single")):
                plan = staging.plan_press(p, price, p.grade, self._trend_ok(p), self.cfg.gears[gear].pressing,
                                          self.cfg.staging)
                if plan.ok:
                    self._propose_add(p, plan, price, is_press=True)
                    continue
            # P4 harvest
            hq = staging.harvest_qty(p, price, self.cfg.staging)
            if hq > 0:
                self._exit(p, hq, "HARVEST")
                continue
            # trailing / ratchet lock -- D-045: tighter tiers on the contract's own expiry day (theta decays
            # fastest into the close, so the same open profit is worth locking in sooner)
            is_expiry_day = p.instrument.expiry is not None and p.instrument.expiry == now.date()
            new = staging.trailing_stop(p, price, self.cfg.staging, is_expiry_day=is_expiry_day)
            rev = self._reversal_lock_stop(p, price)   # D-053
            tightened_by = "trailing"
            if rev is not None and (new is None or p.sign * (rev - new) > 0):
                new, tightened_by = rev, "reversal_evidence"
            if self.targets.trail_required and p.unrealized(price) > 0:
                lock = p.avg_entry + 0.5 * (price - p.avg_entry)
                if (new is None and p.stop_is_better(lock)) or (new is not None and p.sign * (lock - new) > 0):
                    new, tightened_by = lock, "target_trail"
            if new is not None and p.stop_is_better(new) and p.sign * (price - new) > 0:
                p.stop = round(new, 2)
                self._sync_broker_stop(p)
                p.stop_history.append({"ts": now.isoformat(), "stop": p.stop, "reason": tightened_by})

    def _propose_add(self, p: Position, plan: staging.AddPlan, price: float, is_press: bool) -> None:
        ctx = self._risk_ctx()
        v = self.guardian.check_add(ctx, p, plan, price, is_press,
                                    add_margin=self._margin_of(p.segment, price, plan.qty),
                                    add_notional=price * plan.qty)
        if not v.approved:
            return
        kind = "PRESS" if is_press else "CONFIRM_ADD"
        if any(a.position_id == p.id and a.status == "PENDING" for a in self.approvals.values()):
            return
        if self.mode is Mode.LIVE_PILOT or self.pilot_approvals:
            w = self.wall()
            a = Approval(new_id("apr"), kind, w, w + float(self.cfg.approvals["timeout_seconds"]),
                         {"symbol": p.instrument.symbol, "stage": plan.stage.value if plan.stage else kind,
                          "qty": plan.qty, "new_stop": round(plan.new_stop, 2), "rule": plan.rule,
                          "worst_case_after": round(plan.worst_case_after, 2), "planned_risk": round(p.planned_risk, 2),
                          "grade": p.grade.value, "gear": self.gears.effective_gear().value, "price": round(price, 2)},
                         position_id=p.id, add_plan=plan)
            self.approvals[a.id] = a
            return
        self._execute_add(p, plan, is_press, operator="NOT_REQUIRED")

    def _execute_add(self, p: Position, plan: staging.AddPlan, is_press: bool, operator: str) -> dict[str, Any]:
        q = self.quote(p.instrument.key)
        if q is None:
            return {"ok": False, "error": "no quote"}
        price = q.ltp
        fresh = (staging.plan_press(p, price, p.grade, self._trend_ok(p),
                                    self.cfg.gears[self.gears.effective_gear()].pressing, self.cfg.staging)
                 if is_press else staging.plan_confirm_add(p, price, True, self.now, self.cfg.staging))
        if not fresh.ok:
            return {"ok": False, "error": f"add no longer valid: {fresh.reason}"}
        v = self.guardian.check_add(self._risk_ctx(), p, fresh, price, is_press,
                                    add_margin=self._margin_of(p.segment, price, fresh.qty), add_notional=price * fresh.qty)
        if not v.approved:
            return {"ok": False, "error": v.reason}
        side = Side.BUY if p.direction is Direction.LONG else Side.SELL
        purpose = "PRESS" if is_press else "CONFIRM_ADD"
        o = new_order(p.id, p.instrument, p.segment, side, fresh.qty, OrderType.MARKET, purpose, self.now, self.algo_id,
                      model_price=price)
        self.orders[o.id] = o
        self.pending_adds[o.id] = (p.id, fresh)
        self.order_queue.append(o)
        # tighten the stop NOW so the whole position is inside its rule even before the add fills
        if p.stop_is_better(fresh.new_stop):
            p.stop = fresh.new_stop
            self._sync_broker_stop(p)
        self._record_decision(decision_object(
            rec=None, action="ENTER", mode=self.mode, gear=self.gears.effective_gear(), edge_ceiling=self.edge.ceiling_r,
            tier=self.tiers.effective().name, drawdown_state=self.gears.effective_gear().value,
            now=self.now, rules_version=self.rules.version,
            extra={"instrument": p.instrument.key, "symbol": p.instrument.symbol, "strategy": p.strategy,
                   "grade": p.grade.value, "position_stage": fresh.stage.value if fresh.stage else purpose,
                   "position_size": fresh.qty, "stop": round(fresh.new_stop, 2), "planned_risk": round(p.planned_risk, 2),
                   "risk_approval": "APPROVED", "operator_approval": operator, "risk_checks": v.to_dict()["checks"],
                   "explanation": f"{purpose}: {fresh.rule}; worst case after add Rs {fresh.worst_case_after:,.0f}"}))
        self._log("stage", f"{p.instrument.symbol} {purpose} proposed qty {fresh.qty}; stop -> {fresh.new_stop:.2f} ({fresh.rule})")
        return {"ok": True}

    # ================================================================== counterfactual shadows (D6)
    def _manage_shadows(self, quotes: dict[str, Quote]) -> None:
        exit_time = dt.datetime.combine(self.now.date(), self.cal.intraday_exit, tzinfo=IST)
        for s in self.shadows:
            if s.done:
                continue
            p = s.pos
            q = quotes.get(p.instrument.key)
            if q is None:
                continue
            p.mark(q.ltp)
            hit_stop = p.sign * (q.ltp - p.stop) <= 0
            if hit_stop or self.now >= exit_time:
                px = p.stop if hit_stop else q.ltp
                s.system_pnl = p.sign * (px - p.avg_entry) * p.qty
                s.done = True
                self.manual_exits.append({"position_id": p.id, "symbol": p.instrument.symbol,
                                          "manual_pnl": round(s.manual_pnl, 2), "system_pnl": round(s.system_pnl, 2),
                                          "manual_minus_system": round(s.manual_pnl - s.system_pnl, 2),
                                          "fraction": s.fraction, "ts": s.manual_ts.isoformat()})
                continue
            is_expiry_day = p.instrument.expiry is not None and p.instrument.expiry == self.now.date()
            new = staging.trailing_stop(p, q.ltp, self.cfg.staging, is_expiry_day=is_expiry_day)
            if new is not None and p.stop_is_better(new):
                p.stop = new

    # ================================================================== operational safety
    def _operational_checks(self) -> None:
        if self.arbiter.should_flatten(self.now) and not self.faults.order_api_down() and self.positions:
            self._log("risk", "both feeds down > 5 min and order API reachable: flattening per policy")
            self._flatten_all("FLATTEN")
        if self.broker.reject_count(self.now) >= int(self.cfg.execution["reject_storm_threshold"]):
            if not any(e["msg"].startswith("order-reject storm") for e in list(self.events)[-5:]):
                self._log("risk", "order-reject storm: operational halt on new entries")

    # ================================================================== LIVE: Dhan is the truth
    def _adopt_orphan(self, f: Fill) -> None:
        """A real entry fill whose entry had already been given up (cancel raced the fill): track it and close it."""
        o = f.order
        d = Direction.LONG if o.side is Side.BUY else Direction.SHORT
        unit = max(0.02 * f.price, o.instrument.tick_size)
        stop = round(f.price - d.sign * unit, 2)
        p = Position(id=new_id("pos"), candidate_id="orphan", strategy="orphan", instrument=o.instrument,
                     underlying_key=o.instrument.underlying or o.instrument.key, direction=d, segment=o.segment,
                     grade=Grade.B, gear_at_entry=self.gears.effective_gear(), mode=self.mode.value,
                     planned_risk=unit * f.qty, unit_risk=unit, initial_stop=stop, stop=stop,
                     broker_stop=round(stop - d.sign * unit, 2), staging_mode="single", full_qty_target=f.qty,
                     confirm_trigger=f.price, confirm_deadline=f.ts, opened_at=f.ts,
                     legs=[Leg(f.qty, f.price, f.ts, PositionStage.PROBE)], charges=f.charges,
                     defined_risk=o.instrument.is_option, trigger_ts=f.ts)
        p.mark(f.price)
        o.position_id = p.id
        self.positions[p.id] = p
        self._fill_wall[p.id] = self.wall()
        self._log("risk", f"LIVE: {o.instrument.symbol} entry filled {f.qty} @ {f.price:.2f} after it was given up "
                          f"-> closing it now")
        self._exit(p, p.qty, "FLATTEN")

    def _reconcile_broker(self) -> None:
        """A11 broker reconciliation. A position closed outside Sthitaprajna (Dhan app, Dhan's auto square-off) is
        closed here too and its resting stop is cancelled at Dhan, so the stop can never sell a second time. Acts
        only on two consecutive position snapshots fetched after the position's last fill."""
        snap = self.broker.position_snapshot()
        if snap is None or snap[0] == self._recon_seq:
            return
        self._recon_seq, fetched, book = snap
        busy = {o.position_id for o in self.broker.pending.values()}
        for p in list(self.positions.values()):
            if p.id in self.exit_orders or p.id in busy or fetched < self._fill_wall.get(p.id, 0.0) + 1.0:
                self._recon_seen.pop(p.id, None)
                continue
            row = book.get((exchange_segment(p.instrument), str(p.instrument.dhan_security_id)))
            held = p.sign * (row["net"] if row else 0.0)          # Dhan's quantity on this position's side
            if held == p.qty:
                self._recon_seen.pop(p.id, None)
                continue
            if self._recon_seen.get(p.id) != held:                # first sighting: wait for the next snapshot
                self._recon_seen[p.id] = held
                continue
            self._recon_seen.pop(p.id, None)
            if held > p.qty:
                self._log("risk", f"LIVE: Dhan holds {held:g} of {p.instrument.symbol}, Sthitaprajna {p.qty}: the extra "
                                  "is not managed (your own trade?)")
                continue
            gone = p.qty - max(0, int(held))
            ref = (row["sell_avg"] if p.direction is Direction.LONG else row["buy_avg"]) if row else 0.0
            px = ref if ref > 0 else p.last_price
            if gone >= p.qty and p.broker_stop_id:
                self.broker.cancel(p.broker_stop_id, self.now)
                p.broker_stop_id = ""
            elif p.broker_stop_id:
                self.broker.modify_qty(p.broker_stop_id, p.qty - gone, self.now)
            o = new_order(p.id, p.instrument, p.segment, Side.SELL if p.direction is Direction.LONG else Side.BUY,
                          gone, OrderType.MARKET, "EXTERNAL", self.now, self.algo_id, model_price=px)
            o.transition(OrderStatus.SUBMITTED, self.now, "closed outside Sthitaprajna (Dhan positions)")
            ch = self.charges.compute(p.segment, o.side, px, gone, p.instrument.exchange).total
            o.filled_qty, o.avg_price, o.charges = gone, px, ch
            o.transition(OrderStatus.FILLED, self.now, "reconciled from Dhan's positions")
            self.orders[o.id] = o
            self._log("risk", f"LIVE: {p.instrument.symbol} {gone} closed outside Sthitaprajna (Dhan app or Dhan's "
                              f"square-off); recorded at {px:.2f} from Dhan's day average")
            self._on_fill(Fill(o, gone, px, self.now, ch))

    def _flatten_all(self, reason: str) -> None:
        for oid in list(self.pending_entries):
            self.broker.cancel(oid, self.now)
            if not self.live_orders:             # LIVE: the entry leaves when Dhan confirms (a fill may still come)
                self.pending_entries.pop(oid, None)
        for p in list(self.positions.values()):
            self._exit(p, p.qty, reason)   # no-op if an exit is already working

    # ================================================================== day boundaries
    def _process_score(self) -> dict[str, Any]:
        pr = self.process
        items: dict[str, float] = {}
        items["pre_market_state_check"] = 1.0 if pr["state_check_done"] else 0.0
        total_apr = pr["approvals_answered"] + pr["approvals_timed_out"]
        if total_apr:
            items["approvals_answered_in_time"] = pr["approvals_answered"] / total_apr
        cov = pr["stop_coverage_ticks"] + pr["uncovered_ticks"]
        if cov:
            items["broker_stop_coverage"] = pr["stop_coverage_ticks"] / cov
        exits = pr["rule_exits"] + pr["other_exits"]
        if exits:
            items["exits_by_rule"] = pr["rule_exits"] / exits
        if pr["eod_flat"] is not None:
            items["flat_before_square_off"] = 1.0 if pr["eod_flat"] else 0.0
        score = sum(items.values()) / len(items) if items else 1.0
        return {"score": round(score, 4), "items": {k: round(v, 4) for k, v in items.items()},
                "manual_exits": pr["manual_exits"]}

    def _end_of_day(self) -> None:
        day = self.cal.prev_trading_day(self.market.day) if self.market.now.time() == self.cal.session_open else self.market.day
        self.process["eod_flat"] = not self.positions
        for p in list(self.positions.values()):   # nothing intraday survives the night at T1 (D7 off)
            if self.live_orders:                   # never invent a fill for a real position
                self._log("risk", f"LIVE: {p.instrument.symbol} is still open in Sthitaprajna at the close. Dhan's "
                                  "auto square-off should have closed it: check the Dhan app (reconciliation records "
                                  "it from Dhan's positions)")
                continue
            q = self.market.quote(p.instrument.key)
            px = q.ltp if q else p.last_price
            o = new_order(p.id, p.instrument, p.segment, Side.SELL if p.direction is Direction.LONG else Side.BUY,
                          p.qty, OrderType.MARKET, "EOD", self.now, self.algo_id)
            o.transition(OrderStatus.SUBMITTED, self.now)
            self.orders[o.id] = o
            self._on_fill(self.broker._fill(o, px, self.now, "session close"))
        ps = self._process_score()
        u, ch = self.unrealized()
        day_pnl = self.ledger.equity - self.ledger.day_start
        row = self.ledger.end_day(day, self.gears.effective_gear().value, ps["score"], self.ledger.trades_today)
        bounded = not self.cfg.is_unbounded
        ev = self.gears.on_day_close(self.ledger.equity, self.ledger.daily_loss_breached, self.now, day_pnl=day_pnl) \
            if bounded else None
        if ev:
            self._log("gear", f"{ev.frm.value} -> {ev.to.value}: {ev.reason}")
        tev = self.tiers.on_day_close(self.ledger.equity, day_pnl, self.ledger.daily_loss_breached) if bounded else None
        if tev:
            self._log("tier", f"tier {tev['type']}: {tev.get('from', '')} -> {tev['to']} {tev.get('note', '')}")
        todays = [t for t in self.ledger.closed_trades if t["closed"][:10] == day.isoformat()]
        wins = [t for t in todays if t["net"] > 0]
        entry = {"date": day.isoformat(), "label": self.perf_label.value, "process": ps, "pnl": row["pnl"],
                 "equity": row["equity"], "gear": row["gear"], "trades": len(todays), "wins": len(wins),
                 "best": max((t["r"] for t in todays), default=None), "worst": min((t["r"] for t in todays), default=None),
                 "funnel": self.funnel.funnel_counts(), "drop_reasons": dict(self.funnel.drop_reasons),
                 "kpis": self.kpis.to_dict(), "regime": self.market.regime,
                 "reflection": self._reflection(ps, todays)}
        self.journal.append(entry)
        if self.store:
            self.store.journal(entry)
            self._snapshot_iv_history(day)
        self._log("journal", f"{day.isoformat()} closed: P&L Rs {row['pnl']:,.0f}, process {ps['score']:.0%} "
                  f"[{self.perf_label.value}]")

    def _snapshot_iv_history(self, day: dt.date) -> None:
        """One EOD ATM-IV point per index underlying, for the Chain view's IV rank/percentile (D-039).
        Best-effort: a chain fetch hiccup must never block end-of-day close-out."""
        if self.cash_only:
            return
        for und in ("NIFTY", "SENSEX", "BANKNIFTY"):
            try:
                exps = self.market.expiries(und)
                if not exps:
                    continue
                spot, rows = self.market.chain(und, exps[0])
                if rows:
                    self.store.iv_snapshot(day, und, chain_summary(rows, spot)["atm_iv"])
            except Exception as exc:
                self._log("system", f"IV history snapshot skipped for {und}: {exc}")

    def _reflection(self, ps: dict[str, Any], todays: list[dict[str, Any]]) -> str:
        parts = [f"Process {ps['score']:.0%} first; P&L second."]
        if not self.process["state_check_done"]:
            parts.append("Pre-market state check was skipped -- answer it before the open.")
        if self.process["approvals_timed_out"]:
            parts.append(f"{self.process['approvals_timed_out']} approvals timed out (safe default: skipped).")
        if self.process["manual_exits"]:
            parts.append(f"{self.process['manual_exits']} manual exits -- see counterfactuals.")
        if not todays:
            parts.append("No trade qualified. Cash is a position.")
        return " ".join(parts)

    def _close_period_review(self, period_type: str, period_key: str, pnl: float, base_equity: float,
                             equity_after: float) -> None:
        """D-054: persist plan-vs-actual for a week/month that just ended, using the ratchet/target state
        as it stood right before reset_period() wipes it -- so a target hit (or missed) this period is
        still reviewable afterwards, not just visible while the period is live. Read back via /api/review."""
        snap = self.targets.to_dict({period_type: pnl}, {period_type: base_equity})[period_type]
        entry = {"period_type": period_type, "period_key": period_key, "closed_at": self.now.isoformat(),
                 "pnl": round(pnl, 2), "equity_after": round(equity_after, 2), "target": snap}
        self.period_reviews[period_type].insert(0, entry)
        self.period_reviews[period_type] = self.period_reviews[period_type][:104]
        if self.store:
            self.store.period_review(entry)

    def _start_of_day(self) -> None:
        d = self.market.day
        vst = self._vix_state()
        self.vix_prev_close = vst.prev_close if vst else self.vix_prev_close
        self.straddle_open.clear()
        prev_week, prev_month = self.ledger.week_key, self.ledger.month_key
        prev_week_start, prev_month_start = self.ledger.week_start, self.ledger.month_start
        prev_equity = self.ledger.equity
        self.ledger.start_day(d)
        self.funnel.new_day()
        self.reentries_today.clear()
        self.stopped_setups.clear()
        self.recent_stop_exits.clear()
        self.targets.reset_period("day")
        if prev_week != self.ledger.week_key:
            if prev_week:
                self._close_period_review("week", prev_week, prev_equity - prev_week_start, prev_week_start,
                                          prev_equity)
            self.targets.reset_period("week")
            self.sleeve_used["week"] = 0.0
        if prev_month != self.ledger.month_key:
            if prev_month:
                self._close_period_review("month", prev_month, prev_equity - prev_month_start, prev_month_start,
                                          prev_equity)
            self.targets.reset_period("month")
            self.sleeve_used["month"] = 0.0
        self.sleeve_used["expiry"] = 0.0
        self.process = self._new_process()
        self.state_check = None
        self._log("system", f"{d.isoformat()} pre-market: gear {self.gears.effective_gear().value}, "
                  f"R {gear_r(self.cfg, self.tiers.effective(), self.gears.effective_gear(), self.edge.ceiling_r):.2%}"
                  + (" -- live Dhan data, PAPER fills" if self.live_data else f", regime (SIM) {self.market.regime}"))

    # ================================================================== operator commands (D6, D10)
    # None of these can open, add, enlarge, widen a stop, or raise the gear.
    def close(self, position_id: str, fraction: float = 1.0) -> dict[str, Any]:
        p = self.positions.get(position_id)
        if p is None:
            return {"ok": False, "error": "no open position with that id"}
        fraction = 0.5 if fraction < 1 else 1.0
        lot = p.instrument.lot_size
        qty = p.qty if fraction >= 1 else max(lot, (int(p.qty * fraction) // lot) * lot)
        if qty >= p.qty:
            qty = p.qty
        shadow = copy.deepcopy(p)
        shadow.legs = [Leg(qty, p.avg_entry, p.opened_at, PositionStage.PROBE)]
        shadow.closed_legs = []
        manual = p.sign * (p.last_price - p.avg_entry) * qty
        self.shadows.append(Shadow(shadow, manual, self.now, fraction))
        p.manual_exit = {"ts": self.now.isoformat(), "fraction": fraction, "price": round(p.last_price, 2)}
        self._exit(p, qty, "MANUAL_CLOSE" if qty == p.qty else "MANUAL_REDUCE")
        self._log("operator", f"manual {'close' if qty == p.qty else 'close 50%'} {p.instrument.symbol} ({qty})")
        return {"ok": True, "qty": qty}

    def trail_tighter(self, position_id: str) -> dict[str, Any]:
        p = self.positions.get(position_id)
        if p is None:
            return {"ok": False, "error": "no open position with that id"}
        new = staging.tighten_manual(p, p.last_price)
        if new is None:
            return {"ok": False, "error": "stop cannot be tightened further safely"}
        p.stop = round(new, 2)
        self._sync_broker_stop(p)
        self._log("operator", f"trail tighter {p.instrument.symbol}: stop -> {p.stop:.2f}")
        return {"ok": True, "stop": p.stop}

    def close_all(self) -> dict[str, Any]:
        n = len(self.positions)
        for pid in list(self.positions):
            self.close(pid, 1.0)
        return {"ok": True, "closed": n}

    def kill(self) -> dict[str, Any]:
        """/kill: one step -- flatten everything and halt (D10)."""
        self.kill_switch = True
        for a in self.approvals.values():
            if a.status == "PENDING":
                a.status = "DENIED"
        self._flatten_all("KILL")
        self._log("risk", "KILL SWITCH: everything flattened, new entries halted until operator review")
        return {"ok": True}

    def resume(self, reviewed: bool) -> dict[str, Any]:
        if not reviewed:
            return {"ok": False, "error": "operator review required"}
        msg = []
        if self.kill_switch:
            self.kill_switch = False
            msg.append("kill switch released")
        if self.gears.gear is Gear.HALTED:
            self.gears.resume_after_halt(self.mtm(), self.now, reviewed=True)
            msg.append("resumed from HALTED in GUARD")
        kill_at = float(self.cfg.sleeve["kill_cumulative_loss"]) * self.ledger.equity_sod
        if -self.sleeve_cumulative_pnl >= kill_at - 1e-6:
            self.sleeve_cumulative_pnl = 0.0
            msg.append("Zero-to-Hero sleeve re-enabled (D8 cumulative-loss kill cleared)")
        if not msg:
            return {"ok": False, "error": "nothing to resume"}
        self._log("operator", "; ".join(msg))
        return {"ok": True, "result": msg}

    def lock(self, period: str) -> dict[str, Any]:
        self.targets.lock(period)
        self._log("operator", f"/lock {period}: no new entries this {period}")
        return {"ok": True}

    def set_target(self, period: str, value: float, unit: str) -> dict[str, Any]:
        t = self.targets.set_target(period, value, unit, self.now)
        from sthitaprajna.research.target_plan import feasibility
        base = {"day": self.ledger.day_start, "week": self.ledger.week_start, "month": self.ledger.month_start}[period]
        days = max(1, len(self.ledger.daily))
        tpd = max(0.5, len(self.ledger.r_outcomes) / days) if self.ledger.r_outcomes else 2.0
        feas = feasibility(self.cfg, target_pct=t.rupees(base) / base, period=period,
                           r_outcomes=self.ledger.r_outcomes, label=self.perf_label.value, equity=base,
                           trades_per_day=tpd)
        self._log("operator", f"/target {period} {value}{'%' if unit.upper() == 'PCT' else ' Rs'} -> {feas['label']} "
                  "(targets never change risk)")
        return {"ok": True, "feasibility": feas}

    def state_check_answer(self, slept_well: bool, calm: bool, undistracted: bool) -> dict[str, Any]:
        self.gears.apply_state_check(slept_well, calm, undistracted)
        self.state_check = {"slept_well": slept_well, "calm": calm, "undistracted": undistracted,
                            "ts": self.now.isoformat()}
        self.process["state_check_done"] = True
        eff = self.gears.effective_gear().value
        self._log("operator", f"state check {'all yes' if slept_well and calm and undistracted else 'has a no -> one gear lower today'}"
                  f" (effective gear {eff})")
        return {"ok": True, "effective_gear": eff}

    def set_pilot_approvals(self, on: bool) -> dict[str, Any]:
        if self.mode in (Mode.LIVE_PILOT, Mode.LIVE_FULL) and not self.cfg.is_unbounded:
            return {"ok": False, "error": "approval policy is fixed by D9 in live modes"}
        self.pilot_approvals = bool(on)
        self._log("operator", f"pilot approvals {'ON' if on else 'OFF'} ({self.mode.value})")
        return {"ok": True}

    def set_speed(self, speed: float) -> dict[str, Any]:
        if self.mode is not Mode.SIM:
            return {"ok": False, "error": "speed control exists only in SIM"}
        self.speed = max(1.0, min(float(speed), 240.0))
        return {"ok": True, "speed": self.speed}

    def inject(self, fault: str, seconds: float = 60.0) -> dict[str, Any]:
        if self.mode is not Mode.SIM:
            return {"ok": False, "error": "failure injection exists only in SIM"}
        msg = self.faults.inject(fault, seconds)
        self._log("fault", msg)
        return {"ok": True, "msg": msg}
