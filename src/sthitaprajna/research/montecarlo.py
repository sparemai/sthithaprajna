"""Monte Carlo survival engine (A10.11, D12.1) -- generalises research/aggressive_day1_sim.py.

`reproduce_d12_table()` runs the exact scenarios, policies, seed and RNG call order of the delivered
script, so its numbers match the spec's D12.1 table digit for digit (a regression test holds it there).
Everything here is SIMULATION, not a forecast: independent trades, no daily/weekly/monthly limits,
no correlated streaks, no per-strategy demotion.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from sthitaprajna.config import OperatorConfig
from sthitaprajna.domain import Gear

Ladder = Callable[[np.ndarray], np.ndarray]

D12_SCENARIOS = {"Real edge": (0.45, 1.8), "Weak edge": (0.40, 1.6), "No edge": (0.38, 1.6)}


def base_ladder(dd: np.ndarray) -> np.ndarray:
    """Conservative baseline: 1.5% halving down to a 15% halt."""
    return np.select([dd < .05, dd < .08, dd < .12, dd < .15], [.015, .01125, .0075, .00375], 0.0)


def attack_ladder_from(cfg: OperatorConfig) -> Ladder:
    g = cfg.gears
    th = [g[x].dd_below for x in (Gear.ATTACK, Gear.PRESS, Gear.GUARD, Gear.PRESERVE)]
    rs = [g[x].r for x in (Gear.ATTACK, Gear.PRESS, Gear.GUARD, Gear.PRESERVE)]

    def ladder(dd: np.ndarray) -> np.ndarray:
        return np.select([dd < th[0], dd < th[1], dd < th[2], dd < th[3]], rs, 0.0)
    return ladder


def attack_ladder(dd: np.ndarray) -> np.ndarray:
    return np.select([dd < .10, dd < .15, dd < .20, dd < .25], [.025, .020, .010, .005], 0.0)


@dataclass
class MCResult:
    terminal: np.ndarray
    halted: np.ndarray
    bands: dict[str, list[float]] | None
    max_dd: np.ndarray

    def summary(self) -> dict[str, Any]:
        t = self.terminal
        return {
            "median": float(np.median(t)), "p5": float(np.percentile(t, 5)), "p25": float(np.percentile(t, 25)),
            "p75": float(np.percentile(t, 75)), "p95": float(np.percentile(t, 95)),
            "p_halt": float(self.halted.mean()), "p_loss": float((t < 1).mean()),
            "p_dd_20": float((self.max_dd >= 0.20).mean()), "p_dd_30": float((self.max_dd >= 0.30).mean()),
            "mean_max_dd": float(self.max_dd.mean()),
        }


def run_policy(rng: np.random.Generator, p: float, b: float, ladder: Ladder, brake: bool, *, n: int = 20000,
               t: int = 250, gap_p: float = 0.04, gap_loss: float = 2.5, bands: bool = False,
               outcomes: np.ndarray | None = None) -> MCResult:
    """Identical arithmetic to aggressive_day1_sim.run(); optional percentile bands and max drawdown.
    `outcomes` (N x T of R multiples) may be supplied to bootstrap empirical trade results instead."""
    if outcomes is None:
        win = rng.random((n, t)) < p
        gap = rng.random((n, t)) < gap_p
        out = np.where(win, b, np.where(gap, -gap_loss, -1.0))
    else:
        out = outcomes
        n, t = out.shape
    eq = np.ones(n)
    peak = np.ones(n)
    halted = np.zeros(n, bool)
    max_dd = np.zeros(n)
    nw = np.zeros(n)
    sw = np.zeros(n)
    nl = np.zeros(n)
    sl = np.zeros(n)
    pct = {k: [] for k in ("p5", "p25", "p50", "p75", "p95")} if bands else None
    for i in range(t):
        dd = 1 - eq / peak
        f = ladder(dd)
        halted |= f == 0
        if brake and i >= 50:
            pw = nw / i
            bb = np.where(nl > 0, (sw / np.maximum(nw, 1)) / (sl / np.maximum(nl, 1)), 10)
            kelly = np.where(bb > 0, pw - (1 - pw) / np.where(bb > 0, bb, 1), -1.0)
            f = np.minimum(f, np.maximum(0.010, 0.25 * kelly))
        f = np.where(halted, 0.0, f)
        o = out[:, i]
        eq *= 1 + f * o
        peak = np.maximum(peak, eq)
        max_dd = np.maximum(max_dd, 1 - eq / peak)
        traded = ~halted
        nw += (o > 0) & traded
        sw += np.where((o > 0) & traded, o, 0)
        nl += (o < 0) & traded
        sl += np.where((o < 0) & traded, -o, 0)
        if pct is not None:
            q = np.percentile(eq, [5, 25, 50, 75, 95])
            for k, v in zip(pct, q):
                pct[k].append(round(float(v), 5))
    return MCResult(eq, halted, pct, max_dd)


def ev_per_trade(p: float, b: float, gap_p: float = 0.04, gap_loss: float = 2.5) -> float:
    return p * b - (1 - p) * (1 + gap_p * (gap_loss - 1))


def reproduce_d12_table(n: int = 20000, t: int = 250, seed: int = 2026) -> list[dict[str, Any]]:
    """Same seed, same scenario/policy order, same RNG draws as research/aggressive_day1_sim.py."""
    rng = np.random.default_rng(seed)
    pols = [("BASE 1.5%, halt 15%", base_ladder, False), ("ATTACK day 1, halt 25%", attack_ladder, False),
            ("ATTACK day 1 + edge brake", attack_ladder, True)]
    rows = []
    for s, (p, b) in D12_SCENARIOS.items():
        for name, lad, br in pols:
            r = run_policy(rng, p, b, lad, br, n=n, t=t)
            sm = r.summary()
            rows.append({"scenario": s, "policy": name, "median": round(sm["median"], 2), "p5": round(sm["p5"], 2),
                         "p_halt": round(sm["p_halt"] * 100, 1), "p_loss": round(sm["p_loss"] * 100, 1),
                         "ev_r": round(ev_per_trade(p, b), 2)})
    return rows


def run_custom(cfg: OperatorConfig, *, p: float, b: float, policy: str, n: int = 20000, t: int = 250,
               gap_p: float = 0.04, gap_loss: float = 2.5, seed: int = 7) -> dict[str, Any]:
    """Interactive MC for the Research page (SIMULATION label enforced by the caller/UI)."""
    n = int(min(max(n, 1000), 50000))
    t = int(min(max(t, 20), 1000))
    rng = np.random.default_rng(seed)
    lad = {"baseline": base_ladder, "attack": attack_ladder_from(cfg), "attack_brake": attack_ladder_from(cfg)}[policy]
    r = run_policy(rng, p, b, lad, policy == "attack_brake", n=n, t=t, gap_p=gap_p, gap_loss=gap_loss, bands=True)
    hist, edges = np.histogram(np.clip(r.terminal, 0, 6), bins=48, range=(0, 6))
    return {"label": "SIMULATION (not a forecast)", "inputs": {"p": p, "b": b, "policy": policy, "n": n, "t": t,
            "gap_p": gap_p, "gap_loss": gap_loss, "seed": seed}, "ev_r": ev_per_trade(p, b, gap_p, gap_loss),
            "summary": r.summary(), "bands": r.bands,
            "histogram": {"counts": hist.tolist(), "edges": [round(float(x), 3) for x in edges]}}


def bootstrap(outcomes_r: Sequence[float], n: int, t: int, rng: np.random.Generator) -> np.ndarray:
    arr = np.asarray(outcomes_r, float)
    return arr[rng.integers(0, len(arr), size=(n, t))]
