"""S1 — long-only trend breakout for well-known coins ("majors" sleeve).

Entry (on the most recent closed candle):
  * EMA(fast) > EMA(slow)              -> uptrend
  * close > EMA(fast)
  * close breaks above the highest high of the previous `breakout_lookback` candles
Stop = close - atr_stop_mult * ATR, take-profit = close + reward_risk * (close - stop).
Exit early when a candle closes below EMA(slow).
"""
from __future__ import annotations

import math

from ..config import StrategyConfig
from ..models import Candle, Signal
from .base import Indicators, Strategy, atr, ema, prior_max


class TrendAtrStrategy(Strategy):
    name = "trend_atr"

    def __init__(self, cfg: StrategyConfig, timeframe: str = "1h"):
        self.cfg = cfg
        self.timeframe = timeframe

    @property
    def min_candles(self) -> int:
        return max(self.cfg.ema_slow * 3, self.cfg.breakout_lookback + 2, self.cfg.atr_period * 3)

    def prepare(self, candles: list[Candle]) -> Indicators:
        closes = [c.close for c in candles]
        return {
            "ts": [c.ts for c in candles],
            "close": closes,
            "ema_fast": ema(closes, self.cfg.ema_fast),
            "ema_slow": ema(closes, self.cfg.ema_slow),
            "atr": atr(candles, self.cfg.atr_period),
            "prior_high": prior_max([c.high for c in candles], self.cfg.breakout_lookback),
        }

    def entry_at(self, ind: Indicators, i: int, symbol: str, now: float) -> Signal | None:
        if i < self.min_candles - 1:
            return None
        close, fast, slow, a, ph = (ind["close"][i], ind["ema_fast"][i], ind["ema_slow"][i], ind["atr"][i],
                                    ind["prior_high"][i])
        if math.isnan(ph) or not (fast > slow and close > fast and close > ph and a > 0):
            return None
        stop = close - self.cfg.atr_stop_mult * a
        if stop <= 0:
            return None
        tp = close + self.cfg.reward_risk * (close - stop)
        return Signal(
            symbol=symbol, side="buy", entry=close, stop=stop, take_profit=tp,
            created_at=now, expires_at=now + self.cfg.signal_ttl_sec, candle_ts=ind["ts"][i],
            reason=f"breakout>{ph:.6g} ema{self.cfg.ema_fast}={fast:.6g}>ema{self.cfg.ema_slow}={slow:.6g} "
                   f"atr={a:.6g}",
            sleeve="majors", atr=a, timeframe=self.timeframe,
        )

    def exit_at(self, ind: Indicators, i: int) -> str | None:
        if i < self.cfg.ema_slow:
            return None
        if ind["close"][i] < ind["ema_slow"][i]:
            return f"trend_exit close<ema{self.cfg.ema_slow}"
        return None


def build_strategy(cfg: StrategyConfig, timeframe: str = "1h") -> Strategy:
    if cfg.name == "trend_atr":
        return TrendAtrStrategy(cfg, timeframe)
    raise ValueError(f"unknown strategy {cfg.name}")
