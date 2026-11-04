"""Regime, volatility, expiry and change-point engines (A6.7, A6.14, A6.15) -- features, not beliefs.
Every output carries a confidence and the latency class of its inputs."""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np

from sthitaprajna.domain import Candle
from sthitaprajna.market import indicators as ind


@dataclass
class RegimeState:
    trend: str = "RANGE"              # TREND_UP | TREND_DOWN | RANGE
    trend_strength: float = 0.0       # ADX-like 0..100
    volatility: str = "NORMAL_VOL"    # LOW_VOL | NORMAL_VOL | HIGH_VOL | EXTREME_VOL (+ _EXPANSION/_COMPRESSION tags)
    vol_tags: tuple[str, ...] = ()
    expiry: str | None = None         # PINNING | TREND_EXPIRY | BREAKOUT_EXPIRY | VOLATILITY_CRUSH | GAMMA_ACCELERATION | RANGE_EXPIRY
    change_point: bool = False
    confidence: float = 0.5
    breadth_ad: float = 1.0
    vix: float = 0.0
    vix_change: float = 0.0
    latency: str = "real-time (bar close)"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["vol_tags"] = list(self.vol_tags)
        return d


def vol_regime(vix: float, vix_prev_close: float) -> tuple[str, tuple[str, ...]]:
    lvl = "LOW_VOL" if vix < 11 else "NORMAL_VOL" if vix < 16 else "HIGH_VOL" if vix < 22 else "EXTREME_VOL"
    tags: list[str] = []
    if vix_prev_close > 0:
        ch = vix / vix_prev_close - 1
        if ch > 0.08:
            tags.append("VOL_EXPANSION")
        elif ch < -0.08:
            tags.append("VOL_COMPRESSION")
    return lvl, tuple(tags)


def trend_regime(c5: list[Candle]) -> tuple[str, float, float]:
    if len(c5) < 30:
        return "RANGE", 0.0, 0.3
    x = ind.closes(c5)
    e20, e50 = ind.ema(x, 20)[-1], ind.ema(x, 50)[-1]
    a = ind.adx(c5[-80:], 14)
    if a >= 22 and e20 > e50 and x[-1] > e20:
        return "TREND_UP", a, min(0.9, 0.5 + a / 100)
    if a >= 22 and e20 < e50 and x[-1] < e20:
        return "TREND_DOWN", a, min(0.9, 0.5 + a / 100)
    return "RANGE", a, 0.55


def change_point(c1: list[Candle], window: int = 60) -> bool:
    """Volatility change point: last-window realised vol vs the prior 5x window (simple ratio test)."""
    if len(c1) < window * 6:
        return False
    r = np.diff(np.log(ind.closes(c1[-window * 6:])))
    recent, base = r[-window:], r[:-window]
    s_recent, s_base = float(np.std(recent)), float(np.std(base))
    return s_base > 0 and (s_recent / s_base > 2.2 or s_recent / s_base < 0.35)


def expiry_regime(is_expiry: bool, spot: float, max_pain: float, straddle_now: float, straddle_open: float,
                  trend: str, broke_wall: bool, vix_change: float) -> str | None:
    if not is_expiry:
        return None
    if straddle_open > 0 and straddle_now / straddle_open > 1.1 and vix_change > 0:
        return "GAMMA_ACCELERATION"
    if broke_wall:
        return "BREAKOUT_EXPIRY"
    if straddle_open > 0 and straddle_now / straddle_open < 0.45 and vix_change < -0.05:
        return "VOLATILITY_CRUSH"
    if trend in ("TREND_UP", "TREND_DOWN"):
        return "TREND_EXPIRY"
    if max_pain and abs(spot - max_pain) / spot < 0.002:
        return "PINNING"
    return "RANGE_EXPIRY"


def realised_vol(c1: list[Candle], minutes: int = 120) -> float:
    if len(c1) < minutes + 1:
        return 0.0
    r = np.diff(np.log(ind.closes(c1[-(minutes + 1):])))
    return float(np.std(r) * math.sqrt(375 * 252))
