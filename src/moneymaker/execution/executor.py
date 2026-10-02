"""Order execution. Every position gets a resting stop-loss on the exchange right after entry,
so it stays protected even if this process (or the laptop) dies.
"""
from __future__ import annotations

import logging
import uuid

import ccxt.async_support as ccxt

from ..config import BotConfig
from ..db import Store
from ..exchange.base import ExchangeClient
from ..models import BotStatus, Market, Order, Position, Signal
from ..notify.base import Notifier
from ..risk.engine import Decision

log = logging.getLogger(__name__)

# Errors where the exchange definitely did NOT accept the order.
_DEFINITE_REJECTIONS = (ccxt.InvalidOrder, ccxt.InsufficientFunds, ccxt.BadSymbol, ccxt.AuthenticationError,
                        ccxt.PermissionDenied, ValueError)


def new_client_id(prefix: str) -> str:
    # Tokocrypto/Binance accept up to 36 chars of [A-Za-z0-9_-].
    return f"mm{prefix}{uuid.uuid4().hex[:20]}"


class Executor:
    def __init__(self, ex: ExchangeClient, store: Store, notifier: Notifier, cfg: BotConfig):
        self.ex = ex
        self.store = store
        self.notify = notifier
        self.cfg = cfg
        self.markets: dict[str, Market] = {}

    # ---- helpers ---------------------------------------------------------------------
    def _record(self, cid: str, o: Order) -> None:
        self.store.update_order(cid, exchange_id=o.id, status=o.status, filled=o.filled, average=o.average,
                                fee=o.fee_cost, fee_currency=o.fee_currency, cost=o.cost or None)

    def _quote_fee(self, o: Order, quote: str) -> float:
        if o.fee_currency == quote:
            return o.fee_cost
        if o.fee_currency and o.fee_currency == o.symbol.split("/")[0]:
            return o.fee_cost * (o.average or 0)
        return 0.0  # fee paid in another asset (e.g. TKO); not deducted from this position

    def _net_proceeds(self, o: Order) -> float:
        quote = o.symbol.split("/")[1]
        gross = o.cost or o.filled * o.average
        return gross - (o.fee_cost if o.fee_currency == quote else 0.0)

    # ---- entry -------------------------------------------------------------------------
    async def open_position(self, sig: Signal, decision: Decision) -> int | None:
        base, quote = sig.symbol.split("/")
        cid = new_client_id("e")
        self.store.insert_order(client_id=cid, symbol=sig.symbol, side="buy", type="market", purpose="entry",
                                cost=decision.cost)
        try:
            o = await self.ex.market_buy(sig.symbol, decision.cost, cid)
        except _DEFINITE_REJECTIONS as e:
            self.store.update_order(cid, status="rejected", error=str(e)[:500])
            await self.notify.send(f"⚠️ Entry {sig.symbol} rejected by exchange: {e}")
            return None
        except Exception as e:  # noqa: BLE001 - outcome unknown (timeout etc.)
            self.store.update_order(cid, status="unknown", error=str(e)[:500])
            self.store.set_status(BotStatus.PAUSED, f"entry outcome unknown for {sig.symbol}")
            self.store.audit("system", "pause", f"entry outcome unknown {sig.symbol}: {e}")
            await self.notify.send(f"🛑 Entry {sig.symbol} outcome UNKNOWN ({e}). Bot PAUSED. "
                                   f"Check the exchange, then /resume.")
            return None
        self._record(cid, o)
        if o.filled <= 0:
            await self.notify.send(f"⚠️ Entry {sig.symbol} not filled (status {o.status})")
            return None

        received = o.filled - (o.fee_cost if o.fee_currency == base else 0.0)
        bal = await self.ex.fetch_balance()
        amount = self.ex.amount_to_precision(sig.symbol, min(received, bal.free_of(base)))
        cost = (o.cost or o.filled * o.average) + (o.fee_cost if o.fee_currency == quote else 0.0)
        avg = o.average or (cost / o.filled)
        pos_id = self.store.open_position(symbol=sig.symbol, amount=amount, entry_price=avg, cost=cost,
                                          stop_price=sig.stop, take_profit=sig.take_profit,
                                          fees=self._quote_fee(o, quote))
        self.store.update_order(cid, position_id=pos_id)
        self.store.audit("engine", "entry", f"{sig.symbol} pos={pos_id} amount={amount} avg={avg:.8g} cost={cost:.2f}")

        if not self.exchange_stop_supported(sig.symbol):
            await self.notify.send(
                f"🟢 BUY {sig.symbol} {amount:.8g} @ {avg:.8g} (cost {cost:.2f} {quote})\n"
                f"stop {sig.stop:.8g} (dijaga BOT — hanya aktif selama bot menyala) · target {sig.take_profit:.8g}\n"
                f"{sig.reason}"
            )
            return pos_id
        stop_id = await self.place_stop(pos_id, sig.symbol, amount, sig.stop)
        if stop_id is None:
            pos = self._position(pos_id)
            await self.notify.send(f"🛑 Could not place stop for {sig.symbol}; closing position immediately.")
            if pos:
                await self.close_position(pos, "stop_placement_failed")
            return None
        await self.notify.send(
            f"🟢 BUY {sig.symbol} {amount:.8g} @ {avg:.8g} (cost {cost:.2f} {quote})\n"
            f"stop {sig.stop:.8g} · target {sig.take_profit:.8g}\n{sig.reason}"
        )
        return pos_id

    def exchange_stop_supported(self, symbol: str) -> bool:
        m = self.markets.get(symbol)
        return m is None or m.supports_stop_limit

    def _position(self, pos_id: int) -> Position | None:
        return next((p for p in self.store.open_positions() if p.id == pos_id), None)

    async def place_stop(self, pos_id: int, symbol: str, amount: float, stop_price: float) -> str | None:
        stop_p = self.ex.price_to_precision(symbol, stop_price)
        limit_p = self.ex.price_to_precision(symbol, stop_price * (1 - self.cfg.risk.stop_limit_offset_pct / 100))
        cid = new_client_id("s")
        self.store.insert_order(client_id=cid, symbol=symbol, side="sell", type="stop_limit", purpose="stop",
                                position_id=pos_id, amount=amount, price=limit_p, stop_price=stop_p)
        try:
            o = await self.ex.stop_loss_limit_sell(symbol, amount, stop_p, limit_p, cid)
        except Exception as e:  # noqa: BLE001
            log.exception("stop placement failed")
            self.store.update_order(cid, status="failed", error=str(e)[:500])
            return None
        self._record(cid, o)
        self.store.set_position_stop(pos_id, o.id, stop_price)
        return o.id

    # ---- exit --------------------------------------------------------------------------
    async def finalize_stop_fill(self, pos: Position, stop: Order, reason: str = "stop_loss") -> float:
        proceeds = self._net_proceeds(stop)
        quote = pos.symbol.split("/")[1]
        pnl = self.store.close_position(pos.id, exit_price=stop.average, proceeds=proceeds,
                                        extra_fees=self._quote_fee(stop, quote), reason=reason)
        self.store.update_order_by_exchange_id(stop.id, status="closed", filled=stop.filled, average=stop.average,
                                               fee=stop.fee_cost, fee_currency=stop.fee_currency)
        self.store.audit("engine", "exit", f"{pos.symbol} pos={pos.id} {reason} pnl={pnl:.2f}")
        await self.notify.send(f"🔴 {reason.upper()} {pos.symbol} @ {stop.average:.8g} · PnL {pnl:+.2f} {quote}")
        return pnl

    async def close_position(self, pos: Position, reason: str, purpose: str = "exit") -> float | None:
        """Cancel the resting stop, then market-sell what the position holds."""
        base, quote = pos.symbol.split("/")
        extra_proceeds, extra_fees = 0.0, 0.0
        if pos.stop_order_id:
            try:
                await self.ex.cancel_order(pos.stop_order_id, pos.symbol)
                stop = await self.ex.fetch_order(pos.stop_order_id, pos.symbol)
            except Exception:  # noqa: BLE001
                log.exception("cancel/fetch stop failed for %s", pos.symbol)
                stop = None
            if stop is not None and stop.status == "closed":
                return await self.finalize_stop_fill(pos, stop, reason="stop_loss")  # stop won the race
            if stop is not None and stop.filled > 0:  # partially filled before cancel
                extra_proceeds = self._net_proceeds(stop)
                extra_fees = self._quote_fee(stop, quote)
            self.store.set_position_stop(pos.id, None)

        bal = await self.ex.fetch_balance()
        amount = self.ex.amount_to_precision(pos.symbol, min(pos.amount, bal.free_of(base)))
        ticker = await self.ex.fetch_ticker(pos.symbol)
        m = self.markets.get(pos.symbol)
        min_cost = max(m.min_cost, m.min_amount * ticker.bid) if m else 0.0
        if amount <= 0 or amount * ticker.bid < min_cost:
            pnl = self.store.close_position(pos.id, exit_price=ticker.bid,
                                            proceeds=extra_proceeds + max(amount, 0) * ticker.bid,
                                            extra_fees=extra_fees, reason=f"{reason}_dust")
            await self.notify.send(f"⚪ {pos.symbol} closed as dust/empty ({amount:.8g} left) · PnL {pnl:+.2f}")
            return pnl

        cid = new_client_id("x")
        self.store.insert_order(client_id=cid, symbol=pos.symbol, side="sell", type="market", purpose=purpose,
                                position_id=pos.id, amount=amount)
        try:
            o = await self.ex.market_sell(pos.symbol, amount, cid)
        except Exception as e:  # noqa: BLE001
            self.store.update_order(cid, status="failed", error=str(e)[:500])
            await self.notify.send(f"🛑 SELL {pos.symbol} FAILED: {e}. Position still open without stop!")
            # Try to restore protection.
            if reason != "stop_placement_failed":
                await self.place_stop(pos.id, pos.symbol, amount, pos.stop_price)
            return None
        self._record(cid, o)
        proceeds = extra_proceeds + self._net_proceeds(o)
        pnl = self.store.close_position(pos.id, exit_price=o.average, proceeds=proceeds,
                                        extra_fees=extra_fees + self._quote_fee(o, quote), reason=reason)
        self.store.audit("engine", "exit", f"{pos.symbol} pos={pos.id} {reason} pnl={pnl:.2f}")
        await self.notify.send(f"🔴 SELL {pos.symbol} {amount:.8g} @ {o.average:.8g} ({reason}) · "
                               f"PnL {pnl:+.2f} {quote}")
        return pnl

    # ---- keep each open position protected ---------------------------------------------
    async def sync_position(self, pos: Position) -> bool:
        """Returns True if the position is still open afterwards."""
        ticker = await self.ex.fetch_ticker(pos.symbol)
        limit_price = pos.stop_price * (1 - self.cfg.risk.stop_limit_offset_pct / 100)
        if pos.stop_order_id:
            try:
                stop = await self.ex.fetch_order(pos.stop_order_id, pos.symbol)
            except Exception:  # noqa: BLE001
                log.exception("fetch stop failed for %s", pos.symbol)
                return True
            if stop.status == "closed":
                await self.finalize_stop_fill(pos, stop)
                return False
            if stop.status == "open":
                if ticker.bid < limit_price:
                    # Price gapped through the stop-limit; it will not fill. Get out at market.
                    await self.close_position(pos, "stop_gap_fallback")
                    return False
                return True
            log.warning("stop for %s is %s; re-protecting", pos.symbol, stop.status)
            self.store.set_position_stop(pos.id, None)
            pos.stop_order_id = None

        # No live stop order.
        base = pos.symbol.split("/")[0]
        bal = await self.ex.fetch_balance()
        if bal.total_of(base) < pos.amount * 0.5:
            pnl = self.store.close_position(pos.id, exit_price=ticker.bid, proceeds=bal.total_of(base) * ticker.bid,
                                            extra_fees=0.0, reason="external_close")
            await self.notify.send(f"⚠️ {pos.symbol}: holdings gone without a bot order (sold manually?). "
                                   f"Marked closed, est. PnL {pnl:+.2f}")
            return False
        if not self.exchange_stop_supported(pos.symbol):
            # Bot-side stop: the bot itself sells when price reaches the stop.
            if ticker.bid <= pos.stop_price:
                await self.close_position(pos, "stop_loss_bot")
                return False
            return True
        if ticker.bid <= pos.stop_price:
            await self.close_position(pos, "stop_breached_unprotected")
            return False
        amount = self.ex.amount_to_precision(pos.symbol, min(pos.amount, bal.free_of(base)))
        if await self.place_stop(pos.id, pos.symbol, amount, pos.stop_price) is None:
            await self.notify.send(f"🛑 {pos.symbol}: failed to re-place stop. Closing.")
            await self.close_position(pos, "stop_placement_failed")
            return False
        return True
