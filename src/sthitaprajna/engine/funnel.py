"""Staged Pickup Funnel (AGG-2.1), Global Opportunity Queue (A10.1) and decision objects (A18).

S1 SCAN -> S2 QUALIFY -> S3 GRADE -> S4 CONFIRM -> S5 ARM -> (S6 RISK + APPROVAL -> S7 PROBE in runtime)

The funnel never lowers a threshold to create trades; the Opportunity Maximiser only chooses among
candidates that already passed every gate. Every candidate records the last stage it reached, so every
missed opportunity can be diagnosed.
"""
from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from sthitaprajna.aggression.sizer import SizingDecision, planned_risk_fraction, size_by_capital, size_position
from sthitaprajna.config import OperatorConfig, TierSpec
from sthitaprajna.domain import (
    HALF_LIFE_SECONDS, Candidate, Direction, Gear, Grade, Mode, PickupStage, Quote, Segment, Side, new_id,
)
from sthitaprajna.market.charges import ChargesModel
from sthitaprajna.signal import ev as ev_engine
from sthitaprajna.signal.ev import EVResult
from sthitaprajna.signal.grading import Confidence, QualityDistribution, grade as grade_fn, quality_score

MAX_SPREAD_BPS = {
    Segment.INDEX_OPTION: 300.0, Segment.STOCK_OPTION: 400.0, Segment.EQ_INTRADAY: 15.0,
    Segment.EQ_DELIVERY: 25.0, Segment.INDEX_FUTURE: 5.0, Segment.STOCK_FUTURE: 10.0,
}


class FunnelEnv(Protocol):
    mode: Mode
    now: dt.datetime
    equity_sod: float
    tier: TierSpec
    gear: Gear
    drawdown: float
    edge_ceiling: float | None
    event_blackout: str | None
    entry_window_open: bool

    def quote(self, key: str) -> Quote | None: ...
    def data_confidence(self, key: str) -> float: ...
    def execution_confidence(self, key: str) -> float: ...
    def bucket_room(self, c: Candidate) -> float: ...
    def is_banned(self, key: str) -> str | None: ...
    def free_capital(self) -> float: ...


@dataclass
class StagePlanSpec:
    entry_lo: float
    entry_hi: float
    stop: float
    broker_stop: float
    probe_qty: int
    full_qty: int
    confirm_trigger: float
    confirm_deadline: dt.datetime
    harvest_at_r: float
    harvest_fraction: float
    press_max_adds: int
    time_stop: dt.datetime
    staging_mode: str

    def to_dict(self) -> dict[str, Any]:
        return {k: (v.isoformat() if isinstance(v, dt.datetime) else (round(v, 2) if isinstance(v, float) else v))
                for k, v in self.__dict__.items()}


