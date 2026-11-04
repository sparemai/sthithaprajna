"""Sthitaprajna 2.0 (D-045): the reversal watch -- extra Confirmation evidence for a fresh, independently
confirmed candidate that sits opposite a recent stop-out on the same underlying. Never a forced flip: the
watched direction only ever adds one Confirmation to a candidate a strategy would have produced anyway."""
from __future__ import annotations

import datetime as dt

from sthitaprajna.domain import (
    Direction, Exchange, Gear, Grade, Instrument, Kind, Leg, PositionStage, Segment,
)
from sthitaprajna.engine.runtime import Engine, REVERSAL_WATCH_MINUTES
from sthitaprajna.portfolio.position import Position
from sthitaprajna.strategies.options import _reversal_confirmation
from test_engine_e2e import CAL, DAY, RULES


def _engine() -> Engine:
    from sthitaprajna.config import load_operator
    e = Engine(load_operator(), RULES, CAL, seed=13, start=DAY, warmup_days=2)
    e.run_until(dt.datetime.combine(DAY, dt.time(10, 30), tzinfo=e.now.tzinfo), step_seconds=30)
    return e


def test_reversal_watch_is_none_with_no_recent_stop():
    e = _engine()
    e.recent_stop_exits.clear()   # the engine may have organically stopped out during warmup/run_until
    assert e.reversal_watch("NSE:NIFTY") is None


def test_reversal_watch_flips_the_exposure_direction():
    e = _engine()
    e.recent_stop_exits["NSE:NIFTY"] = (e.now, Direction.LONG)
    assert e.reversal_watch("NSE:NIFTY") is Direction.SHORT
    e.recent_stop_exits["NSE:NIFTY"] = (e.now, Direction.SHORT)
    assert e.reversal_watch("NSE:NIFTY") is Direction.LONG


def test_reversal_watch_expires_after_the_window():
    e = _engine()
    stale = e.now - dt.timedelta(minutes=REVERSAL_WATCH_MINUTES + 1)
    e.recent_stop_exits["NSE:NIFTY"] = (stale, Direction.LONG)
    assert e.reversal_watch("NSE:NIFTY") is None
    fresh = e.now - dt.timedelta(minutes=REVERSAL_WATCH_MINUTES - 1)
    e.recent_stop_exits["NSE:NIFTY"] = (fresh, Direction.LONG)
    assert e.reversal_watch("NSE:NIFTY") is Direction.SHORT


def _synthetic_position(e: Engine, *, direction: Direction = Direction.LONG) -> Position:
    inst = Instrument("NFO:TEST", "TEST", "t", Kind.OPTION, Exchange.NSE, lot_size=65, tick_size=0.05)
    entry, stop = 100.0, (90.0 if direction is Direction.LONG else 110.0)
    return Position("pos-rev", "c", "index_option_momentum", inst, "NSE:NIFTY", direction, Segment.INDEX_OPTION,
                    Grade.A, Gear.ATTACK, "SIM", planned_risk=650.0, unit_risk=10.0, initial_stop=stop, stop=stop,
                    broker_stop=stop, staging_mode="full", full_qty_target=65, confirm_trigger=entry,
                    confirm_deadline=e.now, opened_at=e.now, legs=[Leg(65, entry, e.now, PositionStage.PROBE)],
                    setup_id=f"index_option_momentum:NIFTY:{e.now:%Y%m%d}:{direction.value}", bias=direction)


def test_a_stop_out_arms_the_watch_for_the_opposite_direction():
    e = _engine()
    p = _synthetic_position(e, direction=Direction.LONG)
    e.positions[p.id] = p
    p.mark(p.stop)
    e._close_position(p, "STOP")
    assert e.reversal_watch(p.underlying_key) is Direction.SHORT

    e2 = _engine()
    p2 = _synthetic_position(e2, direction=Direction.SHORT)
    e2.positions[p2.id] = p2
    p2.mark(p2.stop)
    e2._close_position(p2, "STOP")
    assert e2.reversal_watch(p2.underlying_key) is Direction.LONG


def test_a_harvest_exit_never_arms_the_reversal_watch():
    """Only a stop-out (an invalidated thesis) arms the watch -- taking profit isn't a reversal signal."""
    e = _engine()
    e.recent_stop_exits.clear()
    p = _synthetic_position(e, direction=Direction.LONG)
    e.positions[p.id] = p
    p.mark(105.0)
    e._close_position(p, "HARVEST")
    assert e.reversal_watch(p.underlying_key) is None


class _FakeCtx:
    def __init__(self, watch):
        self._watch = watch

    def reversal_watch(self, key):
        return self._watch


def test_reversal_confirmation_fires_only_on_the_watched_direction():
    long_watch = _FakeCtx(Direction.LONG)
    fired = _reversal_confirmation(long_watch, "NSE:NIFTY", Direction.LONG)
    against = _reversal_confirmation(long_watch, "NSE:NIFTY", Direction.SHORT)
    assert fired.fired and fired.factor == "trend" and fired.name == "reversal_setup"
    assert not against.fired

    no_watch = _FakeCtx(None)
    assert not _reversal_confirmation(no_watch, "NSE:NIFTY", Direction.LONG).fired
