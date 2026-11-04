"""General strategies select future contracts without an expiry-day session veto."""
import datetime as dt
from types import SimpleNamespace as NS

import pytest

from sthitaprajna.config import load_operator
from sthitaprajna.engine.runtime import Engine, _FeatureCacheCtx
from sthitaprajna.strategies.options import (
    DayRangeBreakout, IndexOptionMomentum, IndexRangeReversion, ScalpMomentum,
    StockOptionMomentum, _pick_contract,
)
from test_engine_e2e import CAL, RULES
from test_day_range_breakout import _features
from test_range_reversion import _range_features
from test_scalp_momentum import _scalp_features
from test_stock_options import crafted


@pytest.mark.parametrize('day', [dt.date(2026, 10, n) for n in (19, 20, 21, 22, 23)])
@pytest.mark.parametrize('strategy', [IndexOptionMomentum, IndexRangeReversion,
                                      DayRangeBreakout, ScalpMomentum, StockOptionMomentum])
def test_qualifying_setups_on_every_weekday(day, strategy):
    e = Engine(load_operator(), RULES, CAL, seed=17, start=day, warmup_days=0)
    # Deliberately after the former 13:00 expiry-day veto, inside normal entry windows.
    e.market.now = dt.datetime.combine(day, dt.time(13, 15), tzinfo=e.now.tzinfo)
    e.regime.volatility = 'NORMAL_VOL'
    e.regime.trend_strength = 10
    key = 'NSE:RELIANCE' if strategy is StockOptionMomentum else 'NSE:NIFTY'
    if strategy is DayRangeBreakout:
        features = _features(key, e)
    elif strategy is ScalpMomentum:
        features = _scalp_features(key, e)
    elif strategy is IndexRangeReversion:
        features = _range_features(key, e)
    else:
        features = crafted(e, key=key)
    ctx = _FeatureCacheCtx(e)
    ctx._feat_cache[key] = features
    ctx.option_underlyings = lambda: [('RELIANCE', 'NSE:RELIANCE')] if strategy is StockOptionMomentum else []
    out = [c for c in strategy().scan(ctx) if c.underlying_key == key]
    assert out, (day, strategy.__name__)
    assert all(c.instrument.expiry > day for c in out)
    assert len(out) == 1  # later expiries must not multiply the same directional bet
    if strategy is StockOptionMomentum:
        assert all((c.instrument.expiry - day).days > 3 for c in out)


def picker_context(failure=None):
    today = dt.date(2026, 10, 20)
    near, far = today + dt.timedelta(days=7), today + dt.timedelta(days=14)
    row = NS(strike=25000, ce_delta=.55, pe_delta=-.55,
             ce_bid=99, ce_ask=100, ce_ltp=100, pe_bid=99, pe_ask=100, pe_ltp=100)
    seen = []
    def chain(und, expiry):
        seen.append(expiry)
        if expiry == near and failure == 'empty':
            return 25000, []
        if expiry == near and failure == 'spread':
            return 25000, [NS(**{**vars(row), 'ce_ask': 150, 'pe_ask': 150})]
        return 25000, [row]
    def quote(key):
        if key == near and failure == 'quote':
            return None
        if key == near and failure == 'quote_spread':
            return NS(bid=80, ask=100, ltp=100)
        return NS(bid=99, ask=100, ltp=100)
    ctx = NS(now=dt.datetime.combine(today, dt.time(13)),
             expiries=lambda und: [far, today, near, near, today-dt.timedelta(days=1)],
             chain=chain, quote=quote,
             option_instrument=lambda und, exp, strike, ot: NS(key=exp, option_type=ot))
    return ctx, seen, near, far


@pytest.mark.parametrize('call', [True, False])
@pytest.mark.parametrize('failure', ['empty', 'spread', 'quote', 'quote_spread'])
def test_falls_forward_when_near_contract_is_unusable(call, failure):
    ctx, seen, near, far = picker_context(failure)
    result = _pick_contract(ctx, 'NIFTY', call, .55)
    assert result is not None and result[0] == far
    assert seen == [near, far]


def test_sorts_deduplicates_and_selects_only_one_future_contract():
    ctx, seen, near, _ = picker_context()
    assert _pick_contract(ctx, 'NIFTY', True, .55)[0] == near
    assert seen == [near]


def test_no_future_contract_means_no_trade():
    ctx, seen, _, _ = picker_context()
    ctx.expiries = lambda und: [ctx.now.date()]
    assert _pick_contract(ctx, 'NIFTY', True, .55) is None
    assert seen == []


def test_all_unusable_chains_mean_no_trade():
    ctx, _, _, _ = picker_context()
    ctx.chain = lambda und, exp: (25000, [])
    assert _pick_contract(ctx, 'NIFTY', True, .55) is None


def test_scalp_uses_next_expiry_after_old_late_day_cutoff():
    day = dt.date(2026, 10, 20)  # NIFTY expiry Tuesday
    e = Engine(load_operator(), RULES, CAL, seed=17, start=day, warmup_days=0)
    e.market.now = dt.datetime.combine(day, dt.time(15, 0), tzinfo=e.now.tzinfo)
    e.regime.volatility = 'NORMAL_VOL'
    ctx = _FeatureCacheCtx(e)
    ctx._feat_cache['NSE:NIFTY'] = _scalp_features('NSE:NIFTY', e)
    ctx.option_underlyings = lambda: []
    out = [c for c in ScalpMomentum().scan(ctx) if c.underlying_key == 'NSE:NIFTY']
    assert len(out) == 1 and out[0].instrument.expiry > day
