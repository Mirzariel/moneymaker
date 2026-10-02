"""Exchange interface. Live (Tokocrypto via CCXT) and paper trading both implement it."""
from __future__ import annotations

import math
from abc import ABC, abstractmethod

from ..models import Balance, Candle, Market, Order, Ticker


class MarketData(ABC):
    """Public, read-only market data."""

    @abstractmethod
    async def load_markets(self) -> dict[str, Market]: ...

    @abstractmethod
    async def fetch_tickers(self, symbols: list[str] | None = None) -> dict[str, Ticker]:
        """Tickers for `symbols`, or for every market when None (may be slow)."""

    @abstractmethod
    async def fetch_ticker(self, symbol: str) -> Ticker: ...

    @abstractmethod
    async def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int, since: int | None = None) -> list[Candle]: ...

    def amount_to_precision(self, symbol: str, amount: float) -> float:
        """Round an amount DOWN to what the exchange accepts. Default: 8 decimals."""
        return math.floor(amount * 1e8) / 1e8

    def price_to_precision(self, symbol: str, price: float) -> float:
        return float(f"{price:.8g}")

    def set_volume_hint(self, symbol: str, quote_volume_24h: float) -> None:  # pragma: no cover - optional
        return None

    async def fetch_ask_depth(self, symbol: str, range_pct: float) -> float:
        """Quote value of asks within range_pct of the best ask. Default: unknown = unlimited."""
        return float("inf")

    async def close(self) -> None:  # pragma: no cover - default no-op
        return None


class ExchangeClient(MarketData):
    """Market data plus account and order operations."""

    is_paper: bool = False

    @abstractmethod
    async def fetch_balance(self) -> Balance: ...

    @abstractmethod
    async def market_buy(self, symbol: str, cost: float, client_id: str) -> Order:
        """Spend `cost` units of quote currency at market."""

    @abstractmethod
    async def market_sell(self, symbol: str, amount: float, client_id: str) -> Order: ...

    @abstractmethod
    async def stop_loss_limit_sell(self, symbol: str, amount: float, stop_price: float, limit_price: float,
                                   client_id: str) -> Order:
        """Resting sell that activates when price trades at or below `stop_price`."""

    @abstractmethod
    async def cancel_order(self, order_id: str, symbol: str) -> None: ...

    @abstractmethod
    async def fetch_order(self, order_id: str, symbol: str) -> Order: ...

    @abstractmethod
    async def fetch_open_orders(self, symbol: str) -> list[Order]:
        """Tokocrypto requires a symbol here; there is no account-wide open-orders call."""
