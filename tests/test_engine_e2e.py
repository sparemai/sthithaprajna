"""Phase-2 / Phase-3 exit criteria, end to end through the real runtime.

* A complete paper trade runs probe -> confirm-add -> harvest -> exit, with operator approval.
* Kill switch tested end to end.
* Every position has a broker-side stop in the paper model; open risk never exceeds the ceiling.
* Failure injection: primary feed down, both feeds down, order API down, gap through the stop.
"""
import datetime as dt

import pytest

from sthitaprajna.config import load_holidays, load_operator, load_rules
from sthitaprajna.domain import IST, Candidate, Confirmation, Direction, Gear, HalfLife, PositionStage, Segment, new_id
from sthitaprajna.engine.runtime import Engine
from sthitaprajna.market.calendar import MarketCalendar
from sthitaprajna.risk import ceilings

CFG = load_operator()
RULES = load_rules()
CAL = MarketCalendar.from_rules(RULES, load_holidays())
DAY = dt.date(2026, 9, 28)


class FakeWall:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def scripted_engine(pilot=True) -> tuple[Engine, FakeWall]:
    wall = FakeWall()
    e = Engine(CFG, RULES, CAL, seed=3, start=DAY, warmup_days=2, wall_clock=wall)
    e.strategies = []                 # scripted candidates only
    e.market.freeze = True            # prices move only when the test says so
    e.pilot_approvals = pilot
    e.run_until(dt.datetime.combine(DAY, dt.time(10, 0), tzinfo=IST), step_seconds=30)
    return e, wall


def candidate(e: Engine, key="NSE:RELIANCE", unit=10.0, direction=Direction.LONG) -> Candidate:
    q = e.quote(key)
    inst = e.market.instruments[key]
    entry = q.ask if direction is Direction.LONG else q.bid
    stop = entry - direction.sign * unit
    confs = [Confirmation("rvol", "volume", True), Confirmation("vwap", "flow", True),
             Confirmation("breadth", "breadth", True)]
    feats = {k: 0.9 for k in ("regime", "trend", "volume", "volatility", "breadth", "participant", "liquidity",
                              "execution", "event")}
    return Candidate(new_id("sig"), e.now, "scripted", "1.0", inst, key, direction, Segment.EQ_INTRADAY, entry, stop,
                     2.0, 0.55, False, HalfLife.MINUTES, True, confs, feats, True, False,
                     setup_id=f"scripted:{key}:{direction.value}", signal_bar=f"{key}@1",
                     explanation="scripted test setup", model_confidence=0.8, regime_confidence=0.8, atr=unit / 2)


def step(e: Engine, seconds=30, n=1):
    for _ in range(n):
        e.speed = seconds / 0.25
        e.tick(0.25)


def push(e: Engine, c: Candidate):
    e.funnel.ingest([c], e.now)
    ready = e.funnel.evaluate(__import__("sthitaprajna.engine.runtime", fromlist=["_Env"])._Env(e, e._risk_ctx()))
    e._deploy(ready)


