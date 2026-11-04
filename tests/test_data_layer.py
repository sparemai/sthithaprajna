import datetime as dt
import struct

import httpx
import pytest

from sthitaprajna.config import load_holidays, load_operator, load_rules
from sthitaprajna.data.adapters.dhan import (
    DhanDataAdapter, parse_dhan_option_chain, parse_ticker_packet, ticker_subscribe_payload,
)
from sthitaprajna.data.adapters.upstox import candles_from_upstox, normalize_tick_size, parse_upstox_option_chain
from sthitaprajna.data.arbitration import FeedArbiter
from sthitaprajna.data.feeds import FaultInjector, SimFeed
from sthitaprajna.data.quality import DataQualityGuardian
from sthitaprajna.data.sim import SimMarket
from sthitaprajna.domain import IST, Exchange, Instrument, Kind, Quote, Source
from sthitaprajna.market.calendar import MarketCalendar

CFG = load_operator()
T = dt.datetime(2026, 9, 29, 10, 0, tzinfo=IST)


def test_dhan_chain_parse_matches_verified_shape():
    body = {"data": {"last_price": 1233.9, "oc": {
        "1240.000000": {
            "ce": {"greeks": {"delta": 0.45, "theta": -1.2, "gamma": 0.004, "vega": 0.8}, "implied_volatility": 21.5,
                   "last_price": 18.4, "oi": 120000, "previous_oi": 100000, "volume": 5000, "security_id": 144346,
                   "top_bid_price": 18.3, "top_ask_price": 18.5},
            "pe": {"greeks": {"delta": -0.55, "theta": -1.1, "gamma": 0.004, "vega": 0.8}, "implied_volatility": 22.0,
                   "last_price": 23.1, "oi": 90000, "previous_oi": 95000, "volume": 4000, "security_id": 144347,
                   "top_bid_price": 23.0, "top_ask_price": 23.2}}}}, "status": "success"}
    spot, rows = parse_dhan_option_chain(body, "RELIANCE", dt.date(2026, 9, 29))
    assert spot == 1233.9 and len(rows) == 1
    r = rows[0]
    assert r.strike == 1240.0 and r.ce_oi_chg == 20000 and r.pe_oi_chg == -5000
    assert r.ce_iv == pytest.approx(0.215) and r.pe_delta == -0.55


def test_dhan_ticker_packet_and_subscribe_limit():
    raw = struct.pack("<BHBIfI", 2, 16, 1, 1333, 1500.5, 1790000000)
    t = parse_ticker_packet(raw)
    assert t is not None and t.security_id == 1333 and t.ltp == pytest.approx(1500.5)
    assert parse_ticker_packet(struct.pack("<BHBIfI", 4, 16, 1, 1, 1.0, 1)) is None
    assert parse_ticker_packet(b"\x02\x00") is None
    with pytest.raises(ValueError):
        ticker_subscribe_payload([("NSE_EQ", str(i)) for i in range(5001)])


def test_dhan_quotes_send_integer_security_ids():
    seen = {}

    def handler(request: httpx.Request):
        import json
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"data": {"NSE_EQ": {"11536": {"last_price": 4525.55,
                              "ohlc": {"open": 4500, "high": 4550, "low": 4490, "close": 4510}}}}, "status": "success"})

    a = DhanDataAdapter(http=httpx.Client(transport=httpx.MockTransport(handler), base_url="https://x"))
    inst = Instrument("NSE:TCS", "TCS", "TCS", Kind.EQUITY, Exchange.NSE, dhan_security_id="11536")
    qs = a.quotes([inst])
    assert seen["body"] == {"NSE_EQ": [11536]}   # ints, not strings (confirmed-live quirk)
    assert qs[0].ltp == 4525.55 and qs[0].source is Source.DHAN


def test_dhan_4xx_is_not_retried():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(400, json={"error": "bad"})

    a = DhanDataAdapter(http=httpx.Client(transport=httpx.MockTransport(handler), base_url="https://x"))
    with pytest.raises(httpx.HTTPStatusError):
        a._post("/marketfeed/ohlc", {})
    assert calls["n"] == 1


def test_upstox_helpers():
    assert normalize_tick_size(5) == 0.05 and normalize_tick_size(0.05) == 0.05
    body = {"data": {"candles": [["2026-09-29T09:16:00+05:30", 2, 3, 1, 2.5, 10, 0],
                                 ["2026-09-29T09:15:00+05:30", 1, 2, 0.5, 2, 5, 0],
                                 ["2026-09-29T09:16:00+05:30", 2, 3, 1, 2.5, 10, 0]]}}
    c = candles_from_upstox("NSE:X", body)
    assert [x.start.minute for x in c] == [15, 16]
    chain = {"data": [{"strike_price": 25000, "underlying_spot_price": 25010,
                       "call_options": {"market_data": {"ltp": 90, "oi": 100, "prev_oi": 80, "bid_price": 89.9, "ask_price": 90.1},
                                        "option_greeks": {"delta": 0.52, "iv": 13.1}},
                       "put_options": {"market_data": {"ltp": 80, "oi": 150, "prev_oi": 160},
                                       "option_greeks": {"delta": -0.48, "iv": 13.5}}}]}
    spot, rows = parse_upstox_option_chain(chain, "NIFTY", dt.date(2026, 9, 29))
    assert spot == 25010 and rows[0].ce_oi_chg == 20 and rows[0].pe_iv == pytest.approx(0.135)


