"""Indian charges model by segment (A4 "Charges"; A12.3 "all Indian charges and taxes included").

Every rate comes from market_rules.yaml, so a rate change is a config change with a source URL.
Used identically by the backtester, the paper broker and live P&L.
"""
from __future__ import annotations

from dataclasses import dataclass

from sthitaprajna.domain import Exchange, Segment, Side


@dataclass(frozen=True)
class ChargeBreakdown:
    brokerage: float
    stt: float
    exchange: float
    ipft: float
    sebi: float
    stamp: float
    gst: float
    dp: float

    @property
    def total(self) -> float:
        return (self.brokerage + self.stt + self.exchange + self.ipft + self.sebi + self.stamp + self.gst
                + self.dp)

    def to_dict(self) -> dict[str, float]:
        d = {k: round(getattr(self, k), 4) for k in
             ("brokerage", "stt", "exchange", "ipft", "sebi", "stamp", "gst", "dp")}
        d["total"] = round(self.total, 4)
        return d


class ChargesModel:
    def __init__(self, rules) -> None:
        self.r = rules

    def _brokerage(self, segment: Segment, turnover: float) -> float:
        if segment in (Segment.INDEX_OPTION, Segment.STOCK_OPTION):
            spec = self.r["brokerage_options"]
        elif segment is Segment.EQ_DELIVERY:
            spec = self.r["brokerage_delivery"]
        else:
            spec = self.r["brokerage_intraday_and_futures"]
        flat = float(spec.get("flat") or 0.0)
        pct = spec.get("pct")
        return min(flat, turnover * float(pct)) if pct is not None else flat

    def compute(self, segment: Segment, side: Side, price: float, qty: int,
                exchange: Exchange = Exchange.NSE) -> ChargeBreakdown:
        """Charges for ONE executed order (one side). `price` is the premium for options."""
        turnover = abs(price * qty)
        if turnover == 0:
            return ChargeBreakdown(0, 0, 0, 0, 0, 0, 0, 0)
        r = self.r
        buy = side is Side.BUY
        brokerage = self._brokerage(segment, turnover)

        if segment in (Segment.INDEX_OPTION, Segment.STOCK_OPTION):
            stt = 0.0 if buy else turnover * r["stt_options_sell_premium"]
            if exchange is Exchange.BSE and segment is Segment.INDEX_OPTION:
                exch = turnover * r["exch_txn_bse_index_options_premium"]
                ipft = 0.0
            else:
                exch = turnover * r["exch_txn_nse_options_premium"]
                ipft = turnover * r["ipft_options_premium"]
            stamp = turnover * r["stamp_duty_options_buy"] if buy else 0.0
        elif segment in (Segment.INDEX_FUTURE, Segment.STOCK_FUTURE):
            stt = 0.0 if buy else turnover * r["stt_futures_sell"]
            exch = turnover * r["exch_txn_nse_futures"]
            ipft = turnover * r["ipft_equity_and_futures"]
            stamp = turnover * r["stamp_duty_futures_buy"] if buy else 0.0
        elif segment is Segment.EQ_INTRADAY:
            stt = 0.0 if buy else turnover * r["stt_equity_intraday_sell"]
            exch = turnover * r["exch_txn_nse_equity"]
            ipft = turnover * r["ipft_equity_and_futures"]
            stamp = turnover * r["stamp_duty_intraday_buy"] if buy else 0.0
        else:  # EQ_DELIVERY
            stt = turnover * r["stt_equity_delivery"]
            exch = turnover * r["exch_txn_nse_equity"]
            ipft = turnover * r["ipft_equity_and_futures"]
            stamp = turnover * r["stamp_duty_delivery_buy"] if buy else 0.0

        sebi = turnover * r["sebi_turnover_fee"]
        gst = (brokerage + exch + sebi) * r["gst_rate"]
        dp = 0.0
        if segment is Segment.EQ_DELIVERY and not buy:
            dp = r["dp_charge_per_scrip_sell_day"] * (1 + r["gst_rate"])
        return ChargeBreakdown(brokerage, stt, exch, ipft, sebi, stamp, gst, dp)

    def round_trip(self, segment: Segment, entry: float, exit_: float, qty: int,
                   exchange: Exchange = Exchange.NSE, long: bool = True) -> float:
        """Estimated charges for entry + exit of a whole position (used by the EV engine)."""
        open_side, close_side = (Side.BUY, Side.SELL) if long else (Side.SELL, Side.BUY)
        return (self.compute(segment, open_side, entry, qty, exchange).total
                + self.compute(segment, close_side, exit_, qty, exchange).total)
