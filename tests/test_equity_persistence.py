"""Sthitaprajna 2.0 (D-047): PAPER/SIM equity previously reset to capital.starting on every process
restart. Ledger.restore_from_history() replays every closed trade ever recorded in the store to rebuild
realized P&L and the all-time peak; Engine wires this in before the gear ladder's ref_peak is seeded."""
from __future__ import annotations

import datetime as dt

import pytest
from test_engine_e2e import CAL, DAY, RULES, FakeWall
from test_unbounded import candidate, enter, step, unbounded_cfg

from sthitaprajna.domain import IST
from sthitaprajna.engine.runtime import Engine
from sthitaprajna.engine.store import Store
from sthitaprajna.portfolio.ledger import Ledger


def test_ledger_restore_from_history_rebuilds_equity():
    ledger = Ledger(100_000.0)
    n = ledger.restore_from_history([{"net_pnl": 5000.0}, {"net_pnl": -2000.0}, {"net_pnl": 8000.0}])
    assert n == 3
    assert ledger.equity == pytest.approx(111_000.0)
    assert ledger.all_time_peak == pytest.approx(111_000.0)   # monotonic here: peak == final equity
    assert ledger.equity_sod == pytest.approx(111_000.0)      # sizing basis also carries forward (D1)


def test_ledger_restore_tracks_a_peak_reached_before_a_later_drawdown():
    ledger = Ledger(100_000.0)
    ledger.restore_from_history([{"net_pnl": 20_000.0}, {"net_pnl": -5_000.0}])
    assert ledger.equity == pytest.approx(115_000.0)
    assert ledger.all_time_peak == pytest.approx(120_000.0)   # the ruin floor/halt-drawdown ladder needs this


def test_ledger_restore_with_no_history_is_a_no_op():
    ledger = Ledger(100_000.0)
    assert ledger.restore_from_history([]) == 0
    assert ledger.equity == pytest.approx(100_000.0)


def test_equity_persists_across_a_simulated_restart(tmp_path):
    """A second Engine pointed at the same store picks up where the first left off, instead of resetting
    to capital.starting -- the exact behaviour the operator reported missing after a real restart."""
    db = str(tmp_path / "paper.sqlite")
    cfg = unbounded_cfg(capital_per_trade=0.25)

    e1 = Engine(cfg, RULES, CAL, seed=3, start=DAY, warmup_days=2, wall_clock=FakeWall(), store=Store(db))
    e1.strategies = []
    e1.market.freeze = True
    e1.run_until(dt.datetime.combine(DAY, dt.time(10, 0), tzinfo=IST), step_seconds=30)
    (p,) = enter(e1)
    e1.market.set_price("NSE:RELIANCE", p.target + 0.5)
    step(e1, 30, 2)
    assert not e1.positions
    equity_after_close = e1.ledger.equity
    assert equity_after_close > 100_000      # the scripted candidate is a winner (target hit)
    e1.store.close()

    e2 = Engine(cfg, RULES, CAL, seed=3, start=DAY, warmup_days=2, wall_clock=FakeWall(), store=Store(db))
    assert e2._restored_trades == 1
    assert e2.ledger.equity == pytest.approx(equity_after_close)
    assert e2.ledger.equity_sod == pytest.approx(equity_after_close)
    assert e2.gears.ref_peak >= equity_after_close - 1e-6
    assert any("D-047" in ev["msg"] for ev in e2.events)


def test_no_store_means_no_restore_and_no_crash():
    e = Engine(unbounded_cfg(), RULES, CAL, seed=3, start=DAY, warmup_days=2, wall_clock=FakeWall(), store=None)
    assert e._restored_trades == 0
    assert e.ledger.equity == pytest.approx(100_000.0)