@dataclass
class FunnelRecord:
    id: str
    candidate: Candidate
    first_seen: dt.datetime
    expires_at: dt.datetime
    stage: PickupStage = PickupStage.S1_SCAN
    status: str = "ACTIVE"          # ACTIVE | ARMED | READY | PENDING_APPROVAL | PROBED | DROPPED | REJECTED
    reason: str = ""
    grade: Grade | None = None
    ev: EVResult | None = None
    quality: float | None = None
    quality_pct: float | None = None
    confidence: Confidence | None = None
    sizing: SizingDecision | None = None
    plan: StagePlanSpec | None = None
    verdict: dict[str, Any] | None = None
    utility: float = 0.0
    updated: dt.datetime | None = None
    decision_id: str | None = None
    s2_checks: list[dict[str, Any]] = field(default_factory=list)

    @property
    def terminal(self) -> bool:
        return self.status in ("PROBED", "DROPPED", "REJECTED")

    def to_dict(self) -> dict[str, Any]:
        c = self.candidate
        return {
            "id": self.id, "setup_id": c.setup_id, "strategy": c.strategy, "symbol": c.instrument.symbol,
            "instrument": c.instrument.key, "underlying": c.underlying_key, "direction": c.direction.value,
            "exposure": c.exposure.value, "segment": c.segment.value, "entry": round(c.entry, 2),
            "stop": round(c.stop, 2), "target_r": c.target_r, "p_win": round(c.p_win, 3),
            "p_calibrated": c.p_win_calibrated, "half_life": c.half_life.value, "trigger": c.trigger_fired,
            "confirmations": [{"name": x.name, "factor": x.factor, "fired": x.fired, "detail": x.detail}
                              for x in c.confirmations],
            "independent_confirmations": c.independent_confirmations(),
            "stage": self.stage.value, "status": self.status, "reason": self.reason,
            "grade": self.grade.value if self.grade else None, "ev": self.ev.to_dict() if self.ev else None,
            "quality": round(self.quality, 1) if self.quality is not None else None,
            "quality_pct": round(self.quality_pct, 3) if self.quality_pct is not None else None,
            "confidence": self.confidence.to_dict() if self.confidence else None,
            "sizing": self.sizing.to_dict() if self.sizing else None, "plan": self.plan.to_dict() if self.plan else None,
            "verdict": self.verdict, "utility": round(self.utility, 4), "first_seen": self.first_seen.isoformat(),
            "expires_at": self.expires_at.isoformat(), "explanation": c.explanation, "is_reentry": c.is_reentry,
            "sleeve": c.sleeve, "decision_id": self.decision_id,
        }


