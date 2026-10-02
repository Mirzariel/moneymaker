"""Paper trading: real market data, simulated orders and balances.

Tokocrypto has no testnet, so this is the only safe way to run the full bot end to end.
State is persisted to a JSON file so a restart behaves like a real account would.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import asdict
from pathlib import Path

from ..models import Balance, Candle, Market, Order, Ticker
from .base import ExchangeClient, MarketData


class PaperExchange(ExchangeClient):
    is_paper = True

    def __init__(self, data: MarketData, quote: str, starting_quote: float, buy_fee_pct: float,
                 sell_fee_pct: float, slippage_pct: float, state_path: str | Path | None = None):
        self.data = data
        self.buy_fee = buy_fee_pct / 100
        self.sell_fee = sell_fee_pct / 100
        self.slip = slippage_pct / 100
        self.state_path = Path(state_path) if state_path else None
        self.balances: dict[str, float] = {quote: starting_quote}
        self.orders: dict[str, Order] = {}
        self._markets: dict[str, Market] = {}
        self._load()

    # ---- persistence -------------------------------------------------------------------
    def _load(self) -> None:
        if self.state_path and self.state_path.exists():
            s = json.loads(self.state_path.read_text())
            self.balances = {k: float(v) for k, v in s["balances"].items()}
            self.orders = {k: Order(**v) for k, v in s["orders"].items()}

    def _save(self) -> None:
        if not self.state_path:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"balances": self.balances,
                                   "orders": {k: asdict(v) for k, v in self.orders.items()}}))
        tmp.replace(self.state_path)

    # ---- market data passthrough --------------------------------------------------------
    async def load_markets(self) -> dict[str, Market]:
        self._markets = await self.data.load_markets()
        return self._markets

    async def fetch_tickers(self, symbols: list[str] | None = None) -> dict[str, Ticker]:
        return await self.data.fetch_tickers(symbols)

    async def fetch_ticker(self, symbol: str) -> Ticker:
        return await self.data.fetch_ticker(symbol)

    async def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int, since: int | None = None) -> list[Candle]:
        return await self.data.fetch_ohlcv(symbol, timeframe, limit, since)

    def amount_to_precision(self, symbol: str, amount: float) -> float:
        return self.data.amount_to_precision(symbol, amount)

    def price_to_precision(self, symbol: str, price: float) -> float:
        return self.data.price_to_precision(symbol, price)

    async def close(self) -> None:
        await self.data.close()

    # ---- helpers ---------------------------------------------------------------------
    @staticmethod
    def _split(symbol: str) -> tuple[str, str]:
        base, quote = symbol.split("/")
        return base, quote

    def _locked(self, asset: str) -> float:
        return sum(o.amount - o.filled for o in self.orders.values()
                   if o.status == "open" and o.side == "sell" and self._split(o.symbol)[0] == asset)

    def _add(self, asset: str, delta: float) -> None:
        self.balances[asset] = self.balances.get(asset, 0.0) + delta
        if abs(self.balances[asset]) < 1e-12:
            self.balances[asset] = 0.0

    async def _trigger_stops(self, symbol: str | None = None) -> None:
        """Fill resting stop-limit sells whose trigger has been hit and whose limit is still reachable."""
        changed = False
        for o in self.orders.values():
            if o.status != "open" or o.type != "stop_limit" or (symbol and o.symbol != symbol):
                continue
            t = await self.data.fetch_ticker(o.symbol)
            if t.last > (o.stop_price or 0):
                continue
            fill_price = t.bid * (1 - self.slip)
            if fill_price < (o.price or 0):
                continue  # gapped through the limit: stays open, exactly like a real stop-limit
            fill_price = max(fill_price, o.price or 0)
            base, quote = self._split(o.symbol)
            gross = o.amount * fill_price
            fee = gross * self.sell_fee
            self._add(base, -o.amount)
            self._add(quote, gross - fee)
            o.status, o.filled, o.average, o.cost = "closed", o.amount, fill_price, gross
            o.fee_cost, o.fee_currency = fee, quote
            changed = True
        if changed:
            self._save()

    # ---- account -----------------------------------------------------------------------
    async def fetch_balance(self) -> Balance:
        await self._trigger_stops()
        total = {k: v for k, v in self.balances.items() if v > 0}
        free = {k: v - self._locked(k) for k, v in total.items()}
        return Balance(free=free, total=total)

    async def market_buy(self, symbol: str, cost: float, client_id: str) -> Order:
        base, quote = self._split(symbol)
        if cost <= 0 or self.balances.get(quote, 0.0) + 1e-9 < cost:
            raise ValueError(f"paper: insufficient {quote} for cost {cost}")
        t = await self.data.fetch_ticker(symbol)
        price = t.ask * (1 + self.slip)
        gross_amount = cost / price
        fee = gross_amount * self.buy_fee  # Binance-style: buy fee charged in base asset
        amount = self.amount_to_precision(symbol, gross_amount - fee)
        self._add(quote, -cost)
        self._add(base, amount)
        o = Order(id=uuid.uuid4().hex[:12], client_id=client_id, symbol=symbol, side="buy", type="market",
                  amount=gross_amount, status="closed", filled=gross_amount, average=price, cost=cost,
                  fee_cost=fee, fee_currency=base)
        self.orders[o.id] = o
        self._save()
        return o

    async def market_sell(self, symbol: str, amount: float, client_id: str) -> Order:
        base, quote = self._split(symbol)
        free = self.balances.get(base, 0.0) - self._locked(base)
        if amount <= 0 or free + 1e-12 < amount:
            raise ValueError(f"paper: insufficient free {base} ({free}) to sell {amount}")
        t = await self.data.fetch_ticker(symbol)
        price = t.bid * (1 - self.slip)
        gross = amount * price
        fee = gross * self.sell_fee
        self._add(base, -amount)
        self._add(quote, gross - fee)
        o = Order(id=uuid.uuid4().hex[:12], client_id=client_id, symbol=symbol, side="sell", type="market",
                  amount=amount, status="closed", filled=amount, average=price, cost=gross,
                  fee_cost=fee, fee_currency=quote)
        self.orders[o.id] = o
        self._save()
        return o

    async def stop_loss_limit_sell(self, symbol: str, amount: float, stop_price: float, limit_price: float,
                                   client_id: str) -> Order:
        base, _ = self._split(symbol)
        free = self.balances.get(base, 0.0) - self._locked(base)
        if amount <= 0 or free + 1e-12 < amount:
            raise ValueError(f"paper: insufficient free {base} ({free}) for stop of {amount}")
        o = Order(id=uuid.uuid4().hex[:12], client_id=client_id, symbol=symbol, side="sell", type="stop_limit",
                  amount=amount, status="open", price=limit_price, stop_price=stop_price)
        self.orders[o.id] = o
        self._save()
        await self._trigger_stops(symbol)
        return o

    async def cancel_order(self, order_id: str, symbol: str) -> None:
        o = self.orders.get(order_id)
        if o and o.status == "open":
            o.status = "canceled"
            self._save()

    async def fetch_order(self, order_id: str, symbol: str) -> Order:
        await self._trigger_stops(symbol)
        if order_id not in self.orders:
            raise KeyError(f"paper: order {order_id} not found")
        return self.orders[order_id]

    async def fetch_open_orders(self, symbol: str) -> list[Order]:
        await self._trigger_stops(symbol)
        return [o for o in self.orders.values() if o.status == "open" and o.symbol == symbol]


class StaticMarketData(MarketData):
    """Hand-controlled market data for tests and offline runs."""

    def __init__(self, markets: dict[str, Market] | None = None):
        self.markets = markets or {}
        self.tickers: dict[str, Ticker] = {}
        self.candles: dict[str, list[Candle]] = {}

    def set_price(self, symbol: str, last: float, spread_pct: float = 0.1, quote_volume: float = 5_000_000) -> None:
        half = last * spread_pct / 200
        self.tickers[symbol] = Ticker(symbol, bid=last - half, ask=last + half, last=last, quote_volume=quote_volume)

    async def load_markets(self) -> dict[str, Market]:
        return self.markets

    async def fetch_tickers(self, symbols: list[str] | None = None) -> dict[str, Ticker]:
        return {k: v for k, v in self.tickers.items() if symbols is None or k in symbols}

    async def fetch_ticker(self, symbol: str) -> Ticker:
        return self.tickers[symbol]

    async def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int, since: int | None = None) -> list[Candle]:
        return self.candles.get(symbol, [])[-limit:]