def test_full_staged_trade_with_approvals_and_reconciliation():
    e, wall = scripted_engine(pilot=True)
    key = "NSE:RELIANCE"
    c = candidate(e, key)
    push(e, c)
    pend = [a for a in e.approvals.values() if a.status == "PENDING"]
    assert len(pend) == 1 and pend[0].kind == "ENTRY"
    assert pend[0].summary["grade"] in ("A", "A+")
    assert e.approve(pend[0].id, True)["ok"]
    step(e)                                         # next-tick fill
    assert len(e.positions) == 1
    p = next(iter(e.positions.values()))
    assert p.stage is PositionStage.PROBE and p.broker_stop_id in e.broker.resting
    assert p.sign * (p.stop - p.broker_stop) > 0     # broker stop rests BEYOND the system stop
    probe_qty = p.qty
    entry = p.avg_entry
    unit = p.unit_risk

    # +0.6R with an up bar -> confirm-add proposed -> approval
    e.market.set_price(key, entry + 0.6 * unit)
    step(e, 30, 2)
    adds = [a for a in e.approvals.values() if a.status == "PENDING" and a.kind == "CONFIRM_ADD"]
    assert adds, "confirm-add should request approval in pilot mode"
    assert adds[0].summary["worst_case_after"] <= p.planned_risk + 1e-6
    assert e.approve(adds[0].id, True)["ok"]
    step(e)
    assert p.stage is PositionStage.CONFIRM_ADD and p.qty > probe_qty
    assert p.worst_case_at_stop() <= p.planned_risk + 1e-6

    # +2.2R -> harvest 40% (no approval needed for harvests: D9)
    e.pilot_approvals = False       # keep presses from waiting on approval for this leg of the test
    e.market.set_price(key, p.avg_entry + 2.2 * unit)
    step(e, 30, 3)
    assert p.harvested and p.stage in (PositionStage.HARVEST, PositionStage.PRESS_1, PositionStage.PRESS_2)
    assert p.stop > p.avg_entry                      # profit locked
    # fall to the stop -> exit
    e.market.set_price(key, p.stop - 0.5)
    step(e, 30, 3)
    assert not e.positions and e.closed[-1].id == p.id
    closed = e.closed[-1]
    stages = [h["to"] for h in closed.stage_history]
    assert stages[0] == "PROBE" and "CONFIRM_ADD" in stages and "HARVEST" in stages and stages[-1] == "EXIT"
    # hand reconciliation: net = sum(exit legs pnl) - charges, and the ledger moved by exactly that
    gross = sum(cl.pnl for cl in closed.closed_legs)
    hand = sum(cl.qty * (cl.price - closed.avg_entry) for cl in closed.closed_legs)
    assert gross == pytest.approx(hand)
    assert e.ledger.closed_trades[-1]["net"] == pytest.approx(round(gross - closed.charges, 2), abs=0.01)
    assert e.ledger.equity == pytest.approx(CFG.starting_capital + gross - closed.charges, abs=0.01)
    assert not [o for o in e.broker.resting.values() if o.position_id == p.id]   # no orphan broker stops
    assert e.process["approvals_answered"] == 2


def test_approval_timeout_is_a_skip():
    e, wall = scripted_engine(pilot=True)
    push(e, candidate(e))
    wall.t += 61
    step(e)
    assert not e.positions and not e.pending_entries
    assert e.process["approvals_timed_out"] == 1


def test_kill_switch_end_to_end():
    e, _ = scripted_engine(pilot=False)
    push(e, candidate(e, "NSE:RELIANCE"))
    push(e, candidate(e, "NSE:TCS", unit=20))
    step(e)
    assert len(e.positions) == 2
    e.kill()
    step(e, 30, 2)
    assert not e.positions and e.kill_switch
    push(e, candidate(e, "NSE:INFY", unit=10))
    rec = e.funnel.records["scripted:NSE:INFY:LONG"]
    assert rec.status in ("REJECTED", "DROPPED") and not e.positions
    assert "kill_switch" in rec.reason or "not_halted" in rec.reason or rec.status == "DROPPED"
    assert not e.resume(False)["ok"]
    assert e.resume(True)["ok"] and not e.kill_switch


def test_failure_injection_primary_down_blocks_entries_but_monitors():
    e, _ = scripted_engine(pilot=False)
    push(e, candidate(e))
    step(e)
    assert len(e.positions) == 1
    e.inject("primary_down", 600)
    step(e, 30, 2)
    assert e.arbiter.situation == "PRIMARY_DOWN"
    push(e, candidate(e, "NSE:TCS", unit=20))
    assert "NSE:TCS" not in {p.instrument.key for p in e.positions.values()}
    # position still monitored through the secondary feed: a stop hit still exits
    p = next(iter(e.positions.values()))
    e.market.set_price("NSE:RELIANCE", p.stop - 1)
    step(e, 30, 2)
    assert not e.positions


def test_order_api_down_broker_stop_still_protects_and_gap_fills_worse():
    e, _ = scripted_engine(pilot=False)
    push(e, candidate(e))
    step(e)
    p = next(iter(e.positions.values()))
    e.inject("order_api_down", 600)
    gap_px = p.broker_stop - 15.0                  # gap straight through both stops
    e.market.set_price("NSE:RELIANCE", gap_px)
    step(e, 30, 2)
    assert not e.positions, "resting broker-side stop must fire even with the order API down"
    closed = e.closed[-1]
    assert closed.exit_reason == "BROKER_STOP"
    assert closed.closed_legs[-1].price <= gap_px + 1.0   # filled at the gapped market, not the stop
    stop_based_loss = closed.sign * (closed.avg_entry - closed.stop) * closed.opened_qty
    assert -closed.realized > stop_based_loss       # the gap overshot the stop, as D12.1 warns it can


