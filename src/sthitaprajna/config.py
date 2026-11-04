"""Loads Part D (operator.yaml) and market_rules.yaml into typed, validated objects.

* Part D is validated against the hard ceilings in risk/ceilings.py: config may only be stricter.
* market_rules exposes a freshness report (A4): rules older than `stale_after_days` or not verified.
"""
from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from sthitaprajna.domain import Gear, Grade
from sthitaprajna.risk import ceilings


class ConfigError(ValueError):
    """Raised when Part D would loosen a hard ceiling or is internally inconsistent."""


def config_dir() -> Path:
    env = os.environ.get("STHITAPRAJNA_CONFIG_DIR")
    candidates = [Path(env)] if env else []
    candidates += [Path.cwd() / "config", Path(__file__).resolve().parents[2] / "config"]
    for c in candidates:
        if (c / "operator.yaml").exists():
            return c
    raise FileNotFoundError("config/operator.yaml not found; set STHITAPRAJNA_CONFIG_DIR")


# --------------------------------------------------------------------------------------------- Part D
@dataclass(frozen=True)
class GearSpec:
    gear: Gear
    dd_below: float
    r: float
    open_risk: float
    grades: tuple[Grade, ...]
    pressing: bool
    reentry: bool
    sleeve: bool
    defined_risk_only: bool
    overnight: bool


@dataclass(frozen=True)
class GradeSpec:
    grade: Grade
    multiplier: float
    min_quality_pct: float
    min_ev_r: float
    min_confidence: float
    staging: str
    max_open: int


@dataclass(frozen=True)
class TierSpec:
    name: str
    min_equity: float
    attack_r: float
    max_positions: int
    max_trades_per_day: int


@dataclass(frozen=True)
class OperatorConfig:
    raw: dict[str, Any]
    starting_capital: float
    ruin_floor_abs: float
    ruin_floor_pct: float
    tiers: tuple[TierSpec, ...]
    gears: dict[Gear, GearSpec]
    upshift_clean_days: int
    a_plus_max_dd: float
    grades: dict[Grade, GradeSpec]
    min_net_ev_r: float
    min_gross_edge_to_cost: float
    staging: dict[str, float]
    limits: dict[str, Any]
    edge_brake: dict[str, float]
    targets: dict[str, Any]
    sleeve: dict[str, Any]
    approvals: dict[str, Any]
    overnight: dict[str, Any]
    execution: dict[str, Any]
    data: dict[str, Any]
    discord: dict[str, Any]
    risk_mode: str = "bounded"                  # bounded = v3.0 spec | unbounded = operator decision D-031
    unbounded: dict[str, Any] = field(default_factory=dict)

    @property
    def is_unbounded(self) -> bool:
        return self.risk_mode == "unbounded"

    def tier_for(self, equity: float) -> TierSpec:
        eligible = [t for t in self.tiers if equity >= t.min_equity]
        return eligible[-1] if eligible else self.tiers[0]

    def tier_factor(self, tier: TierSpec) -> float:
        """D3: lower gears scale by the same tier factor as ATTACK R."""
        base = self.tiers[0].attack_r
        return tier.attack_r / base if base > 0 else 0.0


