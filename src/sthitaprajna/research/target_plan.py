"""Target-to-Plan Engine (A16, D5). A target is ALWAYS accepted because it NEVER changes risk.

Uses only validated statistics (OOS / PAPER / LIVE). Before enough exist the official answer is
`PROBABILITY UNKNOWN: INSUFFICIENT LIVE/PAPER DATA`. SIM statistics may be shown as an illustration,
clearly labelled, but never as the verdict.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np

from sthitaprajna.config import OperatorConfig
from sthitaprajna.research.montecarlo import attack_ladder_from, bootstrap, run_policy

PERIODS_PER_YEAR = {"day": 250, "week": 50, "month": 12}
TRADING_DAYS = {"day": 1, "week": 5, "month": 21}
VALIDATED_LABELS = {"WALK-FORWARD-OOS", "PAPER", "LIVE"}


def compounding_math(target_pct: float, period: str) -> dict[str, float]:
    k = PERIODS_PER_YEAR[period]
    return {"per_period": target_pct, "annualised": (1 + target_pct) ** k - 1, "periods_per_year": k}


def feasibility(cfg: OperatorConfig, *, target_pct: float, period: str, r_outcomes: Sequence[float], label: str,
                equity: float, trades_per_day: float = 3.0, n_paths: int = 10000, seed: int = 11) -> dict[str, Any]:
    th = cfg.targets["feasibility"]
    out: dict[str, Any] = {"target_pct": target_pct, "period": period, "equity": equity,
                           "math": compounding_math(target_pct, period), "risk_effect": "NONE (D5)"}
    min_n = int(cfg.targets["min_validated_trades_for_probability"])
    enough = len(r_outcomes) >= min_n

    def _mc() -> dict[str, Any]:
        rng = np.random.default_rng(seed)
        t = max(1, int(round(trades_per_day * TRADING_DAYS[period])))
        outs = bootstrap(r_outcomes, n_paths, t, rng)
        res = run_policy(rng, 0.0, 0.0, attack_ladder_from(cfg), True, outcomes=outs)
        ret = res.terminal - 1
        return {"p_hit": float((ret >= target_pct).mean()), "p_exceed_max_dd": float((res.max_dd >= 0.25).mean()),
                "p_ruin": float((res.max_dd >= 0.30).mean()), "median": float(np.median(ret)),
                "p5": float(np.percentile(ret, 5)), "best_at_50pct": float(np.median(ret)), "trades_modelled": t,
                "sample_trades": len(r_outcomes)}

    if enough and label in VALIDATED_LABELS:
        mc = _mc()
        p = mc["p_hit"]
        out["label"] = "REALISTIC" if p >= th["realistic"] else ("STRETCH" if p >= th["stretch"] else "INFEASIBLE")
        out["mc"] = mc
        out["source"] = label
    else:
        out["label"] = "PROBABILITY UNKNOWN: INSUFFICIENT LIVE/PAPER DATA"
        out["source"] = label
        if enough:   # e.g. SIM: show an illustration, never the verdict
            out["illustration"] = {**_mc(), "label": f"{label} statistics -- illustration only, not validated"}
    return out
