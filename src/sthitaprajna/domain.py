"""Core domain vocabulary shared by every layer (spec A5.2 hierarchy, AGG-1/AGG-2, A18)."""
from __future__ import annotations

import datetime as dt
import itertools
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class StrEnum(str, Enum):
    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return self.value


class Mode(StrEnum):
    SIM = "SIM"                  # synthetic market; nothing here is evidence of edge
    PAPER = "PAPER"
    LIVE_PILOT = "LIVE_PILOT"
    LIVE_FULL = "LIVE_FULL"


class PerfLabel(StrEnum):
    """A2.1: every performance number carries its source."""
    SIMULATED = "SIMULATED"
    BACKTEST = "BACKTEST"
    WALK_FORWARD_OOS = "WALK-FORWARD-OOS"
    PAPER = "PAPER"
    LIVE = "LIVE"


class Gear(StrEnum):
    ATTACK = "ATTACK"
    PRESS = "PRESS"
    GUARD = "GUARD"
    PRESERVE = "PRESERVE"
    HALTED = "HALTED"

    @property
    def rank(self) -> int:
        """0 = most aggressive, 4 = halted. Higher rank == more defensive."""
        return GEAR_ORDER.index(self)

    def down(self, steps: int = 1) -> "Gear":
        return GEAR_ORDER[min(len(GEAR_ORDER) - 1, self.rank + steps)]

    def up(self) -> "Gear":
        return GEAR_ORDER[max(0, self.rank - 1)]

    @staticmethod
    def most_defensive(*gears: "Gear") -> "Gear":
        return max(gears, key=lambda g: g.rank)


GEAR_ORDER: tuple[Gear, ...] = (Gear.ATTACK, Gear.PRESS, Gear.GUARD, Gear.PRESERVE, Gear.HALTED)


class Grade(StrEnum):
    A_PLUS = "A+"
    A = "A"
    B = "B"
    C = "C"

    @property
    def rank(self) -> int:
        return {"A+": 0, "A": 1, "B": 2, "C": 3}[self.value]


class PickupStage(StrEnum):
    S1_SCAN = "S1_SCAN"
    S2_QUALIFY = "S2_QUALIFY"
    S3_GRADE = "S3_GRADE"
    S4_CONFIRM = "S4_CONFIRM"
    S5_ARM = "S5_ARM"
    S6_RISK = "S6_RISK"
    S7_PROBE = "S7_PROBE"


PICKUP_ORDER = tuple(PickupStage)


class PositionStage(StrEnum):
    PROBE = "PROBE"
    CONFIRM_ADD = "CONFIRM_ADD"
    PRESS_1 = "PRESS_1"
    PRESS_2 = "PRESS_2"
    HARVEST = "HARVEST"
    EXIT = "EXIT"


