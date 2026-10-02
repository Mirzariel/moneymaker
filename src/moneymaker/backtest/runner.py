"""Candle backtester. Uses the live Strategy code (prepare/entry_at/exit_at), net of fees and slippage.

Conservative assumptions:
  * entries fill at the NEXT candle's open + slippage (no look-ahead)
  * if stop and take-profit are both touched inside one candle, the stop is assumed to hit first
  * a trailing stop is raised only after a candle closes (never inside the candle that set the high)
  * a gap below the stop-limit exits at that candle's open (mirrors the live market-sell fallback)
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from ..config import BotConfig
from ..exchange.base import MarketData
from ..exchange.tokocrypto import TokocryptoClient
from ..models import Candle
from ..strategy.base import TIMEFRAME_SECONDS, Indicators, Strategy
from ..strategy.trend_atr import build_strategy


@dataclass
class SimTrade:
    symbol: str
    entry_ts: int
    exit_ts: int
    entry: float
    exit: float
    ret: float      # net return per 1 unit of quote spent (fees + slippage included), e.g. 0.031 = +3.1%
    reason: str


@dataclass
class Costs:
    buy_fee: float
    sell_fee: float
    slip: float
    stop_offset: float
    min_edge: float  # target must be >= min_edge x round-trip fees

    @classmethod
    def from_cfg(cls, cfg: BotConfig) -> "Costs":
        return cls(cfg.fees.buy_pct / 100, cfg.fees.sell_pct / 100, cfg.paper.slippage_pct / 100,
                   cfg.risk.stop_limit_offset_pct / 100, cfg.risk.min_edge_fee_multiple)


def simulate(strategy: Strategy, ind: Indicators, candles: list[Candle], start: int, end: int, costs: Costs,
             symbol: str = "", entry_filter: Callable[[int], bool] | None = None) -> list[SimTrade]:
    """Walk candles[start:end]. `entry_filter(ts)` can veto entries (e.g. BTC regime)."""
    trades: list[SimTrade] = []
    rt_fee = costs.buy_fee + costs.sell_fee
    pos: dict | None = None
    pending = None
    tf_s = TIMEFRAME_SECONDS[strategy.timeframe]
    start = max(start, 1)

    def close(i: int, price: float, reason: str) -> None:
        nonlocal pos
        ret = (1 - costs.buy_fee) / pos["entry"] * price * (1 - costs.sell_fee) - 1
        trades.append(SimTrade(symbol, pos["ts"], candles[i].ts, pos["entry"], price, ret, reason))
        pos = None

    for i in range(start, end):
        c = candles[i]
        if pending is not None and pos is None:
            price = c.open * (1 + costs.slip)
            if pending.stop < price and (pending.take_profit - price) / price >= costs.min_edge * rt_fee:
                pos = {"entry": price, "stop": pending.stop, "tp": pending.take_profit, "ts": c.ts,
                       "trailing": pending.trailing, "trail": pending.trail_mult, "atr": pending.atr,
                       "highest": price}
            pending = None

        if pos is not None:
            limit = pos["stop"] * (1 - costs.stop_offset)
            if c.open <= limit:
                close(i, c.open * (1 - costs.slip), "stop_gap")
            elif c.low <= pos["stop"]:
                close(i, max(limit, pos["stop"] * (1 - costs.slip)) if c.low >= limit else limit,
                      "trailing_stop" if pos["trailing"] and pos["stop"] > pos["entry"] else "stop_loss")
            elif not pos["trailing"] and c.high >= pos["tp"]:
                close(i, pos["tp"] * (1 - costs.slip), "take_profit")
            else:
                reason = strategy.exit_at(ind, i)
                if reason:
                    close(i, c.close * (1 - costs.slip), reason)
                elif pos["trailing"]:
                    pos["highest"] = max(pos["highest"], c.high)
                    pos["stop"] = max(pos["stop"], strategy.trail_stop(pos["highest"], pos["atr"], pos["trail"]))

        if pos is None and i + 1 < end:
            sig = strategy.entry_at(ind, i, symbol, (c.ts / 1000) + tf_s)
            if sig is not None and (entry_filter is None or entry_filter(c.ts)):
                pending = sig

    if pos is not None:
        close(end - 1, candles[end - 1].close * (1 - costs.slip), "end_of_data")
    return trades


@dataclass
class Metrics:
    trades: int = 0
    win_rate: float = 0.0
    profit_factor: float | None = 0.0   # None = no losing trades
    total_return_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    avg_trade_pct: float = 0.0

    def as_dict(self) -> dict:
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in self.__dict__.items()}


def metrics(trades: list[SimTrade], fraction: float = 0.5) -> Metrics:
    """Compound trades in exit order, risking `fraction` of equity per trade."""
    if not trades:
        return Metrics()
    gains = sum(t.ret for t in trades if t.ret > 0)
    losses = -sum(t.ret for t in trades if t.ret < 0)
    equity, peak, dd = 1.0, 1.0, 0.0
    for t in sorted(trades, key=lambda t: t.exit_ts):
        equity *= 1 + fraction * t.ret
        peak = max(peak, equity)
        dd = max(dd, (peak - equity) / peak)
    return Metrics(trades=len(trades), win_rate=sum(t.ret > 0 for t in trades) / len(trades) * 100,
                   profit_factor=(gains / losses) if losses > 0 else None,
                   total_return_pct=(equity - 1) * 100, max_drawdown_pct=dd * 100,
                   avg_trade_pct=sum(t.ret for t in trades) / len(trades) * 100)


def resample(candles: list[Candle], timeframe: str) -> list[Candle]:
    """Aggregate finer candles (e.g. 15m) into `timeframe`; incomplete buckets are dropped."""
    tf_ms = TIMEFRAME_SECONDS[timeframe] * 1000
    if len(candles) < 2:
        return list(candles)
    base_ms = candles[1].ts - candles[0].ts
    per = tf_ms // base_ms if base_ms > 0 else 1
    if per <= 1:
        return list(candles)
    out: list[Candle] = []
    bucket: list[Candle] = []
    for c in candles:
        if bucket and c.ts // tf_ms != bucket[0].ts // tf_ms:
            if len(bucket) == per:
                out.append(_merge(bucket, tf_ms))
            bucket = []
        bucket.append(c)
    if len(bucket) == per:
        out.append(_merge(bucket, tf_ms))
    return out


def _merge(b: list[Candle], tf_ms: int) -> Candle:
    return Candle((b[0].ts // tf_ms) * tf_ms, b[0].open, max(c.high for c in b), min(c.low for c in b), b[-1].close,
                  sum(c.volume for c in b))


async def fetch_history(ex: MarketData, symbol: str, timeframe: str, days: int) -> list[Candle]:
    tf_ms = TIMEFRAME_SECONDS[timeframe] * 1000
    since = int((time.time() - days * 86400) * 1000)
    out: list[Candle] = []
    while True:
        batch = await ex.fetch_ohlcv(symbol, timeframe, 1000, since=since)
        batch = [c for c in batch if not out or c.ts > out[-1].ts]
        if not batch:
            break
        out.extend(batch)
        since = out[-1].ts + tf_ms
        if since > time.time() * 1000 - tf_ms:
            break
    now_ms = time.time() * 1000
    return [c for c in out if c.ts + tf_ms <= now_ms]  # closed candles only


# ---- simple single-symbol backtest (CLI) -------------------------------------------------------
@dataclass
class Result:
    symbol: str
    metrics: Metrics
    trades: list[SimTrade] = field(default_factory=list)
    buy_hold_pct: float = 0.0

    def summary(self) -> str:
        m = self.metrics
        pf = "  inf" if m.profit_factor is None else f"{m.profit_factor:5.2f}"
        return (f"{self.symbol:<12} trades {m.trades:>4} | win {m.win_rate:5.1f}% | PF {pf} | "
                f"return {m.total_return_pct:+7.2f}% | maxDD {m.max_drawdown_pct:5.2f}% | "
                f"avg {m.avg_trade_pct:+.2f}%/trade | buy&hold {self.buy_hold_pct:+7.2f}%")


def backtest(symbol: str, candles: list[Candle], cfg: BotConfig, strategy: Strategy | None = None,
             fraction: float | None = None) -> Result:
    strategy = strategy or build_strategy(cfg.strategy, cfg.timeframe)
    ind = strategy.prepare(candles)
    trades = simulate(strategy, ind, candles, strategy.min_candles - 1, len(candles), Costs.from_cfg(cfg), symbol)
    frac = fraction if fraction is not None else cfg.risk.max_position_pct / 100
    bh = 0.0
    if len(candles) > strategy.min_candles:
        bh = (candles[-1].close / candles[strategy.min_candles - 1].close - 1) * 100
    return Result(symbol, metrics(trades, frac), trades, bh)


async def run_backtest_cli(cfg: BotConfig, symbols: list[str], days: int) -> None:
    ex = TokocryptoClient(native_quotes=(cfg.quote,), rate_limit_ms=cfg.exchange_rate_limit_ms)
    try:
        await ex.load_markets()
        print(f"Backtest {days}d {cfg.timeframe}, fees buy {cfg.fees.buy_pct}% / sell {cfg.fees.sell_pct}%, "
              f"slippage {cfg.paper.slippage_pct}%")
        for s in symbols:
            candles = await fetch_history(ex, s, cfg.timeframe, days)
            if len(candles) < 200:
                print(f"{s}: not enough data ({len(candles)} candles)")
                continue
            print(backtest(s, candles, cfg).summary())
    finally:
        await ex.close()
