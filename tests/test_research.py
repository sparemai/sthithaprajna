
import numpy as np
import pytest

from sthitaprajna.config import load_holidays, load_operator, load_rules
from sthitaprajna.domain import Gear
from sthitaprajna.market.calendar import MarketCalendar
from sthitaprajna.research import backtest, montecarlo
from sthitaprajna.research.metrics import max_drawdown, probabilistic_sharpe, report
from sthitaprajna.research.target_plan import compounding_math, feasibility

CFG = load_operator()

# D12.1 table as printed in docs/MASTER_PROMPT.md (median, 5th pct, P(halt)% for real edge)
SPEC = {
    ("Real edge", "BASE 1.5%, halt 15%"): (1.78, 0.95, 19.0),
    ("Real edge", "ATTACK day 1, halt 25%"): (2.49, 0.97, 8.5),
    ("Real edge", "ATTACK day 1 + edge brake"): (2.44, 0.99, 8.1),
    ("Weak edge", "BASE 1.5%, halt 15%"): (0.95, 0.85, None),
    ("Weak edge", "ATTACK day 1, halt 25%"): (0.90, 0.75, None),
    ("Weak edge", "ATTACK day 1 + edge brake"): (0.91, 0.75, None),
    ("No edge", "BASE 1.5%, halt 15%"): (0.91, 0.85, None),
    ("No edge", "ATTACK day 1, halt 25%"): (0.84, 0.75, None),
    ("No edge", "ATTACK day 1 + edge brake"): (0.84, 0.75, None),
}


def test_montecarlo_reproduces_the_spec_d12_table_exactly():
    rows = montecarlo.reproduce_d12_table()
    assert len(rows) == 9
    for r in rows:
        med, p5, halt = SPEC[(r["scenario"], r["policy"])]
        assert r["median"] == pytest.approx(med, abs=0.005), r
        assert r["p5"] == pytest.approx(p5, abs=0.005), r
        if halt is not None:
            assert r["p_halt"] == pytest.approx(halt, abs=0.05), r


def test_attack_ladder_from_config_matches_operator_yaml():
    """attack_ladder() stays pinned to the original D12.1 spec numbers so `mc --reproduce-d12` keeps
    reproducing the master prompt's published table exactly (see test above). 2.0's config.yaml is
    deliberately more aggressive than that reference (docs/decisions.md D-036), so this checks the
    config-driven ladder against CFG's own gear thresholds/values instead of the frozen reference."""
    lad = montecarlo.attack_ladder_from(CFG)
    g = CFG.gears
    edges = [g[x].dd_below for x in (Gear.ATTACK, Gear.PRESS, Gear.GUARD, Gear.PRESERVE)]
    rs = [g[x].r for x in (Gear.ATTACK, Gear.PRESS, Gear.GUARD, Gear.PRESERVE)]
    dd = np.array([0.0, edges[0] - 1e-3, edges[0], edges[1] - 1e-3, edges[1],
                   edges[2] - 1e-3, edges[2], edges[3] - 1e-3, edges[3], 0.9])
    expected = np.array([rs[0], rs[0], rs[1], rs[1], rs[2], rs[2], rs[3], rs[3], 0.0, 0.0])
    assert np.allclose(lad(dd), expected)


def test_custom_mc_has_bands_and_histogram():
    out = montecarlo.run_custom(CFG, p=0.45, b=1.8, policy="attack", n=2000, t=100)
    assert out["label"].startswith("SIMULATION")
    assert len(out["bands"]["p50"]) == 100
    assert sum(out["histogram"]["counts"]) == 2000
    assert out["summary"]["p5"] <= out["summary"]["median"] <= out["summary"]["p95"]


def test_target_plan_is_honest_without_validated_data():
    f = feasibility(CFG, target_pct=0.08, period="month", r_outcomes=[0.5] * 10, label="PAPER", equity=100000)
    assert f["label"].startswith("PROBABILITY UNKNOWN")
    sim = feasibility(CFG, target_pct=0.08, period="month", r_outcomes=list(np.tile([1.8, -1, -1, 1.8, -1], 30)),
                      label="SIMULATED", equity=100000)
    assert sim["label"].startswith("PROBABILITY UNKNOWN") and "illustration" in sim
    assert sim["risk_effect"].startswith("NONE")


def test_target_plan_labels_with_validated_paper_data():
    good = list(np.tile([1.8, -1.0, 1.8, -1.0, -1.0, 1.8, -1.0, 1.8, -1.0, 1.8], 12))   # 50% @1.8R
    f = feasibility(CFG, target_pct=0.01, period="month", r_outcomes=good, label="PAPER", equity=100000)
    assert f["label"] in ("REALISTIC", "STRETCH", "INFEASIBLE") and 0 <= f["mc"]["p_hit"] <= 1
    assert f["label"] == "REALISTIC"
    silly = feasibility(CFG, target_pct=3.0, period="month", r_outcomes=good, label="PAPER", equity=100000)
    assert silly["label"] == "INFEASIBLE"
    # fewer trades per day -> lower probability for the same target (breadth matters, D12.1b)
    busy = feasibility(CFG, target_pct=0.3, period="month", r_outcomes=good, label="PAPER", equity=1e5, trades_per_day=3)
    calm = feasibility(CFG, target_pct=0.3, period="month", r_outcomes=good, label="PAPER", equity=1e5, trades_per_day=1)
    assert calm["mc"]["p_hit"] < busy["mc"]["p_hit"]


def test_compounding_math_matches_spec_example():
    assert compounding_math(0.10, "month")["annualised"] == pytest.approx(2.138, abs=0.01)   # "about 214%"


def test_metrics_basics():
    assert max_drawdown([100, 110, 99, 120, 90])[0] == pytest.approx(0.25)
    assert probabilistic_sharpe([0.01] * 5) is None
    rng = np.random.default_rng(1)
    psr = probabilistic_sharpe(rng.normal(0.002, 0.01, 500))
    assert 0.5 < psr <= 1.0
    r = report([{"r": 1.0, "net": 1000}, {"r": -0.5, "net": -500}], [{"equity": 100500, "pnl_pct": 0.005}],
               "SIMULATED", 100000)
    assert r["label"] == "SIMULATED" and r["win_rate"] == 0.5 and r["profit_factor"] == 2.0


def test_backtest_uses_engine_and_labels_simulated():
    cal = MarketCalendar.from_rules(load_rules(), load_holidays())
    out = backtest.run(CFG, load_rules(), cal, days=1, seed=4, step_seconds=60)
    assert out["report"]["label"] == "SIMULATED"
    assert out["variant"] == "staged"
    single = backtest.single_entry_variant(CFG)
    assert single.staging["probe_fraction"] == 1.0 and CFG.staging["probe_fraction"] == 0.5