class Direction(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"

    @property
    def sign(self) -> int:
        return 1 if self is Direction.LONG else -1


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class Exchange(StrEnum):
    NSE = "NSE"
    BSE = "BSE"


class Kind(StrEnum):
    INDEX = "INDEX"
    EQUITY = "EQUITY"
    OPTION = "OPTION"
    FUTURE = "FUTURE"


class OptionType(StrEnum):
    CE = "CE"
    PE = "PE"


class Segment(StrEnum):
    """Charge/risk segment of a trade."""
    INDEX_OPTION = "INDEX_OPTION"
    STOCK_OPTION = "STOCK_OPTION"
    INDEX_FUTURE = "INDEX_FUTURE"
    STOCK_FUTURE = "STOCK_FUTURE"
    EQ_INTRADAY = "EQ_INTRADAY"
    EQ_DELIVERY = "EQ_DELIVERY"


class Source(StrEnum):
    DHAN = "DHAN"
    UPSTOX = "UPSTOX"
    SIM = "SIM"


class HalfLife(StrEnum):
    SECONDS = "SECONDS"
    MINUTES = "MINUTES"
    HOURS = "HOURS"
    DAYS = "DAYS"
    WEEKS = "WEEKS"


HALF_LIFE_SECONDS = {
    HalfLife.SECONDS: 30, HalfLife.MINUTES: 20 * 60, HalfLife.HOURS: 3 * 3600,
    HalfLife.DAYS: 3 * 86400, HalfLife.WEEKS: 10 * 86400,
}


IST = dt.timezone(dt.timedelta(hours=5, minutes=30), name="IST")

_seq = itertools.count(1)


def new_id(prefix: str) -> str:
    """Short, sortable, unique ids (prefix-counter-random) for logs and the UI."""
    return f"{prefix}-{next(_seq):05d}-{uuid.uuid4().hex[:6]}"


@dataclass(frozen=True)
class Instrument:
    key: str                      # canonical key, e.g. "NSE:RELIANCE", "NFO:NIFTY:2026-09-29:25000:CE"
    symbol: str                   # display symbol
    name: str
    kind: Kind
    exchange: Exchange
    lot_size: int = 1
    tick_size: float = 0.05
    underlying: str | None = None  # canonical key of the underlying
    expiry: dt.date | None = None
    strike: float | None = None
    option_type: OptionType | None = None
    sector: str = ""
    beta: float = 1.0
    freeze_qty: int | None = None
    dhan_security_id: str | None = None
    upstox_key: str | None = None

    @property
    def is_option(self) -> bool:
        return self.kind is Kind.OPTION

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["expiry"] = self.expiry.isoformat() if self.expiry else None
        return d


@dataclass
class Quote:
    """Normalised top-of-book (A5.5 rule 1: every record carries source, exchange_ts, receive_ts)."""
    key: str
    ltp: float
    bid: float
    ask: float
    volume: float
    oi: float
    exchange_ts: dt.datetime
    receive_ts: dt.datetime
    source: Source
    prev_close: float = 0.0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0

    @property
    def mid(self) -> float:
        if self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2
        return self.ltp

    @property
    def spread_bps(self) -> float:
        m = self.mid
        return (self.ask - self.bid) / m * 1e4 if m > 0 and self.ask >= self.bid > 0 else 1e9


@dataclass
class Candle:
    key: str
    start: dt.datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    oi: float = 0.0
    t: int = 0            # cached epoch seconds of `start` (hot path in resampling)

    def __post_init__(self) -> None:
        if not self.t:
            self.t = int(self.start.timestamp())

    def to_dict(self) -> dict[str, Any]:
        return {"t": self.t, "o": round(self.open, 2), "h": round(self.high, 2),
                "l": round(self.low, 2), "c": round(self.close, 2), "v": round(self.volume, 0)}


@dataclass
class Confirmation:
    """S4: an independent confirmation. Two confirmations on the same factor count as one (A9.2)."""
    name: str
    factor: str        # e.g. "volume", "flow", "breadth", "trend", "oi", "volatility"
    fired: bool
    detail: str = ""


@dataclass
class HypothesisCard:
    """A7.2 -- required for every strategy."""
    strategy: str
    version: str
    why: str
    counterparty: str
    not_arbitraged_because: str
    works_when: str
    fails_when: str
    decay: str
    holding_period: str
    data_needed: str
    kill_criteria: str
    validation_status: str = "UNVALIDATED"   # RESEARCH_CANDIDATE .. LIVE-FULL


@dataclass
class Candidate:
    """A setup emitted by a strategy (S1 SCAN). Everything downstream is decided by the funnel."""
    id: str
    ts: dt.datetime
    strategy: str
    strategy_version: str
    instrument: Instrument          # what would be traded (stock, option...)
    underlying_key: str
    direction: Direction
    segment: Segment
    entry: float
    stop: float
    target_r: float                 # expected win in R if it works (before costs)
    p_win: float                    # probability estimate for a win
    p_win_calibrated: bool          # False until Phase 5 calibration exists
    half_life: HalfLife
    trigger_fired: bool
    confirmations: list[Confirmation] = field(default_factory=list)
    quality_features: dict[str, float] = field(default_factory=dict)
    regime_fit: bool = True
    defined_risk: bool = False
    setup_id: str = ""              # stable id of the setup (for re-entry rules)
    signal_bar: str = ""            # bar identity of the trigger (fresh-trigger rule)
    is_reentry: bool = False
    sleeve: bool = False            # Moonshot / Zero-to-Hero
    explanation: str = ""
    model_confidence: float = 0.5
    regime_confidence: float = 0.5
    bias: Direction | None = None   # underlying exposure (a bought PE is LONG the option, SHORT the index)
    liquidity_score: float = 0.5
    atr: float = 0.0                # instrument ATR (price terms) for trailing
    beta: float = 1.0

    @property
    def exposure(self) -> Direction:
        return self.bias or self.direction

    @property
    def unit_risk(self) -> float:
        return abs(self.entry - self.stop)

    def independent_confirmations(self) -> int:
        return len({c.factor for c in self.confirmations if c.fired})


@dataclass
class Leg:
    qty: int
    price: float
    ts: dt.datetime
    stage: PositionStage


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)