class PickupFunnel:
    def __init__(self, cfg: OperatorConfig, charges: ChargesModel, rules) -> None:
        self.cfg = cfg
        self.charges = charges
        self.rules = rules
        self.records: dict[str, FunnelRecord] = {}
        self.quality = QualityDistribution()
        self.stage_counts: dict[str, int] = {s.value: 0 for s in PickupStage}
        self.drop_reasons: dict[str, int] = {}
        self.on_terminal: Callable[[FunnelRecord], None] | None = None

    # ------------------------------------------------------------------ S1
    def ingest(self, candidates: list[Candidate], now: dt.datetime) -> None:
        for c in candidates:
            rec = self.records.get(c.setup_id)
            if rec is not None and not rec.terminal:
                rec.candidate = c   # same setup re-emitted: refresh trigger/confirmations
                rec.updated = now
                continue
            if rec is not None and rec.terminal:
                continue           # re-entries arrive with a new setup_id from the runtime
            hl = HALF_LIFE_SECONDS[c.half_life]
            rec = FunnelRecord(new_id("cand"), c, now, now + dt.timedelta(seconds=hl), updated=now)
            self.records[c.setup_id] = rec
            self.stage_counts[PickupStage.S1_SCAN.value] += 1

    # ------------------------------------------------------------------ costs
    def per_unit_cost(self, c: Candidate, qty: int) -> float:
        """Round-trip charges + modelled slippage (half-spread each way), per unit."""
        if qty <= 0:
            return 0.0
        exch = c.instrument.exchange
        long = c.direction is Direction.LONG
        rt = self.charges.round_trip(c.segment, c.entry, c.stop, qty, exch, long)
        return rt / qty

    def _slippage_per_unit(self, q: Quote | None, c: Candidate) -> float:
        if q is None or q.ask <= 0 or q.bid <= 0:
            return c.instrument.tick_size * 2
        return max(c.instrument.tick_size, (q.ask - q.bid))  # pay ~half spread on entry and on exit

    def _sizing(self, c: Candidate, env: FunnelEnv, g: Grade, adjustments: dict[str, float]) -> SizingDecision:
        if self.cfg.is_unbounded:
            return self._sizing_by_capital(c, env)
        frac = planned_risk_fraction(self.cfg, env.tier, env.gear, g, env.drawdown, env.edge_ceiling)
        probe_only = c.is_reentry or c.sleeve
        if c.sleeve:   # D8: the whole sleeve budget per expiry, no staging, never auto-increased
            frac = min(frac, float(self.cfg.sleeve["per_expiry"])) if frac > 0 else 0.0
        q = env.quote(c.instrument.key)
        slip = self._slippage_per_unit(q, c)
        # iterate once: costs depend on qty (flat brokerage weighs more on small positions)
        probe = size_position(cfg=self.cfg, equity=env.equity_sod, planned_fraction=frac, grade=g, segment=c.segment,
                              entry=c.entry, stop=c.stop, lot_size=c.instrument.lot_size, per_unit_cost=slip,
                              adjustments=adjustments, probe_only=probe_only)
        if not probe.ok:
            return probe
        cost = self.per_unit_cost(c, probe.full_qty) + slip
        return size_position(cfg=self.cfg, equity=env.equity_sod, planned_fraction=frac, grade=g, segment=c.segment,
                             entry=c.entry, stop=c.stop, lot_size=c.instrument.lot_size, per_unit_cost=cost,
                             adjustments=adjustments, probe_only=probe_only)

    def _sizing_by_capital(self, c: Candidate, env: FunnelEnv) -> SizingDecision:
        """Unbounded mode (D-031): the trade gets its share of equity, capped only by the capital still free."""
        ub = self.cfg.unbounded
        share = float(ub["zero_to_hero_capital"] if c.sleeve else ub["capital_per_trade"])
        capital = min(env.equity_sod * share, env.free_capital())
        slip = self._slippage_per_unit(env.quote(c.instrument.key), c)
        kw = dict(capital=capital, equity=env.equity_sod, segment=c.segment, entry=c.entry, stop=c.stop,
                  lot_size=c.instrument.lot_size, leverage=float(ub["intraday_leverage"]))
        first = size_by_capital(per_unit_cost=slip, **kw)
        if not first.ok:
            return first
        return size_by_capital(per_unit_cost=self.per_unit_cost(c, first.full_qty) + slip, **kw)

    def _adjustments(self, c: Candidate, env: FunnelEnv, conf: Confidence) -> dict[str, float]:
        """Size can only decrease (A10.4)."""
        q = env.quote(c.instrument.key)
        adj: dict[str, float] = {}
        if q is not None:
            lim = MAX_SPREAD_BPS.get(c.segment, 50.0)
            adj["spread"] = 1.0 if q.spread_bps <= lim / 3 else max(0.5, 1 - (q.spread_bps - lim / 3) / lim)
        adj["uncertainty"] = 1.0 if conf.effective >= 0.6 else max(0.5, conf.effective / 0.6)
        vol = c.quality_features.get("volatility", 0.5)
        adj["volatility"] = 1.0 if vol >= 0.35 else 0.75   # abnormal vol -> smaller
        return adj

    # ------------------------------------------------------------------ S2..S5
    def evaluate(self, env: FunnelEnv) -> list[FunnelRecord]:
        ready: list[FunnelRecord] = []
        now = env.now
        for rec in list(self.records.values()):
            if rec.terminal or rec.status in ("PENDING_APPROVAL",):
                continue
            c = rec.candidate
            # S2 QUALIFY
            q = env.quote(c.instrument.key)
            dconf = min(env.data_confidence(c.instrument.key), env.data_confidence(c.underlying_key))
            checks = [
                ("regime_fit", c.regime_fit, "strategy not validated for current regime"),
                ("entry_window", env.entry_window_open, "outside entry window"),
                ("quote_available", q is not None and q.ltp > 0, "no live quote"),
                ("spread_ok", q is not None and q.spread_bps <= MAX_SPREAD_BPS.get(c.segment, 50.0),
                 f"spread {q.spread_bps:.0f}bps" if q else "no quote"),
                ("data_confidence", dconf >= float(self.cfg.data["min_data_confidence"]), f"data confidence {dconf:.2f}"),
                ("no_event_blackout", env.event_blackout is None, env.event_blackout or ""),
                ("not_banned", env.is_banned(c.underlying_key) is None, env.is_banned(c.underlying_key) or ""),
            ]
            if not self.cfg.is_unbounded:
                checks.append(("gear_allows_trading", env.gear is not Gear.HALTED, "HALTED"))
            rec.s2_checks = [{"name": n, "ok": ok, "detail": "" if ok else d} for n, ok, d in checks]
            fail = next((x for x in checks if not x[1]), None)
            if fail:
                self._drop(rec, PickupStage.S2_QUALIFY, f"{fail[0]}: {fail[2]}")
                continue
            conf = Confidence(c.model_confidence, dconf, c.regime_confidence, env.execution_confidence(c.instrument.key))
            rec.confidence = conf
            adj = self._adjustments(c, env, conf)
            fit = self._sizing(c, env, Grade.A, adj)
            if not fit.ok:
                label = "capital_fits" if self.cfg.is_unbounded else "one_lot_fits"
                self._drop(rec, PickupStage.S2_QUALIFY, f"{label}: {fit.skip_reason}")
                continue
            if not self.cfg.is_unbounded and env.bucket_room(c) <= 0:
                self._drop(rec, PickupStage.S2_QUALIFY, "correlation budget full for this direction")
                continue
            rec.stage = max(rec.stage, PickupStage.S2_QUALIFY, key=lambda s: list(PickupStage).index(s))

            # S3 GRADE: best grade whose own size still passes the margin of safety
            graded = self._grade(rec, env, adj)
            if graded is None:
                continue
            rec.stage = max(rec.stage, PickupStage.S3_GRADE, key=lambda s: list(PickupStage).index(s))

            # S4 CONFIRM: trigger + >= 2 independent confirmations (A9.2)
            if not (c.trigger_fired and c.independent_confirmations() >= 2):
                if now >= rec.expires_at:
                    self._drop(rec, PickupStage.S4_CONFIRM, "alpha half-life expired before confirmation")
                else:
                    rec.status = "ARMED"
                    rec.reason = (f"waiting: trigger={'yes' if c.trigger_fired else 'no'}, "
                                  f"{c.independent_confirmations()}/2 independent confirmations")
                continue
            rec.stage = PickupStage.S4_CONFIRM

            # S5 ARM: the full plan is computed BEFORE entry
            rec.plan = self._plan(rec, env)
            rec.stage = PickupStage.S5_ARM
            rec.status = "READY"
            rec.reason = "armed: plan computed, awaiting risk + approval"
            ev_net = rec.ev.net_r if rec.ev else 0.0
            rec.utility = ev_net * self.cfg.grades[rec.grade].multiplier * (conf.effective or 0) * (0.5 + c.liquidity_score)
            ready.append(rec)
        for s in PickupStage:
            self.stage_counts[s.value] = max(self.stage_counts[s.value], 0)
        return sorted(ready, key=lambda r: r.utility, reverse=True)

    def _grade(self, rec: FunnelRecord, env: FunnelEnv, adj: dict[str, float]) -> Grade | None:
        c = rec.candidate
        feats = dict(c.quality_features)
        best: tuple[Grade, EVResult, SizingDecision] | None = None
        first_ev: EVResult | None = None
        score = pct = None
        for g in (Grade.A_PLUS, Grade.A, Grade.B):
            s = self._sizing(c, env, g, adj)
            if not s.ok:
                continue
            risk_rupees = s.full_qty * c.unit_risk
            cost = self.per_unit_cost(c, s.full_qty) * s.full_qty + self._slippage_per_unit(env.quote(c.instrument.key), c) * s.full_qty
            # a sleeve ticket's maximum loss IS the premium: no gap-through allowance
            ub = self.cfg.is_unbounded
            e = ev_engine.evaluate(c.p_win, c.target_r, cost, risk_rupees,
                                   min_net_r=float(self.cfg.unbounded["min_net_ev_r"]) if ub else self.cfg.min_net_ev_r,
                                   min_gross_to_cost=1.0 if ub else self.cfg.min_gross_edge_to_cost,
                                   p_calibrated=c.p_win_calibrated, gap_prob=0.0 if c.sleeve else 0.04)
            first_ev = first_ev or e
            if score is None:
                feats["edge"] = max(0.0, min(1.0, 0.5 + e.net_r))
                feats["reward_risk"] = max(0.0, min(1.0, c.target_r / 4))
                score = quality_score(feats)
                if rec.quality is None:
                    self.quality.add(score)
                pct = self.quality.percentile(score)
                rec.quality, rec.quality_pct = score, pct
            got = grade_fn(self.cfg, net_ev_r=e.net_r, margin_ok=e.margin_ok, quality_pct=pct or 0.0,
                           effective_confidence=rec.confidence.effective if rec.confidence else 0.0)
            if ub and got is Grade.C and e.margin_ok:
                got = Grade.B          # unbounded: any setup with non-negative expected value after costs qualifies
            if got is not Grade.C and got.rank <= g.rank:
                best = (g, e, s)
                break
        if best is None:
            rec.ev = first_ev
            self._drop(rec, PickupStage.S3_GRADE, f"grade C: {first_ev.reason if first_ev else 'no size fits'}")
            return None
        rec.grade, rec.ev, rec.sizing = best
        if not self.cfg.is_unbounded and rec.grade not in self.cfg.gears[env.gear].grades:
            self._drop(rec, PickupStage.S3_GRADE, f"{rec.grade.value} not allowed in {env.gear.value}")
            return None
        return rec.grade

    def _plan(self, rec: FunnelRecord, env: FunnelEnv) -> StagePlanSpec:
        c, s, st = rec.candidate, rec.sizing, self.cfg.staging
        assert s is not None
        sign = c.direction.sign
        buf = max(float(self.cfg.execution["broker_stop_buffer_ticks"]) * c.instrument.tick_size,
                  float(self.cfg.execution.get("broker_stop_buffer_r", 0.15)) * c.unit_risk)
        window = min(max(HALF_LIFE_SECONDS[c.half_life], 300), 3600)
        return StagePlanSpec(
            entry_lo=c.entry - 0.1 * c.unit_risk if sign > 0 else c.entry,
            entry_hi=c.entry if sign > 0 else c.entry + 0.1 * c.unit_risk,
            stop=c.stop, broker_stop=c.stop - sign * buf, probe_qty=s.probe_qty, full_qty=s.full_qty,
            confirm_trigger=c.entry + sign * st["confirm_add_min_r"] * c.unit_risk,
            confirm_deadline=env.now + dt.timedelta(seconds=window), harvest_at_r=st["harvest_at_r"],
            harvest_fraction=st["harvest_fraction"], press_max_adds=int(st["press_max_adds"]),
            time_stop=env.now + dt.timedelta(seconds=HALF_LIFE_SECONDS[c.half_life] * 6),
            staging_mode=s.staging_mode,
        )

    # ------------------------------------------------------------------ terminal transitions
    def _drop(self, rec: FunnelRecord, stage: PickupStage, reason: str) -> None:
        rec.stage = stage
        rec.status = "DROPPED"
        rec.reason = reason
        key = reason.split(":")[0]
        self.drop_reasons[key] = self.drop_reasons.get(key, 0) + 1
        if self.on_terminal:
            self.on_terminal(rec)

    def reject(self, rec: FunnelRecord, reason: str, verdict: dict[str, Any] | None = None) -> None:
        rec.stage = PickupStage.S6_RISK
        rec.status = "REJECTED"
        rec.reason = reason
        rec.verdict = verdict or rec.verdict
        key = reason.split(":")[0]
        self.drop_reasons[key] = self.drop_reasons.get(key, 0) + 1
        if self.on_terminal:
            self.on_terminal(rec)

    def probed(self, rec: FunnelRecord) -> None:
        rec.stage = PickupStage.S7_PROBE
        rec.status = "PROBED"
        rec.reason = "probe order sent"
        if self.on_terminal:
            self.on_terminal(rec)

    def new_day(self) -> None:
        self.records.clear()
        self.drop_reasons.clear()
        self.stage_counts = {s.value: 0 for s in PickupStage}

    def funnel_counts(self) -> dict[str, int]:
        order = list(PickupStage)
        counts = {s.value: 0 for s in order}
        for rec in self.records.values():
            for s in order[: order.index(rec.stage) + 1]:
                counts[s.value] += 1
        return counts


