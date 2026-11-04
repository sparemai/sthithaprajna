"""Synthetic Indian market for SIM mode, paper-plumbing tests and failure injection.

EVERYTHING HERE IS SYNTHETIC. Prices, levels, OI and volumes are generated; they are not NSE/BSE data
and any strategy result on them is labelled SIMULATED -- it proves plumbing, never edge (D-006).

Model (per step):
* index day regime drawn at the open: trend-up / trend-down / range (mean-reverting) / volatile;
* stocks = beta x market factor + sector factor + idiosyncratic noise, with occasional catalyst days;
* U-shaped intraday volatility and volume, overnight gaps (with rare large gaps), VIX as an OU process
  that spikes on down moves;
* option chains priced with Black-76 on the forward off a VIX-linked skewed smile; OI walls at round
  strikes that build on the far side of the move and unwind when price crosses them.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field

import numpy as np

from sthitaprajna.domain import Candle, Exchange, Instrument, Kind, OptionType, Quote, Source
from sthitaprajna.market import black76
from sthitaprajna.market.calendar import SESSION_MINUTES, MarketCalendar
from sthitaprajna.market.options import ChainRow

DAY_SECONDS = SESSION_MINUTES * 60


@dataclass
class SimSpec:
    symbol: str
    name: str
    price: float
    beta: float
    sector: str
    vol: float           # annualised idiosyncratic vol
    kind: Kind = Kind.EQUITY
    exchange: Exchange = Exchange.NSE
    lot: int = 1
    base_vol_per_min: float = 20_000


INDICES = [
    SimSpec("NIFTY", "Nifty 50", 25_000.0, 1.0, "INDEX", 0.0, Kind.INDEX, Exchange.NSE, 65),
    SimSpec("SENSEX", "BSE Sensex", 82_000.0, 1.0, "INDEX", 0.0, Kind.INDEX, Exchange.BSE, 20),
    SimSpec("BANKNIFTY", "Nifty Bank", 55_000.0, 1.15, "BANK", 0.0, Kind.INDEX, Exchange.NSE, 30),
    SimSpec("INDIAVIX", "India VIX", 13.0, 0.0, "VOL", 0.0, Kind.INDEX, Exchange.NSE, 1),
]

STOCKS = [
    SimSpec("RELIANCE", "Reliance Industries", 1420, 1.00, "ENERGY", 0.20, base_vol_per_min=45_000),
    SimSpec("HDFCBANK", "HDFC Bank", 980, 0.95, "BANK", 0.18, base_vol_per_min=60_000),
    SimSpec("ICICIBANK", "ICICI Bank", 1390, 1.05, "BANK", 0.20, base_vol_per_min=50_000),
    SimSpec("SBIN", "State Bank of India", 820, 1.25, "BANK", 0.24, base_vol_per_min=70_000),
    SimSpec("KOTAKBANK", "Kotak Mahindra Bank", 2050, 0.90, "BANK", 0.20, base_vol_per_min=18_000),
    SimSpec("AXISBANK", "Axis Bank", 1150, 1.20, "BANK", 0.24, base_vol_per_min=35_000),
    SimSpec("INFY", "Infosys", 1480, 0.80, "IT", 0.24, base_vol_per_min=40_000),
    SimSpec("TCS", "Tata Consultancy Services", 3050, 0.70, "IT", 0.20, base_vol_per_min=15_000),
    SimSpec("HCLTECH", "HCL Technologies", 1450, 0.80, "IT", 0.24, base_vol_per_min=18_000),
    SimSpec("WIPRO", "Wipro", 250, 0.85, "IT", 0.26, base_vol_per_min=60_000),
    SimSpec("TECHM", "Tech Mahindra", 1480, 0.95, "IT", 0.28, base_vol_per_min=14_000),
    SimSpec("BHARTIARTL", "Bharti Airtel", 1900, 0.75, "TELECOM", 0.20, base_vol_per_min=20_000),
    SimSpec("LT", "Larsen & Toubro", 3600, 1.10, "INFRA", 0.22, base_vol_per_min=10_000),
    SimSpec("ITC", "ITC", 410, 0.60, "FMCG", 0.18, base_vol_per_min=80_000),
    SimSpec("HINDUNILVR", "Hindustan Unilever", 2500, 0.55, "FMCG", 0.18, base_vol_per_min=8_000),
    SimSpec("MARUTI", "Maruti Suzuki", 12500, 0.90, "AUTO", 0.22, base_vol_per_min=2_500),
    SimSpec("M&M", "Mahindra & Mahindra", 3300, 1.15, "AUTO", 0.26, base_vol_per_min=12_000),
    SimSpec("BAJAJ-AUTO", "Bajaj Auto", 8600, 0.90, "AUTO", 0.24, base_vol_per_min=3_000),
    SimSpec("TATASTEEL", "Tata Steel", 165, 1.40, "METAL", 0.30, base_vol_per_min=150_000),
    SimSpec("JSWSTEEL", "JSW Steel", 1050, 1.30, "METAL", 0.28, base_vol_per_min=15_000),
    SimSpec("HINDALCO", "Hindalco", 740, 1.35, "METAL", 0.30, base_vol_per_min=30_000),
    SimSpec("SUNPHARMA", "Sun Pharma", 1650, 0.60, "PHARMA", 0.22, base_vol_per_min=14_000),
    SimSpec("DRREDDY", "Dr Reddy's", 1280, 0.55, "PHARMA", 0.24, base_vol_per_min=10_000),
    SimSpec("CIPLA", "Cipla", 1520, 0.55, "PHARMA", 0.22, base_vol_per_min=10_000),
    SimSpec("BAJFINANCE", "Bajaj Finance", 950, 1.35, "FIN", 0.30, base_vol_per_min=40_000),
    SimSpec("BAJAJFINSV", "Bajaj Finserv", 2000, 1.20, "FIN", 0.26, base_vol_per_min=8_000),
    SimSpec("ADANIENT", "Adani Enterprises", 2400, 1.60, "INFRA", 0.40, base_vol_per_min=14_000),
    SimSpec("ADANIPORTS", "Adani Ports", 1400, 1.40, "INFRA", 0.32, base_vol_per_min=15_000),
    SimSpec("ULTRACEMCO", "UltraTech Cement", 12000, 0.85, "CEMENT", 0.22, base_vol_per_min=2_000),
    SimSpec("TITAN", "Titan Company", 3500, 0.95, "CONSUMER", 0.24, base_vol_per_min=7_000),
    SimSpec("ASIANPAINT", "Asian Paints", 2450, 0.70, "CONSUMER", 0.22, base_vol_per_min=7_000),
    SimSpec("NTPC", "NTPC", 340, 0.95, "ENERGY", 0.22, base_vol_per_min=60_000),
    SimSpec("POWERGRID", "Power Grid", 290, 0.85, "ENERGY", 0.20, base_vol_per_min=50_000),
    SimSpec("ONGC", "ONGC", 240, 1.10, "ENERGY", 0.26, base_vol_per_min=60_000),
    SimSpec("TRENT", "Trent", 5200, 1.30, "CONSUMER", 0.34, base_vol_per_min=4_000),
    SimSpec("ETERNAL", "Eternal", 320, 1.50, "CONSUMER", 0.40, base_vol_per_min=120_000),
]

STRIKE_STEP = {"NIFTY": 50, "SENSEX": 100, "BANKNIFTY": 100}


def stock_strike_step(price: float) -> float:
    """Synthetic strike spacing for stock options by price band (SIM only)."""
    for limit, step in ((250, 5.0), (500, 10.0), (1000, 20.0), (2500, 50.0), (5000, 100.0)):
        if price < limit:
            return step
    return 250.0


def sim_fno_lot(price: float) -> int:
    """Synthetic stock-option lot: about Rs 7.5 lakh of stock, in multiples of 25 (SIM only, not NSE data)."""
    return max(25, int(round(750_000 / max(price, 1.0) / 25.0)) * 25)
IV_FACTOR = {"NIFTY": 1.0, "SENSEX": 1.0, "BANKNIFTY": 1.15}


def key_for(spec: SimSpec) -> str:
    return f"{spec.exchange.value}:{spec.symbol}"


def strike_str(strike: float) -> str:
    """Canonical strike text: 25000 -> "25000", 247.5 -> "247.5" (stock strikes can be fractional)."""
    return f"{float(strike):.2f}".rstrip("0").rstrip(".")


def option_key(underlying: str, expiry: dt.date, strike: float, ot: OptionType) -> str:
    ex = "BFO" if underlying == "SENSEX" else "NFO"
    return f"{ex}:{underlying}:{expiry.isoformat()}:{strike_str(strike)}:{ot.value}"


def u_shape(minute: float) -> float:
    """Relative intraday volatility by minute of session (open/close heavy)."""
    x = min(max(minute / SESSION_MINUTES, 0.0), 1.0)
    return 0.75 + 0.95 * math.exp(-x * 9) + 0.45 * math.exp(-(1 - x) * 12)


@dataclass
class _State:
    spec: SimSpec
    key: str
    price: float
    prev_close: float
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    day_volume: float = 0.0
    day_drift: float = 0.0           # expected log-move over the day
    cur_min: Candle | None = None
    candles: list[Candle] = field(default_factory=list)
    last_ts: dt.datetime | None = None


class SimMarket:
    """Generates a coherent synthetic market and serves quotes, candles, chains and depth."""

    source = Source.SIM

    def __init__(self, cal: MarketCalendar, seed: int = 7, start: dt.date | None = None,
                 warmup_days: int = 5, risk_free: float = 0.065, max_candles: int = 3000) -> None:
        self.cal = cal
        self.rng = np.random.default_rng(seed)
        self.r = risk_free
        self.max_candles = max_candles
        d = start or dt.date.today()
        d = d if cal.is_trading_day(d) else cal.next_trading_day(d)
        self.day = d
        self.now = cal.open_dt(d)
        self.day_index = 0
        self.regime = "range"
        self.vix_theta = 13.0
        self.states: dict[str, _State] = {}
        self.instruments: dict[str, Instrument] = {}
        for spec in INDICES + STOCKS:
            k = key_for(spec)
            self.states[k] = _State(spec, k, spec.price, spec.price)
            self.instruments[k] = Instrument(
                k, spec.symbol, spec.name, spec.kind, spec.exchange, lot_size=1 if spec.kind is Kind.EQUITY else spec.lot,
                tick_size=0.05, sector=spec.sector, beta=spec.beta,
            )
        self.sector_drift: dict[str, float] = {}
        self._opt_cache: dict[tuple, tuple[float, dict]] = {}
        self._oi: dict[str, dict[float, list[float]]] = {}   # underlying -> strike -> [ce_oi, pe_oi, ce_prev, pe_prev, ce_vol, pe_vol]
        self._oi_expiry: dict[str, dt.date] = {}
        self.day_events: list[str] = []
        self.pending_gap: float = 0.0
        self._warmup(warmup_days)
        self._start_day(first=warmup_days == 0)

    # ------------------------------------------------------------------ helpers
    @property
    def index_keys(self) -> list[str]:
        return [key_for(s) for s in INDICES]

    @property
    def stock_keys(self) -> list[str]:
        return [key_for(s) for s in STOCKS]

    def _vix(self) -> float:
        return self.states["NSE:INDIAVIX"].price

    def _sigma_day(self) -> float:
        return self._vix() / 100.0 / math.sqrt(252)

    def _draw_regime(self) -> None:
        self.regime = self.rng.choice(["trend_up", "trend_down", "range", "volatile"], p=[0.27, 0.22, 0.36, 0.15])
        sd = self._sigma_day()
        base = {"trend_up": 1.1 * sd, "trend_down": -1.1 * sd, "range": 0.0, "volatile": 0.0}[self.regime]
        self.market_drift = base
        # sorted: a set's order varies with hash randomisation and would break deterministic replay (A5.1)
        self.sector_drift = {sec: float(self.rng.normal(0, 0.5 * sd)) for sec in
                             sorted({s.sector for s in STOCKS})}
        for st in self.states.values():
            sp = st.spec
            if sp.kind is Kind.EQUITY:
                catalyst = self.rng.random() < 0.12
                idio = float(self.rng.normal(0, 1)) * (sp.vol / math.sqrt(252)) * (1.8 if catalyst else 0.4)
                st.day_drift = sp.beta * base + self.sector_drift[sp.sector] + idio
            elif sp.symbol in ("NIFTY", "SENSEX"):
                st.day_drift = base
            elif sp.symbol == "BANKNIFTY":
                st.day_drift = sp.beta * base + self.sector_drift["BANK"]

    def _start_day(self, first: bool = False) -> None:
        self._draw_regime()
        sd = self._sigma_day()
        gap_m = float(self.rng.normal(0, 0.35 * sd))
        if self.rng.random() < 0.04:
            gap_m += float(self.rng.choice([-1, 1])) * 2.2 * sd  # rare large gap
        gap_m += self.pending_gap
        self.pending_gap = 0.0
        for st in self.states.values():
            sp = st.spec
            st.prev_close = st.price
            if not first:
                if sp.symbol == "INDIAVIX":
                    g = -3.0 * gap_m
                elif sp.kind is Kind.INDEX:
                    g = sp.beta * gap_m + float(self.rng.normal(0, 0.05 * sd))
                else:
                    g = sp.beta * gap_m + float(self.rng.normal(0, 0.4 * sp.vol / math.sqrt(252)))
                st.price = st.price * math.exp(g)
            st.open = st.high = st.low = st.price
            st.day_volume = 0.0
            st.cur_min = None
        self._roll_oi()
        self.day_events.append(f"{self.day.isoformat()} open: regime={self.regime}")

    # ------------------------------------------------------------------ price engine
    freeze: bool = False   # test hook: prices move only when a test sets them (scripted scenarios)

    def set_price(self, key: str, price: float) -> None:
        """Test/scripted-scenario hook: move one instrument to an exact price."""
        st = self.states[key]
        st.price = price
        st.high, st.low = max(st.high, price), min(st.low, price)
        self._opt_cache.clear()

    def _advance(self, dt_s: float) -> None:
        if self.freeze:
            for st in self.states.values():
                st.high, st.low = max(st.high, st.price), min(st.low, st.price)
                self._update_candle(st, st.spec.base_vol_per_min * dt_s / 60.0)
            return
        minute = self.cal.minutes_into_session(self.now)
        u = u_shape(minute)
        f = dt_s / DAY_SECONDS
        sd = self._sigma_day()
        vol_mult = 1.6 if self.regime == "volatile" else 1.0
        z_m = float(self.rng.normal())
        nifty = self.states["NSE:NIFTY"]
        mr = 0.0
        if self.regime == "range" and nifty.open > 0:
            mr = -6.0 * math.log(nifty.price / nifty.open) * f
        r_m = self.market_drift * f + sd * vol_mult * u * math.sqrt(f) * z_m + mr
        sector_z = {sec: float(self.rng.normal()) for sec in self.sector_drift}
        for st in self.states.values():
            sp = st.spec
            if sp.symbol == "INDIAVIX":
                v = st.price
                dv = 2.0 * (self.vix_theta - v) * f - 4.0 * r_m * v + 0.6 * v * math.sqrt(f) * float(self.rng.normal()) * 0.3
                st.price = max(8.0, v + dv)
            elif sp.symbol in ("NIFTY",):
                st.price *= math.exp(r_m)
            elif sp.symbol == "SENSEX":
                st.price *= math.exp(r_m + 0.12 * sd * u * math.sqrt(f) * float(self.rng.normal()))
            elif sp.symbol == "BANKNIFTY":
                st.price *= math.exp(sp.beta * r_m + self.sector_drift["BANK"] * f
                                     + 0.5 * sd * u * math.sqrt(f) * sector_z["BANK"])
            else:
                idio_sd = sp.vol / math.sqrt(252)
                r = (sp.beta * r_m + (st.day_drift - sp.beta * self.market_drift) * f
                     + 0.5 * sd * u * math.sqrt(f) * sector_z[sp.sector]
                     + idio_sd * u * math.sqrt(f) * float(self.rng.normal()))
                st.price *= math.exp(r)
            st.price = round(st.price / 0.05) * 0.05 if sp.symbol != "INDIAVIX" else round(st.price, 2)
            st.high = max(st.high, st.price)
            st.low = min(st.low, st.price)
            # volume: U-shaped, heavier on big moves and on trend days
            if sp.kind is Kind.EQUITY or sp.symbol in ("NIFTY", "SENSEX", "BANKNIFTY"):
                trendy = 1.0 + 1.8 * min(1.0, abs(st.day_drift) / max(sd, 1e-9) / 2)
                vol = sp.base_vol_per_min * (dt_s / 60.0) * (u ** 1.6) * trendy * float(self.rng.lognormal(0, 0.35))
                if sp.kind is Kind.INDEX:
                    vol *= 20
                st.day_volume += vol
            else:
                vol = 0.0
            self._update_candle(st, vol)
        self._evolve_oi(r_m)

    def _update_candle(self, st: _State, vol: float) -> None:
        # a price observed at the end of a sub-step belongs to the minute it closes (09:16:00 -> 09:15 bar)
        start = (self.now - dt.timedelta(microseconds=1)).replace(second=0, microsecond=0)
        c = st.cur_min
        if c is None or c.start != start:
            if c is not None:
                st.candles.append(c)
                if len(st.candles) > self.max_candles:
                    del st.candles[: len(st.candles) - self.max_candles]
            st.cur_min = Candle(st.key, start, st.price, st.price, st.price, st.price, vol)
        else:
            c.high = max(c.high, st.price)
            c.low = min(c.low, st.price)
            c.close = st.price
            c.volume += vol

    def _finalize_minutes(self) -> None:
        """Move every completed minute bar into history the instant its minute ends (bar-close data)."""
        for st in self.states.values():
            c = st.cur_min
            if c is not None and c.start + dt.timedelta(seconds=60) <= self.now:
                st.candles.append(c)
                st.cur_min = None
                if len(st.candles) > self.max_candles:
                    del st.candles[: len(st.candles) - self.max_candles]

    def _warmup(self, days: int) -> None:
        """Build `days` sessions of 1-minute history before the live day (indicators need a past)."""
        d = self.day
        for _ in range(days):
            d = self.cal.prev_trading_day(d)
        self.day = d
        self.now = self.cal.open_dt(d)
        self._start_day(first=True)
        for i in range(days):
            for _m in range(SESSION_MINUTES):
                for _q in range(4):
                    self.now += dt.timedelta(seconds=15)
                    self._advance(15.0)
                self._finalize_minutes()
            self._close_day()
            self.day = self.cal.next_trading_day(self.day)
            self.now = self.cal.open_dt(self.day)
            if i < days - 1:
                self._start_day()

    def _close_day(self) -> None:
        for st in self.states.values():
            if st.cur_min is not None:
                st.candles.append(st.cur_min)
                st.cur_min = None

    # ------------------------------------------------------------------ public stepping
    def step(self, seconds: float) -> list[str]:
        """Advance sim time. Returns events: 'minute', 'close', 'open'."""
        events: list[str] = []
        remaining = seconds
        close = self.cal.close_dt(self.day)
        while remaining > 1e-9:
            if self.now >= close:
                self._close_day()
                events.append("close")
                self.day = self.cal.next_trading_day(self.day)
                self.now = self.cal.open_dt(self.day)
                self.day_index += 1
                self._start_day()
                events.append("open")
                return events
            sub = min(remaining, 15.0, (close - self.now).total_seconds())
            prev_min = self.now.minute
            self.now += dt.timedelta(seconds=sub)
            self._advance(sub)
            self._finalize_minutes()
            if self.now.minute != prev_min:
                events.append("minute")
            remaining -= sub
        self._opt_cache.clear()
        return events

    def inject_gap(self, pct: float) -> None:
        """Failure injection: an instantaneous index-wide move (tests gap-through-stop fills)."""
        m = math.log(1 + pct)
        for st in self.states.values():
            if st.spec.symbol == "INDIAVIX":
                st.price = max(8.0, st.price * math.exp(-3 * m))
            else:
                st.price = round(st.price * math.exp(st.spec.beta * m if st.spec.kind is Kind.EQUITY else m) / 0.05) * 0.05
            st.high, st.low = max(st.high, st.price), min(st.low, st.price)
        self._opt_cache.clear()

    # ------------------------------------------------------------------ quotes & candles
    def quote(self, key: str) -> Quote | None:
        st = self.states.get(key)
        if st is not None:
            px = st.price
            if st.spec.kind is Kind.EQUITY:
                half = max(0.05, round(px * 0.00012 / 0.05) * 0.05)
                bid, ask = round(px - half, 2), round(px + half, 2)
            else:
                bid = ask = px
            return Quote(key, px, bid, ask, st.day_volume, 0.0, self.now, self.now, Source.SIM,
                         prev_close=st.prev_close, open=st.open, high=st.high, low=st.low)
        if key.startswith(("NFO:", "BFO:")):
            return self._option_quote(key)
        return None

    def candles(self, key: str, limit: int | None = None, include_forming: bool = False) -> list[Candle]:
        st = self.states.get(key)
        if st is None:
            return []
        out = st.candles[-limit:] if limit else list(st.candles)
        if include_forming and st.cur_min is not None:
            out = out + [st.cur_min]
        return out

    def breadth(self) -> dict[str, float]:
        adv = dec = 0
        for k in self.stock_keys:
            st = self.states[k]
            if st.price > st.prev_close:
                adv += 1
            elif st.price < st.prev_close:
                dec += 1
        return {"advancers": adv, "decliners": dec, "ad_ratio": adv / dec if dec else float(adv)}

    def depth(self, key: str, levels: int = 5) -> dict[str, list[dict[str, float]]]:
        q = self.quote(key)
        if q is None:
            return {"bids": [], "asks": []}
        tick = 0.05
        rng = np.random.default_rng(abs(hash((key, self.now.minute, self.now.second // 5))) % (2**32))
        base = 50 if key.startswith(("NFO", "BFO")) else 400
        bids = [{"price": round(q.bid - i * tick, 2), "qty": int(rng.lognormal(math.log(base), 0.6)),
                 "orders": int(rng.integers(1, 30))} for i in range(levels)]
        asks = [{"price": round(q.ask + i * tick, 2), "qty": int(rng.lognormal(math.log(base), 0.6)),
                 "orders": int(rng.integers(1, 30))} for i in range(levels)]
        return {"bids": bids, "asks": asks}

    # ------------------------------------------------------------------ options
    def expiries(self, underlying: str, count: int = 3) -> list[dt.date]:
        if underlying == "BANKNIFTY" or underlying not in STRIKE_STEP:     # stocks: monthly (last Tuesday)
            return self.cal.monthly_expiries(underlying, self.day, count)
        return self.cal.weekly_expiries(underlying, self.day, count)

    def option_underlyings(self) -> list[tuple[str, str]]:
        return [(self.states[k].spec.symbol, k) for k in self.stock_keys]

    def _underlying_state(self, underlying: str) -> _State:
        ex = "BSE" if underlying == "SENSEX" else "NSE"
        return self.states[f"{ex}:{underlying}"]

    def _strike_step(self, underlying: str) -> float:
        return STRIKE_STEP.get(underlying) or stock_strike_step(self._underlying_state(underlying).price)

    def _iv(self, underlying: str, f: float, k: float, t: float) -> float:
        st = self._underlying_state(underlying)
        if st.spec.kind is Kind.EQUITY:       # stock: idiosyncratic + market vol
            atm = math.sqrt(st.spec.vol ** 2 + (st.spec.beta * self._vix() / 100.0) ** 2)
        else:
            atm = self._vix() / 100.0 * IV_FACTOR.get(underlying, 1.0)
        atm *= 1.0 + 0.10 * math.exp(-t * 252 / 2)       # front-week premium
        if t <= 0:
            return atm
        m = math.log(k / f) / max(atm * math.sqrt(t), 1e-6)
        return max(0.05, atm * (1 - 0.07 * m + 0.035 * m * m))

    def _oi_target(self, und: str, k: float, spot: float, minute: float) -> tuple[float, float]:
        """Target open interest for a strike: peaks at round strikes near spot; calls heavier above spot
        (writers sell resistance), puts heavier below; builds ~35% through the session near ATM."""
        step = STRIKE_STEP[und]
        roundness = 2.2 if k % (step * 10) == 0 else (1.3 if k % (step * 2) == 0 else 0.75)
        dist = abs(k - spot) / (step * 6)
        build = 1.0 + 0.35 * min(1.0, max(0.0, minute) / SESSION_MINUTES) * math.exp(-dist)
        base = 900_000 * roundness * math.exp(-dist) * build
        return base * (1.35 if k > spot else 0.45), base * (1.35 if k < spot else 0.45)

    def _roll_oi(self) -> None:
        """New session: yesterday's closing OI becomes the reference for OI change; new expiry -> fresh book."""
        for und in STRIKE_STEP:
            exps = self.expiries(und, 1)
            if not exps:
                continue
            exp = exps[0]
            st = self._underlying_state(und)
            step = STRIKE_STEP[und]
            atm = round(st.price / step) * step
            book = self._oi.get(und) if self._oi_expiry.get(und) == exp else None
            new: dict[float, list[float]] = {}
            for i in range(-20, 21):
                k = float(atm + i * step)
                if book and k in book:
                    ce, pe = book[k][0], book[k][1]
                else:
                    ce, pe = self._oi_target(und, k, st.price, 0.0)
                    jitter = float(self.rng.lognormal(0, 0.2))
                    ce, pe = ce * jitter, pe * jitter
                new[k] = [ce, pe, ce, pe, 0.0, 0.0]
            self._oi[und] = new
            self._oi_expiry[und] = exp

    def _evolve_oi(self, r_m: float) -> None:
        """Mean-revert each strike's OI toward its spot-relative target (half-life ~10 sim-minutes), so
        walls build on the far side of a move and unwind when price crosses them -- without drifting."""
        minute = self.cal.minutes_into_session(self.now)
        for und, book in self._oi.items():
            st = self._underlying_state(und)
            s_ = st.price
            step = STRIKE_STEP[und]
            for k, row in book.items():
                tce, tpe = self._oi_target(und, k, s_, minute)
                row[0] += 0.025 * (tce - row[0]) + row[0] * 0.004 * float(self.rng.normal())
                row[1] += 0.025 * (tpe - row[1]) + row[1] * 0.004 * float(self.rng.normal())
                row[0], row[1] = max(row[0], 1000.0), max(row[1], 1000.0)
                near = math.exp(-abs(k - s_) / (step * 4))
                row[4] += near * 4000 * float(self.rng.lognormal(0, 0.4))
                row[5] += near * 4000 * float(self.rng.lognormal(0, 0.4))

    def option_instrument(self, underlying: str, expiry: dt.date, strike: float, ot: OptionType) -> Instrument:
        key = option_key(underlying, expiry, strike, ot)
        inst = self.instruments.get(key)
        if inst is None:
            st = self._underlying_state(underlying)
            spec = st.spec
            und_key = key_for(spec)
            sym = f"{underlying} {expiry:%d%b}".upper() + f" {strike_str(strike)} {ot.value}"
            stock = spec.kind is Kind.EQUITY
            inst = Instrument(key, sym, sym, Kind.OPTION, spec.exchange,
                              lot_size=sim_fno_lot(st.price) if stock else spec.lot, tick_size=0.05,
                              underlying=und_key, expiry=expiry, strike=float(strike), option_type=ot,
                              sector=spec.sector if stock else "INDEX", beta=spec.beta if stock else 1.0)
            self.instruments[key] = inst
        return inst

    def _price_option(self, underlying: str, expiry: dt.date, strike: float, is_call: bool) -> dict:
        ck = (underlying, expiry, strike, is_call)
        hit = self._opt_cache.get(ck)
        if hit is not None:
            return hit[1]
        st = self._underlying_state(underlying)
        t = self.cal.year_fraction_to(self.now, expiry)
        f = black76.forward(st.price, t, self.r)
        iv = self._iv(underlying, f, strike, t)
        g = black76.greeks(f, strike, max(t, 1e-6), iv, self.r, is_call)
        px = max(0.05, round(g.price / 0.05) * 0.05)
        half = max(0.05, round((0.0025 * px + 0.05) / 0.05) * 0.05)
        out = {"ltp": px, "bid": max(0.05, round(px - half, 2)), "ask": round(px + half, 2), "iv": iv,
               "delta": g.delta, "gamma": g.gamma, "theta": g.theta, "vega": g.vega, "t": t}
        self._opt_cache[ck] = (self.now.timestamp(), out)
        return out

    def _option_quote(self, key: str) -> Quote | None:
        try:
            _ex, und, exp_s, k_s, ot = key.split(":")
        except ValueError:
            return None
        exp = dt.date.fromisoformat(exp_s)
        if self.now > self.cal.close_dt(exp):
            return None
        p = self._price_option(und, exp, float(k_s), ot == "CE")
        oi_row = self._oi.get(und, {}).get(float(k_s))
        oi = (oi_row[0] if ot == "CE" else oi_row[1]) if oi_row else 0.0
        vol = (oi_row[4] if ot == "CE" else oi_row[5]) if oi_row else 0.0
        return Quote(key, p["ltp"], p["bid"], p["ask"], vol, oi, self.now, self.now, Source.SIM)

    promoted: set = frozenset()        # SIM: every stock is already in the strategy universe

    def promote(self, key: str) -> bool:
        return False

    def demote(self, key: str) -> bool:
        return False

    def _daily_baseline(self, st: "_State") -> dict:
        cache = getattr(self, "_baseline_cache", None)
        if cache is None or cache[0] != self.day:
            self._baseline_cache = cache = (self.day, {})
        hit = cache[1].get(st.key)
        if hit is None:
            from sthitaprajna.intelligence.scanner import baseline_from_daily
            days: dict = {}
            for c in st.candles:
                if c.start.date() < self.day:
                    days.setdefault(c.start.date(), []).append(c)
            bars = [Candle(st.key, cs[0].start, cs[0].open, max(x.high for x in cs), min(x.low for x in cs),
                           cs[-1].close, sum(x.volume for x in cs)) for _d, cs in sorted(days.items())]
            hit = cache[1][st.key] = baseline_from_daily(bars) or {}
        return hit

    def screen_inputs(self) -> list:
        from sthitaprajna.intelligence.scanner import ScreenInput
        out = []
        for k in self.stock_keys:
            st = self.states[k]
            today = [c for c in st.candles if c.start.date() == self.day] + ([st.cur_min] if st.cur_min else [])
            v = sum(c.volume for c in today)
            vwap = sum((c.high + c.low + c.close) / 3 * c.volume for c in today) / v if v else st.price
            closes = [c.close for c in today]

            def mom(n: int, closes=closes, px=st.price) -> float | None:
                return px / closes[-n - 1] - 1 if len(closes) > n and closes[-n - 1] > 0 else None
            q = self.quote(k)
            b = self._daily_baseline(st)
            out.append(ScreenInput(
                k, st.spec.symbol, st.spec.name, st.spec.sector, "EQ", True, sim_fno_lot(st.price), st.price,
                st.prev_close, st.open, st.high, st.low, st.day_volume, vwap,
                max((c.high for c in st.candles), default=st.high), min((c.low for c in st.candles), default=st.low),
                round(st.prev_close * 1.2, 2), round(st.prev_close * 0.8, 2), 0.0, 0.0,
                q.bid if q else 0.0, q.ask if q else 0.0, mom(5), mom(15), b.get("avg_vol_20"), b.get("prev_high"),
                b.get("prev_low"), b.get("atr_pct"), "", True))
        return out

    def search(self, q: str, limit: int = 20) -> list:
        q = q.strip().upper()
        hits = [self.states[k] for k in self.index_keys + self.stock_keys
                if self.states[k].spec.symbol.startswith(q) or q in self.states[k].spec.name.upper()] if q else []
        return [{"key": st.key, "symbol": st.spec.symbol, "name": st.spec.name, "ltp": st.price,
                 "prev_close": st.prev_close, "change_pct": (st.price / st.prev_close - 1) * 100 if st.prev_close else None,
                 "fno": st.spec.kind is Kind.EQUITY, "series": "EQ", "active": True} for st in hits[:limit]]

    def view_candles(self, key: str) -> list:
        return self.candles(key, include_forming=True)

    def describe(self) -> dict:
        return {"source": "SIM", "label": "Synthetic market", "session": "OPEN" if self.cal.is_open(self.now) else "CLOSED",
                "day": self.day.isoformat(), "now": self.now.isoformat(), "next_open": None,
                "secondary": "UPSTOX (sim)", "secondary_configured": True, "instruments": len(self.states),
                "backfill_days": 0, "health": {}, "problems": [], "betas_estimated": 0,
                "scanner": {"universe": len(self.stock_keys), "fresh": len(self.stock_keys), "baselines": len(self.stock_keys),
                            "baselines_pending": 0, "promoted": [], "fno": len(self.stock_keys)}}

    def chain(self, underlying: str, expiry: dt.date | None = None, width: int = 15) -> tuple[float, list[ChainRow]]:
        st = self._underlying_state(underlying)
        expiry = expiry or self.expiries(underlying, 1)[0]
        step = self._strike_step(underlying)
        atm = round(st.price / step) * step
        book = self._oi.get(underlying, {}) if self._oi_expiry.get(underlying) == expiry else {}
        rows = []
        for i in range(-width, width + 1):
            k = float(atm + i * step)
            c = self._price_option(underlying, expiry, k, True)
            p = self._price_option(underlying, expiry, k, False)
            oi = book.get(k)
            if oi is None:  # later expiries: thinner synthetic OI
                dist = abs(k - st.price) / (step * 6)
                base = 250_000 * math.exp(-dist)
                oi = [base * (1.3 if k > st.price else 0.5), base * (1.3 if k < st.price else 0.5),
                      base, base, base * 0.1, base * 0.1]
            ce_key = self.option_instrument(underlying, expiry, k, OptionType.CE).key
            pe_key = self.option_instrument(underlying, expiry, k, OptionType.PE).key
            rows.append(ChainRow(k, ce_key, pe_key, c["ltp"], p["ltp"], c["bid"], c["ask"], p["bid"], p["ask"],
                                 oi[0], oi[1], oi[0] - oi[2], oi[1] - oi[3], oi[4], oi[5], c["iv"], p["iv"],
                                 c["delta"], p["delta"], c["gamma"], p["gamma"], c["theta"], p["theta"],
                                 c["vega"], p["vega"]))
        return st.price, rows
