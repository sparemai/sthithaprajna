"""Dhan read-only data adapter (primary feed). Ported from Kubera v5.5 `ingestion/dhan_market_client.py`
and `ingestion/ws_feed.py`, keeping every live-verified detail:

* POST /v2/charts/historical and /v2/charts/intraday return parallel arrays; `interval` is an int.
* POST /v2/marketfeed/ohlc needs INTEGER security ids in the body (strings -> bare 400, confirmed live).
* POST /v2/optionchain response (VERIFIED on RELIANCE, 2026-09-21): `data.oc` keyed by strike STRING with
  6 decimals; each ce/pe has greeks{delta,theta,gamma,vega}, implied_volatility, last_price, oi,
  previous_oi, volume, security_id (int), top_bid_price, top_ask_price ... No rho.
* Index underlyings for the option chain need the INDEX row's security id -- NOT YET RESOLVED in Kubera
  ([VERIFY] in Phase 0).
* Live feed ticker packet `<BHBIfI` (16 bytes), response code 2, cross-checked against dhanhq's SDK.
* Retries: transport errors, 5xx and 429 only; a 4xx is a bug in the request, not a transient.

Credentials (B0 gate (a)): DHAN_CLIENT_ID, DHAN_DATA_TOKEN in the environment. Never logged.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import os
import struct
from dataclasses import dataclass

import httpx
from tenacity import retry, retry_if_exception, retry_if_exception_type, stop_after_attempt, wait_exponential

from sthitaprajna.data.adapters.base import AdapterUnavailable
from sthitaprajna.domain import IST, Candle, Exchange, Instrument, Kind, OptionType, Quote, Source
from sthitaprajna.market.options import ChainRow

DHAN_BASE_URL = "https://api.dhan.co/v2"
INSTRUMENT_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"
WS_URL_TEMPLATE = "wss://api-feed.dhan.co?version=2&token={token}&clientId={client_id}&authType=2"
REQUEST_CODE_SUBSCRIBE_TICKER = 15
FEED_RESPONSE_CODE_TICKER = 2
MAX_INSTRUMENTS_PER_CONNECTION = 5000


def _is_server_error(exc: BaseException) -> bool:
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code >= 500


def _is_rate_limited(exc: BaseException) -> bool:
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429


_RETRYABLE = (retry_if_exception_type(httpx.TransportError) | retry_if_exception(_is_server_error)
              | retry_if_exception(_is_rate_limited))
_retry = retry(reraise=True, stop=stop_after_attempt(4), wait=wait_exponential(multiplier=1, min=1, max=10),
               retry=_RETRYABLE)


def candles_from_dhan(key: str, data: dict) -> list[Candle]:
    keys = ["open", "high", "low", "close", "volume", "timestamp"]
    if not all(k in data for k in keys):
        return []
    out = []
    for i in range(len(data["timestamp"])):
        out.append(Candle(key, dt.datetime.fromtimestamp(data["timestamp"][i], tz=IST), float(data["open"][i]),
                          float(data["high"][i]), float(data["low"][i]), float(data["close"][i]),
                          float(data["volume"][i])))
    return out


def parse_dhan_option_chain(body: dict, underlying: str, expiry: dt.date, exchange_prefix: str = "NFO"
                            ) -> tuple[float, list[ChainRow]]:
    data = body.get("data", {}) or {}
    spot = float(data.get("last_price") or 0.0)
    rows = []
    for strike_s, legs in (data.get("oc") or {}).items():
        k = float(strike_s)
        ce, pe = legs.get("ce") or {}, legs.get("pe") or {}
        cg, pg = ce.get("greeks") or {}, pe.get("greeks") or {}
        ks = f"{k:.2f}".rstrip("0").rstrip(".")
        rows.append(ChainRow(
            strike=k,
            ce_key=f"{exchange_prefix}:{underlying}:{expiry.isoformat()}:{ks}:CE",
            pe_key=f"{exchange_prefix}:{underlying}:{expiry.isoformat()}:{ks}:PE",
            ce_ltp=float(ce.get("last_price") or 0), pe_ltp=float(pe.get("last_price") or 0),
            ce_bid=float(ce.get("top_bid_price") or 0), ce_ask=float(ce.get("top_ask_price") or 0),
            pe_bid=float(pe.get("top_bid_price") or 0), pe_ask=float(pe.get("top_ask_price") or 0),
            ce_oi=float(ce.get("oi") or 0), pe_oi=float(pe.get("oi") or 0),
            ce_oi_chg=float(ce.get("oi") or 0) - float(ce.get("previous_oi") or 0),
            pe_oi_chg=float(pe.get("oi") or 0) - float(pe.get("previous_oi") or 0),
            ce_vol=float(ce.get("volume") or 0), pe_vol=float(pe.get("volume") or 0),
            ce_iv=float(ce.get("implied_volatility") or 0) / 100.0, pe_iv=float(pe.get("implied_volatility") or 0) / 100.0,
            ce_delta=float(cg.get("delta") or 0), pe_delta=float(pg.get("delta") or 0),
            ce_gamma=float(cg.get("gamma") or 0), pe_gamma=float(pg.get("gamma") or 0),
            ce_theta=float(cg.get("theta") or 0), pe_theta=float(pg.get("theta") or 0),
            ce_vega=float(cg.get("vega") or 0), pe_vega=float(pg.get("vega") or 0),
        ))
    rows.sort(key=lambda r: r.strike)
    return spot, rows


@dataclass
class TickerTick:
    exchange_segment_code: int
    security_id: int
    ltp: float
    ltt: dt.datetime


def parse_ticker_packet(raw: bytes) -> TickerTick | None:
    """`<BHBIfI` = response_code(u8), msg_len(u16), exchange_segment(u8), security_id(u32), LTP(f32), LTT(u32)."""
    if len(raw) < 16:
        return None
    code, _len, seg, sid, ltp, ltt = struct.unpack_from("<BHBIfI", raw, 0)
    if code != FEED_RESPONSE_CODE_TICKER:
        return None
    return TickerTick(seg, sid, float(ltp), dt.datetime.fromtimestamp(ltt, tz=dt.timezone.utc))


def ticker_subscribe_payload(instruments: list[tuple[str, str]]) -> dict:
    if len(instruments) > MAX_INSTRUMENTS_PER_CONNECTION:
        raise ValueError("exceeds Dhan's 5000-instruments-per-connection limit; split connections")
    return {"RequestCode": REQUEST_CODE_SUBSCRIBE_TICKER, "InstrumentCount": len(instruments),
            "InstrumentList": [{"ExchangeSegment": s, "SecurityId": i} for s, i in instruments]}


class DhanDataAdapter:
    source = Source.DHAN

    def __init__(self, client_id: str | None = None, token: str | None = None,
                 http: httpx.Client | None = None) -> None:
        self.client_id = client_id or os.environ.get("DHAN_CLIENT_ID", "")
        self.token = token or os.environ.get("DHAN_DATA_TOKEN", "")
        if not (self.client_id and self.token) and http is None:
            raise AdapterUnavailable("Dhan data credentials missing (DHAN_CLIENT_ID, DHAN_DATA_TOKEN) -- gate (a)")
        self._http = http or httpx.Client(base_url=DHAN_BASE_URL, timeout=15.0, headers={
            "access-token": self.token, "client-id": self.client_id, "Content-Type": "application/json"})
        self._master: list[dict] | None = None

    # instrument master (public, unauthenticated) --------------------------------------------------
    def master(self) -> list[dict]:
        if self._master is None:
            with httpx.Client(timeout=60.0) as c:
                r = c.get(INSTRUMENT_MASTER_URL)
                r.raise_for_status()
            self._master = list(csv.DictReader(io.StringIO(r.text)))
        return self._master

    def resolve(self, symbol: str) -> Instrument | None:
        s = symbol.strip().upper()
        for row in self.master():
            if (row.get("UNDERLYING_SYMBOL", "").strip().upper() == s and row.get("EXCH_ID") == "NSE"
                    and row.get("SEGMENT") == "E" and row.get("INSTRUMENT") == "EQUITY"):
                return Instrument(f"NSE:{s}", s, row.get("DISPLAY_NAME") or s, Kind.EQUITY, Exchange.NSE,
                                  lot_size=int(float(row.get("LOT_SIZE", 1) or 1)),
                                  tick_size=float(row.get("TICK_SIZE", 0.05) or 0.05),
                                  dhan_security_id=row["SECURITY_ID"])
        return None

    # market data ----------------------------------------------------------------------------------
    @_retry
    def _post(self, path: str, payload: dict) -> dict:
        r = self._http.post(path, json=payload)
        r.raise_for_status()
        return r.json()

    def quotes(self, instruments: list[Instrument]) -> list[Quote]:
        by_seg: dict[str, list[int]] = {}
        keymap: dict[str, Instrument] = {}
        for inst in instruments:
            if not inst.dhan_security_id:
                continue
            seg = "NSE_EQ" if inst.kind is Kind.EQUITY else ("BSE_FNO" if inst.exchange is Exchange.BSE else "NSE_FNO")
            by_seg.setdefault(seg, []).append(int(inst.dhan_security_id))
            keymap[str(inst.dhan_security_id)] = inst
        if not by_seg:
            return []
        body = self._post("/marketfeed/ohlc", by_seg)
        now = dt.datetime.now(IST)
        out = []
        for seg_quotes in (body.get("data") or {}).values():
            for sid, q in seg_quotes.items():
                inst = keymap.get(str(sid))
                if inst is None:
                    continue
                o = q.get("ohlc", {})
                ltp = float(q.get("last_price") or 0)
                out.append(Quote(inst.key, ltp, 0.0, 0.0, 0.0, 0.0, now, now, Source.DHAN,
                                 prev_close=float(o.get("close") or 0), open=float(o.get("open") or 0),
                                 high=float(o.get("high") or 0), low=float(o.get("low") or 0)))
        return out

    def candles(self, inst: Instrument, interval_min: int, start: dt.date, end: dt.date) -> list[Candle]:
        if not inst.dhan_security_id:
            return []
        seg = "NSE_EQ" if inst.kind is Kind.EQUITY else "IDX_I"
        if interval_min >= 1440:
            body = self._post("/charts/historical", {"securityId": inst.dhan_security_id, "exchangeSegment": seg,
                                                     "instrument": inst.kind.value, "expiryCode": 0, "oi": False,
                                                     "fromDate": start.isoformat(), "toDate": end.isoformat()})
        else:
            body = self._post("/charts/intraday", {"securityId": inst.dhan_security_id, "exchangeSegment": seg,
                                                   "instrument": inst.kind.value, "interval": int(interval_min),
                                                   "oi": False, "fromDate": start.isoformat(), "toDate": end.isoformat()})
        return candles_from_dhan(inst.key, body)

    def expiries(self, underlying: Instrument) -> list[dt.date]:
        seg = "IDX_I" if underlying.kind is Kind.INDEX else "NSE_EQ"
        body = self._post("/optionchain/expirylist", {"UnderlyingScrip": int(underlying.dhan_security_id or 0),
                                                      "UnderlyingSeg": seg})
        return sorted(dt.date.fromisoformat(x) for x in body.get("data", []))

    def option_chain(self, underlying: Instrument, expiry: dt.date) -> tuple[float, list[ChainRow]]:
        seg = "IDX_I" if underlying.kind is Kind.INDEX else "NSE_EQ"
        body = self._post("/optionchain", {"UnderlyingScrip": int(underlying.dhan_security_id or 0),
                                           "UnderlyingSeg": seg, "Expiry": expiry.isoformat()})
        prefix = "BFO" if underlying.exchange is Exchange.BSE else "NFO"
        return parse_dhan_option_chain(body, underlying.symbol, expiry, prefix)

    def close(self) -> None:
        self._http.close()


def option_type_of(s: str) -> OptionType:
    return OptionType.CE if s.upper() in ("CE", "CALL") else OptionType.PE
