"""Dhan live market data for PAPER mode (read-only): instrument master, quotes with depth, 1-minute
history, option chains and expiries. Rate limited; every failure is a `DhanError` that says what to fix.

Reuses the live-verified details in adapters/dhan.py (Kubera v5.5, 2026-09-21):
* /marketfeed/* bodies need INTEGER security ids;
* /charts/intraday takes an int `interval` and date-only from/to (Dhan's own SDK) and returns parallel arrays;
* index underlyings use the master's INSTRUMENT=INDEX row (NIFTY = 13) with segment "IDX_I";
* option-chain legs carry an int `security_id`.

[VERIFY] (not yet confirmed against the real API; `sthitaprajna dhan-check` prints each on the operator's
first run): /marketfeed/quote field names (depth, last_trade_time), IDX_I inside marketfeed bodies (a
rejected segment falls back to /marketfeed/ohlc automatically), the DH-9xx error table, the rate limits
(config/market_rules.yaml) and the master's TICK_SIZE unit (paise).

Credentials: DHAN_CLIENT_ID + DHAN_DATA_TOKEN only (never the trading token). They are never logged and
never put into an error message.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import os
import threading
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import httpx

from sthitaprajna.data.adapters.base import AdapterUnavailable
from sthitaprajna.data.adapters.dhan import DHAN_BASE_URL, INSTRUMENT_MASTER_URL, candles_from_dhan
from sthitaprajna.domain import IST, Candle, Kind

INDEX_OPTION_UNDERLYINGS = ("NIFTY", "BANKNIFTY", "SENSEX")
INDEX_ALIASES = {
    "NIFTY": ("NIFTY", "NIFTY 50", "NIFTY50"),
    "BANKNIFTY": ("BANKNIFTY", "NIFTY BANK", "NIFTYBANK"),
    "SENSEX": ("SENSEX", "BSE SENSEX"),
    "INDIAVIX": ("INDIA VIX", "INDIAVIX", "INDIA_VIX"),
}
# Used only when the master has no INDEX row for the name. All four were found in the real master on 2026-09-27
# (NIFTY 13 also by Kubera on 2026-09-21); any use of a fallback is still reported as a problem.
INDEX_FALLBACK_IDS = {"NIFTY": "13", "BANKNIFTY": "25", "SENSEX": "51", "INDIAVIX": "21"}
FNO_SEGMENT = {"NIFTY": "NSE_FNO", "BANKNIFTY": "NSE_FNO", "SENSEX": "BSE_FNO"}
QUOTE_MAX_INSTRUMENTS = 1000          # [VERIFY] per /marketfeed request
UNIVERSE_CACHE_DAYS = 7
UNIVERSE_VERSION = 2                  # v2: every NSE equity + stock-option lots (scanner)

ERROR_KINDS = {"DH-901": "AUTH", "DH-902": "NOT_SUBSCRIBED", "DH-903": "ACCOUNT", "DH-904": "RATE",
               "DH-905": "INPUT", "DH-906": "ORDER", "DH-907": "NO_DATA", "DH-908": "SERVER",
               "DH-909": "NETWORK", "DH-910": "OTHER"}
FIXES = {
    "AUTH": "Dhan rejected DHAN_CLIENT_ID / DHAN_DATA_TOKEN (wrong, or the access token has expired). Generate "
            "a new access token in Dhan's web portal (DhanHQ API section), update .env and restart.",
    "NOT_SUBSCRIBED": "This Dhan account has no access to Dhan's Data APIs (live quotes, history and option chains "
                      "need the Data API plan). Enable it in Dhan, then restart.",
    "ACCOUNT": "Dhan reports an account problem (for example a segment that is not activated). Check the account "
               "in Dhan.",
    "RATE": "Dhan's rate limit was hit even after retries. Another program using the same Dhan account's API at "
            "the same time counts too (for example a running `sthitaprajna serve --mode paper`): stop it, wait a "
            "minute and try again. While running, Sthitaprajna backs off by itself.",
    "INPUT": "Dhan rejected a request as invalid. This is a bug in Sthitaprajna's request: run "
             "`sthitaprajna dhan-check` and share its output (it contains no secrets).",
    "ORDER": "Unexpected order error from a read-only call.",
    "NO_DATA": "Dhan returned no data for this request.",
    "SERVER": "Dhan's servers returned an error; retrying.",
    "NETWORK": "Cannot reach Dhan (internet, firewall or DNS); retrying.",
    "OTHER": "Unexpected Dhan response: run `sthitaprajna dhan-check` and share its output.",
}
FATAL_KINDS = frozenset({"AUTH", "NOT_SUBSCRIBED", "ACCOUNT"})   # retrying will not help until fixed


class DhanError(RuntimeError):
    def __init__(self, kind: str, message: str, *, code: str | None = None, status: int | None = None) -> None:
        super().__init__(message)
        self.kind, self.code, self.status = kind, code, status

    @property
    def fix(self) -> str:
        return FIXES.get(self.kind, FIXES["OTHER"])

    @property
    def fatal(self) -> bool:
        return self.kind in FATAL_KINDS

    def explain(self) -> str:
        return f"{self} -- {self.fix}"


def _find(d: Any, names: tuple[str, ...]) -> Any:
    if not isinstance(d, dict):
        return None
    for n in names:
        if d.get(n):
            return d[n]
    for nested in ("remarks", "data", "error"):
        v = _find(d.get(nested), names)
        if v:
            return v
    return None


def error_from_response(r: httpx.Response, secret: str = "") -> DhanError:
    try:
        body = r.json()
    except ValueError:
        body = None
    code = _find(body, ("errorCode", "error_code"))
    msg = _find(body, ("errorMessage", "error_message", "message"))
    kind = ERROR_KINDS.get(str(code).strip()) if code else None
    if kind is None:
        s = r.status_code
        kind = "AUTH" if s in (401, 403) else "RATE" if s == 429 else "SERVER" if s >= 500 else "INPUT"
    text = f"Dhan {r.request.url.path}: HTTP {r.status_code}" + (f" {code}" if code else "")
    if msg:
        m = str(msg)[:200]
        if secret:
            m = m.replace(secret, "***")
        text += f": {m}"
    return DhanError(kind, text, code=str(code) if code else None, status=r.status_code)


def checked_url(url: str) -> str:
    """Credentials only travel over HTTPS; plain HTTP is allowed for a local test server alone."""
    u = urlparse(url)
    if u.scheme == "https" or (u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost")):
        return url.rstrip("/")
    raise ValueError(f"refusing Dhan URL {url!r}: must be https (or http on localhost for tests)")


def normalise_tick(raw: Any, default: float = 0.05) -> float:
    """The master's TICK_SIZE is in paise (e.g. 5.0000 = Rs 0.05) [VERIFY]; values below 1 are rupees already."""
    try:
        x = float(raw)
    except (TypeError, ValueError):
        return default
    if x <= 0:
        return default
    return round(x / 100.0 if x >= 1.0 else x, 4)


