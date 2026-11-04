"""LIVE order execution (D-034) against a fake Dhan order API: nothing here touches a real account.

Covers the order client (payloads, errors, token isolation), and the live broker inside the engine: entry,
broker-side stop at Dhan, target exit that cancels the stop first, a stop that fills at Dhan (and the race where
the engine exits at the same moment: nothing sells twice), rejections, unfilled entries, option orders as
marketable limits with stop-limits and a chasing exit, reconciliation with a position closed in the Dhan app,
the kill switch, startup, and `order-check`.
"""
import datetime as dt

import httpx
import pytest
from fake_dhan import CLIENT_ID, TOKEN, TRADING_TOKEN, FakeClock, FakeDhan, ist
from test_engine_e2e import candidate, push
from test_unbounded import unbounded_cfg

from sthitaprajna.config import load_holidays, load_rules
from sthitaprajna.data.dhan_live import DhanClient
from sthitaprajna.data.live import DisabledFeed, LiveFeed, LiveMarket
from sthitaprajna.domain import (
    Candidate, Confirmation, Direction, Exchange, HalfLife, Instrument, Kind, Mode, OptionType, PerfLabel, Segment,
    new_id,
)
from sthitaprajna.engine.runtime import Engine
from sthitaprajna.execution.live import (
    DhanOrderAPI, DhanOrderError, LiveBroker, correlation_id, exchange_segment, product_type, resolve_mode, round_tick,
)
from sthitaprajna.execution.order_check import run_order_check
from sthitaprajna.execution.paper import PaperBroker
from sthitaprajna.market.calendar import MarketCalendar
from sthitaprajna.market.charges import ChargesModel

RULES = load_rules()
CAL = MarketCalendar.from_rules(RULES, load_holidays())
KEY = "NSE:RELIANCE"


def clients(fake: FakeDhan, clock: FakeClock):
    t = httpx.MockTransport(fake.handler)
    data = DhanClient(CLIENT_ID, TOKEN, transport=t, sleep=lambda s: None, rules=RULES)
    api = DhanOrderAPI(CLIENT_ID, TRADING_TOKEN, transport=t, sleep=lambda s: None, clock=clock.mono, algo_id="")
    return data, api


def live_engine(tmp_path, start=None, **cfg_over):
    clock = FakeClock(start or ist(2026, 9, 28, 10, 0))
    fake = FakeDhan(clock)
    data, api = clients(fake, clock)
    m = LiveMarket(CAL, data, RULES, clock=clock, cache_dir=tmp_path, mono=clock.mono)
    m.bootstrap(progress=lambda s: None, backfill_days=1, start_poller=False)
    broker = LiveBroker(api, ChargesModel(RULES), m.quote, wall=clock.mono, start_poller=False)
    broker.poll_once(account=True)
    e = Engine(unbounded_cfg(**cfg_over), RULES, CAL, mode=Mode.LIVE_FULL, market=m,
               feeds=(LiveFeed(m), DisabledFeed()), broker=broker, wall_clock=clock.mono)
    e.strategies = []
    e.pilot_approvals = False  # these tests exercise broker mechanics, not the D9 approval workflow (covered
                               # separately by test_engine_e2e.py); D-040 defaults LIVE to approval-required
    for _ in range(3):
        cycle(e, clock)
    return e, fake, clock


def cycle(e: Engine, clock: FakeClock, seconds: float = 1.0, n: int = 1, poll_first: bool = True) -> None:
    for _ in range(n):
        clock.advance(seconds)
        e.market.run_once()
        if poll_first:
            e.broker.poll_once()
        e.tick(0.25)
        if not poll_first:
            e.broker.poll_once()


def enter(e: Engine, clock: FakeClock, key: str = KEY, unit: float = 10.0):
    push(e, candidate(e, key, unit=unit))
    cycle(e, clock, n=2)
    return [p for p in e.positions.values() if p.instrument.key == key]


def sells(fake: FakeDhan, sid: int) -> list[dict]:
    return [r for r in fake.orders.values() if r["securityId"] == str(sid) and r["transactionType"] == "SELL"
            and r["orderStatus"] == "TRADED"]


def net(fake: FakeDhan, sid: int, seg: str = "NSE_EQ") -> int:
    n = fake.net.get((seg, sid))
    return int(n["buy_qty"] - n["sell_qty"]) if n else 0


