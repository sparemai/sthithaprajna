"""PAPER mode on live Dhan data: a market object with the same interface the engine uses for SIM.

* Real IST wall clock and the exchange calendar: 'open' at 09:15, 'minute' as each minute bar completes,
  'close' at 15:30. Weekends and holidays produce no events; the terminal shows the last close.
* Quotes (with 5-level depth) are polled in a background thread within Dhan's rate limits, so the engine
  thread never waits on the network. Minute bars are built from those quotes (completed bars only); on
  start the last sessions of 1-minute history are backfilled from Dhan.
* Option chains are fetched on demand (at most one request per 3 s) and cached; any contract the engine
  asks about is added to the quote poll.
* Upstox is optional. Without it the secondary feed is "not configured" and A5.5's single-feed row applies
  (Dhan healthy, Upstox down: entries allowed, data confidence capped at 0.8).

Fills stay simulated (paper broker at bid/ask, next tick). Nothing in this module places an order.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from sthitaprajna.data.adapters.dhan import parse_dhan_option_chain
from sthitaprajna.data.dhan_live import FNO_SEGMENT, DhanClient, DhanError, FeedRecord, Universe, load_universe
from sthitaprajna.data.sim import INDICES, STOCKS, SimSpec, key_for, option_key, strike_str
from sthitaprajna.domain import IST, Candle, Exchange, Instrument, Kind, OptionType, Quote, Source
from sthitaprajna.intelligence.scanner import ScreenInput, baseline_from_daily
from sthitaprajna.market import indicators as ind
from sthitaprajna.market.options import ChainRow

CHAIN_TTL_S = 15.0
WANT_TTL_S = 120.0
SLOW_POLL_S = 30.0
FAST_FROM = dt.time(9, 0)
FAST_UNTIL = dt.time(15, 40)
MAX_INBOX = 30
SCREEN_CHUNK_FAST = 400        # universe stocks added to each in-session quote request (bandwidth-friendly)
BASELINE_LOOKBACK_DAYS = 60    # calendar days of daily candles for the 20-day volume / ATR baselines
SECTORS = {s.symbol: s.sector for s in STOCKS}


@dataclass
class _ScreenState:
    """Scanner view of one NSE stock: quote fields only (no bars) unless it is promoted into `states`."""
    key: str
    symbol: str
    sid: str
    name: str
    series: str
    fno_lot: int
    tick: float
    surveillance: str = ""
    ltp: float = 0.0
    prev_close: float = 0.0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    volume: float = 0.0
    vwap: float = 0.0
    w52_high: float = 0.0
    w52_low: float = 0.0
    upper_circuit: float = 0.0
    lower_circuit: float = 0.0
    buy_qty: float = 0.0
    sell_qty: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    updated: dt.datetime | None = None
    minutes: deque = field(default_factory=lambda: deque(maxlen=20))   # (minute start, last price)

    def momentum(self, now: dt.datetime, minutes: int) -> float | None:
        cut = now.replace(second=0, microsecond=0) - dt.timedelta(minutes=minutes)
        past = [px for m, px in self.minutes if m <= cut]
        return self.ltp / past[-1] - 1 if past and past[-1] > 0 and self.ltp > 0 else None


@dataclass
class _LiveState:
    spec: Any                       # SimSpec: symbol, name, sector, kind, exchange, lot, beta
    key: str
    security_id: str
    segment: str
    price: float = 0.0
    prev_close: float = 0.0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    day_volume: float = 0.0
    vol_seen: bool = False
    cur_min: Candle | None = None
    candles: list[Candle] = field(default_factory=list)
    last_ts: dt.datetime | None = None
    quote: Quote | None = None
    depth: dict[str, list[dict[str, float]]] = field(default_factory=lambda: {"bids": [], "asks": []})
    beta_source: str = "static"


def estimate_beta(stock: list[Candle], index: list[Candle], min_points: int = 60) -> float | None:
    """Beta of 5-minute log returns against the index, same-day returns only (no overnight gaps)."""
    s5 = {c.t: c for c in ind.resample(stock, 5)}
    i5 = {c.t: c for c in ind.resample(index, 5)}
    ts = sorted(set(s5) & set(i5))
    xs, ys = [], []
    for a, b in zip(ts, ts[1:]):
        if b - a != 300 or s5[a].close <= 0 or i5[a].close <= 0:
            continue
        ys.append(math.log(s5[b].close / s5[a].close))
        xs.append(math.log(i5[b].close / i5[a].close))
    if len(xs) < min_points:
        return None
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    var = sum((x - mx) ** 2 for x in xs)
    if var <= 0:
        return None
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return round(min(max(cov / var, 0.2), 2.5), 2)


def normalise_chain(rows: list[ChainRow]) -> list[ChainRow]:
    """A call's delta is >= 0 and a put's <= 0 whatever sign convention the source uses."""
    for r in rows:
        r.ce_delta, r.pe_delta = abs(r.ce_delta), -abs(r.pe_delta)
    return rows


class LiveMarket:
    source = Source.DHAN
    regime = "live"                 # no synthetic day regime: the engine's own regime engine reads real data
    freeze = False

    def __init__(self, cal, client: DhanClient, rules: Any, *, specs: list[Any] | None = None,
                 clock: Callable[[], dt.datetime] | None = None, cache_dir: Path = Path("var/cache"),
                 max_candles: int = 3000, mono: Callable[[], float] = time.monotonic) -> None:
        self.cal, self.client, self.rules = cal, client, rules
        self.clock = clock or (lambda: dt.datetime.now(IST))
        self.mono = mono
        self.specs = list(specs or (INDICES + STOCKS))
        self.cache_dir = cache_dir
        self.max_candles = max_candles
        self.states: dict[str, _LiveState] = {}
        self.instruments: dict[str, Instrument] = {}
        self.universe: Universe | None = None
        self.lots: dict[str, int] = {}
        self.secondary_configured = False
        self.backfill_days = 0
        self._lock = threading.Lock()
        self._inbox: list[tuple[dt.datetime, dt.datetime, dict[tuple[str, str], FeedRecord]]] = []
        self._by_sid: dict[tuple[str, str], str] = {}
        self._opt_ids: dict[str, tuple[str, str]] = {}
        self._opt_by_sid: dict[tuple[str, str], str] = {}
        self._chain_failed: dict[tuple[str, dt.date], float] = {}
        self._opt_quotes: dict[str, tuple[Quote, dict[str, list[dict[str, float]]]]] = {}
        self._wanted_opts: dict[str, float] = {}
        self._chains: dict[tuple[str, dt.date], tuple[float, dt.datetime, float, list[ChainRow]]] = {}
        self._chain_wanted: dict[tuple[str, dt.date], float] = {}
        self._chain_ready: dict[tuple[str, dt.date], threading.Event] = {}
        self._expiries: dict[str, tuple[dt.date, list[dt.date]]] = {}
        self._updated: dict[str, tuple[int, Quote]] = {}
        self._seq = 0
        self._notes: deque[tuple[str, str]] = deque(maxlen=200)
        self._error_logged: dict[str, float] = {}
        self.health: dict[str, Any] = {"last_ok": None, "last_error": None, "error_kind": None, "polls": 0,
                                       "errors": 0}
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()               # set when a chain / contract is wanted: poll now, not in 30 s
        self._errors = 0
        self.screen: dict[str, _ScreenState] = {}          # every NSE stock (scanner universe)
        self._screen_cursor = 0
        self.promoted: set[str] = set()
        self.baselines: dict[str, dict[str, Any]] = {}
        self._baseline_queue: deque[str] = deque()
        self._baselines_dirty = 0
        self._history_jobs: deque[str] = deque()
        self._history_results: dict[str, list[Candle]] = {}
        self._view_cache: dict[str, tuple[float, list[Candle]]] = {}
        self._last_quote_poll = -1e9
        self._stock_exp_wanted: set[str] = set()
        self.day: dt.date = self.clock().date()
        self.session = "CLOSED"
        self._minute_mark: dt.datetime | None = None

    # ================================================================== bootstrap
    def bootstrap(self, progress: Callable[[str], None] = print, backfill_days: int = 5, start_poller: bool = True,
                  refresh_master: bool = False) -> "LiveMarket":
        now = self.clock()
        rule_lots = {"NIFTY": self.rules.get("lot_size_nifty", 65), "BANKNIFTY": self.rules.get("lot_size_banknifty", 30),
                     "SENSEX": self.rules.get("lot_size_sensex", 20)}
        self.universe = load_universe(self.client, self.specs, rule_lots, self.cache_dir, now.date(), progress,
                                      refresh=refresh_master)
        self.lots = dict(self.universe.lots)
        for p in self.universe.problems:
            self._note("system", f"Dhan universe: {p}")
        for spec in self.specs:
            k = key_for(spec)
            res = self.universe.items.get(k)
            if res is None:
                continue
            st = _LiveState(spec, k, res.security_id, res.segment)
            self.states[k] = st
            self._by_sid[(res.segment, str(res.security_id))] = k
            self.instruments[k] = Instrument(
                k, spec.symbol, res.name if spec.kind is Kind.EQUITY else spec.name, spec.kind, spec.exchange,
                lot_size=1 if spec.kind is Kind.EQUITY else self.lots.get(spec.symbol, spec.lot), tick_size=res.tick,
                sector=spec.sector, beta=spec.beta, dhan_security_id=str(res.security_id))
        if "NSE:NIFTY" not in self.states:
            raise DhanError("OTHER", "NIFTY could not be resolved from Dhan's instrument master")
        self._build_screen()
        self._init_session(now)
        progress(f"  quotes: polling {len(self.states)} instruments")
        self.poll_once()
        self._apply_inbox()
        missing = [k for k, st in self.states.items() if st.price <= 0]
        if missing:
            self._note("system", f"no Dhan quote yet for {len(missing)} instruments: {', '.join(missing[:6])}")
        self._backfill(backfill_days, progress)
        self._estimate_betas()
        for und in FNO_SEGMENT:
            try:
                self._fetch_expiries(und)
            except DhanError as exc:
                if exc.fatal:
                    raise
                self._note("system", f"{und} expiries unavailable ({exc}); using the exchange calendar")
        self._load_baselines()
        if self.screen:
            progress(f"  scanner: {len(self.screen):,} NSE stocks, {len(self.baselines):,} with 20-day baselines "
                     f"({len(self._baseline_queue):,} more load in the background)")
        if start_poller:
            self.start()
        return self

    # ================================================================== scanner universe
    def _build_screen(self) -> None:
        for sym, e in (self.universe.equities if self.universe else {}).items():
            key = f"NSE:{sym}"
            sc = _ScreenState(key, sym, str(e["sid"]), e.get("name") or sym, e.get("series") or "EQ",
                              int(e.get("fno_lot") or 0), float(e.get("tick") or 0.05), e.get("surveillance") or "")
            self.screen[key] = sc
            self._by_sid.setdefault(("NSE_EQ", sc.sid), key)

    def _last_completed_session(self) -> dt.date:
        now = self.clock()
        d = now.date()
        if self.cal.is_trading_day(d) and now >= self.cal.close_dt(d):
            return d
        return self.cal.prev_trading_day(d) if self.cal.is_trading_day(d) else self.cal.trading_day_on_or_before(d)

    def _load_baselines(self) -> None:
        path = self.cache_dir / "baselines.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        except (ValueError, OSError):
            data = {}
        want = self._last_completed_session().isoformat()
        items = data.get("items") or {}
        self.baselines = {k: v for k, v in items.items() if v.get("as_of") == want and k in self.screen}
        todo = [k for k, sc in self.screen.items() if k not in self.baselines and sc.series in ("EQ", "")]
        todo.sort(key=lambda k: (0 if k in self.states else 1 if self.screen[k].fno_lot else 2, k))
        self._baseline_queue = deque(todo)

    def save_baselines(self) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path = self.cache_dir / "baselines.json"
        tmp = path.with_suffix(".part")
        tmp.write_text(json.dumps({"items": self.baselines}), encoding="utf-8")
        tmp.replace(path)
        self._baselines_dirty = 0

    def _fetch_baseline(self, key: str) -> None:
        sc = self.screen[key]
        today = self.clock().date()
        bars = self.client.historical(key, sc.sid, "NSE_EQ", "EQUITY",
                                      today - dt.timedelta(days=BASELINE_LOOKBACK_DAYS), today)
        last = self._last_completed_session()
        done = [b for b in sorted(bars, key=lambda c: c.t) if b.start.date() <= last]
        base = baseline_from_daily(done)
        if base is not None:
            base["as_of"] = last.isoformat()      # the session it describes (a holiday gap still counts as fresh)
            self.baselines[key] = base
            self._baselines_dirty += 1

    def roll_baselines(self) -> None:
        """At the close: fold today's session into every baseline from our own quotes (no history refetch)."""
        for key, sc in self.screen.items():
            b = self.baselines.get(key)
            if b is None or sc.ltp <= 0 or sc.high <= 0 or sc.updated is None or sc.updated.date() != self.day:
                continue
            pc = b.get("prev_close") or sc.prev_close or sc.ltp
            tr = max(sc.high - sc.low, abs(sc.high - pc), abs(sc.low - pc))
            atr = (b.get("atr_pct", 0.0) * pc * 13 + tr) / 14
            avg = b.get("avg_vol_20") or sc.volume
            self.baselines[key] = {**b, "avg_vol_20": (avg * 19 + sc.volume) / 20, "prev_high": sc.high,
                                   "prev_low": sc.low, "prev_close": sc.ltp, "atr_pct": atr / sc.ltp,
                                   "as_of": self.day.isoformat()}
        self.save_baselines()

    def promote(self, key: str) -> bool:
        """Move a booming stock into the strategy universe: live bars from now, 1-minute history fetched."""
        sc = self.screen.get(key)
        if sc is None or key in self.states:
            return False
        spec = SimSpec(sc.symbol, sc.name, sc.ltp or 1.0, 1.0, SECTORS.get(sc.symbol, "OTHER"), 0.3)
        st = _LiveState(spec, key, sc.sid, "NSE_EQ", price=sc.ltp, prev_close=sc.prev_close, open=sc.open,
                        high=sc.high, low=sc.low, day_volume=sc.volume, vol_seen=sc.volume > 0)
        self.instruments[key] = Instrument(key, sc.symbol, sc.name, Kind.EQUITY, Exchange.NSE, lot_size=1,
                                           tick_size=sc.tick, sector=spec.sector, beta=1.0, dhan_security_id=sc.sid)
        self.states[key] = st
        self.promoted.add(key)
        with self._lock:
            self._history_jobs.append(key)
        self._wake.set()
        return True

    def demote(self, key: str) -> bool:
        if key not in self.promoted:
            return False
        self.promoted.discard(key)
        self.states.pop(key, None)
        return True

    def screen_inputs(self) -> list[ScreenInput]:
        now = self.clock()
        out = []
        for key, sc in list(self.screen.items()):
            if sc.ltp <= 0:
                continue
            b = self.baselines.get(key) or {}
            out.append(ScreenInput(
                key, sc.symbol, sc.name, SECTORS.get(sc.symbol, ""), sc.series, sc.fno_lot > 0, sc.fno_lot or 1,
                sc.ltp, sc.prev_close, sc.open, sc.high, sc.low, sc.volume, sc.vwap, sc.w52_high, sc.w52_low,
                sc.upper_circuit, sc.lower_circuit, sc.buy_qty, sc.sell_qty, sc.bid, sc.ask,
                sc.momentum(now, 5), sc.momentum(now, 15), b.get("avg_vol_20"), b.get("prev_high"),
                b.get("prev_low"), b.get("atr_pct"), sc.surveillance, key in self.states))
        return out

    def search(self, q: str, limit: int = 20) -> list[dict[str, Any]]:
        q = q.strip().upper()
        if not q:
            return []
        hits = [sc for sc in self.screen.values() if sc.symbol.startswith(q) or q in sc.name.upper()]
        hits.sort(key=lambda sc: (not sc.symbol.startswith(q), -(sc.ltp * sc.volume), sc.symbol))
        return [{"key": sc.key, "symbol": sc.symbol, "name": sc.name, "ltp": sc.ltp, "prev_close": sc.prev_close,
                 "change_pct": (sc.ltp / sc.prev_close - 1) * 100 if sc.ltp > 0 and sc.prev_close > 0 else None,
                 "fno": sc.fno_lot > 0, "series": sc.series, "active": sc.key in self.states} for sc in hits[:limit]]

    def view_candles(self, key: str, ttl_s: float = 60.0) -> list[Candle]:
        """Bars for any universe stock (API threads only): live bars if active, else fetched history."""
        if key in self.states:
            return self.candles(key, include_forming=True)
        sc = self.screen.get(key)
        if sc is None:
            return []
        hit = self._view_cache.get(key)
        if hit and self.mono() - hit[0] < ttl_s:
            return hit[1]
        last = self.cal.trading_day_on_or_before(self.clock().date())
        try:
            bars = self.client.intraday(key, sc.sid, "NSE_EQ", "EQUITY", self.cal.prev_trading_day(last),
                                        self.clock().date() + dt.timedelta(days=1))
        except DhanError as exc:
            self._record_error(exc)
            return []
        bars = [b for b in sorted(bars, key=lambda c: c.t) if self.cal.open_dt(b.start.date()) <= b.start
                < self.cal.close_dt(b.start.date())]
        self._view_cache[key] = (self.mono(), bars)
        return bars

    def service_data_jobs(self, budget_s: float) -> int:
        """History for promoted stocks first, then scanner baselines, while the quote slot allows."""
        done = 0
        end = self.mono() + budget_s
        while self.mono() < end and not self._stop.is_set():
            with self._lock:
                key = self._history_jobs.popleft() if self._history_jobs else None
            if key is not None:
                sc = self.screen.get(key)
                if sc is not None:
                    last = self.cal.trading_day_on_or_before(self.clock().date())
                    start = last
                    for _ in range(max(1, self.backfill_days or 5) - 1):
                        start = self.cal.prev_trading_day(start)
                    bars = self.client.intraday(key, sc.sid, "NSE_EQ", "EQUITY", start,
                                                self.clock().date() + dt.timedelta(days=1))
                    with self._lock:
                        self._history_results[key] = bars
                done += 1
                continue
            if not self._baseline_queue:
                break
            key = self._baseline_queue.popleft()
            if key in self.screen and key not in self.baselines:
                self._fetch_baseline(key)
                done += 1
        if self._baselines_dirty >= 50 or (self._baselines_dirty and not self._baseline_queue):
            self.save_baselines()
        return done

    def _init_session(self, now: dt.datetime) -> None:
        d = now.date()
        if self.cal.is_trading_day(d):
            self.day = d
            self.session = ("PRE_OPEN" if now < self.cal.open_dt(d) else
                            "OPEN" if now < self.cal.close_dt(d) else "CLOSED")
        else:
            self.day, self.session = self.cal.next_trading_day(d), "PRE_OPEN"
        self._minute_mark = now.replace(second=0, microsecond=0)
        if self.session == "OPEN":
            for st in self.states.values():
                st.vol_seen = False

    def _backfill(self, days: int, progress: Callable[[str], None]) -> None:
        if days <= 0:
            return
        now = self.clock()
        last = self.cal.trading_day_on_or_before(now.date())
        start = last
        for _ in range(days - 1):
            start = self.cal.prev_trading_day(start)
        progress(f"  backfilling 1-minute history {start.isoformat()} .. {last.isoformat()} for {len(self.states)} instruments")
        failures = 0
        for i, st in enumerate(self.states.values(), 1):
            res = self.universe.items[st.key]
            try:
                bars = self.client.intraday(st.key, res.security_id, res.segment, res.instrument, start,
                                            now.date() + dt.timedelta(days=1))
            except DhanError as exc:
                if exc.fatal:
                    raise
                failures += 1
                self._note("system", f"history for {st.key} unavailable: {exc}")
                continue
            keep = []
            for b in sorted(bars, key=lambda c: c.t):
                d = b.start.date()
                if not self.cal.is_trading_day(d):
                    continue
                if not (self.cal.open_dt(d) <= b.start < self.cal.close_dt(d)):
                    continue
                if b.start + dt.timedelta(seconds=60) > now:
                    continue                      # the forming bar: strategies see completed bars only
                if keep and keep[-1].t == b.t:
                    keep[-1] = b
                else:
                    keep.append(b)
            st.candles = keep[-self.max_candles:]
            if i % 10 == 0 or i == len(self.states):
                progress(f"  history {i}/{len(self.states)}")
        self.backfill_days = days
        if failures:
            self._note("system", f"history backfill: {failures} instruments failed; their charts start from live bars")

    def _estimate_betas(self) -> None:
        nifty = self.states.get("NSE:NIFTY")
        if nifty is None:
            return
        for st in self.states.values():
            if st.spec.kind is not Kind.EQUITY:
                continue
            b = estimate_beta(st.candles, nifty.candles)
            if b is not None:
                st.spec = dataclasses.replace(st.spec, beta=b)
                st.beta_source = "estimated"
                self.instruments[st.key] = dataclasses.replace(self.instruments[st.key], beta=b)

    # ================================================================== background poller
    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="dhan-poller", daemon=True)
            self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self.client.close()

    def _fast(self, now: dt.datetime) -> bool:
        return self.cal.is_trading_day(now.date()) and FAST_FROM <= now.time() < FAST_UNTIL

    def _run(self) -> None:
        while not self._stop.is_set():
            wait = self.run_once()
            self._wake.wait(wait)
            self._wake.clear()

    def run_once(self) -> float:
        """One poller iteration; returns how long to wait before the next. Never raises."""
        now = self.clock()
        fast = self._fast(now)
        wait = 0.05 if fast else 0.5
        if fast or self.mono() - self._last_quote_poll >= SLOW_POLL_S:
            try:
                self.poll_once()
                self._last_quote_poll = self.mono()
                self._errors = 0
            except DhanError as exc:
                self._errors += 1
                self._record_error(exc)
                self._last_quote_poll = self.mono()
                wait = 60.0 if exc.fatal else min(30.0, 2.0 ** min(self._errors, 5))
                if exc.fatal:
                    return wait
            except Exception as exc:   # the poller must never die silently
                self._errors += 1
                self._record_error(DhanError("OTHER", f"poller: {exc!r}"))
                wait = 10.0
        budget = max(0.0, self.client.limits["quote"].remaining() - 0.35) if fast else 4.0
        for job in (self.service_chains, self.service_expiries, lambda: self.service_data_jobs(budget)):
            try:
                job()                                  # isolated: never stall the quotes
            except DhanError as exc:
                self._record_error(exc)
            except Exception as exc:
                self._record_error(DhanError("OTHER", f"data job: {exc!r}"))
        if not fast and not (self._baseline_queue or self._history_jobs):
            wait = max(wait, SLOW_POLL_S - (self.mono() - self._last_quote_poll))
        return wait

    def _subscription(self) -> dict[str, list[int]]:
        """Fast lane (strategy universe + contracts in play) every poll, plus the next chunk of the scanner universe."""
        from sthitaprajna.data.dhan_live import QUOTE_MAX_INSTRUMENTS
        segs: dict[str, list[int]] = {}
        for st in list(self.states.values()):
            segs.setdefault(st.segment, []).append(int(st.security_id))
        cutoff = self.mono() - WANT_TTL_S
        with self._lock:
            wanted = [k for k, t in self._wanted_opts.items() if t >= cutoff]
            ids = [self._opt_ids[k] for k in wanted if k in self._opt_ids]
        for seg, sid in ids:
            segs.setdefault(seg, []).append(int(sid))
        n = sum(len(v) for v in segs.values())
        room = QUOTE_MAX_INSTRUMENTS - n - 10
        if self._fast(self.clock()):
            room = min(room, SCREEN_CHUNK_FAST)
        rest = [sc for k, sc in list(self.screen.items()) if k not in self.states]
        if rest and room > 0:
            start = self._screen_cursor % len(rest)
            chunk = (rest[start:] + rest[:start])[:room]
            self._screen_cursor = (start + len(chunk)) % len(rest)
            segs.setdefault("NSE_EQ", []).extend(int(sc.sid) for sc in chunk)
        return {s: sorted(set(v)) for s, v in segs.items()}

    def poll_once(self) -> None:
        """One quote request for every subscribed instrument (the quote limiter spaces calls >= ~1 s)."""
        segs = self._subscription()
        lim = self.client.limits["quote"]
        if lim.remaining() > 0:                            # wait here so t0 is the real request time
            lim.sleep(lim.remaining())
        t0 = self.clock()
        recs = self.client.quotes(segs)
        t1 = self.clock()
        with self._lock:
            self._inbox.append((t0, t1, recs))
            del self._inbox[:-MAX_INBOX]
        self.health.update(last_ok=t1.isoformat(), polls=self.health["polls"] + 1, error_kind=None)
        while self.client.notes:
            self._note("system", self.client.notes.pop(0))

    def service_chains(self) -> None:
        """Refresh at most one wanted option chain per call, only when the chain limiter is free."""
        if self.client.limits["chain"].remaining() > 0:
            return
        now_m = self.mono()
        with self._lock:
            due = []
            for k, asked in self._chain_wanted.items():
                if now_m - asked > WANT_TTL_S or now_m - self._chain_failed.get(k, -1e9) < 30.0:
                    continue
                hit = self._chains.get(k)
                if hit is None or now_m - hit[0] >= CHAIN_TTL_S:
                    due.append((hit[0] if hit else 0.0, k))
        if not due:
            return
        _, (und, expiry) = min(due)
        try:
            self.fetch_chain(und, expiry)
        except DhanError:
            self._chain_failed[(und, expiry)] = self.mono()
            raise

    def _und_ref(self, und: str) -> tuple[str, str, str, str] | None:
        """(underlying security id, its segment, the options' segment, key prefix) for an index or F&O stock."""
        if und in FNO_SEGMENT:
            st = self.states.get(self._index_key(und))
            return (st.security_id, "IDX_I", FNO_SEGMENT[und], "BFO" if und == "SENSEX" else "NFO") if st else None
        sc = self.screen.get(f"NSE:{und}")          # verified path (Kubera): equity sid with UnderlyingSeg NSE_EQ
        return (sc.sid, "NSE_EQ", "NSE_FNO", "NFO") if sc is not None and sc.fno_lot else None

    def option_underlyings(self) -> list[tuple[str, str]]:
        """F&O stocks currently in the strategy universe."""
        return [(k.split(":", 1)[1], k) for k in self.stock_keys if (sc := self.screen.get(k)) is not None and sc.fno_lot]

    def fetch_chain(self, und: str, expiry: dt.date) -> None:
        ref = self._und_ref(und)
        if ref is None:
            return
        sid, und_seg, seg, prefix = ref
        body = self.client.option_chain(sid, und_seg, expiry)
        spot, rows = parse_dhan_option_chain(body, und, expiry, prefix)
        rows = normalise_chain(rows)
        ids: dict[str, tuple[str, str]] = {}
        for strike_s, legs in (((body or {}).get("data") or {}).get("oc") or {}).items():
            k = float(strike_s)
            for leg, ot in (("ce", OptionType.CE), ("pe", OptionType.PE)):
                sid = ((legs or {}).get(leg) or {}).get("security_id")
                if sid:
                    ids[option_key(und, expiry, k, ot)] = (seg, str(sid))
        ts = self.clock()
        with self._lock:
            self._chains[(und, expiry)] = (self.mono(), ts, spot, rows)
            self._opt_ids.update(ids)
            self._opt_by_sid.update({v: k for k, v in ids.items()})
            ev = self._chain_ready.setdefault((und, expiry), threading.Event())
        for key, (_, sid) in ids.items():
            inst = self.instruments.get(key)
            if inst is not None and not inst.dhan_security_id:
                self.instruments[key] = dataclasses.replace(inst, dhan_security_id=sid)
        ev.set()

    def service_expiries(self) -> None:
        today = self.clock().date()
        with self._lock:
            stock_wants = sorted(self._stock_exp_wanted)
        for und in list(FNO_SEGMENT) + stock_wants:
            got = self._expiries.get(und)
            if got is None or got[0] != today:
                try:
                    self._fetch_expiries(und)
                except DhanError:
                    self._expiries[und] = (today, [])  # calendar fallback for today; retry tomorrow
                    raise
                finally:
                    with self._lock:
                        self._stock_exp_wanted.discard(und)
                return                                 # one data call per loop

    def _fetch_expiries(self, und: str) -> None:
        ref = self._und_ref(und)
        if ref is None:
            return
        exps = self.client.expiries(ref[0], ref[1])
        self._expiries[und] = (self.clock().date(), exps)

    def _record_error(self, exc: DhanError) -> None:
        self.health.update(last_error=str(exc), error_kind=exc.kind, errors=self.health["errors"] + 1)
        now_m = self.mono()
        if now_m - self._error_logged.get(exc.kind, -1e9) >= 300:
            self._error_logged[exc.kind] = now_m
            self._note("error", f"Dhan data: {exc.explain()}")

    def _note(self, kind: str, msg: str) -> None:
        self._notes.append((kind, msg))

    def drain_notes(self) -> list[tuple[str, str]]:
        out = []
        while self._notes:
            out.append(self._notes.popleft())
        return out

    # ================================================================== engine-thread stepping
    @property
    def now(self) -> dt.datetime:
        return self.clock()

    def step(self, seconds: float = 0.0) -> list[str]:
        """Apply polled quotes and follow the real session. Returns 'open' / 'minute' / 'close' events."""
        now = self.clock()
        events: list[str] = []
        if self.session == "CLOSED" and now.date() > self.day:
            nxt = now.date() if self.cal.is_trading_day(now.date()) else self.cal.next_trading_day(now.date())
            self.day, self.session = nxt, "PRE_OPEN"
        if self.session == "PRE_OPEN" and now >= self.cal.open_dt(self.day):
            if now < self.cal.close_dt(self.day):
                self.session = "OPEN"
                self._new_day()
                self._minute_mark = now.replace(second=0, microsecond=0)
                events.append("open")
            else:                                     # asleep through the whole session
                self.session = "CLOSED"
        self._apply_inbox()                           # after the open, so the first 09:15 print starts the bar
        self._merge_history()
        if self.session == "OPEN":
            self._finalize_minutes(now)
            m = now.replace(second=0, microsecond=0)
            if self._minute_mark is not None and m > self._minute_mark:
                events.append("minute")
            self._minute_mark = m
            if now >= self.cal.close_dt(self.day):
                for st in self.states.values():
                    if st.cur_min is not None:
                        self._push(st, st.cur_min)
                        st.cur_min = None
                self.session = "CLOSED"
                events.append("close")
                try:
                    self.roll_baselines()
                except OSError as exc:
                    self._note("error", f"could not save scanner baselines: {exc}")
        return events

    def _merge_history(self) -> None:
        with self._lock:
            if not self._history_results:
                return
            results, self._history_results = self._history_results, {}
        now = self.clock()
        nifty = self.states.get("NSE:NIFTY")
        for key, bars in results.items():
            st = self.states.get(key)
            if st is None:
                continue
            keep = [b for b in sorted(bars, key=lambda c: c.t)
                    if self.cal.is_trading_day(b.start.date())
                    and self.cal.open_dt(b.start.date()) <= b.start < self.cal.close_dt(b.start.date())
                    and b.start + dt.timedelta(seconds=60) <= now]
            first_live = st.candles[0].t if st.candles else None
            hist = [b for b in keep if first_live is None or b.t < first_live]
            st.candles = (hist + st.candles)[-self.max_candles:]
            if nifty is not None and st.spec.kind is Kind.EQUITY:
                beta = estimate_beta(st.candles, nifty.candles)
                if beta is not None:
                    st.spec = dataclasses.replace(st.spec, beta=beta)
                    st.beta_source = "estimated"
                    self.instruments[key] = dataclasses.replace(self.instruments[key], beta=beta)

    def _new_day(self) -> None:
        for st in self.states.values():
            if st.price > 0:
                st.prev_close = st.price
            st.open = st.high = st.low = 0.0
            st.day_volume = 0.0
            st.vol_seen = True
            st.cur_min = None

    def _apply_inbox(self) -> None:
        with self._lock:
            batch, self._inbox = self._inbox, []
        for t0, t1, recs in batch:
            self._seq += 1
            for (seg, sid), rec in recs.items():
                key = self._by_sid.get((seg, sid)) or self._opt_by_sid.get((seg, sid))
                if key is None:
                    continue
                if rec.ltp <= 0:
                    continue
                sc = self.screen.get(key)
                if sc is not None:
                    self._apply_screen(sc, rec, t0)
                st = self.states.get(key)
                if st is None and sc is not None:
                    continue                          # scanner-only stock: no bars, no feed arbitration
                q = Quote(key, rec.ltp, rec.bid, rec.ask, rec.volume, rec.oi, t0, t1, Source.DHAN,
                          rec.prev_close, rec.open, rec.high, rec.low)
                if st is not None:
                    self._apply_state(st, q, rec)
                else:
                    self._opt_quotes[key] = (q, rec.depth)
                self._updated[key] = (self._seq, q)

    @staticmethod
    def _apply_screen(sc: _ScreenState, rec: FeedRecord, ts: dt.datetime) -> None:
        sc.ltp = rec.ltp
        for f in ("prev_close", "open", "high", "low", "vwap", "w52_high", "w52_low", "upper_circuit",
                  "lower_circuit"):
            v = getattr(rec, f)
            if v > 0:
                setattr(sc, f, v)
        sc.volume, sc.buy_qty, sc.sell_qty, sc.bid, sc.ask = rec.volume, rec.buy_qty, rec.sell_qty, rec.bid, rec.ask
        sc.updated = ts
        m = ts.replace(second=0, microsecond=0)
        if sc.minutes and sc.minutes[-1][0] == m:
            sc.minutes[-1] = (m, rec.ltp)
        else:
            sc.minutes.append((m, rec.ltp))

    def _apply_state(self, st: _LiveState, q: Quote, rec: FeedRecord) -> None:
        st.quote, st.depth, st.last_ts = q, rec.depth, q.exchange_ts
        st.price = q.ltp
        if rec.prev_close > 0:
            st.prev_close = rec.prev_close
        if rec.open > 0:
            st.open = rec.open
        hi, lo = rec.high or q.ltp, rec.low or q.ltp
        st.high = max(st.high, hi, q.ltp) if st.high else max(hi, q.ltp)
        st.low = min(st.low, lo, q.ltp) if st.low else min(lo, q.ltp)
        delta = max(0.0, rec.volume - st.day_volume) if st.vol_seen else 0.0
        st.day_volume = max(st.day_volume, rec.volume)
        st.vol_seen = True
        if self.session != "OPEN":
            return
        ts = q.exchange_ts
        if not (self.cal.open_dt(self.day) <= ts < self.cal.close_dt(self.day)):
            return
        m = ts.replace(second=0, microsecond=0)
        c = st.cur_min
        if c is not None and m < c.start:
            return                                    # late result for a minute already closed
        if c is None or c.start != m:
            if c is not None:
                self._push(st, c)
            st.cur_min = Candle(st.key, m, q.ltp, q.ltp, q.ltp, q.ltp, delta)
        else:
            c.high, c.low, c.close = max(c.high, q.ltp), min(c.low, q.ltp), q.ltp
            c.volume += delta

    def _push(self, st: _LiveState, c: Candle) -> None:
        if st.candles and st.candles[-1].t >= c.t:
            if st.candles[-1].t == c.t:
                st.candles[-1] = c
            return
        st.candles.append(c)
        if len(st.candles) > self.max_candles:
            del st.candles[: len(st.candles) - self.max_candles]

    def _finalize_minutes(self, now: dt.datetime) -> None:
        for st in self.states.values():
            c = st.cur_min
            if c is not None and c.start + dt.timedelta(seconds=60) <= now:
                self._push(st, c)
                st.cur_min = None

    # ================================================================== the SimMarket-compatible interface
    @property
    def index_keys(self) -> list[str]:
        return [key_for(s) for s in INDICES if key_for(s) in self.states]

    @property
    def stock_keys(self) -> list[str]:
        core = [key_for(s) for s in STOCKS if key_for(s) in self.states]
        return core + sorted(k for k in self.promoted if k in self.states)

    def updates_since(self, seq: int, keys: list[str]) -> tuple[int, list[Quote]]:
        out = [u[1] for k in keys if (u := self._updated.get(k)) is not None and u[0] > seq]
        return self._seq, out

    def quote(self, key: str) -> Quote | None:
        st = self.states.get(key)
        if st is not None:
            return st.quote
        if key.startswith(("NFO:", "BFO:")):
            with self._lock:
                new = key not in self._wanted_opts
                self._wanted_opts[key] = self.mono()
            if new:
                self._wake.set()
            hit = self._opt_quotes.get(key)
            if hit is not None:
                return hit[0]
            return self._quote_from_chain(key)
        return None

    def _quote_from_chain(self, key: str) -> Quote | None:
        try:
            _ex, und, exp_s, k_s, ot = key.split(":")
            exp, strike = dt.date.fromisoformat(exp_s), float(k_s)
        except ValueError:
            return None
        hit = self._chains.get((und, exp))
        if hit is None:
            with self._lock:
                self._chain_wanted[(und, exp)] = self.mono()
            self._wake.set()
            return None
        _, ts, _spot, rows = hit
        r = next((x for x in rows if x.strike == strike), None)
        if r is None:
            return None
        if ot == "CE":
            return Quote(key, r.ce_ltp, r.ce_bid, r.ce_ask, r.ce_vol, r.ce_oi, ts, ts, Source.DHAN)
        return Quote(key, r.pe_ltp, r.pe_bid, r.pe_ask, r.pe_vol, r.pe_oi, ts, ts, Source.DHAN)

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
            if st.price <= 0 or st.prev_close <= 0:
                continue
            if st.price > st.prev_close:
                adv += 1
            elif st.price < st.prev_close:
                dec += 1
        return {"advancers": adv, "decliners": dec, "ad_ratio": adv / dec if dec else float(adv)}

    def depth(self, key: str, levels: int = 5) -> dict[str, list[dict[str, float]]]:
        st = self.states.get(key)
        d = st.depth if st is not None else (self._opt_quotes.get(key) or (None, {"bids": [], "asks": []}))[1]
        return {"bids": d.get("bids", [])[:levels], "asks": d.get("asks", [])[:levels]}

    def expiries(self, underlying: str, count: int = 3) -> list[dt.date]:
        today = self.clock().date()
        got = self._expiries.get(underlying)
        live = [e for e in (got[1] if got else []) if e >= today]
        if live:
            return live[:count]
        if underlying not in FNO_SEGMENT:                 # a stock: fetch its list; monthly calendar meanwhile
            if got is None or got[0] != today:
                with self._lock:
                    self._stock_exp_wanted.add(underlying)
            return self.cal.monthly_expiries(underlying, self.day, count)
        if underlying == "BANKNIFTY":
            return self.cal.monthly_expiries(underlying, self.day, count)
        return self.cal.weekly_expiries(underlying, self.day, count)

    def _index_key(self, underlying: str) -> str:
        return f"{'BSE' if underlying == 'SENSEX' else 'NSE'}:{underlying}"

    def _und_price(self, underlying: str) -> float:
        st = self.states.get(self._index_key(underlying)) or self.states.get(f"NSE:{underlying}")
        if st is not None:
            return st.price
        sc = self.screen.get(f"NSE:{underlying}")
        return sc.ltp if sc else 0.0

    def chain(self, underlying: str, expiry: dt.date | None = None, width: int = 15) -> tuple[float, list[ChainRow]]:
        """Cached chain; never blocks (the poller fetches wanted chains). Empty until the first fetch."""
        exps = self.expiries(underlying, 1)
        expiry = expiry or (exps[0] if exps else None)
        if expiry is None:
            return self._und_price(underlying), []
        with self._lock:
            self._chain_wanted[(underlying, expiry)] = self.mono()
            hit = self._chains.get((underlying, expiry))
        if hit is None or self.mono() - hit[0] >= CHAIN_TTL_S:
            self._wake.set()
        if hit is None:
            return self._und_price(underlying), []
        _, _ts, spot, rows = hit
        spot = self._und_price(underlying) or spot
        if rows and width:
            strikes = sorted(r.strike for r in rows)
            atm = min(strikes, key=lambda k: abs(k - spot))
            i = strikes.index(atm)
            lo, hi = strikes[max(0, i - width)], strikes[min(len(strikes) - 1, i + width)]
            rows = [r for r in rows if lo <= r.strike <= hi]
        for r in rows:
            self.option_instrument(underlying, expiry, r.strike, OptionType.CE)
            self.option_instrument(underlying, expiry, r.strike, OptionType.PE)
        return spot, rows

    def chain_blocking(self, underlying: str, expiry: dt.date | None = None, width: int = 15,
                       timeout: float = 8.0) -> tuple[float, list[ChainRow]]:
        """For API threads only (never the engine loop): wait for the first fetch of a chain."""
        spot, rows = self.chain(underlying, expiry, width)
        if rows:
            return spot, rows
        exps = self.expiries(underlying, 1)
        expiry = expiry or (exps[0] if exps else None)
        if expiry is None:
            return spot, rows
        with self._lock:
            ev = self._chain_ready.setdefault((underlying, expiry), threading.Event())
        if self._thread is None:
            try:
                self.fetch_chain(underlying, expiry)
            except DhanError as exc:
                self._record_error(exc)
        else:
            ev.wait(timeout)
        return self.chain(underlying, expiry, width)

    def option_instrument(self, underlying: str, expiry: dt.date, strike: float, ot: OptionType) -> Instrument:
        key = option_key(underlying, expiry, strike, ot)
        inst = self.instruments.get(key)
        if inst is None:
            sid = self._opt_ids.get(key)
            sym = f"{underlying} {expiry:%d%b}".upper() + f" {strike_str(strike)} {ot.value}"
            spec = next((s for s in INDICES if s.symbol == underlying), None)
            if spec is not None:
                inst = Instrument(key, sym, sym, Kind.OPTION, spec.exchange, lot_size=self.lots.get(underlying, spec.lot),
                                  tick_size=0.05, underlying=key_for(spec), expiry=expiry, strike=float(strike),
                                  option_type=ot, sector="INDEX", beta=1.0, dhan_security_id=sid[1] if sid else None)
            else:                                       # stock option: lot from the master's OPTSTK rows
                und_key = f"NSE:{underlying}"
                sc = self.screen.get(und_key)
                base = self.instruments.get(und_key)
                inst = Instrument(key, sym, sym, Kind.OPTION, Exchange.NSE, lot_size=sc.fno_lot if sc else 1,
                                  tick_size=0.05, underlying=und_key, expiry=expiry, strike=float(strike),
                                  option_type=ot, sector=SECTORS.get(underlying, "OTHER"),
                                  beta=base.beta if base else 1.0, dhan_security_id=sid[1] if sid else None)
            self.instruments[key] = inst
        return inst

    def describe(self) -> dict[str, Any]:
        now = self.clock()
        nxt = None
        if self.session == "PRE_OPEN":
            nxt = self.cal.open_dt(self.day)
        elif self.session == "CLOSED":
            nxt = self.cal.open_dt(self.cal.next_trading_day(self.day))
        return {"source": "DHAN", "label": "Live Dhan market data", "session": self.session,
                "day": self.day.isoformat(), "now": now.isoformat(),
                "next_open": nxt.isoformat() if nxt else None,
                "secondary": "UPSTOX" if self.secondary_configured else None,
                "secondary_configured": self.secondary_configured, "instruments": len(self.states),
                "backfill_days": self.backfill_days, "health": dict(self.health),
                "problems": (self.universe.problems if self.universe else [])[:12],
                "betas_estimated": sum(1 for s in self.states.values() if s.beta_source == "estimated"),
                "scanner": {"universe": len(self.screen),
                            "fresh": sum(1 for sc in list(self.screen.values())
                                         if sc.updated and (now - sc.updated).total_seconds() < 120),
                            "baselines": len(self.baselines), "baselines_pending": len(self._baseline_queue),
                            "promoted": sorted(self.promoted), "fno": sum(1 for sc in self.screen.values() if sc.fno_lot)}}