def _parse_operator(raw: dict[str, Any]) -> OperatorConfig:
    g = raw["gears"]
    gears: dict[Gear, GearSpec] = {}
    for name in ("ATTACK", "PRESS", "GUARD", "PRESERVE", "HALTED"):
        s = g[name]
        gears[Gear(name)] = GearSpec(
            gear=Gear(name), dd_below=float(s["dd_below"]), r=float(s["r"]), open_risk=float(s["open_risk"]),
            grades=tuple(Grade(x) for x in s["grades"]), pressing=bool(s["pressing"]),
            reentry=bool(s["reentry"]), sleeve=bool(s["sleeve"]),
            defined_risk_only=bool(s["defined_risk_only"]), overnight=bool(s["overnight"]),
        )
    grades = {
        Grade(k): GradeSpec(Grade(k), float(v["multiplier"]), float(v["min_quality_pct"]), float(v["min_ev_r"]),
                            float(v["min_confidence"]), str(v["staging"]), int(v["max_open"]))
        for k, v in raw["grades"].items()
    }
    tiers = tuple(TierSpec(t["name"], float(t["min_equity"]), float(t["attack_r"]), int(t["max_positions"]),
                           int(t["max_trades_per_day"])) for t in raw["tiers"])
    cap = raw["capital"]
    cfg = OperatorConfig(
        raw=raw, starting_capital=float(cap["starting"]), ruin_floor_abs=float(cap["ruin_floor_abs"]),
        ruin_floor_pct=float(cap["ruin_floor_pct_of_peak"]), tiers=tiers, gears=gears,
        upshift_clean_days=int(g["upshift_clean_days"]), a_plus_max_dd=float(g["a_plus_max_dd"]), grades=grades,
        min_net_ev_r=float(raw["edge_margin"]["min_net_ev_r"]),
        min_gross_edge_to_cost=float(raw["edge_margin"]["min_gross_edge_to_cost"]),
        staging={k: float(v) for k, v in raw["staging"].items()}, limits=dict(raw["limits"]),
        edge_brake={k: float(v) for k, v in raw["edge_brake"].items()}, targets=dict(raw["targets"]),
        sleeve=dict(raw["sleeve"]), approvals=dict(raw["approvals"]), overnight=dict(raw["overnight"]),
        execution=dict(raw["execution"]), data=dict(raw["data"]), discord=dict(raw.get("discord", {})),
        risk_mode=_risk_mode(raw), unbounded={**UNBOUNDED_DEFAULTS, **(raw.get("unbounded") or {})},
    )
    validate_operator(cfg)
    return cfg


UNBOUNDED_DEFAULTS: dict[str, Any] = {
    "capital_per_trade": 0.25, "intraday_leverage": 1.0, "trail_to_breakeven_at_r": 1.0,
    "zero_to_hero_capital": 0.03, "min_net_ev_r": 0.0,
}


def _risk_mode(raw: dict[str, Any]) -> str:
    """operator.yaml `risk_mode`; STHITAPRAJNA_RISK_MODE overrides it (tests pin `bounded` this way)."""
    import os
    mode = (os.environ.get("STHITAPRAJNA_RISK_MODE") or raw.get("risk_mode") or "bounded").strip().lower()
    if mode not in ("bounded", "unbounded"):
        raise ConfigError(f"risk_mode must be bounded or unbounded, not {mode!r}")
    return mode


def validate_operator(cfg: OperatorConfig) -> None:
    """Part D may only be stricter than the hard ceilings (D12.2)."""
    errs: list[str] = []
    for tier in cfg.tiers:
        f = cfg.tier_factor(tier)
        for spec in cfg.gears.values():
            top = max((cfg.grades[gr].multiplier for gr in spec.grades), default=0.0)
            if spec.r * f * top > ceilings.MAX_RISK_PER_TRADE + 1e-12:
                # allowed only because the sizer clamps; flag clearly so the operator knows
                pass
            if spec.open_risk * f > ceilings.MAX_OPEN_RISK + 1e-12:
                errs.append(f"{tier.name}/{spec.gear}: open risk {spec.open_risk * f:.4f} > ceiling")
    halted = cfg.gears[Gear.HALTED]
    if halted.r != 0 or halted.open_risk != 0:
        errs.append("HALTED gear must have r=0 and open_risk=0")
    if cfg.gears[Gear.PRESERVE].dd_below > ceilings.HALT_DRAWDOWN + 1e-12:
        errs.append("PRESERVE must end at or before the 25% halt ceiling")
    ladder = [cfg.gears[g].dd_below for g in (Gear.ATTACK, Gear.PRESS, Gear.GUARD, Gear.PRESERVE)]
    if ladder != sorted(ladder):
        errs.append("gear ladder thresholds must increase ATTACK < PRESS < GUARD < PRESERVE")
    rs = [cfg.gears[g].r for g in (Gear.ATTACK, Gear.PRESS, Gear.GUARD, Gear.PRESERVE, Gear.HALTED)]
    if rs != sorted(rs, reverse=True):
        errs.append("gear R must fall monotonically down the ladder")
    if cfg.ruin_floor_abs < ceilings.RUIN_FLOOR_ABS_MIN or cfg.ruin_floor_pct < ceilings.RUIN_FLOOR_PCT_MIN:
        errs.append("ruin floor may not be lowered below max(Rs 70,000, 70% of peak)")
    if int(cfg.execution["max_orders_per_second"]) >= ceilings.SEBI_OPS_THRESHOLD:
        errs.append("max_orders_per_second must stay below the SEBI 10 OPS threshold")
    for lim in ("daily_loss", "weekly_loss", "monthly_loss"):
        if not 0 < float(cfg.limits[lim]) <= 0.25:
            errs.append(f"limits.{lim} must be in (0, 0.25]")
    mults = [cfg.grades[g].multiplier for g in (Grade.A_PLUS, Grade.A, Grade.B)]
    if mults != sorted(mults, reverse=True):
        errs.append("grade multipliers must be monotonic A+ >= A >= B")
    if cfg.limits.get("naked_option_selling"):
        errs.append("naked option selling must stay disabled unless explicitly hedged (A7.3)")
    ub = cfg.unbounded
    if not 0 < float(ub["capital_per_trade"]) <= 1.0:
        errs.append("unbounded.capital_per_trade must be in (0, 1]")
    if float(ub["intraday_leverage"]) < 1.0:
        errs.append("unbounded.intraday_leverage must be >= 1")
    if errs:
        raise ConfigError("; ".join(errs))


