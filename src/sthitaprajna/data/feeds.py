"""Two simulated broker feeds over one SimMarket truth, with failure injection (Phase 3 exit criterion:
"kill each feed, both feeds, and the order API mid-session in PAPER").

In SIM mode the Dhan-like feed is primary and the Upstox-like feed secondary, each with its own latency
profile. Faults are injected from the API/UI (System page) or tests; nothing injects faults by itself.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import numpy as np

from sthitaprajna.data.sim import SimMarket
from sthitaprajna.domain import Quote, Source

FAULTS = ("primary_down", "secondary_down", "both_down", "disagree", "order_api_down", "gap_down", "gap_up",
          "reject_storm")


@dataclass
class SimFeed:
    market: SimMarket
    name: str
    source: Source
    latency_ms: float
    jitter_ms: float
    seed: int
    down_until: dt.datetime | None = None
    offsets_bps: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.rng = np.random.default_rng(self.seed)

    def is_down(self, now: dt.datetime) -> bool:
        return self.down_until is not None and now < self.down_until

    def poll(self, keys: list[str]) -> list[Quote]:
        now = self.market.now
        if self.is_down(now):
            return []
        out = []
        for k in keys:
            q = self.market.quote(k)
            if q is None:
                continue
            lat = max(1.0, self.latency_ms + float(self.rng.normal(0, self.jitter_ms)))
            off = self.offsets_bps.get(k, 0.0)
            ltp = q.ltp * (1 + off / 1e4) if off else q.ltp
            out.append(Quote(q.key, round(ltp, 2), q.bid, q.ask, q.volume, q.oi, q.exchange_ts,
                             q.exchange_ts + dt.timedelta(milliseconds=lat), self.source, q.prev_close, q.open,
                             q.high, q.low))
        return out


class FaultInjector:
    def __init__(self, market: SimMarket, primary: SimFeed, secondary: SimFeed) -> None:
        self.market = market
        self.primary = primary
        self.secondary = secondary
        self.order_api_down_until: dt.datetime | None = None
        self.reject_storm_until: dt.datetime | None = None
        self.log: list[dict[str, str]] = []

    def inject(self, fault: str, seconds: float = 60.0, key: str | None = None, bps: float = 25.0) -> str:
        now = self.market.now
        until = now + dt.timedelta(seconds=seconds)
        if fault == "primary_down":
            self.primary.down_until = until
        elif fault == "secondary_down":
            self.secondary.down_until = until
        elif fault == "both_down":
            self.primary.down_until = self.secondary.down_until = until
        elif fault == "disagree":
            self.secondary.offsets_bps[key or "NSE:NIFTY"] = bps
        elif fault == "order_api_down":
            self.order_api_down_until = until
        elif fault == "reject_storm":
            self.reject_storm_until = until
        elif fault in ("gap_down", "gap_up"):
            self.market.inject_gap(-0.015 if fault == "gap_down" else 0.015)
        elif fault == "clear":
            self.primary.down_until = self.secondary.down_until = None
            self.secondary.offsets_bps.clear()
            self.order_api_down_until = self.reject_storm_until = None
        else:
            raise ValueError(f"unknown fault {fault!r}; choose from {FAULTS + ('clear',)}")
        msg = f"{fault} injected at {now:%H:%M:%S}" + (f" for {seconds:.0f}s" if fault.endswith(("down", "storm")) else "")
        self.log.append({"ts": now.isoformat(), "msg": msg})
        return msg

    def order_api_down(self) -> bool:
        return self.order_api_down_until is not None and self.market.now < self.order_api_down_until

    def reject_storm(self) -> bool:
        return self.reject_storm_until is not None and self.market.now < self.reject_storm_until