# ---------------------------------------------------------------- helpers and the order client
def test_helpers_and_mode_resolution(monkeypatch):
    assert round_tick(101.03, 0.05, down=True) == 101.0 and round_tick(101.01, 0.05, down=False) == 101.05
    assert round_tick(99.999, 0.01, down=True) == 99.99 and round_tick(100.0, 0.05, down=True) == 100.0
    cid = correlation_id("ord-00012-a1b2c3")
    assert cid == "spord00012a1b2c3" and cid.isalnum() and len(correlation_id("x" * 40)) == 20
    eq = Instrument("NSE:SBIN", "SBIN", "SBI", Kind.EQUITY, Exchange.NSE)
    nfo = Instrument("NFO:NIFTY:2026-09-29:25000:CE", "N", "N", Kind.OPTION, Exchange.NSE)
    bfo = Instrument("BFO:SENSEX:2026-10-01:82000:PE", "S", "S", Kind.OPTION, Exchange.BSE)
    assert [exchange_segment(i) for i in (eq, nfo, bfo)] == ["NSE_EQ", "NSE_FNO", "BSE_FNO"]
    assert product_type(Segment.EQ_INTRADAY) == product_type(Segment.STOCK_OPTION) == "INTRADAY"
    assert product_type(Segment.EQ_DELIVERY) == "CNC"
    monkeypatch.delenv("DHAN_TRADING_TOKEN", raising=False)
    mode, missing = resolve_mode(Mode.LIVE_FULL)
    assert mode is Mode.PAPER and "DHAN_TRADING_TOKEN" in missing
    assert resolve_mode(Mode.LIVE_FULL, broker_live=True) == (Mode.LIVE_FULL, [])
    assert resolve_mode(Mode.PAPER) == (Mode.PAPER, [])


def test_order_client_needs_the_trading_token_and_never_leaks_it(monkeypatch):
    monkeypatch.delenv("DHAN_TRADING_TOKEN", raising=False)
    monkeypatch.setenv("DHAN_CLIENT_ID", CLIENT_ID)
    with pytest.raises(PermissionError, match="DHAN_TRADING_TOKEN"):
        DhanOrderAPI()
    clock = FakeClock(ist(2026, 9, 28, 10, 0))
    fake = FakeDhan(clock)
    wrong = DhanOrderAPI(CLIENT_ID, TOKEN, transport=httpx.MockTransport(fake.handler), sleep=lambda s: None)
    with pytest.raises(DhanOrderError) as err:          # the DATA token cannot place orders (isolation)
        wrong.funds()
    assert err.value.kind == "AUTH" and "DHAN_TRADING_TOKEN" in err.value.explain() and TOKEN not in err.value.explain()
    _data, api = clients(fake, clock)
    sid = fake.sid("RELIANCE")
    bad = {"dhanClientId": CLIENT_ID, "transactionType": "BUY", "exchangeSegment": "NSE_EQ", "productType": "INTRADAY",
           "orderType": "LIMIT", "validity": "DAY", "securityId": str(sid), "quantity": 1, "price": 1400.03}
    resp = api.place(bad)
    assert resp["orderStatus"] == "REJECTED" and "tick" in api.order(resp["orderId"])["omsErrorDescription"]
    with pytest.raises(DhanOrderError) as err:
        api.place({**bad, "exchangeSegment": "NSE_FNO"})
    assert err.value.kind == "INPUT" and "bug" in err.value.explain()
    assert api.funds()["availabelBalance"] == 100_000


# ---------------------------------------------------------------- the live broker in the engine
def test_live_equity_trade_places_a_stop_at_dhan_and_exits_at_target(tmp_path):
    e, fake, clock = live_engine(tmp_path)
    assert e.mode is Mode.LIVE_FULL and e.perf_label is PerfLabel.LIVE and isinstance(e.broker, LiveBroker)
    assert any("REAL order" in ev["msg"] for ev in e.events)
    (p,) = enter(e, clock)
    sid = fake.sid("RELIANCE")
    buy = fake.orders_where(transactionType="BUY")[0]
    assert buy["orderType"] == "MARKET" and buy["productType"] == "INTRADAY" and buy["exchangeSegment"] == "NSE_EQ"
    assert buy["securityId"] == str(sid) and buy["quantity"] == p.qty and buy["correlationId"].startswith("sp")
    assert p.qty * p.avg_entry <= 40_000 + 1 and p.avg_entry == buy["averageTradedPrice"]
    (stop,) = fake.orders_where(orderType="STOP_LOSS_MARKET")
    assert stop["orderStatus"] == "PENDING" and stop["transactionType"] == "SELL" and stop["quantity"] == p.qty
    assert stop["triggerPrice"] == round_tick(p.broker_stop, 0.05, down=True) and p.broker_stop_id in e.broker.resting
    fake.set_price("RELIANCE", p.target + 1.0)
    cycle(e, clock, n=3)
    assert not e.positions
    t = e.ledger.closed_trades[-1]
    assert t["reason"] == "TARGET" and t["label"] == "LIVE" and t["net"] > 0
    assert stop["orderStatus"] == "CANCELLED"                 # the stop left Dhan before the exit went out
    assert len(sells(fake, sid)) == 1 and net(fake, sid) == 0