def decision_object(*, rec: FunnelRecord | None, action: str, mode: Mode, gear: Gear, edge_ceiling: float | None,
                    tier: str, drawdown_state: str, now: dt.datetime, extra: dict[str, Any] | None = None,
                    rules_version: str = "", data_sources: list[str] | None = None,
                    feed_agreement: str = "AGREE") -> dict[str, Any]:
    """A18 Decision Output Object. Every proposed or rejected trade produces one."""
    c = rec.candidate if rec else None
    conf = rec.confidence if rec else None
    e = rec.ev if rec else None
    s = rec.sizing if rec else None
    d: dict[str, Any] = {
        "decision_id": new_id("dec"), "timestamp": now.isoformat(), "mode": mode.value, "action": action,
        "instrument": c.instrument.key if c else "", "symbol": c.instrument.symbol if c else "",
        "strategy": c.strategy if c else "", "strategy_version": c.strategy_version if c else "",
        "direction": c.direction.value if c else "", "exposure": c.exposure.value if c else "",
        "regime": (c.quality_features.get("regime_label") if c else "") or "", "volatility_regime": "", "expiry_regime": "",
        "entry": c.entry if c else 0, "stop": c.stop if c else 0, "exit_logic": "staged: probe/confirm/press/harvest/trail",
        "position_size": s.full_qty if s else 0, "capital_used": 0, "capital_at_risk": s.risk_at_full if s else 0,
        "risk_in_R": 1.0 if s else 0,
        "p_win_calibrated": c.p_win if c else 0, "p_is_calibrated": c.p_win_calibrated if c else False,
        "expected_win": e.win_r if e else 0, "expected_loss": e.loss_r if e else 0,
        "expected_costs": e.cost_r if e else 0, "expected_value": e.net_r if e else 0,
        "expected_payoff": c.target_r if c else 0,
        "model_confidence": conf.model if conf else 0, "data_confidence": conf.data if conf else 0,
        "regime_confidence": conf.regime if conf else 0, "execution_confidence": conf.execution if conf else 0,
        "effective_confidence": conf.effective if conf else 0,
        "liquidity_score": c.liquidity_score if c else 0, "trade_quality_score": rec.quality if rec and rec.quality else 0,
        "large_participant_score": (c.quality_features.get("participant", 0) * 100) if c else 0,
        "alpha_half_life": c.half_life.value if c else "", "correlated_exposure": "",
        "tier": tier, "drawdown_state": drawdown_state, "gear": gear.value, "edge_brake_ceiling": edge_ceiling or 0,
        "grade": rec.grade.value if rec and rec.grade else "C",
        "grade_multiplier": 0, "planned_risk": s.planned_risk if s else 0,
        "pickup_stage_reached": rec.stage.value if rec else "", "position_stage": "", "is_reentry": c.is_reentry if c else False,
        "data_sources": data_sources or ["SIM"], "feed_agreement": feed_agreement, "broker_side_stop_id": "",
        "risk_approval": "", "rejection_reason": rec.reason if rec and rec.status in ("DROPPED", "REJECTED") else "",
        "operator_approval": "NOT_REQUIRED", "algo_id": "", "rules_version": rules_version,
        "data_snapshot_id": "", "explanation": c.explanation if c else "",
        "risk_checks": (rec.verdict or {}).get("checks", []) if rec else [],
        "s2_checks": rec.s2_checks if rec else [],
    }
    if extra:
        d.update(extra)
    return d


def sides_for(direction: Direction) -> tuple[Side, Side]:
    return (Side.BUY, Side.SELL) if direction is Direction.LONG else (Side.SELL, Side.BUY)
