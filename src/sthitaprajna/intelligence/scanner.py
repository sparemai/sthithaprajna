"""Opportunity Scanner over the whole stock universe (spec A5.1 / S1 SCAN, "continuously rank the allowed stock
universe"): which stocks are booming (or breaking down) right now, and can they be traded intraday at all?

A heuristic screen, not a trading signal: the component weights are PLACEHOLDERS until Phase 5 calibration, and
strategies still decide entries on their own analysis. Every score carries its reasons ("flags") so the UI and
the log can say why a stock is on the list.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any

WEIGHTS = {"move": 0.25, "rvol": 0.25, "breakout": 0.20, "vwap": 0.10, "momentum": 0.10, "flow": 0.10}  # PLACEHOLDER
WEIGHTS_STATUS = "PLACEHOLDER (uncalibrated) -- rank only"
SESSION_MINUTES = 375


@dataclass
class ScreenInput:
    key: str
    symbol: str
    name: str = ""
    sector: str = ""
    series: str = "EQ"
    fno: bool = False
    lot: int = 1
    ltp: float = 0.0
    prev_close: float = 0.0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    volume: float = 0.0
    vwap: float = 0.0
    w52_high: float = 0.0
    w52_low: float = 0.0
    upper_circuit: float = 0.0
    lower_circuit: float = 0.0
    buy_qty: float = 0.0
    sell_qty: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    mom5: float | None = None          # price change over the last 5 minutes
    mom15: float | None = None
    avg_vol_20: float | None = None    # baseline: 20-day average daily volume
    prev_high: float | None = None
    prev_low: float | None = None
    atr_pct: float | None = None       # baseline: 14-day ATR as a fraction of price
    surveillance: str = ""             # ASM/GSM stage when the master reports it
    active: bool = False               # already in the strategy universe (full bars + history)


@dataclass
class ScreenRow:
    key: str
    symbol: str
    name: str
    sector: str
    fno: bool
    lot: int
    ltp: float
    chg_pct: float
    gap_pct: float | None
    rvol: float | None
    vwap_dist_pct: float | None
    range_pos: float | None
    w52_dist_pct: float | None
    turnover_cr: float
    imbalance: float | None
    spread_bps: float | None
    score: float
    score_up: float
    score_down: float
    direction: str
    flags: list[str] = field(default_factory=list)
    tradable: bool = True
    why_not: str = ""
    active: bool = False

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k, v in d.items():
            if isinstance(v, float):
                d[k] = round(v, 4)
        return d


def expected_volume_fraction(minutes_into: float) -> float:
    """Share of a normal day's volume traded by this minute: a U-shaped intraday profile (heavy open and close)."""
    x = min(max(minutes_into, 0.0), SESSION_MINUTES) / SESSION_MINUTES
    if x <= 0:
        return 0.0

    def cum(t: float) -> float:   # integral of 0.75 + 0.95 e^(-9t) + 0.45 e^(-12(1-t))
        return 0.75 * t + 0.95 / 9 * (1 - math.exp(-9 * t)) + 0.45 / 12 * (math.exp(-12 * (1 - t)) - math.exp(-12))
    return cum(x) / cum(1.0)


def _clip(x: float) -> float:
    return 0.0 if x != x else max(0.0, min(1.0, x))


def _blend(parts: dict[str, float | None]) -> float:
    have = {k: v for k, v in parts.items() if v is not None}
    w = sum(WEIGHTS[k] for k in have)
    return sum(WEIGHTS[k] * v for k, v in have.items()) / w if w > 0 else 0.0


def min_turnover_cr(minutes_into: float) -> float:
    """Liquidity bar that grows through the day: Rs 2 Cr traded at the open to Rs 20 Cr by the close."""
    return 2.0 + 18.0 * min(max(minutes_into, 0.0), SESSION_MINUTES) / SESSION_MINUTES