def test_stop_that_fills_at_dhan_while_the_engine_exits_never_sells_twice(tmp_path):
    e, fake, clock = live_engine(tmp_path)
    (p,) = enter(e, clock)
    sid = fake.sid("RELIANCE")
    fake.set_price("RELIANCE", p.broker_stop - 2.0)           # through the system stop AND the broker stop
    cycle(e, clock, n=3, poll_first=False)                    # the engine decides to exit on a stale order book
    assert not e.positions
    assert e.ledger.closed_trades[-1]["reason"] == "BROKER_STOP"
    assert len(sells(fake, sid)) == 1 and net(fake, sid) == 0  # the stop sold; no second sell, no short
    assert any("already sold first" in (o.reject_reason or "") for o in e.orders.values())


def test_a_dhan_rejection_is_reported_and_leaves_no_position(tmp_path):
    e, fake, clock = live_engine(tmp_path)
    fake.reject_next = "Insufficient margin"
    assert enter(e, clock) == []
    assert any("Insufficient margin" in ev["msg"] for ev in e.events)
    assert not e.pending_entries and e.broker.reject_count(e.now) == 1


def test_an_entry_not_filled_in_time_is_cancelled(tmp_path):
    e, fake, clock = live_engine(tmp_path)
    fake.auto_fill = False
    assert enter(e, clock) == []
    assert e.pending_entries
    cycle(e, clock, seconds=2.0, n=10)
    (o,) = fake.orders_where(transactionType="BUY")
    assert o["orderStatus"] == "CANCELLED" and not e.pending_entries and not e.positions
    assert any("not filled" in ev["msg"] for ev in e.events)


def option_candidate(e: Engine, fake: FakeDhan, clock: FakeClock) -> Candidate:
    e.market.chain("NIFTY")
    cycle(e, clock, n=2)
    spot, rows = e.market.chain("NIFTY")
    atm = min(rows, key=lambda r: abs(r.strike - spot))
    exp = e.market.expiries("NIFTY", 1)[0]
    inst = e.market.option_instrument("NIFTY", exp, atm.strike, OptionType.CE)
    cycle(e, clock, n=2)
    q = e.quote(inst.key)
    confs = [Confirmation("a", "volume", True), Confirmation("b", "flow", True), Confirmation("c", "breadth", True)]
    feats = {k: 0.9 for k in ("regime", "trend", "volume", "volatility", "breadth", "participant", "liquidity",
                              "execution", "event")}
    return Candidate(new_id("sig"), e.now, "scripted", "1.0", inst, "NSE:NIFTY", Direction.LONG, Segment.INDEX_OPTION,
                     q.ask, round(q.ask * 0.8, 2), 2.0, 0.45, False, HalfLife.MINUTES, True, confs, feats, True, True,
                     setup_id=f"scripted:{inst.key}", signal_bar=f"{inst.key}@1", explanation="scripted option",
                     model_confidence=0.8, regime_confidence=0.8, atr=q.ask * 0.1)


def test_option_orders_are_marketable_limits_with_a_stop_limit_and_a_chasing_exit(tmp_path):
    e, fake, clock = live_engine(tmp_path)
    c = option_candidate(e, fake, clock)
    lot = c.instrument.lot_size
    push(e, c)
    cycle(e, clock, n=2)
    (p,) = e.positions.values()
    (buy,) = fake.orders_where(transactionType="BUY")
    assert buy["orderType"] == "LIMIT" and buy["exchangeSegment"] == "NSE_FNO" and buy["price"] >= c.entry
    assert buy["quantity"] == p.qty and p.qty % lot == 0 and p.qty * p.avg_entry <= 40_000 + 1
    (stop,) = fake.orders_where(orderType="STOP_LOSS")
    assert stop["price"] < stop["triggerPrice"] < p.avg_entry and stop["quantity"] == p.qty
    fake.auto_fill = False                                    # the book goes thin: the exit does not fill at first
    assert e.close(p.id)["ok"]
    cycle(e, clock, n=2)
    (sell,) = fake.orders_where(transactionType="SELL", orderType="LIMIT")
    first = sell["price"]
    cycle(e, clock, seconds=2.0, n=3)
    assert sell["price"] < first                              # re-priced lower (bid - 10%)
    assert any("exit not filled" in ev["msg"] for ev in e.events)
    fake.auto_fill = True
    cycle(e, clock, n=3)
    assert not e.positions and net(fake, int(c.instrument.dhan_security_id), "NSE_FNO") == 0


