"""Single control surface used by Telegram, the HTTP API and the CLI."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from .execution.executor import new_client_id
from .models import BotStatus

if TYPE_CHECKING:
    from .engine import Engine

log = logging.getLogger(__name__)


class ControlService:
    def __init__(self, engine: "Engine"):
        self.engine = engine
        self.store = engine.store
        self._panic_lock = asyncio.Lock()

    def status(self) -> dict[str, Any]:
        s = self.store
        positions = s.open_positions()
        last_eq = s.rows("SELECT * FROM equity ORDER BY ts DESC LIMIT 1")
        return {
            "status": s.status.value,
            "status_reason": s.get("bot_status_reason", ""),
            "status_changed_at": s.get("bot_status_changed_at"),
            "mode": "paper" if self.engine.ex.is_paper else "live",
            "quote": self.engine.cfg.quote,
            "timeframe": self.engine.cfg.timeframe,
            "last_cycle_at": self.engine.last_cycle_at,
            "last_error": self.engine.last_error,
            "open_positions": len(positions),
            "equity": last_eq[0]["equity"] if last_eq else None,
            "quote_free": last_eq[0]["quote_free"] if last_eq else None,
            "exposure": last_eq[0]["exposure"] if last_eq else None,
            "day_pnl": self.engine.last_day_pnl,
            "universe": sorted(self.engine.universe),
            "server_time": time.time(),
        }

    async def pause(self, actor: str, reason: str = "manual") -> dict[str, Any]:
        self.store.set_status(BotStatus.PAUSED, reason)
        self.store.audit(actor, "pause", reason)
        await self.engine.notify.send(f"⏸️ Bot PAUSED by {actor}: {reason}. Open positions keep their stop-loss.")
        return self.status()

    async def resume(self, actor: str) -> dict[str, Any]:
        # Fresh start for the losing-streak breaker; the daily-loss rule still applies for the rest of the day.
        self.store.set("loss_streak_since", time.time())
        self.store.set_status(BotStatus.RUNNING, f"resumed by {actor}")
        self.store.audit(actor, "resume", "")
        mode = "PAPER" if self.engine.ex.is_paper else "LIVE ⚠️ real money"
        await self.engine.notify.send(f"▶️ Bot RUNNING ({mode}) — resumed by {actor}")
        return self.status()

    async def panic(self, actor: str) -> dict[str, Any]:
        """Pause, cancel every open order, market-sell positions, verify, report."""
        async with self._panic_lock:
            # 1. Persist PAUSED first so a crash/restart mid-panic never resumes trading.
            self.store.set_status(BotStatus.PAUSED, f"PANIC by {actor}")
            self.store.audit(actor, "panic", "started")
            await self.engine.notify.send(f"🚨 PANIC by {actor}: cancelling orders and selling positions…")

            # 2. Wait (bounded) for the current engine cycle to finish so we don't race it.
            acquired = False
            try:
                await asyncio.wait_for(self.engine.cycle_lock.acquire(), timeout=30)
                acquired = True
            except asyncio.TimeoutError:
                log.warning("panic: engine cycle still busy after 30s, proceeding anyway")
            try:
                report = await self._liquidate()
            finally:
                if acquired:
                    self.engine.cycle_lock.release()

            self.store.audit(actor, "panic", str(report))
            lines = [f"🚨 PANIC done. Bot stays PAUSED until you /resume.",
                     f"cancelled orders: {report['cancelled']}",
                     f"sold: {', '.join(report['sold']) or '-'}"]
            if report["errors"]:
                lines.append("ERRORS: " + "; ".join(report["errors"]))
            if report["leftover"]:
                lines.append("still holding: " + ", ".join(f"{k} {v:.8g}" for k, v in report["leftover"].items()))
            await self.engine.notify.send("\n".join(lines))
            return report

    async def _liquidate(self) -> dict[str, Any]:
        eng, ex = self.engine, self.engine.ex
        quote = eng.cfg.quote
        report: dict[str, Any] = {"cancelled": 0, "sold": [], "errors": [], "leftover": {}}
        if not eng.markets:
            try:
                await eng.refresh_markets()
            except Exception as e:  # noqa: BLE001
                report["errors"].append(f"load markets: {e}")

        positions = self.store.open_positions()
        symbols = {p.symbol for p in positions}
        symbols |= {r["symbol"] for r in self.store.orders_with_status("open", "pending", "unknown")}
        balance = None
        try:
            balance = await ex.fetch_balance()
        except Exception as e:  # noqa: BLE001
            report["errors"].append(f"balance: {e}")
        if eng.cfg.panic_scope == "all_non_quote" and balance:
            for asset, amt in balance.total.items():
                if asset != quote and amt > 0 and f"{asset}/{quote}" in eng.markets:
                    symbols.add(f"{asset}/{quote}")

        # 3. Cancel every open order on every relevant symbol (no account-wide cancel on Tokocrypto).
        for sym in sorted(symbols):
            try:
                for o in await ex.fetch_open_orders(sym):
                    await ex.cancel_order(o.id, sym)
                    self.store.update_order_by_exchange_id(o.id, status="canceled")
                    report["cancelled"] += 1
            except Exception as e:  # noqa: BLE001
                report["errors"].append(f"cancel {sym}: {e}")

        # 4. Close bot positions.
        for pos in positions:
            try:
                pos.stop_order_id = None  # already cancelled above
                pnl = await eng.executor.close_position(pos, "panic", purpose="panic")
                if pnl is not None:
                    report["sold"].append(f"{pos.symbol} ({pnl:+.2f})")
                else:
                    report["errors"].append(f"sell {pos.symbol} failed")
            except Exception as e:  # noqa: BLE001
                report["errors"].append(f"sell {pos.symbol}: {e}")

        # 5. Optionally sell everything else that isn't the quote currency.
        if eng.cfg.panic_scope == "all_non_quote":
            try:
                balance = await ex.fetch_balance()
                for asset, free in balance.free.items():
                    sym = f"{asset}/{quote}"
                    if asset == quote or free <= 0 or sym not in eng.markets:
                        continue
                    t = await ex.fetch_ticker(sym)
                    m = eng.markets[sym]
                    amount = ex.amount_to_precision(sym, free)
                    if amount <= 0 or amount * t.bid < max(m.min_cost, m.min_amount * t.bid):
                        continue
                    await ex.market_sell(sym, amount, new_client_id("p"))
                    report["sold"].append(sym)
            except Exception as e:  # noqa: BLE001
                report["errors"].append(f"sell non-quote: {e}")

        # 6. Verify.
        try:
            balance = await ex.fetch_balance()
            for asset, amt in balance.total.items():
                if asset == quote or amt <= 0:
                    continue
                sym = f"{asset}/{quote}"
                tracked = eng.cfg.panic_scope == "all_non_quote" or sym in symbols
                if tracked and sym in eng.markets:
                    report["leftover"][asset] = amt
        except Exception as e:  # noqa: BLE001
            report["errors"].append(f"verify: {e}")
        return report
