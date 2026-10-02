"""Plain data structures shared across modules."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class BotStatus(str, Enum):
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"


@dataclass(frozen=True)
class Market:
    symbol: str  # unified, e.g. "BTC/USDT"
    base: str
    quote: str
    min_amount: float = 0.0
    min_cost: float = 0.0
    active: bool = True
    supports_stop_limit: bool = True
    # Tokocrypto "native" market (possibly the IDR pairs; `moneymaker check` shows): no 24h ticker API, data comes from order book + klines.
    native: bool = False


@dataclass(frozen=True)
class Ticker:
    symbol: str
    bid: float
    ask: float
    last: float
    quote_volume: float = 0.0

    @property
    def spread_pct(self) -> float:
        if self.bid <= 0 or self.ask <= 0:
            return float("inf")
        mid = (self.bid + self.ask) / 2
        return (self.ask - self.bid) / mid * 100


@dataclass(frozen=True)
class Candle:
    ts: int  # open time, ms
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Order:
    id: str
    client_id: str
    symbol: str
    side: str  # buy | sell
    type: str  # market | stop_limit
    amount: float
    status: str  # open | closed | canceled | rejected | expired
    filled: float = 0.0
    average: float = 0.0
    cost: float = 0.0
    price: float | None = None
    stop_price: float | None = None
    fee_cost: float = 0.0
    fee_currency: str = ""


@dataclass
class Balance:
    free: dict[str, float] = field(default_factory=dict)
    total: dict[str, float] = field(default_factory=dict)

    def free_of(self, asset: str) -> float:
        return float(self.free.get(asset, 0.0) or 0.0)

    def total_of(self, asset: str) -> float:
        return float(self.total.get(asset, 0.0) or 0.0)


@dataclass
class Signal:
    symbol: str
    side: str  # only "buy" in v1 (spot, long only)
    entry: float
    stop: float
    take_profit: float
    created_at: float  # epoch seconds
    expires_at: float
    reason: str
    candle_ts: int = 0


@dataclass
class Position:
    id: int
    symbol: str
    amount: float
    entry_price: float
    cost: float
    stop_price: float
    take_profit: float
    stop_order_id: str | None
    opened_at: float
    status: str = "open"
    fees: float = 0.0
