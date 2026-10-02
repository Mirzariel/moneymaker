"""Baseline long-only trend strategy.

Entry (on the most recent closed candle):
  * EMA(fast) > EMA(slow)              -> uptrend
  * close > EMA(fast)
  * close breaks above the highest high of the previous `breakout_lookback` candles
Stop = close - atr_stop_mult * ATR, take-profit = close + reward_risk * (close - stop).
Exit early when a candle closes below EMA(slow).

This is a pipeline baseline, not a proven edge. Backtest it net of fees before trusting it.
"""
from __future__ import annotations

from ..config import StrategyConfig
from ..models import Candle, Signal
from .base import Strategy, atr, ema


class TrendAtrStrategy(Strategy):
    name = "trend_atr"

    def __init__(self, cfg: StrategyConfig):
        self.cfg = cfg

    @property
    def min_candles(self) -> int:
        return max(self.cfg.ema_slow * 3, self.cfg.breakout_lookback + 2, self.cfg.atr_period * 3)

    def entry_signal(self, symbol: str, candles: list[Candle], now: float) -> Signal | None:
        if len(candles) < self.min_candles:
            return None
        closes = [c.close for c in candles]
        fast = ema(closes, self.cfg.ema_fast)[-1]
        slow = ema(closes, self.cfg.ema_slow)[-1]
        a = atr(candles, self.cfg.atr_period)[-1]
        last = candles[-1]
        prior_high = max(c.high for c in candles[-self.cfg.breakout_lookback - 1:-1])

        if not (fast > slow and last.close > fast and last.close > prior_high and a > 0):
            return None
        stop = last.close - self.cfg.atr_stop_mult * a
        if stop <= 0:
            return None
        tp = last.close + self.cfg.reward_risk * (last.close - stop)
        return Signal(
            symbol=symbol, side="buy", entry=last.close, stop=stop, take_profit=tp,
            created_at=now, expires_at=now + self.cfg.signal_ttl_sec, candle_ts=last.ts,
            reason=f"breakout>{prior_high:.6g} ema{self.cfg.ema_fast}={fast:.6g}>ema{self.cfg.ema_slow}={slow:.6g} "
                   f"atr={a:.6g}",
        )

    def should_exit(self, candles: list[Candle]) -> str | None:
        if len(candles) < self.cfg.ema_slow:
            return None
        slow = ema([c.close for c in candles], self.cfg.ema_slow)[-1]
        if candles[-1].close < slow:
            return f"trend_exit close<ema{self.cfg.ema_slow}"
        return None


def build_strategy(cfg: StrategyConfig) -> Strategy:
    if cfg.name == "trend_atr":
        return TrendAtrStrategy(cfg)
    raise ValueError(f"unknown strategy {cfg.name}")
