"""Performance metrics (A19). Every report carries its source label (A2.1)."""
from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

import numpy as np

from sthitaprajna.market.black76 import norm_cdf


def max_drawdown(equity: Sequence[float]) -> tuple[float, int]:
    peak, mdd, dur, cur = -1e18, 0.0, 0, 0
    for e in equity:
        if e >= peak:
            peak, cur = e, 0
        else:
            cur += 1
            mdd = max(mdd, 1 - e / peak)
            dur = max(dur, cur)
    return mdd, dur


def probabilistic_sharpe(returns: Sequence[float], sr_benchmark: float = 0.0) -> float | None:
    """PSR (Bailey & Lopez de Prado): P(true SR > benchmark) given sample skew/kurtosis (per-period SR)."""
    r = np.asarray(returns, float)
    n = len(r)
    if n < 10 or r.std() == 0:
        return None
    sr = r.mean() / r.std(ddof=1)
    z = (r - r.mean()) / r.std()
    skew = float((z ** 3).mean())
    kurt = float((z ** 4).mean())
    denom = math.sqrt(max(1e-12, 1 - skew * sr + (kurt - 1) / 4 * sr * sr))
    return norm_cdf((sr - sr_benchmark) * math.sqrt(n - 1) / denom)


def report(trades: list[dict[str, Any]], daily: list[dict[str, Any]], label: str, starting: float) -> dict[str, Any]:
    rs = [t["r"] for t in trades]
    nets = [t["net"] for t in trades]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    eq = [starting] + [d["equity"] for d in daily]
    rets = [d["pnl_pct"] for d in daily]
    mdd, dur = max_drawdown(eq)
    mean_r = float(np.mean(rs)) if rs else None
    ci = None
    if len(rs) >= 2:
        se = float(np.std(rs, ddof=1) / math.sqrt(len(rs)))
        ci = [mean_r - 1.96 * se, mean_r + 1.96 * se]
    streak = longest = 0
    for x in nets:
        streak = streak + 1 if x <= 0 else 0
        longest = max(longest, streak)
    daily_sd = float(np.std(rets, ddof=1)) if len(rets) > 1 else 0.0
    downside = [x for x in rets if x < 0]
    sharpe = (float(np.mean(rets)) / daily_sd * math.sqrt(250)) if daily_sd > 0 else None
    sortino = (float(np.mean(rets)) / float(np.std(downside, ddof=1)) * math.sqrt(250)) if len(downside) > 1 else None
    total = (eq[-1] / starting - 1) if starting else 0.0
    years = max(len(daily) / 250, 1 / 250)
    cagr = (eq[-1] / starting) ** (1 / years) - 1 if eq[-1] > 0 else -1.0
    return {
        "label": label, "trades": len(trades), "net_pnl": round(sum(nets), 2),
        "win_rate": len(wins) / len(nets) if nets else None,
        "avg_win": float(np.mean(wins)) if wins else None, "avg_loss": float(np.mean(losses)) if losses else None,
        "expectancy_r": mean_r, "expectancy_r_ci95": ci, "expectancy_rupees": float(np.mean(nets)) if nets else None,
        "profit_factor": (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else None,
        "max_drawdown": mdd, "max_dd_duration_days": dur, "total_return": total, "cagr": cagr,
        "sharpe": sharpe, "sortino": sortino, "calmar": (cagr / mdd) if mdd > 0 else None,
        "psr_vs_zero": probabilistic_sharpe(rets), "longest_losing_streak": longest,
        "days": len(daily), "note": "Report fewer than 100 trades as inconclusive (A12.9).",
    }