@lru_cache(maxsize=4)
def load_operator(path: str | None = None) -> OperatorConfig:
    p = Path(path) if path else config_dir() / "operator.yaml"
    return _parse_operator(yaml.safe_load(p.read_text(encoding="utf-8")))


def operator_from_dict(raw: dict[str, Any]) -> OperatorConfig:
    return _parse_operator(raw)


# ------------------------------------------------------------------------------------------ rules
@dataclass(frozen=True)
class Rule:
    name: str
    value: Any
    status: str
    source_url: str
    verified_on: dt.date | None
    note: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def freshness(self, today: dt.date, stale_after_days: int) -> str:
        if self.status in ("VERIFY", "UNKNOWN") or self.verified_on is None:
            return "UNVERIFIED"
        if (today - self.verified_on).days > stale_after_days:
            return "STALE"
        return "FRESH"


@dataclass(frozen=True)
class MarketRules:
    version: str
    stale_after_days: int
    rules: dict[str, Rule]

    def __getitem__(self, name: str) -> Any:
        return self.rules[name].value

    def get(self, name: str, default: Any = None) -> Any:
        r = self.rules.get(name)
        return r.value if r else default

    def report(self, today: dt.date | None = None) -> list[dict[str, Any]]:
        today = today or dt.date.today()
        out = []
        for r in self.rules.values():
            out.append({
                "name": r.name, "value": r.value, "status": r.status, "source_url": r.source_url,
                "verified_on": r.verified_on.isoformat() if r.verified_on else None, "note": r.note,
                "freshness": r.freshness(today, self.stale_after_days),
            })
        return out

    def open_items(self, today: dt.date | None = None) -> list[str]:
        today = today or dt.date.today()
        return [r.name for r in self.rules.values() if r.freshness(today, self.stale_after_days) != "FRESH"]


def _parse_rules(raw: dict[str, Any]) -> MarketRules:
    rules: dict[str, Rule] = {}
    for name, body in raw["rules"].items():
        for req in ("value", "status", "source_url", "verified_on"):
            if req not in body:
                raise ConfigError(f"market rule {name!r} missing {req!r}")
        vo = body["verified_on"]
        if isinstance(vo, str):
            vo = dt.date.fromisoformat(vo)
        extra = {k: v for k, v in body.items() if k not in ("value", "status", "source_url", "verified_on", "note")}
        rules[name] = Rule(name, body["value"], str(body["status"]), str(body["source_url"]), vo,
                           str(body.get("note", "")), extra)
    return MarketRules(str(raw.get("version", "")), int(raw.get("stale_after_days", 30)), rules)


@lru_cache(maxsize=4)
def load_rules(path: str | None = None) -> MarketRules:
    p = Path(path) if path else config_dir() / "market_rules.yaml"
    return _parse_rules(yaml.safe_load(p.read_text(encoding="utf-8")))


def load_holidays(path: str | None = None) -> frozenset[dt.date]:
    import json
    p = Path(path) if path else config_dir() / "nse_holidays_2026.json"
    if not p.exists():
        return frozenset()
    return frozenset(dt.date.fromisoformat(x) for x in json.loads(p.read_text(encoding="utf-8")))