class LiveFeed:
    """The engine's primary feed in PAPER: hands the arbiter each quote once, as it arrives from Dhan."""

    def __init__(self, market: LiveMarket, name: str = "DHAN", source: Source = Source.DHAN) -> None:
        self.market, self.name, self.source = market, name, source
        self.down_until = None
        self.offsets_bps: dict[str, float] = {}
        self._seq = 0

    def is_down(self, now: dt.datetime) -> bool:
        return False

    def poll(self, keys: list[str]) -> list[Quote]:
        self._seq, out = self.market.updates_since(self._seq, keys)
        return out


class DisabledFeed:
    """A secondary feed that is not configured (no Upstox token): always empty, never 'recovers'."""

    def __init__(self, name: str = "UPSTOX", source: Source = Source.UPSTOX) -> None:
        self.name, self.source = name, source
        self.down_until = None
        self.offsets_bps: dict[str, float] = {}

    def is_down(self, now: dt.datetime) -> bool:
        return True

    def poll(self, keys: list[str]) -> list[Quote]:
        return []


class NullFaults:
    """PAPER has no failure injection (real failures are enough)."""

    def __init__(self) -> None:
        self.log: list[dict[str, str]] = []

    def order_api_down(self) -> bool:
        return False

    def reject_storm(self) -> bool:
        return False

    def inject(self, *_a: Any, **_k: Any) -> str:
        raise ValueError("failure injection exists only in SIM")