def score(s: ScreenInput, minutes_into: float) -> ScreenRow:
    ltp, pc = s.ltp, s.prev_close
    chg = ltp / pc - 1 if ltp > 0 and pc > 0 else 0.0
    gap = s.open / pc - 1 if s.open > 0 and pc > 0 else None
    rng = (ltp - s.low) / (s.high - s.low) if s.high > s.low > 0 else None
    vwap_d = ltp / s.vwap - 1 if s.vwap > 0 and ltp > 0 else None
    w52_d = ltp / s.w52_high - 1 if s.w52_high > 0 and ltp > 0 else None
    turnover = ltp * s.volume / 1e7
    frac = expected_volume_fraction(minutes_into)
    rvol = (s.volume / (s.avg_vol_20 * frac)) if s.avg_vol_20 and frac > 0.002 else None
    imb = math.log(s.buy_qty / s.sell_qty) if s.buy_qty > 0 and s.sell_qty > 0 else None
    spread = (s.ask - s.bid) / ((s.ask + s.bid) / 2) * 1e4 if s.ask > 0 and s.bid > 0 and s.ask >= s.bid else None
    atr = s.atr_pct if s.atr_pct and s.atr_pct > 0 else 0.02

    def side(sign: int) -> tuple[float, list[str]]:
        flags: list[str] = []
        move = _clip(sign * chg / (1.5 * atr))
        if gap is not None and sign * gap >= 0.01:
            flags.append(f"GAP {'UP' if sign > 0 else 'DOWN'} {abs(gap):.1%}")
        brk = 0.0
        if rng is not None and (rng if sign > 0 else 1 - rng) >= 0.9:
            brk = max(brk, (rng if sign > 0 else 1 - rng))
            flags.append("AT DAY HIGH" if sign > 0 else "AT DAY LOW")
        level = s.prev_high if sign > 0 else s.prev_low
        if level and sign * (ltp - level) > 0:
            brk = 1.0
            flags.append("ABOVE PREV HIGH" if sign > 0 else "BELOW PREV LOW")
        if sign > 0 and s.w52_high > 0:
            if ltp >= s.w52_high * 0.998:
                brk = 1.0
                flags.append("NEW 52W HIGH")
            elif w52_d is not None and w52_d >= -0.02:
                brk = max(brk, 0.6)
                flags.append("NEAR 52W HIGH")
        if sign < 0 and s.w52_low > 0 and ltp <= s.w52_low * 1.002:
            brk = 1.0
            flags.append("NEW 52W LOW")
        vw = _clip(sign * vwap_d / 0.01) if vwap_d is not None else None
        if vw and vw > 0.3:
            flags.append("ABOVE VWAP" if sign > 0 else "BELOW VWAP")
        mom = _clip(sign * s.mom15 / 0.01) if s.mom15 is not None else None
        flow = _clip(sign * imb / 1.0) if imb is not None else None
        if flow and flow > 0.5:
            ratio = math.exp(abs(imb))
            flags.append(f"{'BUYERS' if sign > 0 else 'SELLERS'} {ratio:.1f}x")
        rv = _clip((rvol - 1) / 2) if rvol is not None else None
        sc = _blend({"move": move, "rvol": rv if sign * chg > 0 else (0.0 if rv is not None else None),
                     "breakout": brk, "vwap": vw, "momentum": mom, "flow": flow})
        return sc, flags

    up, up_flags = side(+1)
    down, down_flags = side(-1)
    direction = "UP" if up >= down else "DOWN"
    flags = up_flags if direction == "UP" else down_flags
    if rvol is not None and rvol >= 1.5:
        flags.insert(0, f"RVOL {rvol:.1f}x")

    why = ""
    if s.series.upper() not in ("EQ", ""):
        why = f"{s.series} series: no intraday (trade-to-trade or SME)"
    elif s.surveillance:
        why = f"under surveillance ({s.surveillance})"
    elif ltp < 20:
        why = "price below Rs 20"
    elif turnover < min_turnover_cr(minutes_into):
        why = f"illiquid today (Rs {turnover:.1f} Cr traded)"
    elif spread is not None and spread > 25:
        why = f"spread {spread:.0f} bps"
    else:
        room = (s.upper_circuit / ltp - 1) if direction == "UP" else (1 - s.lower_circuit / ltp)
        if (s.upper_circuit if direction == "UP" else s.lower_circuit) > 0 and room < 0.015:
            why = f"within {room:.1%} of the {'upper' if direction == 'UP' else 'lower'} circuit"
            flags.append("NEAR CIRCUIT")
    return ScreenRow(s.key, s.symbol, s.name, s.sector, s.fno, s.lot, ltp, chg * 100,
                     gap * 100 if gap is not None else None, rvol, vwap_d * 100 if vwap_d is not None else None,
                     rng, w52_d * 100 if w52_d is not None else None, turnover, imb, spread,
                     max(up, down), up, down, direction, flags, not why, why, s.active)


def scan(inputs: list[ScreenInput], minutes_into: float) -> list[ScreenRow]:
    rows = [score(s, minutes_into) for s in inputs if s.ltp > 0]
    rows.sort(key=lambda r: (r.tradable, r.score), reverse=True)
    return rows


def baseline_from_daily(bars: list[Any], atr_n: int = 14, vol_n: int = 20) -> dict[str, float] | None:
    """From completed daily candles (oldest first): 20-day average volume, previous day's levels, ATR%."""
    if len(bars) < 3:
        return None
    last = bars[-1]
    vols = [b.volume for b in bars[-vol_n:] if b.volume > 0]
    trs = []
    for prev, b in zip(bars[:-1], bars[1:]):
        trs.append(max(b.high - b.low, abs(b.high - prev.close), abs(b.low - prev.close)))
    atr = sum(trs[-atr_n:]) / len(trs[-atr_n:]) if trs else 0.0
    return {"avg_vol_20": sum(vols) / len(vols) if vols else 0.0, "prev_high": last.high, "prev_low": last.low,
            "prev_close": last.close, "atr_pct": atr / last.close if last.close > 0 else 0.0,
            "days": float(len(bars)), "as_of": last.start.date().isoformat()}
