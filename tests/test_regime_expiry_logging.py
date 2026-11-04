"""Sthitaprajna 2.0 (D-056): regime.expiry transitions are now logged. Previously silent, so an operator
had no way to tell, after the fact, whether a quiet Zero-to-Hero sleeve on an expiry afternoon meant "no
GAMMA_ACCELERATION/BREAKOUT_EXPIRY/TREND_EXPIRY was ever classified" (expected on most expiry days -- most
tickets expire worthless, D8) or a real gap worth chasing."""
from __future__ import annotations

import datetime as dt

from sthitaprajna.engine.runtime import Engine
from test_engine_e2e import CAL, DAY, RULES


def _engine(at: dt.time = dt.time(10, 30)) -> Engine:
    from sthitaprajna.config import load_operator
    e = Engine(load_operator(), RULES, CAL, seed=11, start=DAY, warmup_days=2)
    e.run_until(dt.datetime.combine(DAY, at, tzinfo=e.now.tzinfo), step_seconds=30)
    return e


def _expiry_logs(e: Engine) -> list[dict]:
    return [ev for ev in e.events if ev["kind"] == "regime" and ev["msg"].startswith("expiry regime ->")]


def test_expiry_regime_change_is_logged_once(monkeypatch):
    e = _engine()
    monkeypatch.setattr(type(e.cal), "is_expiry_day", lambda self, und, d: und == "NIFTY")
    before = len(_expiry_logs(e))
    e._update_regime()
    after = _expiry_logs(e)
    assert len(after) == before + 1
    assert after[-1]["msg"].startswith("expiry regime -> ")


def test_expiry_regime_is_not_logged_again_when_unchanged(monkeypatch):
    e = _engine()
    monkeypatch.setattr(type(e.cal), "is_expiry_day", lambda self, und, d: und == "NIFTY")
    e._update_regime()
    n = len(_expiry_logs(e))
    e._update_regime()    # same tick, same chain/trend data -> same classification
    assert len(_expiry_logs(e)) == n


def test_expiry_regime_logging_is_a_no_op_on_a_non_expiry_day():
    e = _engine()   # DAY is not an index expiry day in this fixture by default
    before = len(_expiry_logs(e))
    e._update_regime()
    assert len(_expiry_logs(e)) == before
