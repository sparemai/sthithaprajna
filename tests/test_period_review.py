"""Sthitaprajna 2.0 (D-054): planning/retrospection infrastructure -- daily/weekly/monthly plan vs
actual, an expiry calendar, and Zero-to-Hero capture tracking, all backed by the SQLite store so the
operator can review any period after the fact, not just while it's live."""
from __future__ import annotations

import datetime as dt

import pytest
from test_engine_e2e import CAL, DAY, RULES, FakeWall
from test_unbounded import enter, step, unbounded_cfg

from sthitaprajna.domain import IST
from sthitaprajna.engine.runtime import Engine
from sthitaprajna.engine.snapshot import expiry_calendar, period_review, zero_to_hero_report
from sthitaprajna.engine.store import Store


def _engine(tmp_path, **over) -> Engine:
    e = Engine(unbounded_cfg(**over), RULES, CAL, seed=3, start=DAY, warmup_days=2, wall_clock=FakeWall(),
              store=Store(str(tmp_path / "paper.sqlite")))
    e.strategies = []
    e.market.freeze = True
    e.run_until(dt.datetime.combine(DAY, dt.time(10, 0), tzinfo=IST), step_seconds=30)
    return e


def test_store_period_review_round_trips(tmp_path):
    s = Store(str(tmp_path / "s.sqlite"))
    s.period_review({"period_type": "week", "period_key": "2026-W40", "closed_at": "2026-10-02T15:30:00",
                     "pnl": 1500.0})
    s.period_review({"period_type": "week", "period_key": "2026-W41", "closed_at": "2026-10-09T15:30:00",
                     "pnl": -200.0})
    rows = s.period_reviews("week")
    assert len(rows) == 2 and rows[0]["period_key"] == "2026-W41"      # newest first
    assert s.period_reviews("month") == []


def test_close_period_review_persists_and_caches(tmp_path):
    e = _engine(tmp_path)
    e._close_period_review("week", "2026-W40", 2500.0, 100_000.0, 102_500.0)
    assert e.period_reviews["week"][0]["pnl"] == 2500.0
    assert e.period_reviews["week"][0]["target"]["pnl"] == 2500.0      # reuses TargetBook.to_dict's own shape
    assert e.store.period_reviews("week")[0]["period_key"] == "2026-W40"


def test_period_review_snapshot_reports_completed_and_live(tmp_path):
    e = _engine(tmp_path)
    e._close_period_review("week", "2026-W40", 2500.0, 100_000.0, 102_500.0)
    out = period_review(e)
    assert out["completed"]["week"][0]["pnl"] == 2500.0 and out["completed"]["month"] == []
    assert "day" in out["current"] and "week" in out["current"] and "month" in out["current"]


def test_week_boundary_closes_the_period_review_automatically(tmp_path):
    """The real end-to-end path: open and close a winning trade within the week starting DAY (a Monday,
    ISO week 40), then run past the weekend into the next week (ISO week 41) and confirm _start_of_day
    wrote a week review for the week that just ended, before resetting the ratchet state for it."""
    e = _engine(tmp_path)
    (p,) = enter(e)
    e.market.set_price("NSE:RELIANCE", p.target + 0.5)
    step(e, 30, 2)
    assert not e.positions
    equity_after_win = e.ledger.equity
    assert equity_after_win > 100_000
    week_key_before = e.ledger.week_key
    e.run_until(dt.datetime.combine(DAY + dt.timedelta(days=7), dt.time(10, 0), tzinfo=IST), step_seconds=60)
    assert e.ledger.week_key != week_key_before
    closed = e.period_reviews["week"]
    assert closed and closed[0]["period_key"] == week_key_before
    assert closed[0]["pnl"] == pytest.approx(equity_after_win - 100_000.0, abs=0.01)
    assert closed[0]["equity_after"] == pytest.approx(equity_after_win, abs=0.01)


def test_expiry_calendar_lists_every_sleeve_underlying_sorted_by_date(tmp_path):
    e = _engine(tmp_path)
    cal = expiry_calendar(e, count=2)
    underlyings = {row["underlying"] for row in cal}
    assert underlyings == {"NIFTY", "SENSEX", "BANKNIFTY"}
    assert all(row["days_to_expiry"] >= 0 for row in cal)
    assert cal == sorted(cal, key=lambda r: (r["expiry"], r["underlying"]))


def test_zero_to_hero_report_groups_by_expiry_and_ignores_non_sleeve_trades(tmp_path):
    e = _engine(tmp_path)
    e.store.position({"id": "z1", "closed_at": "2026-10-01T15:30:00", "sleeve": True, "net_pnl": 500.0,
                      "instrument": {"expiry": "2026-10-01"}})
    e.store.position({"id": "z2", "closed_at": "2026-10-01T15:30:01", "sleeve": True, "net_pnl": -200.0,
                      "instrument": {"expiry": "2026-10-01"}})
    e.store.position({"id": "z3", "closed_at": "2026-10-08T15:30:00", "sleeve": True, "net_pnl": 300.0,
                      "instrument": {"expiry": "2026-10-08"}})
    e.store.position({"id": "o1", "closed_at": "2026-10-01T15:30:02", "sleeve": False, "net_pnl": 9_999.0,
                      "instrument": {"expiry": "2026-10-01"}})
    report = zero_to_hero_report(e)
    assert report["total_trades"] == 3 and report["total_net_pnl"] == pytest.approx(600.0)
    first = next(r for r in report["captures_by_expiry"] if r["expiry"] == "2026-10-01")
    assert first["trades"] == 2 and first["wins"] == 1 and first["net_pnl"] == pytest.approx(300.0)
    assert first["win_rate"] == pytest.approx(0.5)
    assert "current" in report and "cumulative_pnl" in report["current"]
