"""Tokocrypto via CCXT (async)."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, TypeVar

import ccxt.async_support as ccxt

from ..models import Balance, Candle, Market, Order, Ticker
from .base import ExchangeClient

log = logging.getLogger(__name__)
T = TypeVar("T")

_STATUS_MAP = {"open": "open", "closed": "closed", "canceled": "canceled", "canceling": "canceled",
               "rejected": "rejected", "expired": "expired"}


def _f(x: Any, default: float = 0.0) -> float:
    try:
        return float(x) if x is not None else default
    except (TypeError, ValueError):
        return default


def parse_order(o: dict, fallback_client_id: str = "") -> Order:
    fee = o.get("fee") or {}
    stop_price = o.get("triggerPrice") or o.get("stopPrice")
    order_type = "stop_limit" if stop_price else (o.get("type") or "").lower()
    return Order(
        id=str(o.get("id")),
        client_id=o.get("clientOrderId") or fallback_client_id,
        symbol=o.get("symbol") or "",
        side=o.get("side") or "",
        type=order_type,
        amount=_f(o.get("amount")),
        status=_STATUS_MAP.get(str(o.get("status")), str(o.get("status"))),
        filled=_f(o.get("filled")),
        average=_f(o.get("average")) or _f(o.get("price")),
        cost=_f(o.get("cost")),
        price=o.get("price"),
        stop_price=_f(stop_price) or None,
        fee_cost=_f(fee.get("cost")),
        fee_currency=fee.get("currency") or "",
    )


class TokocryptoClient(ExchangeClient):
    """Thin wrapper: retries read-only calls; never retries order placement (reconcile handles that)."""

    def __init__(self, api_key: str = "", secret: str = "", read_retries: int = 3):
        self.ex = ccxt.tokocrypto({"apiKey": api_key, "secret": secret, "enableRateLimit": True})
        self.read_retries = read_retries
        self._markets: dict[str, Market] = {}

    async def _read(self, fn: Callable[[], Awaitable[T]]) -> T:
        delay = 1.0
        for attempt in range(self.read_retries + 1):
            try:
                return await fn()
            except (ccxt.NetworkError, ccxt.RateLimitExceeded) as e:
                if attempt == self.read_retries:
                    raise
                log.warning("exchange read failed (%s), retry in %.0fs", e, delay)
                await asyncio.sleep(delay)
                delay *= 2
        raise RuntimeError("unreachable")

    async def close(self) -> None:
        await self.ex.close()

    # ---- market data ----------------------------------------------------------------
    async def load_markets(self) -> dict[str, Market]:
        raw = await self._read(lambda: self.ex.load_markets(reload=True))
        out: dict[str, Market] = {}
        for sym, m in raw.items():
            if not m.get("spot"):
                continue
            limits = m.get("limits") or {}
            order_types = (m.get("info") or {}).get("orderTypes") or []
            out[sym] = Market(
                symbol=sym,
                base=m["base"],
                quote=m["quote"],
                min_amount=_f((limits.get("amount") or {}).get("min")),
                min_cost=_f((limits.get("cost") or {}).get("min")),
                active=bool(m.get("active", True)),
                supports_stop_limit="STOP_LOSS_LIMIT" in order_types if order_types else False,
            )
        self._markets = out
        return out

    async def fetch_tickers(self) -> dict[str, Ticker]:
        # Only Binance-backed (type 1) markets have 24h stats; native ones are omitted by CCXT.
        raw = await self._read(lambda: self.ex.fetch_tickers())
        out = {}
        for sym, t in raw.items():
            out[sym] = Ticker(symbol=sym, bid=_f(t.get("bid")), ask=_f(t.get("ask")), last=_f(t.get("last")),
                              quote_volume=_f(t.get("quoteVolume")))
        return out

    async def fetch_ticker(self, symbol: str) -> Ticker:
        t = await self._read(lambda: self.ex.fetch_ticker(symbol))
        return Ticker(symbol=symbol, bid=_f(t.get("bid")), ask=_f(t.get("ask")), last=_f(t.get("last")),
                      quote_volume=_f(t.get("quoteVolume")))

    async def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int, since: int | None = None) -> list[Candle]:
        raw = await self._read(lambda: self.ex.fetch_ohlcv(symbol, timeframe, since=since, limit=limit))
        return [Candle(int(r[0]), _f(r[1]), _f(r[2]), _f(r[3]), _f(r[4]), _f(r[5])) for r in raw]

    def amount_to_precision(self, symbol: str, amount: float) -> float:
        return float(self.ex.amount_to_precision(symbol, amount))  # CCXT truncates (rounds down)

    def price_to_precision(self, symbol: str, price: float) -> float:
        return float(self.ex.price_to_precision(symbol, price))

    # ---- account -----------------------------------------------------------------------
    async def fetch_balance(self) -> Balance:
        b = await self._read(lambda: self.ex.fetch_balance())
        return Balance(free={k: _f(v) for k, v in (b.get("free") or {}).items()},
                       total={k: _f(v) for k, v in (b.get("total") or {}).items()})

    async def market_buy(self, symbol: str, cost: float, client_id: str) -> Order:
        o = await self.ex.create_order(symbol, "market", "buy", None, None,
                                       {"cost": cost, "clientOrderId": client_id})
        return await self._complete(o, symbol, client_id)

    async def market_sell(self, symbol: str, amount: float, client_id: str) -> Order:
        o = await self.ex.create_order(symbol, "market", "sell", amount, None, {"clientOrderId": client_id})
        return await self._complete(o, symbol, client_id)

    async def stop_loss_limit_sell(self, symbol: str, amount: float, stop_price: float, limit_price: float,
                                   client_id: str) -> Order:
        o = await self.ex.create_order(symbol, "limit", "sell", amount, limit_price,
                                       {"triggerPrice": stop_price, "clientOrderId": client_id})
        return parse_order(o, client_id)

    async def _complete(self, raw: dict, symbol: str, client_id: str) -> Order:
        """Market orders may come back without fill details; fetch them once more."""
        order = parse_order(raw, client_id)
        if order.status != "closed" or order.filled <= 0:
            await asyncio.sleep(1.0)
            order = await self.fetch_order(order.id, symbol)
            order.client_id = order.client_id or client_id
        return order

    async def cancel_order(self, order_id: str, symbol: str) -> None:
        try:
            await self.ex.cancel_order(order_id, symbol)
        except ccxt.OrderNotFound:
            log.info("cancel %s: already gone", order_id)

    async def fetch_order(self, order_id: str, symbol: str) -> Order:
        o = await self._read(lambda: self.ex.fetch_order(order_id, symbol))
        return parse_order(o)

    async def fetch_open_orders(self, symbol: str) -> list[Order]:
        raw = await self._read(lambda: self.ex.fetch_open_orders(symbol))
        return [parse_order(o) for o in raw]
