"""Stock options for booming F&O stocks (unbounded mode, D-031): SIM and live Dhan chains for stocks, fractional
strikes, lots from the master, the StockOptionMomentum strategy, and a full paper trade to its target."""
import dataclasses
import datetime as dt

import httpx
import pytest
from fake_dhan import CLIENT_ID, TOKEN, FakeClock, FakeDhan, ist
from test_engine_e2e import CAL, DAY, RULES, push, step
from test_unbounded import ub_engine, unbounded_cfg

from sthitaprajna.config import load_operator
from sthitaprajna.data.adapters.dhan import parse_dhan_option_chain
from sthitaprajna.data.dhan_live import DhanClient
from sthitaprajna.data.live import DisabledFeed, LiveFeed, LiveMarket
from sthitaprajna.data.sim import option_key, sim_fno_lot, stock_strike_step, strike_str
from sthitaprajna.domain import Candle, Direction, Kind, Mode, OptionType, Segment
from sthitaprajna.engine.runtime import Engine, _FeatureCacheCtx
from sthitaprajna.strategies.base import Features
from sthitaprajna.strategies.options import ROLL_DAYS, StockOptionMomentum

KEY = "NSE:RELIANCE"


# ---------------------------------------------------------------- keys and SIM chains
def test_fractional_strikes_keep_distinct_keys():
    d = dt.date(2026, 10, 27)
    assert strike_str(25000.0) == "25000" and strike_str(247.5) == "247.5" and strike_str(12.25) == "12.25"
    assert option_key("TATASTEEL", d, 247.5, OptionType.CE) == "NFO:TATASTEEL:2026-10-27:247.5:CE"
    assert option_key("TATASTEEL", d, 247.5, OptionType.CE) != option_key("TATASTEEL", d, 247.0, OptionType.CE)
    body = {"data": {"last_price": 248.1, "oc": {
        "247.500000": {"ce": {"last_price": 6.1}, "pe": {"last_price": 5.2}},
        "250.000000": {"ce": {"last_price": 4.9}, "pe": {"last_price": 6.8}}}}}
    _spot, rows = parse_dhan_option_chain(body, "TATASTEEL", d, "NFO")
    assert [r.ce_key for r in rows] == ["NFO:TATASTEEL:2026-10-27:247.5:CE", "NFO:TATASTEEL:2026-10-27:250:CE"]


def test_sim_stock_chain_expiries_lot_and_quotes():
    e, _ = ub_engine()
    m = e.market
    assert ("RELIANCE", KEY) in m.option_underlyings()
    exps = m.expiries("RELIANCE")
    assert exps == CAL.monthly_expiries("RELIANCE", DAY, 3)             # stocks: monthly contracts only
    spot, rows = m.chain("RELIANCE", exps[1])
    step_ = stock_strike_step(spot)
    strikes = [r.strike for r in rows]
    assert len(rows) == 31 and all(b - a == step_ for a, b in zip(strikes, strikes[1:]))
    atm = min(rows, key=lambda r: abs(r.strike - spot))
    assert 0.4 < atm.ce_delta < 0.65 and -0.65 < atm.pe_delta < -0.35
    assert atm.ce_iv > 0.15                                              # stock vol, not index vol
    inst = m.option_instrument("RELIANCE", exps[1], atm.strike, OptionType.CE)
    stock = m.instruments[KEY]
    assert inst.key == atm.ce_key and inst.kind is Kind.OPTION and inst.underlying == KEY
    assert inst.lot_size == sim_fno_lot(spot) and inst.sector == stock.sector and inst.beta == stock.beta
    q = m.quote(inst.key)
    assert q is not None and q.ask > q.bid > 0


