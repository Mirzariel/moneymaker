"""Main trading loop: scanner -> strategy -> risk -> execution, plus position protection."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from .config import BotConfig
from .db import Store
from .exchange.base import ExchangeClient
from .execution.executor import Executor
from .execution.reconcile import reconcile
from .models import BotStatus, Market, Ticker
from .notify.base import Notifier
from .risk.engine import RiskContext, RiskEngine
from .strategy.base import TIMEFRAME_SECONDS, Strategy, closed_candles
from .strategy.trend_atr import build_strategy

log = logging.getLogger(__name__)


class Engine:
    def __init__(self, ex: ExchangeClient, store: Store, notifier: Notifier, cfg: BotConfig,
                 strategy: Strategy | None = None):
        self.ex = ex
        self.store = store
        self.notify = notifier
        self.cfg = cfg
        self.strategy = strategy or build_strategy(cfg.strategy)
        self.risk = RiskEngine(cfg.risk, cfg.fees, cfg.scanner.min_quote_volume_24h, cfg.scanner.max_spread_pct)
        self.executor = Executor(ex, store, notifier, cfg)
        self.cycle_lock = asyncio.Lock()
        self.markets: dict[str, Market] = {}
        self.universe: list[str] = []
        self._universe_at = 0.0
        self._last_eval: dict[str, int] = {}  # symbol -> last evaluated candle ts
        self.last_cycle_at: float | None = None
        self.last_error: str | None = None
        self.last_day_pnl: float | None = None
        self.last_prices: dict[str, float] = {}
        self._stop = asyncio.Event()
        self.reconciled = False
        self.tz = ZoneInfo(cfg.timezone)

    # ---- markets & universe ------------------------------------------------------------
    async def refresh_markets(self) -> None:
        self.markets = await self.ex.load_markets()
        self.executor.markets = self.markets

    async def refresh_universe(self, tickers: dict[str, Ticker]) -> list[str]:
        sc = self.cfg.scanner
        candidates = []
        for sym, m in self.markets.items():
            if m.quote != self.cfg.quote or not m.active or m.base in sc.exclude_bases:
                continue
            if sym in sc.blacklist or (sc.whitelist and sym not in sc.whitelist):
                continue
            if m.base.endswith(("UP", "DOWN", "BULL", "BEAR")):
                continue  # leveraged tokens
            if not m.supports_stop_limit and not self.cfg.risk.allow_bot_side_stop:
                continue  # no exchange-side stop possible
            t = tickers.get(sym)
            if not t or t.quote_volume < sc.min_quote_volume_24h or t.spread_pct > sc.max_spread_pct:
                continue
            candidates.append((t.quote_volume, sym))
        candidates.sort(reverse=True)
        self.universe = [s for _, s in candidates[: sc.max_symbols]]
        self._universe_at = time.time()
        return self.universe

    # ---- accounting --------------------------------------------------------------------
    def _day_start_ts(self, now: float) -> float:
        d = datetime.fromtimestamp(now, self.tz)
        return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    async def snapshot(self, tickers: dict[str, Ticker]) -> RiskContext:
        now = time.time()
        bal = await self.ex.fetch_balance()
        positions = self.store.open_positions()
        exposure, unrealized = 0.0, 0.0
        for p in positions:
            t = tickers.get(p.symbol) or await self.ex.fetch_ticker(p.symbol)
            value = p.amount * t.bid
            exposure += value
            unrealized += value - p.cost
        quote_total = bal.total_of(self.cfg.quote)
        equity = quote_total + exposure
        self.store.record_equity(equity, bal.free_of(self.cfg.quote), exposure)

        day_start = self._day_start_ts(now)
        key = f"day_start_equity:{datetime.fromtimestamp(now, self.tz).date().isoformat()}"
        day_start_equity = self.store.get(key)
        if day_start_equity is None:
            day_start_equity = equity
            self.store.set(key, equity)
        # Day PnL = realized today + unrealized now (unrealized of positions opened before today is approximate).
        day_pnl = self.store.realized_pnl_since(day_start) + unrealized
        self.last_day_pnl = day_pnl
        streak_since = self.store.get("loss_streak_since", 0.0)
        return RiskContext(
            now=now, status=self.store.status, equity=equity, quote_free=bal.free_of(self.cfg.quote),
            exposure=exposure, open_symbols={p.symbol for p in positions}, day_start_equity=day_start_equity,
            day_pnl=day_pnl, consecutive_losses=self.store.consecutive_losses(streak_since),
        )

    # ---- one cycle -----------------------------------------------------------------------
    async def cycle(self) -> None:
        async with self.cycle_lock:
            self.last_error = None
            if not self.markets:
                await self.refresh_markets()
            running = self.store.status == BotStatus.RUNNING
            if running and (not self.universe
                            or time.time() - self._universe_at > self.cfg.scanner.refresh_minutes * 60):
                wl = self.cfg.scanner.whitelist
                await self.refresh_universe(await self.ex.fetch_tickers(list(wl) if wl else None))
            needed = set(self.universe) | {p.symbol for p in self.store.open_positions()}
            tickers = await self.ex.fetch_tickers(sorted(needed)) if needed else {}
            self.last_prices = {s: t.last for s, t in tickers.items()}

            # 1. Protect / exit open positions (runs even when PAUSED).
            await self.manage_positions(tickers)

            # 2. Account snapshot + circuit breakers.
            ctx = await self.snapshot(tickers)
            if ctx.status == BotStatus.RUNNING:
                reason = self.risk.circuit_breaker(ctx)
                if reason:
                    self.store.set_status(BotStatus.PAUSED, f"circuit breaker: {reason}")
                    self.store.risk_event("circuit_breaker", reason, "pause")
                    self.store.audit("risk", "pause", reason)
                    await self.notify.send(f"🛑 Auto-PAUSED: {reason}. Positions keep their stops. /resume to continue.")
                    return

            if self.store.status != BotStatus.RUNNING:
                return

            # 3. Scan for new entries.
            await self.scan_entries(tickers, ctx)

    async def manage_positions(self, tickers: dict[str, Ticker]) -> None:
        for pos in self.store.open_positions():
            try:
                still_open = await self.executor.sync_position(pos)
                if not still_open:
                    continue
                t = await self.ex.fetch_ticker(pos.symbol)
                if t.bid >= pos.take_profit:
                    await self.executor.close_position(pos, "take_profit")
                    continue
                candles = await self.ex.fetch_ohlcv(pos.symbol, self.cfg.timeframe, self.strategy.min_candles + 5)
                reason = self.strategy.should_exit(closed_candles(candles, self.cfg.timeframe, time.time()))
                if reason:
                    await self.executor.close_position(pos, reason)
            except Exception as e:  # noqa: BLE001
                log.exception("manage %s failed", pos.symbol)
                self.last_error = f"manage {pos.symbol}: {e}"

    async def scan_entries(self, tickers: dict[str, Ticker], ctx: RiskContext) -> None:
        tf_ms = TIMEFRAME_SECONDS[self.cfg.timeframe] * 1000
        for sym in self.universe:
            if self.store.status != BotStatus.RUNNING:
                return  # paused mid-scan (panic / breaker)
            if sym in ctx.open_symbols:
                continue
            try:
                candles = await self.ex.fetch_ohlcv(sym, self.cfg.timeframe, self.strategy.min_candles + 5)
                now = time.time()
                closed = closed_candles(candles, self.cfg.timeframe, now)
                if not closed or self._last_eval.get(sym) == closed[-1].ts:
                    continue  # nothing new since last look
                self._last_eval[sym] = closed[-1].ts
                if now - (closed[-1].ts + tf_ms) / 1000 > TIMEFRAME_SECONDS[self.cfg.timeframe]:
                    continue  # stale data feed
                sig = self.strategy.entry_signal(sym, closed, now)
                if sig is None:
                    continue
                ctx.now = time.time()
                ctx.status = self.store.status
                ctx.ticker = await self.ex.fetch_ticker(sym)
                ctx.market = self.markets.get(sym)
                ctx.last_entry_ts = self.store.last_entry_time(sym)
                decision = self.risk.evaluate(sig, ctx)
                if not decision.approved:
                    self.store.record_signal(sig, "rejected", f"{decision.rule}: {decision.detail}")
                    self.store.risk_event(decision.rule, decision.detail, "reject", sym)
                    continue
                for note in decision.notes:
                    self.store.risk_event("resize", note, "resize", sym)
                self.store.record_signal(sig, "approved", decision.detail)
                pos_id = await self.executor.open_position(sig, decision)
                if pos_id is not None:
                    ctx = await self.snapshot(tickers)  # refresh exposure/equity for the next candidate
            except Exception as e:  # noqa: BLE001
                log.exception("scan %s failed", sym)
                self.last_error = f"scan {sym}: {e}"

    # ---- loop ------------------------------------------------------------------------------
    async def run(self) -> None:
        log.info("engine loop started (interval %ss)", self.cfg.loop_interval_sec)
        consecutive_failures = 0
        while not self._stop.is_set():
            try:
                if not self.reconciled:
                    # Retried every interval until it succeeds; no trading happens before that.
                    await reconcile(self)
                    self.reconciled = True
                await self.cycle()
                self.last_cycle_at = time.time()
                consecutive_failures = 0
            except Exception as e:  # noqa: BLE001
                consecutive_failures += 1
                self.last_error = f"cycle: {e}"
                log.exception("cycle failed (%d in a row)", consecutive_failures)
                if consecutive_failures in (3, 20, 100):
                    await self.notify.send(f"⚠️ Engine error x{consecutive_failures}: {e}")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.cfg.loop_interval_sec)
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()