def test_a_position_closed_in_the_dhan_app_is_reconciled_and_its_stop_cancelled(tmp_path):
    e, fake, clock = live_engine(tmp_path)
    (p,) = enter(e, clock)
    sid = fake.sid("RELIANCE")
    fake.external_trade(sid, "NSE_EQ", "SELL", p.qty)          # the operator sells it in the Dhan app
    (stop,) = fake.orders_where(orderType="STOP_LOSS_MARKET")
    cycle(e, clock, seconds=8.0, n=6)                          # two position snapshots agree
    assert not e.positions and e.ledger.closed_trades[-1]["reason"] == "EXTERNAL"
    assert stop["orderStatus"] == "CANCELLED" and net(fake, sid) == 0   # the old stop can never sell again
    assert any("closed outside Sthitaprajna" in ev["msg"] for ev in e.events)


def test_kill_switch_flattens_real_positions(tmp_path):
    e, fake, clock = live_engine(tmp_path)
    (p,) = enter(e, clock)
    sid = fake.sid("RELIANCE")
    assert e.kill()["ok"]
    cycle(e, clock, n=3)
    assert not e.positions and e.ledger.closed_trades[-1]["reason"] == "KILL"
    assert net(fake, sid) == 0 and fake.orders_where(orderType="STOP_LOSS_MARKET")[0]["orderStatus"] == "CANCELLED"


def test_paper_mode_never_gets_the_live_broker(tmp_path):
    e, fake, clock = live_engine(tmp_path)
    paper = Engine(unbounded_cfg(), RULES, CAL, mode=Mode.PAPER, market=e.market,
                   feeds=(LiveFeed(e.market), DisabledFeed()), broker=e.broker, wall_clock=clock.mono)
    assert isinstance(paper.broker, PaperBroker) and paper.mode is Mode.PAPER and not paper.live_orders


def test_build_live_engine_reports_funds_and_unmanaged_positions(tmp_path):
    from sthitaprajna.api.server import build_live_engine
    clock = FakeClock(ist(2026, 9, 28, 10, 0))
    fake = FakeDhan(clock)
    fake.external_trade(fake.sid("INFY"), "NSE_EQ", "BUY", 5)  # from an earlier run, or the operator's own trade
    data, api = clients(fake, clock)
    e = build_live_engine(client=data, order_api=api, clock=clock, start_poller=False, backfill_days=1,
                          cache_dir=tmp_path, wall=clock.mono, progress=lambda s: None)
    msgs = " | ".join(ev["msg"] for ev in e.events)
    assert e.mode is Mode.LIVE_FULL and "Dhan funds available" in msgs and "NOT managed" in msgs
    e.market.close()


# ---------------------------------------------------------------- order-check
def test_order_check_is_read_only_unless_asked(tmp_path):
    clock = FakeClock(ist(2026, 9, 28, 10, 0))
    fake = FakeDhan(clock)
    data, api = clients(fake, clock)
    out: list[str] = []
    assert run_order_check(out.append, api=api, data_client=data, cache_dir=tmp_path,
                           today=dt.date(2026, 9, 28), sleep=lambda s: None) == 0
    text = "\n".join(out)
    assert "READ-ONLY CHECKS PASSED" in text and "available Rs 100,000.00" in text and "[VERIFY]" in text
    assert not [c for c in fake.order_calls if c[0] in ("POST", "PUT", "DELETE")]
    assert TRADING_TOKEN not in text and CLIENT_ID not in text
    out.clear()
    assert run_order_check(out.append, api=api, data_client=data, place_test=True, symbol="RELIANCE",
                           cache_dir=tmp_path, today=dt.date(2026, 9, 28), sleep=lambda s: None) == 0
    (o,) = fake.orders.values()
    ltp = fake.px[fake.sid("RELIANCE")]
    assert o["orderType"] == "LIMIT" and o["quantity"] == 1 and o["price"] <= ltp * 0.97 + 0.05
    assert o["orderStatus"] == "CANCELLED" and "READY for LIVE orders" in "\n".join(out)


def test_live_mode_defaults_to_approval_required_on_every_entry(tmp_path):
    """D9/D-040: the first LIVE session requires operator approval on every entry and stage-add by default,
    even though `live_engine()` (used by every other test here) opts back out to isolate broker mechanics."""
    clock = FakeClock(ist(2026, 9, 28, 10, 0))
    fake = FakeDhan(clock)
    data, api = clients(fake, clock)
    m = LiveMarket(CAL, data, RULES, clock=clock, cache_dir=tmp_path, mono=clock.mono)
    m.bootstrap(progress=lambda s: None, backfill_days=1, start_poller=False)
    broker = LiveBroker(api, ChargesModel(RULES), m.quote, wall=clock.mono, start_poller=False)
    broker.poll_once(account=True)
    e = Engine(unbounded_cfg(), RULES, CAL, mode=Mode.LIVE_FULL, market=m,
              feeds=(LiveFeed(m), DisabledFeed()), broker=broker, wall_clock=clock.mono)
    assert e.live_orders and e.pilot_approvals is True