# ---------------------------------------------------------------- the strategy
def crafted(e: Engine, *, up: bool = True, rvol: float = 2.2, key: str = KEY) -> Features:
    px = e.market.states[key].price
    atr5 = round(px * 0.004, 2)
    sign = 1 if up else -1
    level = px - sign * 0.5 * atr5                     # just broken, well inside 1.5 x ATR5
    t = e.now.replace(second=0, microsecond=0)
    c5 = [Candle(key, t - dt.timedelta(minutes=5 * (3 - i)), px, px, px, px + sign * 0.1 * i, 5000) for i in range(3)]
    c5[-1] = dataclasses.replace(c5[-1], close=px)
    return Features(key, px, vwap=px - sign * atr5, atr1=atr5 / 2, atr5=atr5,
                    or_high=level if up else px + 3 * atr5, or_low=px - 3 * atr5 if up else level,
                    ema20=px, ema50=px - sign, adx=28.0, rvol=rvol, c5=c5, day=c5, htf_up=up, htf_down=not up,
                    bar_id=f"{key}@crafted")


def ctx_for(e: Engine, feats: Features, und: str = "RELIANCE") -> _FeatureCacheCtx:
    ctx = _FeatureCacheCtx(e)
    ctx.option_underlyings = lambda: [(und, feats.key)]
    ctx._feat_cache[feats.key] = feats
    return ctx


def test_strategy_buys_a_call_on_an_upside_break_and_a_put_on_a_downside_break():
    e, _ = ub_engine()
    today = e.now.date()
    (c,) = StockOptionMomentum().scan(ctx_for(e, crafted(e)))
    inst = c.instrument
    assert c.segment is Segment.STOCK_OPTION and c.direction is Direction.LONG and c.bias is Direction.LONG
    assert inst.option_type is OptionType.CE and inst.underlying == KEY and c.underlying_key == KEY
    assert (inst.expiry - today).days > ROLL_DAYS                        # Sep-29 expiry is tomorrow: rolled
    assert inst.expiry == [x for x in CAL.monthly_expiries("RELIANCE", today, 3) if (x - today).days > ROLL_DAYS][0]
    q = e.quote(inst.key)
    assert c.entry == pytest.approx(q.ask) and c.trigger_fired and c.defined_risk and c.target_r == 2.0
    assert 0.60 * c.entry - 0.01 <= c.stop <= 0.90 * c.entry + 0.01      # stop 10-40% of the premium
    assert {x.name for x in c.confirmations} >= {"relative_volume", "vwap_acceptance", "htf_structure"}
    assert "RELIANCE breaks above" in c.explanation and f"lot {inst.lot_size}" in c.explanation
    (p,) = StockOptionMomentum().scan(ctx_for(e, crafted(e, up=False)))
    assert p.instrument.option_type is OptionType.PE and p.direction is Direction.LONG and p.bias is Direction.SHORT


@pytest.mark.parametrize("why", ["quiet", "extreme_vol", "too_early", "too_far"])
def test_strategy_stays_out(why):
    e, _ = ub_engine()
    f = crafted(e, rvol=1.2 if why == "quiet" else 2.2)
    if why == "extreme_vol":
        e.regime = dataclasses.replace(e.regime, volatility="EXTREME_VOL")
    if why == "too_far":                                                # chasing: > 1.5 x ATR5 past the level
        f = dataclasses.replace(f, or_high=f.price - 2.0 * f.atr5)
    ctx = ctx_for(e, f)
    if why == "too_early":
        ctx.now = e.now.replace(hour=9, minute=25)
    assert StockOptionMomentum().scan(ctx) == []


def test_strategy_runs_in_both_risk_modes():
    """2.0 is options-only: stock options trade at every tier, in bounded and unbounded risk mode alike."""
    e, _ = ub_engine()
    assert any(isinstance(s, StockOptionMomentum) for s in Engine(unbounded_cfg(), RULES, CAL, seed=1).strategies)
    assert any(isinstance(s, StockOptionMomentum) for s in Engine(load_operator(), RULES, CAL, seed=1).strategies)


