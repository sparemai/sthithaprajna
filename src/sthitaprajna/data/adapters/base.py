"""BrokerDataAdapter (A5.5 rule 1): one adapter per broker behind a normalised schema.

Strategies never call a broker SDK directly. Every record carries source, exchange_ts, receive_ts.
Adapters are READ-ONLY: order placement lives only in execution/dhan_orders.py (credential isolation).
"""
from __future__ import annotations

import datetime as dt
from typing import Protocol

from sthitaprajna.domain import Candle, Instrument, Quote, Source
from sthitaprajna.market.options import ChainRow


class BrokerDataAdapter(Protocol):
    source: Source

    def resolve(self, symbol: str) -> Instrument | None: ...
    def quotes(self, instruments: list[Instrument]) -> list[Quote]: ...
    def candles(self, inst: Instrument, interval_min: int, start: dt.date, end: dt.date) -> list[Candle]: ...
    def expiries(self, underlying: Instrument) -> list[dt.date]: ...
    def option_chain(self, underlying: Instrument, expiry: dt.date) -> tuple[float, list[ChainRow]]: ...
    def close(self) -> None: ...


class AdapterUnavailable(RuntimeError):
    """Raised when credentials are missing -- a B0 human gate (a), never silently ignored."""