def test_both_feeds_down_flattens_after_five_minutes():
    e, _ = scripted_engine(pilot=False)
    push(e, candidate(e))
    step(e)
    assert e.positions
    e.inject("both_down", 900)
    step(e, 30, 4)
    assert e.positions            # < 5 min: broker-side stops are the safety net
    step(e, 30, 8)                # > 5 min with the order API reachable -> flatten per policy
    assert not e.positions


def test_daily_loss_limit_blocks_new_entries():
    e, _ = scripted_engine(pilot=False)
    e.ledger.realized_net -= 8200            # -8.2% today (2.0's daily_loss limit is 8%, D-036)
    push(e, candidate(e))
    rec = e.funnel.records["scripted:NSE:RELIANCE:LONG"]
    assert rec.status == "REJECTED" and "daily_loss_ok" in rec.reason


def _invariant_hook(e: Engine, log: list):
    orig = e._process_market

    def wrapped(sim_dt, minute):
        orig(sim_dt, minute)
        total = sum(p.open_risk for p in e.positions.values())
        assert total <= ceilings.MAX_OPEN_RISK * e.ledger.equity_sod + 1e-6, "open risk above 6% ceiling"
        for p in e.positions.values():
            assert p.worst_case_at_stop() <= p.planned_risk + 1e-6, f"{p.instrument.symbol} outside plan"
            assert p.planned_risk <= ceilings.MAX_RISK_PER_TRADE * e.ledger.equity_sod + 1e-6
            if p.id not in e.exit_orders and p.qty > 0:
                assert p.broker_stop_id in e.broker.resting, f"{p.instrument.symbol} has no broker-side stop"
        if e.gears.gear is Gear.HALTED:
            log.append("halted")
    e._process_market = wrapped


@pytest.mark.parametrize("seed", [2, 9, 20])
def test_runtime_invariants_hold_every_tick(seed):
    e = Engine(CFG, RULES, CAL, seed=seed, start=DAY, warmup_days=2)
    log: list = []
    _invariant_hook(e, log)
    e.run_until(CAL.close_dt(dt.date(2026, 9, 29)) + dt.timedelta(minutes=2), step_seconds=30)
    enters = [d for d in e.decisions if d["action"] == "ENTER"]
    assert all(d["risk_approval"] == "APPROVED" for d in enters)
    assert all(t["label"] == "SIMULATED" for t in e.ledger.closed_trades)


def test_replay_is_deterministic():
    runs = []
    for _ in range(2):
        e = Engine(CFG, RULES, CAL, seed=21, start=DAY, warmup_days=2)
        e.run_until(dt.datetime.combine(DAY, dt.time(14, 0), tzinfo=IST), step_seconds=30)
        runs.append((round(e.ledger.equity, 4), [t["symbol"] for t in e.ledger.closed_trades],
                     round(e.market.quote("NSE:NIFTY").ltp, 2)))
    assert runs[0] == runs[1]


def test_position_never_stays_without_a_broker_side_stop():
    """Red-team fix: if the broker stop cannot be placed, retry each tick; exit after 60 s unprotected."""
    e, _ = scripted_engine(pilot=False)
    push(e, candidate(e))
    step(e)
    p = next(iter(e.positions.values()))
    e.broker.cancel(p.broker_stop_id, e.now)         # the resting stop disappears (e.g. rejected by the RMS)
    step(e)
    assert p.broker_stop_id in e.broker.resting      # re-placed on the next tick
    e.inject("order_api_down", 900)
    e.broker.cancel(p.broker_stop_id, e.now)
    step(e, 30, 4)                                   # cannot re-place while the API is down
    e.inject("clear")
    step(e, 30, 2)
    assert not e.positions or all(x.broker_stop_id in e.broker.resting for x in e.positions.values())


def test_silent_alert_channel_pauses_entries(monkeypatch):
    wall = FakeWall()
    e = Engine(CFG, RULES, CAL, seed=3, start=DAY, warmup_days=2, wall_clock=wall)
    e.alert_channel_required = True
    e.alert_heartbeat = wall.t
    wall.t += 121
    assert "alert channel (Discord) unreachable" in e._risk_ctx().operational_halts