def parse_last_trade_time(s: Any) -> dt.datetime | None:
    if not s or not isinstance(s, str):
        return None
    for fmt in ("%d/%m/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            t = dt.datetime.strptime(s.strip(), fmt).replace(tzinfo=IST)
            return None if t.year < 2000 else t
        except ValueError:
            continue
    return None


class MinInterval:
    """Per endpoint family: the next request waits until `seconds` after the previous RESPONSE (`done()`).

    Spacing from the response, not the send, survives network jitter: a first request that also opens the TLS
    connection can reach Dhan late, and a 1.05 s send-to-send gap was seen to arrive as < 1 s (HTTP 429).
    """

    def __init__(self, seconds: float, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.seconds, self.clock, self.sleep = seconds, clock, sleep
        self._next = 0.0
        self._lock = threading.Lock()

    def remaining(self) -> float:
        return max(0.0, self._next - self.clock())

    def wait(self) -> None:
        with self._lock:
            delay = self._next - self.clock()
            if delay > 0:
                self.sleep(delay)
            self._next = self.clock() + self.seconds      # provisional until done()

    def done(self, extra: float = 0.0) -> None:
        with self._lock:
            self._next = max(self._next, self.clock() + self.seconds + extra)


@dataclass
class FeedRecord:
    """One instrument from /marketfeed/quote (or /ohlc when depth is unavailable)."""
    ltp: float
    prev_close: float
    open: float
    high: float
    low: float
    volume: float
    oi: float
    bid: float
    ask: float
    depth: dict[str, list[dict[str, float]]]
    last_trade: dt.datetime | None
    endpoint: str
    vwap: float = 0.0              # average_price
    w52_high: float = 0.0
    w52_low: float = 0.0
    upper_circuit: float = 0.0
    lower_circuit: float = 0.0
    buy_qty: float = 0.0           # total pending buy quantity
    sell_qty: float = 0.0


def parse_feed_record(q: dict[str, Any], endpoint: str) -> FeedRecord:
    o = q.get("ohlc") or {}
    dep = q.get("depth") or {}

    def levels(side: str) -> list[dict[str, float]]:
        out = []
        for lv in dep.get(side) or []:
            px = float(lv.get("price") or 0)
            if px > 0:
                out.append({"price": px, "qty": int(float(lv.get("quantity") or 0)), "orders": int(float(lv.get("orders") or 0))})
        return out

    bids, asks = levels("buy"), levels("sell")
    return FeedRecord(
        ltp=float(q.get("last_price") or 0), prev_close=float(o.get("close") or 0), open=float(o.get("open") or 0),
        high=float(o.get("high") or 0), low=float(o.get("low") or 0), volume=float(q.get("volume") or 0),
        oi=float(q.get("oi") or 0), bid=bids[0]["price"] if bids else 0.0, ask=asks[0]["price"] if asks else 0.0,
        depth={"bids": bids, "asks": asks}, last_trade=parse_last_trade_time(q.get("last_trade_time")),
        endpoint=endpoint, vwap=_f(q.get("average_price")), w52_high=_f(q.get("52_week_high")),
        w52_low=_f(q.get("52_week_low")), upper_circuit=_f(q.get("upper_circuit_limit")),
        lower_circuit=_f(q.get("lower_circuit_limit")), buy_qty=_f(q.get("buy_quantity")),
        sell_qty=_f(q.get("sell_quantity")))


def _f(x: Any) -> float:
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def records_from_body(body: Any, endpoint: str) -> dict[tuple[str, str], FeedRecord]:
    out: dict[tuple[str, str], FeedRecord] = {}
    for seg, rows in ((body or {}).get("data") or {}).items():
        if isinstance(rows, dict):
            for sid, q in rows.items():
                if isinstance(q, dict):
                    out[(seg, str(sid))] = parse_feed_record(q, endpoint)
    return out


def _retry_after(r: httpx.Response) -> float | None:
    try:
        return min(10.0, max(0.5, float(r.headers.get("retry-after", ""))))
    except ValueError:
        return None


class DhanClient:
    def __init__(self, client_id: str | None = None, token: str | None = None, *, base_url: str | None = None,
                 master_url: str | None = None, transport: httpx.BaseTransport | None = None, rules: Any = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> None:
        cid = (client_id or os.environ.get("DHAN_CLIENT_ID", "")).strip()
        tok = (token or os.environ.get("DHAN_DATA_TOKEN", "")).strip()
        if not (cid and tok):
            raise AdapterUnavailable("Dhan data credentials missing: set DHAN_CLIENT_ID and DHAN_DATA_TOKEN in .env")
        self._secret = tok
        self.base_url = checked_url(base_url or os.environ.get("STHITAPRAJNA_DHAN_BASE_URL") or DHAN_BASE_URL)
        self.master_url = checked_url(master_url or os.environ.get("STHITAPRAJNA_DHAN_MASTER_URL") or INSTRUMENT_MASTER_URL)
        self._transport = transport
        self._http = httpx.Client(base_url=self.base_url, timeout=httpx.Timeout(10.0, connect=5.0), transport=transport,
                                  headers={"access-token": tok, "client-id": cid, "Content-Type": "application/json",
                                           "Accept": "application/json"})

        def rule(name: str, default: float) -> float:
            try:
                return float(rules.get(name, default)) if rules is not None else default
            except (TypeError, ValueError):
                return default

        self.limits = {"quote": MinInterval(rule("dhan_quote_min_interval_s", 1.25), clock, sleep),
                       "chain": MinInterval(rule("dhan_option_chain_min_interval_s", 3.1), clock, sleep),
                       "data": MinInterval(rule("dhan_data_min_interval_s", 0.34), clock, sleep)}
        self.segments_without_quote: set[str] = set()
        self.notes: list[str] = []

    # ------------------------------------------------------------------ transport
    def _post(self, family: str, path: str, payload: dict[str, Any], retries: int = 2) -> Any:
        """POST a read-only request. A rate-limit answer (429 / DH-904) is retried with backoff."""
        lim = self.limits[family]
        for attempt in range(retries + 1):
            lim.wait()
            try:
                r = self._http.post(path, json=payload)
            except httpx.TimeoutException:
                lim.done()
                raise DhanError("NETWORK", f"Dhan {path}: timed out") from None
            except httpx.TransportError as exc:
                lim.done()
                raise DhanError("NETWORK", f"Dhan {path}: {type(exc).__name__}") from None
            lim.done()
            err: DhanError | None = None
            body: Any = None
            if r.status_code >= 400:
                err = error_from_response(r, self._secret)
            else:
                try:
                    body = r.json()
                except ValueError:
                    raise DhanError("OTHER", f"Dhan {path}: response is not JSON") from None
                if isinstance(body, dict) and str(body.get("status", "")).lower() == "failure":
                    err = error_from_response(r, self._secret)
            if err is None:
                return body
            if err.kind == "RATE" and attempt < retries:
                lim.done(extra=_retry_after(r) or 1.5 * (attempt + 1))
                continue
            raise err
        raise AssertionError("unreachable")

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------------ quotes
    def marketfeed(self, segments: dict[str, list[int]], endpoint: str = "quote") -> dict[tuple[str, str], FeedRecord]:
        body = self._post("quote", f"/marketfeed/{endpoint}",
                          {seg: [int(x) for x in ids] for seg, ids in segments.items() if ids})
        return records_from_body(body, endpoint)

    def quotes(self, segments: dict[str, list[int]]) -> dict[tuple[str, str], FeedRecord]:
        """Quotes with depth; a segment that /marketfeed/quote rejects falls back to /marketfeed/ohlc for good."""
        total = sum(len(v) for v in segments.values())
        if total > QUOTE_MAX_INSTRUMENTS:
            raise ValueError(f"{total} instruments exceed Dhan's {QUOTE_MAX_INSTRUMENTS} per request")
        want_quote = {s: v for s, v in segments.items() if v and s not in self.segments_without_quote}
        want_ohlc = {s: v for s, v in segments.items() if v and s in self.segments_without_quote}
        out: dict[tuple[str, str], FeedRecord] = {}
        if want_quote:
            try:
                out.update(self.marketfeed(want_quote, "quote"))
            except DhanError as exc:
                if exc.kind != "INPUT":
                    raise
                for seg, ids in want_quote.items():     # find the segment Dhan will not quote
                    try:
                        out.update(self.marketfeed({seg: ids}, "quote"))
                    except DhanError as exc2:
                        if exc2.kind != "INPUT":
                            raise
                        self.segments_without_quote.add(seg)
                        want_ohlc[seg] = ids
                        self.notes.append(f"Dhan /marketfeed/quote rejected segment {seg} ({exc2}); using "
                                          f"/marketfeed/ohlc for it (no bid/ask depth)")
        if want_ohlc:
            out.update(self.marketfeed(want_ohlc, "ohlc"))
        return out

    # ------------------------------------------------------------------ history, expiries, chains
    def intraday(self, key: str, security_id: str, segment: str, instrument: str, start: dt.date, end: dt.date,
                 interval: int = 1) -> list[Candle]:
        body = self._post("data", "/charts/intraday", {
            "securityId": str(security_id), "exchangeSegment": segment, "instrument": instrument,
            "interval": int(interval), "oi": False, "fromDate": start.isoformat(), "toDate": end.isoformat()})
        data = body if isinstance(body, dict) and "timestamp" in body else (body or {}).get("data") or {}
        return candles_from_dhan(key, data)

    def historical(self, key: str, security_id: str, segment: str, instrument: str, start: dt.date,
                   end: dt.date) -> list[Candle]:
        """Daily candles (verified request shape, Kubera: expiryCode 0, oi false, date-only range)."""
        body = self._post("data", "/charts/historical", {
            "securityId": str(security_id), "exchangeSegment": segment, "instrument": instrument,
            "expiryCode": 0, "oi": False, "fromDate": start.isoformat(), "toDate": end.isoformat()})
        data = body if isinstance(body, dict) and "timestamp" in body else (body or {}).get("data") or {}
        return candles_from_dhan(key, data)

    def expiries(self, security_id: str, segment: str) -> list[dt.date]:
        body = self._post("data", "/optionchain/expirylist", {"UnderlyingScrip": int(security_id), "UnderlyingSeg": segment})
        out = []
        for x in (body or {}).get("data") or []:
            try:
                out.append(dt.date.fromisoformat(str(x)[:10]))
            except ValueError:
                continue
        return sorted(set(out))

    def option_chain(self, security_id: str, segment: str, expiry: dt.date) -> dict[str, Any]:
        return self._post("chain", "/optionchain", {"UnderlyingScrip": int(security_id), "UnderlyingSeg": segment,
                                                    "Expiry": expiry.isoformat()})

    # ------------------------------------------------------------------ instrument master (public file)
    def download_master(self, dest: Path, progress: Callable[[str], None] | None = None) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".part")
        try:
            with httpx.Client(timeout=httpx.Timeout(120.0, connect=10.0), transport=self._transport) as c:
                with c.stream("GET", self.master_url) as r:     # no credentials: the master is public
                    if r.status_code >= 400:
                        raise DhanError("NETWORK", f"instrument master download: HTTP {r.status_code}")
                    got, last = 0, 0
                    with open(tmp, "wb") as f:
                        for chunk in r.iter_bytes(1 << 16):
                            f.write(chunk)
                            got += len(chunk)
                            if progress and got - last >= 8 << 20:
                                last = got
                                progress(f"  downloading Dhan instrument master... {got / 1e6:.0f} MB")
        except httpx.TransportError as exc:
            raise DhanError("NETWORK", f"instrument master download: {type(exc).__name__}") from None
        tmp.replace(dest)
        return dest


# ---------------------------------------------------------------------- master scan + universe
_SLIM = ("EXCH_ID", "SEGMENT", "SECURITY_ID", "INSTRUMENT", "UNDERLYING_SYMBOL", "SYMBOL_NAME", "DISPLAY_NAME",
         "LOT_SIZE", "TICK_SIZE", "SERIES", "SM_EXPIRY_DATE", "ASM_GSM_FLAG", "ASM_GSM_CATEGORY")


@dataclass
class MasterScan:
    equities: dict[str, list[dict[str, str]]] = field(default_factory=dict)     # every NSE equity, by symbol
    indices: list[dict[str, str]] = field(default_factory=list)
    index_options: list[dict[str, str]] = field(default_factory=list)
    stock_options: list[dict[str, str]] = field(default_factory=list)
    rows: int = 0


def scan_master(path: Path, symbols: set[str] | None = None) -> MasterScan:
    """Streams the (large) master CSV and keeps every NSE equity, the index rows and option lot sizes."""
    scan = MasterScan()
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
        for row in csv.DictReader(f):
            scan.rows += 1
            inst = (row.get("INSTRUMENT") or "").strip().upper()
            und = (row.get("UNDERLYING_SYMBOL") or "").strip().upper()
            if inst == "EQUITY":
                if row.get("EXCH_ID") == "NSE" and row.get("SEGMENT") == "E" and und:
                    scan.equities.setdefault(und, []).append({k: (row.get(k) or "").strip() for k in _SLIM})
            elif inst == "INDEX":
                scan.indices.append({k: (row.get(k) or "").strip() for k in _SLIM})
            elif inst in ("OPTIDX", "OPTSTK") and und:
                slim = {k: (row.get(k) or "").strip() for k in ("UNDERLYING_SYMBOL", "SM_EXPIRY_DATE", "LOT_SIZE", "EXCH_ID")}
                if inst == "OPTIDX":
                    if und in INDEX_OPTION_UNDERLYINGS:
                        scan.index_options.append(slim)
                elif row.get("EXCH_ID") == "NSE":
                    scan.stock_options.append(slim)
    return scan


@dataclass
class Resolved:
    key: str
    symbol: str
    name: str
    kind: str
    security_id: str
    segment: str
    instrument: str      # INDEX | EQUITY (Dhan's `instrument` field for charts)
    lot: int
    tick: float
    source: str          # master | fallback


@dataclass
class Universe:
    items: dict[str, Resolved]
    lots: dict[str, int]
    problems: list[str]
    built: str
    equities: dict[str, dict[str, Any]] = field(default_factory=dict)   # every NSE equity: scanner universe
    version: int = UNIVERSE_VERSION

    def to_json(self) -> str:
        return json.dumps({"version": self.version, "built": self.built,
                           "items": {k: asdict(v) for k, v in self.items.items()}, "lots": self.lots,
                           "problems": self.problems, "equities": self.equities}, indent=1)

    @classmethod
    def from_json(cls, text: str) -> "Universe":
        d = json.loads(text)
        return cls({k: Resolved(**v) for k, v in d["items"].items()}, {k: int(v) for k, v in d["lots"].items()},
                   list(d.get("problems", [])), d["built"], dict(d.get("equities") or {}), int(d.get("version", 1)))

    @property
    def fno_symbols(self) -> list[str]:
        return sorted(s for s, e in self.equities.items() if e.get("fno_lot"))


def _find_index_row(indices: list[dict[str, str]], symbol: str, exchange: str) -> dict[str, str] | None:
    names = INDEX_ALIASES.get(symbol, (symbol,))
    hits = [r for r in indices if r["UNDERLYING_SYMBOL"].upper() in names or r["SYMBOL_NAME"].upper() in names]
    if not hits:
        return None
    same_exchange = [r for r in hits if r["EXCH_ID"].upper() == exchange]
    exact = [r for r in (same_exchange or hits) if r["UNDERLYING_SYMBOL"].upper() == names[0]]
    return (exact or same_exchange or hits)[0]


def option_lots(rows: list[dict[str, str]], today: dt.date) -> dict[str, int]:
    """Lot size per underlying from its nearest live expiry's option rows (most common value)."""
    live: dict[str, list[tuple[dt.date, int]]] = {}
    for r in rows:
        try:
            exp = dt.date.fromisoformat(r["SM_EXPIRY_DATE"][:10])
            lot = int(float(r["LOT_SIZE"]))
        except ValueError:
            continue
        if exp >= today and lot > 0:
            live.setdefault(r["UNDERLYING_SYMBOL"].upper(), []).append((exp, lot))
    out = {}
    for und, xs in live.items():
        nearest = min(e for e, _ in xs)
        out[und] = Counter(lot for e, lot in xs if e == nearest).most_common(1)[0][0]
    return out


def index_option_lots(rows: list[dict[str, str]], today: dt.date) -> dict[str, int]:
    """Lot size per index from the nearest live expiry's OPTIDX rows (most common value)."""
    out: dict[str, int] = {}
    for und in INDEX_OPTION_UNDERLYINGS:
        live = []
        for r in rows:
            if r["UNDERLYING_SYMBOL"].upper() != und:
                continue
            try:
                exp = dt.date.fromisoformat(r["SM_EXPIRY_DATE"][:10])
                lot = int(float(r["LOT_SIZE"]))
            except ValueError:
                continue
            if exp >= today and lot > 0:
                live.append((exp, lot))
        if live:
            nearest = min(e for e, _ in live)
            out[und] = Counter(lot for e, lot in live if e == nearest).most_common(1)[0][0]
    return out


def resolve_universe(scan: MasterScan, specs: list[Any], rule_lots: dict[str, int], today: dt.date) -> Universe:
    items: dict[str, Resolved] = {}
    problems: list[str] = []
    lots = index_option_lots(scan.index_options, today)
    for und, lot in rule_lots.items():
        if und not in lots:
            lots[und] = int(lot)
            problems.append(f"{und} lot size not found in the master: using market_rules value {lot}")
    for spec in specs:
        key = f"{spec.exchange.value}:{spec.symbol}"
        if spec.kind is Kind.INDEX:
            row = _find_index_row(scan.indices, spec.symbol, spec.exchange.value)
            if row is not None:
                sid, source, name = row["SECURITY_ID"], "master", row["DISPLAY_NAME"] or spec.name
            elif spec.symbol in INDEX_FALLBACK_IDS:
                sid, source, name = INDEX_FALLBACK_IDS[spec.symbol], "fallback", spec.name
                problems.append(f"{spec.symbol}: no INDEX row in the master, using fallback security id {sid} [VERIFY]")
            else:
                problems.append(f"{spec.symbol}: index not found in the master; dropped")
                continue
            items[key] = Resolved(key, spec.symbol, name, Kind.INDEX.value, sid, "IDX_I", "INDEX",
                                  lots.get(spec.symbol, spec.lot), 0.05, source)
        else:
            rows = scan.equities.get(spec.symbol.upper())
            if not rows:
                problems.append(f"{spec.symbol}: not in Dhan's instrument master (renamed or delisted?); dropped")
                continue
            row = next((r for r in rows if r["SERIES"].upper() == "EQ"), rows[0])
            items[key] = Resolved(key, spec.symbol, row["DISPLAY_NAME"] or spec.name, Kind.EQUITY.value,
                                  row["SECURITY_ID"], "NSE_EQ", "EQUITY", 1, normalise_tick(row["TICK_SIZE"]), "master")
    stock_lots = option_lots(scan.stock_options, today)
    equities: dict[str, dict[str, Any]] = {}
    for sym, rows in scan.equities.items():
        row = next((r for r in rows if r["SERIES"].upper() == "EQ"), rows[0])
        flag = (row.get("ASM_GSM_FLAG") or "").strip().upper()
        equities[sym] = {"sid": row["SECURITY_ID"], "name": row["DISPLAY_NAME"] or sym,
                         "series": row["SERIES"].upper() or "EQ", "tick": normalise_tick(row["TICK_SIZE"]),
                         "fno_lot": stock_lots.get(sym, 0),
                         "surveillance": (row.get("ASM_GSM_CATEGORY") or flag) if flag not in ("", "N", "NO", "0") else ""}
    return Universe(items, lots, problems, today.isoformat(), equities)


def load_universe(client: DhanClient, specs: list[Any], rule_lots: dict[str, int], cache_dir: Path, today: dt.date,
                  progress: Callable[[str], None] = print, refresh: bool = False) -> Universe:
    """Resolved universe from a cache younger than 7 days, else from a fresh master download."""
    cache = cache_dir / "dhan-universe.json"
    want = {f"{s.exchange.value}:{s.symbol}" for s in specs}
    if cache.is_file() and not refresh:
        try:
            u = Universe.from_json(cache.read_text(encoding="utf-8"))
            age = (today - dt.date.fromisoformat(u.built)).days
            if u.version == UNIVERSE_VERSION and 0 <= age < UNIVERSE_CACHE_DAYS and want <= set(u.items) | _dropped(u):
                progress(f"  instrument ids from cache ({u.built}): {len(u.items)} instruments")
                return u
        except (ValueError, KeyError, TypeError):
            pass
    master = cache_dir / f"dhan-master-{today.isoformat()}.csv"
    if not master.is_file():
        progress("  downloading Dhan instrument master (public file, can take a minute)...")
        client.download_master(master, progress)
    for old in cache_dir.glob("dhan-master-*.csv"):
        if old != master:
            old.unlink(missing_ok=True)
    scan = scan_master(master)
    u = resolve_universe(scan, specs, rule_lots, today)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache.write_text(u.to_json(), encoding="utf-8")
    progress(f"  instrument master: {scan.rows:,} rows scanned, {len(u.items)} core instruments, "
             f"{len(u.equities):,} NSE stocks to scan ({len(u.fno_symbols)} with options)")
    return u


def _dropped(u: Universe) -> set[str]:
    """Keys the cached universe already knows were dropped (so a missing symbol does not force re-downloads)."""
    out = set()
    for p in u.problems:
        sym = p.split(":", 1)[0]
        if "dropped" in p:
            out.update({f"NSE:{sym}", f"BSE:{sym}"})
    return out
