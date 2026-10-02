"""Candle backtester that reuses the live Strategy and RiskEngine, net of fees and slippage.

Conservative assumptions:
  * entries fill at the NEXT candle's open + slippage (no look-ahead)
  * if stop and take-profit are both touched inside one candle, the stop is assumed to hit first
  * a gap below the stop-limit exits at that candle's open (mirrors the live market-sell fallback)
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..config import BotConfig
from ..exchange.tokocrypto import TokocryptoClient
from ..models import BotStatus, Candle, Market, Ticker
from ..risk.engine import RiskContext, RiskEngine
from ..strategy.base import TIMEFRAME_SECONDS
from ..strategy.trend_atr import build_strategy

WINDOW = 500  # candles fed to the strategy each step (EMA warm-up is far shorter)


@dataclass
class Trade:
    entry_ts: int
    exit_ts: int
    entry: float
    exit: float
    cost: float
    proceeds: float
    reason: str

    @property
    def pnl(self) -> float:
        return self.proceeds - self.cost


@dataclass
class Result:
    symbol: str
    start_equity: float
    end_equity: float
    trades: list[Trade] = field(default_factory=list)
    max_drawdown_pct: float = 0.0
    fees_paid: float = 0.0
    buy_hold_pct: float = 0.0
    rejected: dict[str, int] = field(default_factory=dict)

    @property
    def return_pct(self) -> float:
        return (self.end_equity / self.start_equity - 1) * 100

    @property
    def win_rate(self) -> float:
        return sum(t.pnl > 0 for t in self.trades) / len(self.trades) * 100 if self.trades else 0.0

    @property
    def profit_factor(self) -> float:
        gains = sum(t.pnl for t in self.trades if t.pnl > 0)
        losses = -sum(t.pnl for t in self.trades if t.pnl < 0)
        return gains / losses if losses > 0 else float("inf") if gains > 0 else 0.0

    def summary(self) -> str:
        return (f"{self.symbol:<12} trades {len(self.trades):>4} | win {self.win_rate:5.1f}% | "
                f"PF {self.profit_factor:5.2f} | return {self.return_pct:+7.2f}% | "
                f"maxDD {self.max_drawdown_pct:5.2f}% | fees {self.fees_paid:8.2f} | "
                f"buy&hold {self.buy_hold_pct:+7.2f}%")


def backtest(symbol: str, candles: list[Candle], cfg: BotConfig, start_equity: float = 1000.0,
             market: Market | None = None) -> Result:
    strategy = build_strategy(cfg.strategy)
    risk = RiskEngine(cfg.risk, cfg.fees, min_quote_volume_24h=0, max_spread_pct=100)
    buy_fee = cfg.fees.buy_pct / 100
    sell_fee = cfg.fees.sell_pct / 100
    slip = cfg.paper.slippage_pct / 100
    offset = cfg.risk.stop_limit_offset_pct / 100
    market = market or Market(symbol, *symbol.split("/"), supports_stop_limit=True)

    cash = start_equity
    pos: dict | None = None
    peak, max_dd = start_equity, 0.0
    res = Result(symbol, start_equity, start_equity)
    pending = None  # signal waiting for next candle's open
    last_trade_ts: float | None = None

    def close(i: int, price: float, reason: str) -> None:
        nonlocal cash, pos
        gross = pos["qty"] * price
        f = gross * sell_fee
        res.fees_paid += f
        cash += gross - f
        res.trades.append(Trade(pos["ts"], candles[i].ts, pos["entry"], price, pos["cost"], gross - f, reason))
        pos = None

    for i in range(strategy.min_candles, len(candles)):
        c = candles[i]
        # 1. Fill a pending entry at this candle's open.
        if pending is not None and pos is None:
            price = c.open * (1 + slip)
            now = pending.created_at
            ctx = RiskContext(now=now, status=BotStatus.RUNNING, equity=cash, quote_free=cash, exposure=0.0,
                              open_symbols=set(), day_start_equity=cash, day_pnl=0.0, consecutive_losses=0,
                              ticker=Ticker(symbol, bid=c.open, ask=price, last=c.open, quote_volume=1e18),
                              market=market, last_entry_ts=last_trade_ts)
            d = risk.evaluate(pending, ctx)
            if d.approved:
                f = d.cost * buy_fee
                res.fees_paid += f
                cash -= d.cost
                pos = {"qty": (d.cost - f) / price, "entry": price, "cost": d.cost, "stop": pending.stop,
                       "tp": pending.take_profit, "ts": c.ts}
                last_trade_ts = now
            else:
                res.rejected[d.rule] = res.rejected.get(d.rule, 0) + 1
            pending = None

        # 2. Manage an open position within this candle.
        if pos is not None:
            limit = pos["stop"] * (1 - offset)
            if c.open <= limit:
                close(i, c.open * (1 - slip), "stop_gap")
            elif c.low <= pos["stop"]:
                close(i, max(limit, pos["stop"] * (1 - slip)) if c.low >= limit else limit, "stop_loss")
            elif c.high >= pos["tp"]:
                close(i, pos["tp"] * (1 - slip), "take_profit")
            else:
                window = candles[max(0, i - WINDOW): i + 1]
                reason = strategy.should_exit(window)
                if reason:
                    close(i, c.close * (1 - slip), reason)

        # 3. New signal on this closed candle -> enter next candle.
        if pos is None and i + 1 < len(candles):
            window = candles[max(0, i - WINDOW): i + 1]
            t_close = (c.ts / 1000) + TIMEFRAME_SECONDS[cfg.timeframe]
            pending = strategy.entry_signal(symbol, window, t_close)

        equity = cash + (pos["qty"] * c.close if pos else 0.0)
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak * 100)

    if pos is not None:
        close(len(candles) - 1, candles[-1].close * (1 - slip), "end_of_data")
    res.end_equity = cash
    res.max_drawdown_pct = max_dd
    if candles:
        first = candles[min(strategy.min_candles, len(candles) - 1)].open
        res.buy_hold_pct = (candles[-1].close / first - 1) * 100
    return res


async def fetch_history(ex: TokocryptoClient, symbol: str, timeframe: str, days: int) -> list[Candle]:
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


async def run_backtest_cli(cfg: BotConfig, symbols: list[str], days: int) -> None:
    ex = TokocryptoClient(native_quotes=(cfg.quote,))
    try:
        markets = await ex.load_markets()
        print(f"Backtest {days}d {cfg.timeframe}, fees buy {cfg.fees.buy_pct}% / sell {cfg.fees.sell_pct}%, "
              f"slippage {cfg.paper.slippage_pct}%")
        for s in symbols:
            candles = await fetch_history(ex, s, cfg.timeframe, days)
            if len(candles) < 200:
                print(f"{s}: not enough data ({len(candles)} candles)")
                continue
            r = backtest(s, candles, cfg, cfg.paper.starting_quote_balance, markets.get(s))
            print(r.summary())
            if r.rejected:
                print(f"{'':<12} rejected signals: {r.rejected}")
    finally:
        await ex.close()
