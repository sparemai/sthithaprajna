"""Option-chain analytics (A6.5): PCR, max pain, OI walls, straddle-implied move, skew.

Works on the normalised chain rows produced by any adapter (Dhan, Upstox or SIM).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ChainRow:
    strike: float
    ce_key: str
    pe_key: str
    ce_ltp: float
    pe_ltp: float
    ce_bid: float
    ce_ask: float
    pe_bid: float
    pe_ask: float
    ce_oi: float
    pe_oi: float
    ce_oi_chg: float
    pe_oi_chg: float
    ce_vol: float
    pe_vol: float
    ce_iv: float
    pe_iv: float
    ce_delta: float
    pe_delta: float
    ce_gamma: float = 0.0
    pe_gamma: float = 0.0
    ce_theta: float = 0.0
    pe_theta: float = 0.0
    ce_vega: float = 0.0
    pe_vega: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in self.__dict__.items()}


def atm_strike(rows: list[ChainRow], spot: float) -> float:
    return min(rows, key=lambda r: abs(r.strike - spot)).strike if rows else spot


def pcr(rows: list[ChainRow]) -> dict[str, float]:
    ce_oi = sum(r.ce_oi for r in rows)
    pe_oi = sum(r.pe_oi for r in rows)
    ce_v = sum(r.ce_vol for r in rows)
    pe_v = sum(r.pe_vol for r in rows)
    return {"oi": pe_oi / ce_oi if ce_oi else 0.0, "volume": pe_v / ce_v if ce_v else 0.0}


def max_pain(rows: list[ChainRow]) -> float:
    """Strike minimising total intrinsic payout to option holders. Weak evidence -- test it (A6.5)."""
    if not rows:
        return 0.0
    best, best_val = rows[0].strike, float("inf")
    for cand in rows:
        k = cand.strike
        pain = sum(max(0.0, k - r.strike) * r.ce_oi + max(0.0, r.strike - k) * r.pe_oi for r in rows)
        if pain < best_val:
            best, best_val = k, pain
    return best


def oi_walls(rows: list[ChainRow], spot: float, top: int = 3) -> dict[str, list[dict[str, float]]]:
    calls = sorted((r for r in rows if r.strike >= spot), key=lambda r: r.ce_oi, reverse=True)[:top]
    puts = sorted((r for r in rows if r.strike <= spot), key=lambda r: r.pe_oi, reverse=True)[:top]
    return {
        "resistance": [{"strike": r.strike, "oi": r.ce_oi, "chg": r.ce_oi_chg} for r in calls],
        "support": [{"strike": r.strike, "oi": r.pe_oi, "chg": r.pe_oi_chg} for r in puts],
    }


def straddle_move(rows: list[ChainRow], spot: float) -> dict[str, float]:
    """ATM straddle as implied move to expiry (A6.5)."""
    if not rows:
        return {"straddle": 0.0, "pct": 0.0}
    k = atm_strike(rows, spot)
    r = next(x for x in rows if x.strike == k)
    s = r.ce_ltp + r.pe_ltp
    return {"straddle": s, "pct": s / spot if spot else 0.0, "strike": k}


def risk_reversal_25d(rows: list[ChainRow]) -> float:
    """Approximate 25-delta risk reversal: IV(25d call) - IV(25d put)."""
    if not rows:
        return 0.0
    c = min(rows, key=lambda r: abs(r.ce_delta - 0.25))
    p = min(rows, key=lambda r: abs(r.pe_delta + 0.25))
    return c.ce_iv - p.pe_iv


# --------------------------------------------------------------------------------------- A6.4 OI build-up
BUILDUP_LABELS = ("LONG_BUILDUP", "SHORT_BUILDUP", "SHORT_COVERING", "LONG_UNWINDING", "FLAT")


def _buildup(price_up: bool | None, oi_chg: float, flat_tol: float = 0.0) -> str:
    """A6.4's price+OI matrix, applied per option side. `price_up` is the UNDERLYING's direction (the
    classic definition), not the option premium's -- an option's own premium reacts to IV/theta too and
    would misclassify writer behaviour. None (no reliable price-change input) or OI flat -> FLAT."""
    if price_up is None or abs(oi_chg) <= flat_tol:
        return "FLAT"
    if price_up:
        return "LONG_BUILDUP" if oi_chg > 0 else "SHORT_COVERING"
    return "SHORT_BUILDUP" if oi_chg > 0 else "LONG_UNWINDING"


def oi_buildup(rows: list[ChainRow], underlying_chg_pct: float | None, near: int = 5,
              spot: float = 0.0) -> dict[str, Any]:
    """Classifies each of the `near` strikes closest to spot on both sides (A6.4), plus an aggregate call
    for the whole chain from total OI change. `underlying_chg_pct` is the underlying's move since the
    reference used for the rest of the chain (e.g. previous close); None when that reference isn't known
    yet (first tick of the session) -- every row then reports FLAT rather than a guess."""
    price_up = None if underlying_chg_pct is None else underlying_chg_pct > 0
    ordered = sorted(rows, key=lambda r: abs(r.strike - spot)) if spot else rows
    per_strike = [
        {"strike": r.strike, "ce": _buildup(price_up, r.ce_oi_chg), "pe": _buildup(price_up, r.pe_oi_chg)}
        for r in ordered[:near]
    ]
    ce_total = sum(r.ce_oi_chg for r in rows)
    pe_total = sum(r.pe_oi_chg for r in rows)
    return {
        "underlying_chg_pct": underlying_chg_pct, "per_strike": per_strike,
        "aggregate": {"ce": _buildup(price_up, ce_total), "pe": _buildup(price_up, pe_total)},
    }


# --------------------------------------------------------------------------------------- A6.5/A6.7 vol
def iv_rank(current: float, history: list[float]) -> float | None:
    """Where current IV sits between the lookback window's low and high, 0-1 (A6.5 IV rank). None until
    there are at least 5 prior sessions -- a rank on 1-2 points is noise, not information."""
    if len(history) < 5 or current <= 0:
        return None
    lo, hi = min(history), max(history)
    return 0.5 if hi <= lo else max(0.0, min(1.0, (current - lo) / (hi - lo)))


def iv_percentile(current: float, history: list[float]) -> float | None:
    """Share of the lookback window's sessions with a lower IV than today (A6.5 IV percentile)."""
    if len(history) < 5 or current <= 0:
        return None
    return sum(1 for h in history if h < current) / len(history)


