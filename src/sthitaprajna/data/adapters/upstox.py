"""Upstox read-only data adapter (secondary feed / cross-check). Ported from Kubera v5.5
`ingestion/upstox_market_client.py` and `ingestion/upstox_options.py`:

* NSE BOD instrument JSON (gzip) is public; tick_size can arrive in paise-like integers (5 -> Rs 0.05).
* GET /v3/market-quote/ltp, /v3/historical-candle/{key}/minutes/{n}/{to}/{from},
  /v3/historical-candle/intraday/{key}/minutes/{n}; candles arrive newest-first -> dedupe + sort.
* GET /v2/option/chain -> data[] rows with call_options / put_options {market_data, option_greeks}.
* A still-forming current-day candle is dropped before any analysis (bar-close only).

Credentials (gate (a)): UPSTOX_ANALYTICS_TOKEN (preferred for data) or UPSTOX_ACCESS_TOKEN.
Upstox never places orders at T1-T2 (A5.5).
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import os
from urllib.parse import quote as urlquote

import httpx
from tenacity import retry, retry_if_exception, retry_if_exception_type, stop_after_attempt, wait_exponential

from sthitaprajna.data.adapters.base import AdapterUnavailable
from sthitaprajna.domain import IST, Candle, Exchange, Instrument, Kind, Quote, Source
from sthitaprajna.market.options import ChainRow


def strike_str(k: float) -> str:
    return f"{float(k):.2f}".rstrip("0").rstrip(".")

UPSTOX_BASE_URL = "https://api.upstox.com"
UPSTOX_NSE_INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
INDEX_KEYS = {"NIFTY": "NSE_INDEX|Nifty 50", "BANKNIFTY": "NSE_INDEX|Nifty Bank", "INDIAVIX": "NSE_INDEX|India VIX",
              "SENSEX": "BSE_INDEX|SENSEX"}


def _retryable(exc: BaseException) -> bool:
    return isinstance(exc, httpx.HTTPStatusError) and (exc.response.status_code >= 500 or exc.response.status_code == 429)


_retry = retry(reraise=True, stop=stop_after_attempt(4), wait=wait_exponential(min=1, max=10),
               retry=retry_if_exception_type(httpx.TransportError) | retry_if_exception(_retryable))


def normalize_tick_size(value) -> float:
    raw = float(value or 0.05)
    return raw / 100.0 if raw >= 1.0 else raw


def candles_from_upstox(key: str, body: dict) -> list[Candle]:
    out: dict[dt.datetime, Candle] = {}
    for row in (body.get("data") or {}).get("candles", []) or []:
        if len(row) < 6:
            continue
        ts = dt.datetime.fromisoformat(str(row[0]).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt.timezone.utc)
        out[ts] = Candle(key, ts.astimezone(IST), float(row[1]), float(row[2]), float(row[3]), float(row[4]),
                         float(row[5] or 0))
    return [out[t] for t in sorted(out)]


def completed_only(candles: list[Candle], interval_min: int, now: dt.datetime) -> list[Candle]:
    step = dt.timedelta(minutes=interval_min)
    return [c for c in candles if c.start + step <= now]


def parse_upstox_option_chain(body: dict, underlying: str, expiry: dt.date) -> tuple[float, list[ChainRow]]:
    rows, spot = [], 0.0
    prefix = "BFO" if underlying == "SENSEX" else "NFO"
    for r in body.get("data", []) or []:
        k = float(r.get("strike_price") or 0)
        spot = float(r.get("underlying_spot_price") or spot)
        c, p = r.get("call_options") or {}, r.get("put_options") or {}
        cm, pm = c.get("market_data") or {}, p.get("market_data") or {}
        cg, pg = c.get("option_greeks") or {}, p.get("option_greeks") or {}
        rows.append(ChainRow(
            k, f"{prefix}:{underlying}:{expiry.isoformat()}:{strike_str(k)}:CE", f"{prefix}:{underlying}:{expiry.isoformat()}:{strike_str(k)}:PE",
            float(cm.get("ltp") or 0), float(pm.get("ltp") or 0), float(cm.get("bid_price") or 0),
            float(cm.get("ask_price") or 0), float(pm.get("bid_price") or 0), float(pm.get("ask_price") or 0),
            float(cm.get("oi") or 0), float(pm.get("oi") or 0),
            float(cm.get("oi") or 0) - float(cm.get("prev_oi") or 0), float(pm.get("oi") or 0) - float(pm.get("prev_oi") or 0),
            float(cm.get("volume") or 0), float(pm.get("volume") or 0),
            float(cg.get("iv") or 0) / 100.0, float(pg.get("iv") or 0) / 100.0,
            float(cg.get("delta") or 0), float(pg.get("delta") or 0), float(cg.get("gamma") or 0), float(pg.get("gamma") or 0),
            float(cg.get("theta") or 0), float(pg.get("theta") or 0), float(cg.get("vega") or 0), float(pg.get("vega") or 0),
        ))
    rows.sort(key=lambda x: x.strike)
    return spot, rows


class UpstoxDataAdapter:
    source = Source.UPSTOX

    def __init__(self, token: str | None = None, http: httpx.Client | None = None) -> None:
        self.token = (token or os.environ.get("UPSTOX_ANALYTICS_TOKEN") or os.environ.get("UPSTOX_ACCESS_TOKEN") or "").strip()
        if not self.token and http is None:
            raise AdapterUnavailable("Upstox token missing (UPSTOX_ANALYTICS_TOKEN) -- gate (a)")
        self._http = http or httpx.Client(base_url=UPSTOX_BASE_URL, timeout=20.0, headers={
            "Accept": "application/json", "Authorization": f"Bearer {self.token}", "User-Agent": "Sthitaprajna/3.0"})
        self._master: list[dict] | None = None

    def master(self) -> list[dict]:
        if self._master is None:
            with httpx.Client(timeout=60.0) as c:
                r = c.get(UPSTOX_NSE_INSTRUMENTS_URL)
                r.raise_for_status()
                raw = r.content
            if raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
            self._master = json.loads(raw.decode("utf-8"))
        return self._master

    def resolve(self, symbol: str) -> Instrument | None:
        s = symbol.strip().upper()
        if s in INDEX_KEYS:
            ex = Exchange.BSE if s == "SENSEX" else Exchange.NSE
            return Instrument(f"{ex.value}:{s}", s, s, Kind.INDEX, ex, upstox_key=INDEX_KEYS[s])
        for row in self.master():
            if str(row.get("trading_symbol", "")).upper() == s and row.get("segment") == "NSE_EQ" \
                    and row.get("instrument_type") == "EQ":
                return Instrument(f"NSE:{s}", s, row.get("short_name") or s, Kind.EQUITY, Exchange.NSE,
                                  lot_size=int(float(row.get("lot_size", 1) or 1)),
                                  tick_size=normalize_tick_size(row.get("tick_size", 5)), upstox_key=row["instrument_key"])
        return None

    @_retry
    def _get(self, path: str, params: dict | None = None) -> dict:
        r = self._http.get(path, params=params)
        r.raise_for_status()
        return r.json()

    def quotes(self, instruments: list[Instrument]) -> list[Quote]:
        keys = {i.upstox_key: i for i in instruments if i.upstox_key}
        if not keys:
            return []
        body = self._get("/v3/market-quote/ltp", {"instrument_key": ",".join(keys)})
        now = dt.datetime.now(IST)
        out = []
        for q in (body.get("data") or {}).values():
            inst = keys.get(q.get("instrument_token"))
            if inst is None:
                continue
            out.append(Quote(inst.key, float(q.get("last_price") or 0), 0.0, 0.0, float(q.get("volume") or 0), 0.0,
                             now, now, Source.UPSTOX, prev_close=float(q.get("cp") or 0)))
        return out

    def candles(self, inst: Instrument, interval_min: int, start: dt.date, end: dt.date) -> list[Candle]:
        if not inst.upstox_key:
            return []
        key = urlquote(inst.upstox_key, safe="")
        today = dt.date.today()
        out: list[Candle] = []
        hist_to = min(end, today - dt.timedelta(days=1))
        if start <= hist_to:
            out += candles_from_upstox(inst.key, self._get(
                f"/v3/historical-candle/{key}/minutes/{int(interval_min)}/{hist_to.isoformat()}/{start.isoformat()}"))
        if start <= today <= end:
            cur = candles_from_upstox(inst.key, self._get(f"/v3/historical-candle/intraday/{key}/minutes/{int(interval_min)}"))
            out += completed_only(cur, interval_min, dt.datetime.now(IST))
        by = {c.start: c for c in out}
        return [by[t] for t in sorted(by)]

    def expiries(self, underlying: Instrument) -> list[dt.date]:
        body = self._get("/v2/option/contract", {"instrument_key": underlying.upstox_key or ""})
        exps = {str(r.get("expiry"))[:10] for r in body.get("data", []) if r.get("expiry")}
        return sorted(dt.date.fromisoformat(x) for x in exps)

    def option_chain(self, underlying: Instrument, expiry: dt.date) -> tuple[float, list[ChainRow]]:
        body = self._get("/v2/option/chain", {"instrument_key": underlying.upstox_key or "", "expiry_date": expiry.isoformat()})
        return parse_upstox_option_chain(body, underlying.symbol, expiry)

    def close(self) -> None:
        self._http.close()