def _q(key, px, ts, src=Source.DHAN):
    return Quote(key, px, px - 0.05, px + 0.05, 0, 0, ts, ts, src)


def test_arbitration_table_every_row():
    arb = FeedArbiter(CFG.data)
    arb.update(T, [_q("NSE:NIFTY", 25000, T)], [_q("NSE:NIFTY", 25000, T, Source.UPSTOX)])
    assert arb.situation == "BOTH_AGREE" and arb.data_confidence("NSE:NIFTY") == 1.0 and arb.entries_allowed()
    # disagreement must persist > 3 s before it counts
    t1 = T + dt.timedelta(seconds=1)
    arb.update(t1, [_q("NSE:NIFTY", 25000, t1)], [_q("NSE:NIFTY", 25050, t1, Source.UPSTOX)])
    assert arb.situation == "BOTH_AGREE"
    t5 = T + dt.timedelta(seconds=5)
    arb.update(t5, [_q("NSE:NIFTY", 25000, t5)], [_q("NSE:NIFTY", 25050, t5, Source.UPSTOX)])
    assert arb.situation == "DISAGREE" and arb.data_confidence("NSE:NIFTY") < 0.6
    assert arb.data_confidence("NSE:RELIANCE") == 1.0          # only that instrument is blocked
    # secondary down -> capped at 0.8, entries allowed
    t20 = T + dt.timedelta(seconds=20)
    arb.update(t20, [_q("NSE:NIFTY", 25000, t20)], [])
    assert arb.situation == "SECONDARY_DOWN" and arb.data_confidence() == 0.8 and arb.entries_allowed()
    # primary down -> low, entries blocked, secondary used to monitor
    arb2 = FeedArbiter(CFG.data)
    arb2.update(T, [_q("NSE:NIFTY", 25000, T)], [_q("NSE:NIFTY", 25000, T, Source.UPSTOX)])
    arb2.update(t20, [], [_q("NSE:NIFTY", 25001, t20, Source.UPSTOX)])
    assert arb2.situation == "PRIMARY_DOWN" and not arb2.entries_allowed()
    assert arb2.best_quote("NSE:NIFTY").source is Source.UPSTOX
    # both down -> none; flatten after 5 minutes
    t40 = T + dt.timedelta(seconds=40)
    arb2.update(t40, [], [])
    assert arb2.situation == "BOTH_DOWN" and arb2.data_confidence() == 0.0
    assert not arb2.should_flatten(t40 + dt.timedelta(seconds=100))
    assert arb2.should_flatten(t40 + dt.timedelta(seconds=301))


def test_quality_guardian_blocks_bad_prints_and_stale():
    g = DataQualityGuardian()
    g.check(T, [_q("NSE:X", 100, T)], ["NSE:X"], set())
    t2 = T + dt.timedelta(seconds=1)
    g.check(t2, [_q("NSE:X", 130, t2)], ["NSE:X"], set())
    assert "bad print" in g.blocked["NSE:X"]
    t9 = T + dt.timedelta(seconds=9)
    g.check(t9, [], ["NSE:X"], set())
    assert g.blocked["NSE:X"] == "stale ticks"
    g.check(t9, [], [], set(), local_clock=t9 + dt.timedelta(seconds=5))
    assert "clock_drift" in g.halts


@pytest.fixture(scope="module")
def sim():
    cal = MarketCalendar.from_rules(load_rules(), load_holidays())
    return SimMarket(cal, seed=11, start=dt.date(2026, 9, 28), warmup_days=2)


def test_sim_bars_are_complete_minutes_without_lookahead(sim):
    before = len(sim.candles("NSE:NIFTY"))
    ev = sim.step(60)
    assert "minute" in ev
    c = sim.candles("NSE:NIFTY")
    assert len(c) == before + 1
    assert c[-1].start + dt.timedelta(minutes=1) <= sim.now          # completed bar only


def test_sim_feeds_and_fault_injection(sim):
    p = SimFeed(sim, "DHAN", Source.DHAN, 60, 10, 1)
    s = SimFeed(sim, "UPSTOX", Source.UPSTOX, 110, 20, 2)
    fi = FaultInjector(sim, p, s)
    keys = ["NSE:NIFTY", "NSE:RELIANCE"]
    assert len(p.poll(keys)) == 2
    fi.inject("primary_down", 30)
    assert p.poll(keys) == [] and len(s.poll(keys)) == 2
    fi.inject("disagree", key="NSE:NIFTY", bps=30)
    a, b = sim.quote("NSE:NIFTY").ltp, s.poll(["NSE:NIFTY"])[0].ltp
    assert b == pytest.approx(a * 1.003, rel=1e-4)
    fi.inject("clear")
    assert len(p.poll(keys)) == 2


def test_sim_option_chain_is_arbitrage_consistent(sim):
    spot, rows = sim.chain("NIFTY")
    calls = [r.ce_ltp for r in rows]
    puts = [r.pe_ltp for r in rows]
    assert all(a >= b - 0.05 for a, b in zip(calls, calls[1:]))     # calls fall with strike
    assert all(a <= b + 0.05 for a, b in zip(puts, puts[1:]))       # puts rise with strike
    assert all(r.ce_bid <= r.ce_ltp <= r.ce_ask for r in rows)
    q = sim.quote(rows[len(rows) // 2].ce_key)
    assert q is not None and q.bid <= q.ask
