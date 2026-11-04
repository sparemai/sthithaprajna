"""A fake Dhan v2 API for tests and local end-to-end runs: no network, no real account, no real data.

Shapes follow adapters/dhan.py (live-verified parts) and Dhan's documented /marketfeed/quote format [VERIFY].
Used as `httpx.MockTransport(fake.handler)` in tests, or as an ASGI app (`fake.asgi()`) for a browser run.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import math
from typing import Any, Callable

import httpx

from sthitaprajna.config import load_holidays, load_rules
from sthitaprajna.data.sim import INDICES, STOCKS, stock_strike_step
from sthitaprajna.domain import IST, Kind
from sthitaprajna.market import black76
from sthitaprajna.market.calendar import MarketCalendar

TOKEN = "fake-data-token-7f3a"
TRADING_TOKEN = "fake-trading-token-2b9c"     # the order endpoints accept only this one (credential isolation)
CLIENT_ID = "1100000000"
ORDER_API = ("/orders", "/trades", "/positions", "/fundlimit", "/killswitch")
WORKING = ("PENDING", "TRANSIT", "TRIGGERED", "PART_TRADED")
INDEX_IDS = {"NIFTY": 13, "BANKNIFTY": 25, "SENSEX": 51, "INDIAVIX": 21}
STEP = {"NIFTY": 50, "BANKNIFTY": 100, "SENSEX": 100}
LOTS = {"NIFTY": 65, "BANKNIFTY": 30, "SENSEX": 20}
IV = {"index": 0.13, "stock": 0.28}
MASTER_COLUMNS = ["EXCH_ID", "SEGMENT", "SECURITY_ID", "ISIN", "INSTRUMENT", "UNDERLYING_SECURITY_ID",
                  "UNDERLYING_SYMBOL", "SYMBOL_NAME", "DISPLAY_NAME", "INSTRUMENT_TYPE", "SERIES", "LOT_SIZE",
                  "SM_EXPIRY_DATE", "STRIKE_PRICE", "OPTION_TYPE", "TICK_SIZE", "ASM_GSM_FLAG", "ASM_GSM_CATEGORY"]


class FakeDhan:
    def __init__(self, clock: Callable[[], dt.datetime], *, omit: tuple[str, ...] = ("ETERNAL",),
                 quote_rejects_idx: bool = False, pe_delta_positive: bool = False, extra_stocks: int = 0) -> None:
        self.clock = clock
        self.cal = MarketCalendar.from_rules(load_rules(), load_holidays())
        self.quote_rejects_idx = quote_rejects_idx
        self.pe_delta_positive = pe_delta_positive
        self.error: dict[str, Any] | None = None           # {"status": 401, "code": "DH-901"} for every /v2 call
        self.fail_paths: dict[str, dict[str, Any]] = {}      # per-path failures
        self.rate_limit: dict[str, int] = {}                 # path suffix -> how many 429s to answer next
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.seg_of: dict[int, str] = {}
        self.symbol_of: dict[int, str] = {}
        self.kind_of: dict[int, Kind] = {}
        self.px: dict[int, float] = {}
        self.prev: dict[int, float] = {}
        self.day_open: dict[int, float] = {}
        self.hi: dict[int, float] = {}
        self.lo: dict[int, float] = {}
        self.vol: dict[int, float] = {}
        self.options: dict[int, tuple[str, dt.date, float, str]] = {}
        self._opt_sids: dict[tuple[str, dt.date, float, str], int] = {}
        self.master_downloads = 0
        for spec in INDICES:
            self._add(INDEX_IDS[spec.symbol], "IDX_I", spec.symbol, Kind.INDEX, spec.price)
        for i, spec in enumerate(STOCKS):
            if spec.symbol not in omit:
                self._add(2000 + i, "NSE_EQ", spec.symbol, Kind.EQUITY, spec.price)
        # a synthetic wider universe for the scanner: every 5th is trade-to-trade (BE), every 3rd has options,
        # every 17th is under surveillance (ASM)
        self.series: dict[int, str] = {}
        self.fno_lot: dict[int, int] = {}
        self.asm: dict[int, str] = {}
        for i in range(extra_stocks):
            sid = 30000 + i
            self._add(sid, "NSE_EQ", f"SYN{i:03d}", Kind.EQUITY, 100.0 + 7 * i)
            self.series[sid] = "BE" if i % 5 == 4 else "EQ"
            if i % 3 == 0:
                self.fno_lot[sid] = 500
            if i % 17 == 16:
                self.asm[sid] = "ASM Stage 1"
        self.base_vol: dict[int, float] = {s: 100_000.0 for s in self.symbol_of}
        self.w52: dict[int, tuple[float, float]] = {}
        self.book: dict[int, tuple[float, float]] = {}      # (buy_quantity, sell_quantity)
        self.vwap: dict[int, float] = {}
        # order API (a tiny exchange + broker): order book, trade book, intraday positions, funds
        self.orders: dict[str, dict[str, Any]] = {}
        self.trades: list[dict[str, Any]] = []
        self.net: dict[tuple[str, int], dict[str, float]] = {}
        self.funds = 100_000.0
        self.auto_fill = True                                # marketable orders fill at once; resting stops trigger
        self.reject_next: str | None = None                  # the next placement is rejected with this reason
        self.cancel_fails: set[str] = set()
        self.order_calls: list[tuple[str, str, dict[str, Any]]] = []

    def _add(self, sid: int, seg: str, symbol: str, kind: Kind, price: float) -> None:
        self.seg_of[sid], self.symbol_of[sid], self.kind_of[sid] = seg, symbol, kind
        self.px[sid] = self.prev[sid] = self.day_open[sid] = self.hi[sid] = self.lo[sid] = float(price)
        self.vol[sid] = 0.0

    # ------------------------------------------------------------------ scripting helpers
    def sid(self, symbol: str) -> int:
        return next(s for s, sym in self.symbol_of.items() if sym == symbol)

    def set_price(self, symbol: str, price: float, volume_add: float = 0.0) -> None:
        s = self.sid(symbol)
        self.px[s] = float(price)
        self.hi[s], self.lo[s] = max(self.hi[s], price), min(self.lo[s], price)
        self.vol[s] += volume_add

    def walk(self, t: float) -> None:
        """Deterministic gentle motion for browser runs (not a market model)."""
        for s, kind in self.kind_of.items():
            base = self.prev[s]
            amp = 0.002 if kind is Kind.INDEX else 0.004
            p = base * (1 + amp * math.sin(t / 97.0 + s) + 0.6 * amp * math.sin(t / 23.0 + 2 * s))
            self.px[s] = round(p, 2) if self.symbol_of[s] == "INDIAVIX" else round(p / 0.05) * 0.05
            self.hi[s], self.lo[s] = max(self.hi[s], self.px[s]), min(self.lo[s], self.px[s])
            if kind is Kind.EQUITY:
                self.vol[s] += 1500

    # ------------------------------------------------------------------ master
    def master_csv(self) -> str:
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=MASTER_COLUMNS)
        w.writeheader()
        blank = {c: "" for c in MASTER_COLUMNS}
        for s, sym in self.symbol_of.items():
            if self.kind_of[s] is Kind.INDEX:
                w.writerow({**blank, "EXCH_ID": "BSE" if sym == "SENSEX" else "NSE", "SEGMENT": "I", "SECURITY_ID": s,
                            "INSTRUMENT": "INDEX", "UNDERLYING_SYMBOL": "INDIA VIX" if sym == "INDIAVIX" else sym,
                            "SYMBOL_NAME": "INDIA VIX" if sym == "INDIAVIX" else sym, "DISPLAY_NAME": sym.title(),
                            "LOT_SIZE": "1.0", "TICK_SIZE": "5.0000"})
            else:
                row = {**blank, "EXCH_ID": "NSE", "SEGMENT": "E", "SECURITY_ID": s, "INSTRUMENT": "EQUITY",
                       "UNDERLYING_SYMBOL": sym, "SYMBOL_NAME": sym, "DISPLAY_NAME": f"{sym.title()} Ltd",
                       "SERIES": self.series.get(s, "EQ"), "LOT_SIZE": "1.0", "TICK_SIZE": "5.0000",
                       "ASM_GSM_FLAG": "Y" if s in self.asm else "N", "ASM_GSM_CATEGORY": self.asm.get(s, "NA")}
                if sym == "RELIANCE":     # a decoy BE-series row listed first: the EQ row must win
                    w.writerow({**row, "SECURITY_ID": 999001, "SERIES": "BE"})
                w.writerow(row)
        today = self.clock().date()
        exp = self.cal.monthly_expiries("BANKNIFTY", today, 1)[0]
        for s, lot in self.fno_lot.items():
            w.writerow({**blank, "EXCH_ID": "NSE", "SEGMENT": "D", "SECURITY_ID": 80000 + s, "INSTRUMENT": "OPTSTK",
                        "UNDERLYING_SECURITY_ID": s, "UNDERLYING_SYMBOL": self.symbol_of[s], "LOT_SIZE": f"{lot}.0",
                        "SM_EXPIRY_DATE": exp.isoformat(), "STRIKE_PRICE": "100.0", "OPTION_TYPE": "CE"})
        for und, lot in LOTS.items():
            for i, exp in enumerate(self._expiries(und, today, 3)):
                w.writerow({**blank, "EXCH_ID": "BSE" if und == "SENSEX" else "NSE", "SEGMENT": "D",
                            "SECURITY_ID": 70000 + i, "INSTRUMENT": "OPTIDX", "UNDERLYING_SECURITY_ID": 26000,
                            "UNDERLYING_SYMBOL": und, "SYMBOL_NAME": f"{und}-X", "LOT_SIZE": f"{lot if i == 0 else lot + 10}.0",
                            "SM_EXPIRY_DATE": exp.isoformat(), "STRIKE_PRICE": "25000.00000", "OPTION_TYPE": "CE",
                            "TICK_SIZE": "5.0000"})
        w.writerow({**blank, "EXCH_ID": "NSE", "SEGMENT": "D", "SECURITY_ID": 88001, "INSTRUMENT": "FUTSTK",
                    "UNDERLYING_SYMBOL": "RELIANCE", "LOT_SIZE": "500.0"})
        return buf.getvalue()

    def _expiries(self, und: str, today: dt.date, n: int) -> list[dt.date]:
        if und == "BANKNIFTY":
            return self.cal.monthly_expiries(und, today, n)
        return self.cal.weekly_expiries(und, today, n)

    # ------------------------------------------------------------------ http
    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith(".csv"):
            self.master_downloads += 1
            return httpx.Response(200, text=self.master_csv())
        body = json.loads(request.content or b"{}")
        self.requests.append((path, body))
        order_api = any(x in path for x in ORDER_API)
        want = TRADING_TOKEN if order_api else TOKEN
        if request.headers.get("access-token") != want or request.headers.get("client-id") != CLIENT_ID:
            return self._err(401, "DH-901", "Client ID or user generated access token is invalid or expired.")
        for suffix, n in self.rate_limit.items():
            if n > 0 and path.endswith(suffix):
                self.rate_limit[suffix] = n - 1
                return self._err(429, "DH-904", "Too many requests")
        fail = self.fail_paths.get(path) or self.error
        if fail:
            return self._err(fail["status"], fail["code"], fail.get("message", "failure"))
        if order_api:
            return self._order_api(request.method, path, body)
        if path.endswith("/marketfeed/quote"):
            if self.quote_rejects_idx and "IDX_I" in body:
                return self._err(400, "DH-905", "Invalid segment for quote")
            return httpx.Response(200, json={"status": "success", "data": self._feed(body, depth=True)})
        if path.endswith("/marketfeed/ohlc"):
            return httpx.Response(200, json={"status": "success", "data": self._feed(body, depth=False)})
        if path.endswith("/charts/intraday"):
            return httpx.Response(200, json=self._intraday(body))
        if path.endswith("/charts/historical"):
            return httpx.Response(200, json=self._daily(body))
        if path.endswith("/optionchain/expirylist") or path.endswith("/optionchain"):
            sid = int(body.get("UnderlyingScrip") or 0)
            if sid not in self.symbol_of or body.get("UnderlyingSeg") != self._und_seg(sid):
                return self._err(400, "DH-905", "Invalid UnderlyingScrip / UnderlyingSeg")
            if self.kind_of[sid] is Kind.EQUITY and sid not in self.fno_lot:
                return self._err(400, "DH-905", "No derivatives on this security")
            if path.endswith("/optionchain"):
                return httpx.Response(200, json=self._chain(body))
            return httpx.Response(200, json={"status": "success", "data": [
                e.isoformat() for e in self._expiries(self.symbol_of[sid], self.clock().date(), 4)]})
        return self._err(404, "DH-905", f"unknown path {path}")

    @staticmethod
    def _err(status: int, code: str, msg: str) -> httpx.Response:
        return httpx.Response(status, json={"errorType": "Error", "errorCode": code, "errorMessage": msg})

    def _feed(self, body: dict[str, list[int]], depth: bool) -> dict[str, Any]:
        now = self.clock()
        out: dict[str, Any] = {}
        for seg, ids in body.items():
            rows = out.setdefault(seg, {})
            for sid in ids:
                sid = int(sid)
                if sid in self.options:
                    ltp, bid, ask, oi = self._option_quote(sid)
                    prev = ltp
                elif sid in self.px and self.seg_of[sid] == seg:
                    ltp, prev = self.px[sid], self.prev[sid]
                    half = 0.0 if self.kind_of[sid] is Kind.INDEX else max(0.05, round(ltp * 0.0001 / 0.05) * 0.05)
                    bid, ask, oi = ltp - half, ltp + half, 0.0
                else:
                    continue
                rec: dict[str, Any] = {"last_price": round(ltp, 2),
                                       "ohlc": {"open": self.day_open.get(sid, ltp), "close": prev,
                                                "high": self.hi.get(sid, ltp), "low": self.lo.get(sid, ltp)}}
                if depth:
                    is_index = self.kind_of.get(sid) is Kind.INDEX
                    hi52, lo52 = self.w52.get(sid, (prev * 1.25, prev * 0.75))
                    bq, sq = self.book.get(sid, (10_000.0, 10_000.0))
                    rec.update({"52_week_high": round(hi52, 2), "52_week_low": round(lo52, 2),
                                "upper_circuit_limit": round(prev * 1.2, 2), "lower_circuit_limit": round(prev * 0.8, 2),
                                "buy_quantity": bq, "sell_quantity": sq, "last_quantity": 10,
                                "oi_day_high": 0, "oi_day_low": 0})
                    rec.update({
                        "volume": int(self.vol.get(sid, 0)), "oi": oi, "net_change": round(ltp - prev, 2),
                        "last_trade_time": now.strftime("%d/%m/%Y %H:%M:%S"), "average_price": self.vwap.get(sid, ltp),
                        "depth": {"buy": [{"quantity": 0 if is_index else 100 * (i + 1), "orders": 0 if is_index else 3,
                                           "price": 0 if is_index else round(bid - 0.05 * i, 2)} for i in range(5)],
                                  "sell": [{"quantity": 0 if is_index else 100 * (i + 1), "orders": 0 if is_index else 3,
                                            "price": 0 if is_index else round(ask + 0.05 * i, 2)} for i in range(5)]}})
                rows[str(sid)] = rec
        return out

    def _intraday(self, body: dict[str, Any]) -> dict[str, list[float]]:
        sid = int(body["securityId"])
        start, end = dt.date.fromisoformat(body["fromDate"]), dt.date.fromisoformat(body["toDate"])
        now = self.clock()
        o: list[float] = []
        h: list[float] = []
        lo: list[float] = []
        c: list[float] = []
        v: list[float] = []
        ts: list[int] = []
        base = self.prev.get(sid, 100.0)
        d = start
        while d < end:
            if self.cal.is_trading_day(d):
                t = self.cal.open_dt(d)
                while t < self.cal.close_dt(d) and t + dt.timedelta(seconds=60) <= now + dt.timedelta(minutes=1):
                    i = int(t.timestamp()) // 60
                    p = base * (1 + 0.003 * math.sin(i / 37.0 + sid) + 0.001 * math.sin(i / 5.0))
                    o.append(round(p, 2))
                    c.append(round(p * (1 + 0.0004 * math.sin(i)), 2))
                    h.append(round(max(o[-1], c[-1]) * 1.0006, 2))
                    lo.append(round(min(o[-1], c[-1]) * 0.9994, 2))
                    v.append(0.0 if self.kind_of.get(sid) is Kind.INDEX else 1200.0)
                    ts.append(int(t.timestamp()))
                    t += dt.timedelta(minutes=1)
            d += dt.timedelta(days=1)
        return {"open": o, "high": h, "low": lo, "close": c, "volume": v, "timestamp": ts}

    def _daily(self, body: dict[str, Any]) -> dict[str, list[float]]:
        sid = int(body["securityId"])
        start, end = dt.date.fromisoformat(body["fromDate"]), dt.date.fromisoformat(body["toDate"])
        out: dict[str, list[float]] = {k: [] for k in ("open", "high", "low", "close", "volume", "timestamp")}
        base, d, i = self.prev.get(sid, 100.0), start, 0
        while d < end:                      # toDate exclusive, like the intraday endpoint in this fake
            if self.cal.is_trading_day(d):
                i += 1
                c = base * (1 + 0.004 * math.sin(i + sid))
                for k, v in (("open", c * 0.998), ("high", c * 1.01), ("low", c * 0.99), ("close", c),
                             ("volume", self.base_vol.get(sid, 100_000.0))):
                    out[k].append(round(v, 2))
                out["timestamp"].append(int(self.cal.open_dt(d).replace(hour=0, minute=0).timestamp()))
            d += dt.timedelta(days=1)
        if out["close"]:
            out["close"][-1] = round(base, 2)           # the last completed day closes at the previous close
        return out

    def _option_sid(self, und: str, exp: dt.date, strike: float, ot: str) -> int:
        k = (und, exp, float(strike), ot)
        sid = self._opt_sids.setdefault(k, 500000 + len(self._opt_sids))
        self.options[sid] = k
        return sid

    def _und_seg(self, sid: int) -> str:
        """What /optionchain expects as UnderlyingSeg: IDX_I for an index, the equity's own segment for a stock."""
        return "IDX_I" if self.kind_of[sid] is Kind.INDEX else self.seg_of[sid]

    def _und_sid(self, und: str) -> int:
        return INDEX_IDS[und] if und in INDEX_IDS else self.sid(und)

    def _iv(self, und: str) -> float:
        return IV["index"] if und in INDEX_IDS else IV["stock"]

    def _greeks(self, und: str, exp: dt.date, strike: float, is_call: bool) -> black76.Greeks:
        spot = self.px[self._und_sid(und)]
        t = max(self.cal.year_fraction_to(self.clock(), exp), 1e-4)
        return black76.greeks(black76.forward(spot, t, 0.065), strike, t, self._iv(und), 0.065, is_call)

    def _option_quote(self, sid: int) -> tuple[float, float, float, float]:
        und, exp, strike, ot = self.options[sid]
        g = self._greeks(und, exp, strike, ot == "CE")
        px = max(0.05, round(g.price / 0.05) * 0.05)
        return px, max(0.05, px - 0.05), px + 0.05, 150000.0

    def _chain(self, body: dict[str, Any]) -> dict[str, Any]:
        und = self.symbol_of[int(body["UnderlyingScrip"])]
        exp = dt.date.fromisoformat(body["Expiry"])
        spot = self.px[self._und_sid(und)]
        step = STEP.get(und) or stock_strike_step(spot)
        atm = round(spot / step) * step
        oc: dict[str, Any] = {}
        for i in range(-20, 21):
            k = float(atm + i * step)
            if k <= 0:
                continue
            legs = {}
            for leg, is_call in (("ce", True), ("pe", False)):
                g = self._greeks(und, exp, k, is_call)
                px = max(0.05, round(g.price / 0.05) * 0.05)
                delta = abs(g.delta) if (self.pe_delta_positive or is_call) else g.delta
                legs[leg] = {"last_price": px, "top_bid_price": max(0.05, px - 0.05), "top_ask_price": px + 0.05,
                             "oi": 100000 + 5000 * abs(i), "previous_oi": 95000 + 5000 * abs(i), "volume": 20000,
                             "implied_volatility": self._iv(und) * 100, "greeks": {"delta": round(delta, 4), "theta": round(g.theta, 4),
                                                                    "gamma": round(g.gamma, 6), "vega": round(g.vega, 4)},
                             "security_id": self._option_sid(und, exp, k, "CE" if is_call else "PE")}
            oc[f"{k:.6f}"] = legs
        return {"status": "success", "data": {"last_price": spot, "oc": oc}}

    # ------------------------------------------------------------------ order API
    def _order_api(self, method: str, path: str, body: dict[str, Any]) -> httpx.Response:
        parts = path.split("/v2", 1)[-1].split("?")[0].strip("/").split("/")
        self.order_calls.append((method, "/" + "/".join(parts), body))
        self.check_orders()
        head = parts[0]
        if head == "fundlimit":
            used = sum(max(0.0, n["buy_qty"] - n["sell_qty"]) * n["buy_avg"] for n in self.net.values())
            return httpx.Response(200, json={"dhanClientId": CLIENT_ID, "availabelBalance": round(self.funds - used, 2),
                                             "sodLimit": self.funds, "utilizedAmount": round(used, 2),
                                             "withdrawableBalance": round(self.funds - used, 2)})
        if head == "positions":
            return httpx.Response(200, json=self.position_rows())
        if head == "trades":
            rows = [t for t in self.trades if len(parts) == 1 or t["orderId"] == parts[1]]
            return httpx.Response(200, json=rows)
        if head == "killswitch":
            return httpx.Response(200, json={"dhanClientId": CLIENT_ID, "killSwitchStatus": "activated"})
        if len(parts) == 1:
            if method == "POST":
                return self._place(body)
            return httpx.Response(200, json=list(self.orders.values()))
        if parts[1] == "external":
            row = next((r for r in self.orders.values() if r["correlationId"] == parts[2]), None)
            return httpx.Response(200, json=row) if row else self._err(404, "DH-905", "no such order")
        row = self.orders.get(parts[1])
        if row is None:
            return self._err(404, "DH-905", "no such order")
        if method == "GET":
            return httpx.Response(200, json=row)
        if method == "PUT":
            return self._modify(row, body)
        if method == "DELETE":
            if parts[1] in self.cancel_fails or row["orderStatus"] not in WORKING:
                return self._err(400, "DH-906", f"Order is {row['orderStatus']}, it cannot be cancelled")
            row["orderStatus"] = "CANCELLED"
            return httpx.Response(200, json={"orderId": row["orderId"], "orderStatus": "CANCELLED"})
        return self._err(405, "DH-905", "method not allowed")

    def _lot(self, sid: int) -> int:
        if sid in self.options:
            und = self.options[sid][0]
            return LOTS.get(und) or self.fno_lot.get(self.sid(und), 1)
        return 1

    def _bbo(self, sid: int) -> tuple[float, float, float]:
        if sid in self.options:
            ltp, bid, ask, _oi = self._option_quote(sid)
            return ltp, bid, ask
        ltp = self.px[sid]
        half = max(0.05, round(ltp * 0.0001 / 0.05) * 0.05)
        return ltp, ltp - half, ltp + half

    def _place(self, b: dict[str, Any]) -> httpx.Response:
        need = ("dhanClientId", "transactionType", "exchangeSegment", "productType", "orderType", "validity",
                "securityId", "quantity")
        if any(k not in b for k in need) or b["dhanClientId"] != CLIENT_ID:
            return self._err(400, "DH-905", "Missing or invalid fields")
        sid, seg = int(b["securityId"]), b["exchangeSegment"]
        eq_ok = seg == "NSE_EQ" and sid in self.kind_of and self.kind_of[sid] is Kind.EQUITY
        if not (eq_ok or (seg in ("NSE_FNO", "BSE_FNO") and sid in self.options)):
            return self._err(400, "DH-905", "Invalid securityId for exchangeSegment")
        if b["orderType"] not in ("MARKET", "LIMIT", "STOP_LOSS", "STOP_LOSS_MARKET") or b["productType"] not in (
                "INTRADAY", "CNC") or b["transactionType"] not in ("BUY", "SELL"):
            return self._err(400, "DH-905", "Invalid order parameters")
        now = self.clock().strftime("%Y-%m-%d %H:%M:%S")
        oid = str(52000000 + len(self.orders) + 1)
        qty, price, trig = int(b["quantity"]), float(b.get("price") or 0), float(b.get("triggerPrice") or 0)
        row = {"dhanClientId": CLIENT_ID, "orderId": oid, "correlationId": b.get("correlationId", ""),
               "orderStatus": "PENDING", "transactionType": b["transactionType"], "exchangeSegment": seg,
               "productType": b["productType"], "orderType": b["orderType"], "validity": b["validity"],
               "tradingSymbol": self.symbol_of.get(sid, str(sid)), "securityId": str(sid), "quantity": qty,
               "price": price, "triggerPrice": trig, "filledQty": 0, "remainingQuantity": qty,
               "averageTradedPrice": 0.0, "omsErrorCode": "", "omsErrorDescription": "", "createTime": now,
               "updateTime": now, "algoId": b.get("algoId", "")}
        ltp = self._bbo(sid)[0]
        why = self.reject_next
        self.reject_next = None
        if why is None and qty % self._lot(sid):
            why = f"Quantity should be a multiple of lot size {self._lot(sid)}"
        if why is None and any(abs(round(x / 0.05) * 0.05 - x) > 1e-6 for x in (price, trig)):
            why = "Price is not a multiple of the tick size"
        if why is None and b["orderType"] == "MARKET" and seg.endswith("FNO"):
            why = "Market orders are not allowed for this contract"
        if why is None and b["orderType"].startswith("STOP_LOSS"):
            sell = b["transactionType"] == "SELL"
            if (sell and trig >= ltp) or (not sell and trig <= ltp):
                why = "Trigger price is on the wrong side of the last traded price"
            elif b["orderType"] == "STOP_LOSS" and ((sell and price > trig) or (not sell and price < trig)):
                why = "Limit price is on the wrong side of the trigger price"
        if why:
            row.update(orderStatus="REJECTED", omsErrorDescription=why, omsErrorCode="RMS")
        self.orders[oid] = row
        if row["orderStatus"] == "PENDING":
            self._try_fill(row)
        return httpx.Response(200, json={"orderId": oid, "orderStatus": "REJECTED" if why else "TRANSIT"})

    def _modify(self, row: dict[str, Any], b: dict[str, Any]) -> httpx.Response:
        if row["orderStatus"] not in WORKING:
            return self._err(400, "DH-906", f"Order is {row['orderStatus']}, it cannot be modified")
        for k in ("quantity", "price", "triggerPrice", "orderType"):
            if b.get(k) not in (None, ""):
                row[k] = b[k] if k == "orderType" else (int(b[k]) if k == "quantity" else float(b[k]))
        row["remainingQuantity"] = row["quantity"] - row["filledQty"]
        self._try_fill(row)
        return httpx.Response(200, json={"orderId": row["orderId"], "orderStatus": "TRANSIT"})

    def check_orders(self) -> None:
        """The exchange: resting stops trigger on the last price; marketable limits fill."""
        for row in list(self.orders.values()):
            if row["orderStatus"] in ("PENDING", "TRIGGERED"):
                self._try_fill(row)

    def _try_fill(self, row: dict[str, Any]) -> None:
        if not self.auto_fill:
            return
        sid, buy, ot = int(row["securityId"]), row["transactionType"] == "BUY", row["orderType"]
        ltp, bid, ask = self._bbo(sid)
        if ot.startswith("STOP_LOSS"):
            hit = row["orderStatus"] == "TRIGGERED" or (ltp >= row["triggerPrice"] if buy else ltp <= row["triggerPrice"])
            if not hit:
                return
            row["orderStatus"] = "TRIGGERED"
            if ot == "STOP_LOSS" and ((buy and ask > row["price"]) or (not buy and bid < row["price"])):
                return                                   # triggered, but the limit is not reachable yet
            self._execute(row, ask if buy else bid)
        elif ot == "MARKET":
            self._execute(row, ask if buy else bid)
        elif (buy and row["price"] >= ask) or (not buy and row["price"] <= bid):
            self._execute(row, ask if buy else bid)

    def _execute(self, row: dict[str, Any], px: float) -> None:
        qty = row["quantity"] - row["filledQty"]
        px = round(px, 2)
        row.update(orderStatus="TRADED", filledQty=row["quantity"], remainingQuantity=0, averageTradedPrice=px)
        sid, seg = int(row["securityId"]), row["exchangeSegment"]
        self.trades.append({"dhanClientId": CLIENT_ID, "orderId": row["orderId"], "exchangeSegment": seg,
                            "securityId": str(sid), "transactionType": row["transactionType"],
                            "tradedQuantity": qty, "tradedPrice": px, "productType": row["productType"]})
        n = self.net.setdefault((seg, sid), {"buy_qty": 0.0, "buy_val": 0.0, "sell_qty": 0.0, "sell_val": 0.0,
                                             "buy_avg": 0.0})
        side = "buy" if row["transactionType"] == "BUY" else "sell"
        n[f"{side}_qty"] += qty
        n[f"{side}_val"] += qty * px
        n["buy_avg"] = n["buy_val"] / n["buy_qty"] if n["buy_qty"] else 0.0

    def position_rows(self) -> list[dict[str, Any]]:
        out = []
        for (seg, sid), n in self.net.items():
            net = n["buy_qty"] - n["sell_qty"]
            out.append({"dhanClientId": CLIENT_ID, "tradingSymbol": self.symbol_of.get(sid, str(sid)),
                        "securityId": str(sid), "exchangeSegment": seg, "productType": "INTRADAY",
                        "positionType": "LONG" if net > 0 else "SHORT" if net < 0 else "CLOSED",
                        "buyQty": int(n["buy_qty"]), "sellQty": int(n["sell_qty"]), "netQty": int(net),
                        "buyAvg": round(n["buy_avg"], 2),
                        "sellAvg": round(n["sell_val"] / n["sell_qty"], 2) if n["sell_qty"] else 0.0})
        return out

    def external_trade(self, sid: int, seg: str, side: str, qty: int) -> None:
        """Someone else trades this account (the operator in the Dhan app): a filled order we did not place."""
        self._place({"dhanClientId": CLIENT_ID, "transactionType": side, "exchangeSegment": seg,
                     "productType": "INTRADAY", "orderType": "MARKET" if seg == "NSE_EQ" else "LIMIT",
                     "validity": "DAY", "securityId": str(sid), "quantity": qty,
                     "price": 0 if seg == "NSE_EQ" else round((self._bbo(sid)[2 if side == "BUY" else 1]) / 0.05) * 0.05})

    def orders_where(self, **kw: Any) -> list[dict[str, Any]]:
        return [r for r in self.orders.values() if all(r.get(k) == v for k, v in kw.items())]

    # ------------------------------------------------------------------ ASGI wrapper (browser runs)
    def asgi(self):
        from starlette.applications import Starlette
        from starlette.requests import Request
        from starlette.responses import Response
        from starlette.routing import Route

        async def any_path(req: Request) -> Response:
            content = await req.body()
            r = self.handler(httpx.Request(req.method, str(req.url), headers=dict(req.headers), content=content))
            return Response(r.content, status_code=r.status_code, media_type=r.headers.get("content-type"))

        return Starlette(routes=[Route("/{path:path}", any_path, methods=["GET", "POST"])])


class FakeClock:
    def __init__(self, start: dt.datetime) -> None:
        self.t = start
        self.mono_t = 1000.0

    def __call__(self) -> dt.datetime:
        return self.t

    def mono(self) -> float:
        return self.mono_t

    def advance(self, seconds: float) -> None:
        self.t += dt.timedelta(seconds=seconds)
        self.mono_t += seconds


def ist(y: int, mo: int, d: int, h: int = 0, mi: int = 0, s: int = 0) -> dt.datetime:
    return dt.datetime(y, mo, d, h, mi, s, tzinfo=IST)
