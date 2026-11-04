"""Strategy interface + shared bar-close features.

Strategies are HYPOTHESES (A7): each carries a hypothesis card and starts UNVALIDATED. Their P(win)
values are priors, flagged p_win_calibrated=False, until Phase 4-5 walk-forward + calibration replace
them. Strategies only emit candidates; the funnel, Aggression Layer and Risk Guardian decide everything.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Protocol

from sthitaprajna.domain import Candidate, Candle, Direction, HypothesisCard, Instrument, OptionType, Quote
from sthitaprajna.intelligence.regime import RegimeState
from sthitaprajna.market import indicators as ind
from sthitaprajna.market.calendar import MarketCalendar
from sthitaprajna.market.options import ChainRow


class StrategyContext(Protocol):
    now: dt.datetime
    cal: MarketCalendar
    regime: RegimeState
    equity_sod: float

    def candles(self, key: str) -> list[Candle]: ...
    def quote(self, key: str) -> Quote | None: ...
    def instrument(self, key: str) -> Instrument: ...
    def stock_keys(self) -> list[str]: ...
    def index_keys(self) -> list[str]: ...
    def breadth(self) -> dict[str, float]: ...
    def sector_change(self, sector: str) -> float: ...
    def chain(self, underlying: str, expiry: dt.date | None = None) -> tuple[float, list[ChainRow]]: ...
    def expiries(self, underlying: str) -> list[dt.date]: ...
    def option_instrument(self, underlying: str, expiry: dt.date, strike: float, ot: OptionType) -> Instrument: ...
    def option_underlyings(self) -> list[tuple[str, str]]: ...
    def sleeve_cfg(self) -> dict[str, float]: ...
    def reversal_watch(self, key: str) -> Direction | None: ...
    def iv_history(self, underlying: str) -> list[float]: ...
    def log(self, msg: str) -> None: ...


@dataclass
class Features:
    key: str
    price: float
    vwap: float
    atr1: float
    atr5: float
    or_high: float | None
    or_low: float | None
    ema20: float
    ema50: float
    adx: float
    rvol: float
    c5: list[Candle]
    day: list[Candle]
    htf_up: bool
    htf_down: bool
    bar_id: str

    @property
    def above_vwap(self) -> bool:
        return self.price > self.vwap


def compute_features(ctx: StrategyContext, key: str) -> Features | None:
    cache = getattr(ctx, "_feat_cache", None)
    if cache is not None and key in cache:
        return cache[key]
    f = _compute_features(ctx, key)
    if cache is not None:
        cache[key] = f
    return f


def _compute_features(ctx: StrategyContext, key: str) -> Features | None:
    c1 = ctx.candles(key)
    if len(c1) < 200:
        return None
    day = ind.session_candles(c1)
    if len(day) < 15:
        return None
    recent = c1[-375 * 3:]
    c5 = ind.resample(recent, 5)
    c15 = ind.resample(recent, 15)
    x5 = ind.closes(c5)
    e20, e50 = ind.ema(x5, 20)[-1], ind.ema(x5, 50)[-1]
    x15 = ind.closes(c15)
    h20, h50 = (ind.ema(x15, 20)[-1], ind.ema(x15, 50)[-1]) if len(x15) >= 10 else (e20, e50)
    orng = ind.opening_range(c1, 15)
    return Features(
        key=key, price=c1[-1].close, vwap=ind.session_vwap(c1), atr1=ind.atr(c1[-60:], 14),
        atr5=ind.atr(c5[-60:], 14), or_high=orng[0] if orng else None, or_low=orng[1] if orng else None,
        ema20=float(e20), ema50=float(e50), adx=ind.adx(c5[-80:], 14), rvol=ind.relative_volume(c1),
        c5=c5, day=day, htf_up=bool(h20 > h50), htf_down=bool(h20 < h50),
        bar_id=f"{key}@{c1[-1].start:%Y%m%d%H%M}",
    )


class Strategy:
    name: str = "base"
    version: str = "0.1.0"
    card: HypothesisCard

    def scan(self, ctx: StrategyContext) -> list[Candidate]:  # pragma: no cover - interface
        raise NotImplementedError

    def info(self) -> dict[str, Any]:
        return {"name": self.name, "version": self.version, "card": self.card.__dict__}


def clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))