def iv_rv_spread(atm_iv: float, realized_vol: float | None) -> float | None:
    """Volatility risk premium: implied minus realized (A6.5/A6.7). Positive = options rich vs recent
    realized moves (the usual state); negative = IV cheap vs what the underlying has actually been doing."""
    if realized_vol is None or atm_iv <= 0:
        return None
    return atm_iv - realized_vol


def pin_risk(rows: list[ChainRow], spot: float, dte_days: float, near_pct: float = 0.0015) -> dict[str, Any]:
    """Expiry pin-risk proxy (A6.6): flags when spot is very close to the chain's highest combined-OI
    strike on an expiry day. A proxy, not a certainty -- pinning needs dealer/writer hedging flows this
    system cannot observe directly."""
    if not rows or dte_days > 0.5:
        return {"active": False, "strike": None, "distance_pct": None}
    peak = max(rows, key=lambda r: r.ce_oi + r.pe_oi)
    dist = abs(spot - peak.strike) / spot if spot else 1.0
    return {"active": dist <= near_pct, "strike": peak.strike, "distance_pct": dist,
           "combined_oi": peak.ce_oi + peak.pe_oi}


def summary(rows: list[ChainRow], spot: float, *, underlying_chg_pct: float | None = None,
           iv_history: list[float] | None = None, realized_vol: float | None = None,
           dte_days: float | None = None) -> dict[str, Any]:
    k = atm_strike(rows, spot)
    atm = next((x for x in rows if x.strike == k), None)
    atm_iv = ((atm.ce_iv + atm.pe_iv) / 2) if atm else 0.0
    out = {
        "spot": spot, "atm": k, "pcr": pcr(rows), "max_pain": max_pain(rows), "walls": oi_walls(rows, spot),
        "straddle": straddle_move(rows, spot), "rr25": risk_reversal_25d(rows), "atm_iv": atm_iv,
        "buildup": oi_buildup(rows, underlying_chg_pct, spot=spot),
        "iv_rank": iv_rank(atm_iv, iv_history or []), "iv_percentile": iv_percentile(atm_iv, iv_history or []),
        "iv_rv_spread": iv_rv_spread(atm_iv, realized_vol), "realized_vol": realized_vol,
    }
    if dte_days is not None:
        out["pin_risk"] = pin_risk(rows, spot, dte_days)
    return out
