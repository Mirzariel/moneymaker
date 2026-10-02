"""Main trading loop: radar -> strategy (per sleeve) -> risk -> execution, plus position protection.

Request budget matters: Tokocrypto via CCXT allows roughly one request every 2 seconds. So a cycle only
fetches what changed: tickers for open positions, and candles for a symbol only after a new candle closed.
The market radar and the learning job run as background tasks and never hold the cycle lock.
"""
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
from .learning import ModelStore, Trainer, alts_strategy, majors_strategy
from .market_sweeper import MarketSweeper
from .models import BotStatus, Candle, Market, Ticker
from .notify.base import Notifier
from .risk.engine import RiskContext, RiskEngine
from .scanner import candidate_symbols
from .strategy.base import TIMEFRAME_SECONDS, Strategy, closed_candles

log = logging.getLogger(__name__)
SLEEVES = ("majors", "alts")


class Engine:
    def __init__(self, ex: ExchangeClient, store: Store, notifier: Notifier, cfg: BotConfig,
                 strategy: Strategy | None = None):
        self.ex = ex
        self.store = store
        self.notify = notifier
        self.cfg = cfg
        self.risk = RiskEngine(cfg.risk, cfg.fees, cfg.scanner.min_quote_volume_24h, cfg.scanner.max_spread_pct)
        self.executor = Executor(ex, store, notifier, cfg)
        self.cycle_lock = asyncio.Lock()
        self.markets: dict[str, Market] = {}
        self.sweeper = MarketSweeper(ex, cfg, lambda: self.markets)
        self.model = ModelStore(self)
        self.trainer = Trainer(self)
        self._fixed_strategy = strategy  # tests / manual override for the majors sleeve
        self.strategies: dict[str, Strategy] = {}
        self._candles: dict[tuple[str, str], tuple[float, list[Candle]]] = {}
        self._last_eval: dict[tuple[str, str], int] = {}
        self.last_cycle_at: float | None = None
        self.last_error: str | None = None
        self.last_day_pnl: float | None = None
        self.last_prices: dict[str, float] = {}
        self._stop = asyncio.Event()
        self.reconciled = False
        self.tz = ZoneInfo(cfg.timezone)
        self.apply_model()

    # ---- strategies / model ------------------------------------------------------------
    @property
    def strategy(self) -> Strategy:
        return self.strategies["majors"]

    def apply_model(self) -> None:
        m = self.model.load()
        use = self.cfg.learning.enabled
        self.strategies["majors"] = self._fixed_strategy or majors_strategy(
            self.cfg, m["majors"]["params"] if use else None,
            m["majors"]["timeframe"] if use and m["majors"]["params"] else self.cfg.timeframe)
        self.strategies["alts"] = alts_strategy(self.cfg, m["alts"]["params"] if use else None)

    def sleeve_block(self, sleeve: str) -> str | None:
        """Why a sleeve may not open new positions right now (None = allowed)."""
        if not getattr(self.cfg.sleeves, sleeve).enabled:
            return "kantong dinonaktifkan di config"
        lc = self.cfg.learning
        if self._fixed_strategy is None and lc.enabled and lc.require_validated_edge:
            st = self.model.load()[sleeve]
            if not st["valid"]:
                return st["reason"] or "model belum tervalidasi"
        return None

    def majors_candidates(self) -> list[str]:
        return candidate_symbols(self.markets, self.cfg)

    @property
    def universe(self) -> list[str]:
        return self.majors_candidates() + self.sweeper.top_alts(self.cfg.scanner.alt_candidates)

    # ---- markets & candles -------------------------------------------------------------
    async def refresh_markets(self) -> None:
        self.markets = await self.ex.load_markets()
        self.executor.markets = self.markets

    async def closed_candles_for(self, symbol: str, timeframe: str, n: int) -> list[Candle]:
        """Closed candles, re-fetched only when a newer candle should exist (saves rate-limit budget)."""
        tf_s = TIMEFRAME_SECONDS[timeframe]
        now = time.time()
        expected_last_open_ms = (int(now // tf_s) - 1) * tf_s * 1000
        cached = self._candles.get((symbol, timeframe))
        if cached is not None:
            fetched_at, candles = cached
            fresh = candles and candles[-1].ts >= expected_last_open_ms
            if len(candles) >= n and (fresh or now - fetched_at < 120):
                return candles
        candles = closed_candles(await self.ex.fetch_ohlcv(symbol, timeframe, n + 5), timeframe, now)
        self._candles[(symbol, timeframe)] = (now, candles)
        return candles

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
            tickers: dict[str, Ticker] = {}
            for p in self.store.open_positions():
                tickers[p.symbol] = await self.ex.fetch_ticker(p.symbol)
            self.last_prices = {s: st.last for s, st in self.sweeper.stats.items()}
            self.last_prices.update({s: t.last for s, t in tickers.items()})

            # 1. Protect / exit open positions (runs even when PAUSED).
            await self.manage_positions(tickers)

            # 2. Account snapshot + circuit breakers + live-performance monitor.
            ctx = await self.snapshot(tickers)
            if ctx.status == BotStatus.RUNNING:
                reason = self.risk.circuit_breaker(ctx)
                if reason:
                    self.store.set_status(BotStatus.PAUSED, f"circuit breaker: {reason}")
                    self.store.risk_event("circuit_breaker", reason, "pause")
                    self.store.audit("risk", "pause", reason)
                    await self.notify.send(f"🛑 Auto-PAUSED: {reason}. Positions keep their stops. /resume to continue.")
                    return
            await self.check_live_performance()

            if self.store.status != BotStatus.RUNNING:
                return

            # 3. Scan for new entries.
            await self.scan_entries(ctx)

    async def manage_positions(self, tickers: dict[str, Ticker]) -> None:
        for pos in self.store.open_positions():
            try:
                t = tickers.get(pos.symbol)
                still_open = await self.executor.sync_position(pos, t)
                if not still_open:
                    continue
                t = t or await self.ex.fetch_ticker(pos.symbol)
                if pos.trailing:
                    await self.executor.update_trailing(pos, t)
                    continue
                if t.bid >= pos.take_profit:
                    await self.executor.close_position(pos, "take_profit")
                    continue
                strat = self.strategies.get(pos.sleeve) or self.strategy
                candles = await self.closed_candles_for(pos.symbol, strat.timeframe, strat.min_candles + 5)
                reason = strat.should_exit(candles)
                if reason:
                    await self.executor.close_position(pos, reason)
            except Exception as e:  # noqa: BLE001
                log.exception("manage %s failed", pos.symbol)
                self.last_error = f"manage {pos.symbol}: {e}"

    async def check_live_performance(self) -> None:
        if self._fixed_strategy is not None or not self.cfg.learning.enabled:
            return
        for sleeve in SLEEVES:
            if self.model.load()[sleeve]["status"] != "ok":
                continue
            reason = self.model.degraded_reason(sleeve)
            if reason:
                self.model.set_status(sleeve, "degraded", f"Performa live menurun: {reason}")
                self.store.risk_event("model_degraded", f"{sleeve}: {reason}", "pause")
                await self.notify.send(f"⚠️ Kantong {sleeve} dijeda: {reason}. Model dilatih ulang.")
                self.trainer.start((sleeve,))

    async def scan_entries(self, ctx: RiskContext) -> None:
        regime = self.sweeper.regime or {}
        if regime.get("all_blocked"):
            return
        for sleeve in SLEEVES:
            if self.sleeve_block(sleeve):
                continue
            if sleeve == "alts" and (regime.get("alts_blocked") or not self.sweeper.sweeps_completed):
                continue
            strat = self.strategies[sleeve]
            symbols = self.majors_candidates() if sleeve == "majors" else \
                self.sweeper.top_alts(self.cfg.scanner.alt_candidates)
            for sym in symbols:
                if self.store.status != BotStatus.RUNNING:
                    return  # paused mid-scan (panic / breaker)
                if sym in ctx.open_symbols:
                    continue
                try:
                    ctx = await self._try_entry(sleeve, strat, sym, ctx)
                except Exception as e:  # noqa: BLE001
                    log.exception("scan %s failed", sym)
                    self.last_error = f"scan {sym}: {e}"

    async def _try_entry(self, sleeve: str, strat: Strategy, sym: str, ctx: RiskContext) -> RiskContext:
        tf = strat.timeframe
        tf_s = TIMEFRAME_SECONDS[tf]
        closed = await self.closed_candles_for(sym, tf, strat.min_candles + 5)
        if not closed or self._last_eval.get((sym, tf)) == closed[-1].ts:
            return ctx  # nothing new since last look
        self._last_eval[(sym, tf)] = closed[-1].ts
        now = time.time()
        if now - (closed[-1].ts / 1000 + tf_s) > tf_s:
            return ctx  # stale data feed
        sig = strat.entry_signal(sym, closed, now)
        if sig is None:
            return ctx
        sig.sleeve, sig.timeframe = sleeve, tf
        ctx.now = time.time()
        ctx.status = self.store.status
        ctx.ticker = await self.ex.fetch_ticker(sym)
        ctx.market = self.markets.get(sym)
        ctx.last_entry_ts = self.store.last_entry_time(sym)
        ctx.sleeve = getattr(self.cfg.sleeves, sleeve)
        ctx.sleeve_open = sum(1 for p in self.store.open_positions() if p.sleeve == sleeve)
        decision = self.risk.evaluate(sig, ctx)
        if decision.approved:
            f = self.cfg.alt_filters
            depth = await self.ex.fetch_ask_depth(sym, f.depth_range_pct)
            if depth < f.min_depth_multiple * decision.cost:
                decision.approved, decision.rule = False, "thin_order_book"
                decision.detail = (f"order book tipis: {depth:,.0f} dalam {f.depth_range_pct}% "
                                   f"< {f.min_depth_multiple}x order {decision.cost:,.0f}")
        if not decision.approved:
            self.store.record_signal(sig, "rejected", f"{decision.rule}: {decision.detail}")
            self.store.risk_event(decision.rule, decision.detail, "reject", sym)
            return ctx
        for note in decision.notes:
            self.store.risk_event("resize", note, "resize", sym)
        self.store.record_signal(sig, "approved", f"[{sleeve}] {decision.detail}")
        if await self.executor.open_position(sig, decision) is not None:
            return await self.snapshot({})  # refresh exposure/equity for the next candidate
        return ctx

    # ---- loop ------------------------------------------------------------------------------
    def next_training_at(self) -> float | None:
        if not self.cfg.learning.enabled:
            return None
        m = self.model.load()
        times = [m[s]["trained_at"] for s in SLEEVES if getattr(self.cfg.sleeves, s).enabled]
        if any(t is None for t in times):
            return time.time()
        return min(times) + self.cfg.learning.relearn_days * 86400 if times else None

    async def learning_loop(self) -> None:
        while not self._stop.is_set():
            if self.reconciled and self.cfg.learning.enabled and self._fixed_strategy is None \
                    and not self.trainer.running:
                m = self.model.load()
                due = tuple(s for s in SLEEVES if getattr(self.cfg.sleeves, s).enabled and (
                    m[s]["trained_at"] is None
                    or time.time() - m[s]["trained_at"] > self.cfg.learning.relearn_days * 86400))
                if due:
                    self.trainer.start(due)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=60 if self.reconciled else 5)
            except asyncio.TimeoutError:
                pass

    async def run(self) -> None:
        log.info("engine loop started (interval %ss)", self.cfg.loop_interval_sec)
        background = [asyncio.create_task(self.sweeper.run(self._stop), name="radar"),
                      asyncio.create_task(self.learning_loop(), name="learning")]
        consecutive_failures = 0
        try:
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
        finally:
            for t in background:
                t.cancel()
            if self.trainer._task is not None:
                self.trainer._task.cancel()

    def stop(self) -> None:
        self._stop.set()