# ---------------------------------------------------------------- a full paper trade
def test_unbounded_engine_trades_the_stock_option_to_its_target():
    e, _ = ub_engine()
    (c,) = StockOptionMomentum().scan(ctx_for(e, crafted(e)))
    push(e, c)
    step(e)
    (p,) = e.positions.values()
    lot = c.instrument.lot_size
    assert p.segment is Segment.STOCK_OPTION and p.qty % lot == 0 and p.qty >= lot
    assert p.qty * p.avg_entry <= 40_000 + 1                             # 40% of Rs 1,00,000 buys whole lots (D-051)
    assert (p.qty + lot) * p.avg_entry > 40_000                          # ...as many as fit
    assert p.target == pytest.approx(p.avg_entry + 2.0 * p.unit_risk, abs=0.01)
    assert p.broker_stop_id in e.broker.resting
    px = e.market.states[KEY].price
    for i in range(1, 30):                                               # the stock keeps running
        e.market.set_price(KEY, round(px * (1 + 0.004 * i), 1))
        step(e)
        if not e.positions:
            break
    t = e.ledger.closed_trades[-1]
    assert t["reason"] == "TARGET" and t["net"] > 0 and t["stages"] == ["PROBE", "EXIT"]


# ---------------------------------------------------------------- live Dhan stock chains (fake API)
def test_live_stock_chain_uses_the_equity_segment_and_the_master_lot(tmp_path):
    clock = FakeClock(ist(2026, 9, 28, 11, 0))
    fake = FakeDhan(clock, extra_stocks=12)
    client = DhanClient(CLIENT_ID, TOKEN, transport=httpx.MockTransport(fake.handler), sleep=lambda s: None, rules=RULES)
    m = LiveMarket(CAL, client, RULES, clock=clock, cache_dir=tmp_path, mono=clock.mono)
    m.bootstrap(progress=lambda s: None, backfill_days=0, start_poller=False)
    e = Engine(unbounded_cfg(), RULES, CAL, mode=Mode.PAPER, market=m, feeds=(LiveFeed(m), DisabledFeed()),
               wall_clock=clock.mono)
    assert m.option_underlyings() == []                   # no core stock has an OPTSTK row in this master
    for _ in range(4):
        clock.advance(2)
        m.run_once()
        e.tick(0.25)
    assert m.promote("NSE:SYN003") and m.promote("NSE:SYN001")
    assert m.option_underlyings() == [("SYN003", "NSE:SYN003")]         # SYN001 has no options
    sid = fake.sid("SYN003")
    first = m.expiries("SYN003")                                       # calendar until Dhan's list arrives
    assert first == CAL.monthly_expiries("SYN003", clock().date(), 3)
    for _ in range(3):
        clock.advance(2)
        m.run_once()
    asked = [b for p, b in fake.requests if p.endswith("/optionchain/expirylist") and b["UnderlyingScrip"] == sid]
    assert asked and asked[-1]["UnderlyingSeg"] == "NSE_EQ"
    exp = m.expiries("SYN003")[1]
    assert m.chain("SYN003", exp)[1] == []                             # never blocks: the poller fetches it
    for _ in range(3):
        clock.advance(4)
        m.run_once()
    spot, rows = m.chain("SYN003", exp)
    assert rows and spot == pytest.approx(fake.px[sid])
    body = [b for p, b in fake.requests if p.endswith("/optionchain") and b["UnderlyingScrip"] == sid][-1]
    assert body["UnderlyingSeg"] == "NSE_EQ" and body["Expiry"] == exp.isoformat()
    strikes = sorted(r.strike for r in rows)
    assert strikes[1] - strikes[0] == stock_strike_step(spot)
    atm = min(rows, key=lambda r: abs(r.strike - spot))
    inst = m.option_instrument("SYN003", exp, atm.strike, OptionType.CE)
    assert inst.lot_size == 500 and inst.dhan_security_id and inst.underlying == "NSE:SYN003"
    q = m.quote(inst.key)
    assert q is not None and q.ask > 0
    assert m.chain("SYN001", exp)[1] == [] and m._und_ref("SYN001") is None
    clock.advance(4)
    m.run_once()
    assert not [b for p, b in fake.requests if p.endswith("/optionchain") and b["UnderlyingScrip"] == fake.sid("SYN001")]
