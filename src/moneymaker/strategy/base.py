"""Strategy interface.

A strategy only *proposes*. It never sizes or sends orders; the risk engine decides.

Indicators are computed once for a whole candle series (`prepare`), then evaluated at any index
(`entry_at` / `exit_at`). Live trading calls them at the last index; backtests and the learning loop
walk every index. Same code path for both, so a backtest measures exactly what runs live.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ..models import Candle, Signal

Indicators = dict[str, Any]


class Strategy(ABC):
    name: str = "base"
    timeframe: str = "1h"
    trailing: bool = False

    @property
    @abstractmethod
    def min_candles(self) -> int: ...

    @abstractmethod
    def prepare(self, candles: list[Candle]) -> Indicators: ...

    @abstractmethod
    def entry_at(self, ind: Indicators, i: int, symbol: str, now: float) -> Signal | None:
        """Signal on CLOSED candle i (entry at the next candle's open)."""

    def exit_at(self, ind: Indicators, i: int) -> str | None:
        """Exit reason for an open long position after candle i closed, or None to hold."""
        return None

    def trail_stop(self, highest: float, atr: float, trail_mult: float) -> float:
        return highest - trail_mult * atr

    # ---- live helpers (last closed candle) ------------------------------------------------
    def entry_signal(self, symbol: str, candles: list[Candle], now: float) -> Signal | None:
        if len(candles) < self.min_candles:
            return None
        return self.entry_at(self.prepare(candles), len(candles) - 1, symbol, now)

    def should_exit(self, candles: list[Candle]) -> str | None:
        if len(candles) < self.min_candles:
            return None
        return self.exit_at(self.prepare(candles), len(candles) - 1)


def ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def atr(candles: list[Candle], period: int) -> list[float]:
    """Wilder's ATR. Same length as candles."""
    if not candles:
        return []
    trs = [candles[0].high - candles[0].low]
    for prev, c in zip(candles, candles[1:]):
        trs.append(max(c.high - c.low, abs(c.high - prev.close), abs(c.low - prev.close)))
    out = [trs[0]]
    for tr in trs[1:]:
        out.append((out[-1] * (period - 1) + tr) / period)
    return out


def prior_max(values: list[float], lookback: int) -> list[float]:
    """out[i] = max(values[i-lookback:i]) (excludes i); nan when not enough history. O(n) monotonic deque."""
    from collections import deque
    out = [float("nan")] * len(values)
    dq: deque[int] = deque()
    for i, v in enumerate(values):
        while dq and dq[0] < i - lookback:
            dq.popleft()
        if i >= lookback:
            out[i] = values[dq[0]]
        while dq and values[dq[-1]] <= v:
            dq.pop()
        dq.append(i)
    return out


def prior_mean(values: list[float], period: int) -> list[float]:
    """out[i] = mean(values[i-period:i]) (excludes i); nan when not enough history."""
    out = [float("nan")] * len(values)
    s = 0.0
    for i, v in enumerate(values):
        if i >= period:
            out[i] = s / period
            s -= values[i - period]
        s += v
    return out


TIMEFRAME_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "2h": 7200, "4h": 14400,
                     "6h": 21600, "12h": 43200, "1d": 86400}


def closed_candles(candles: list[Candle], timeframe: str, now: float) -> list[Candle]:
    """Drop the still-forming last candle."""
    tf_ms = TIMEFRAME_SECONDS[timeframe] * 1000
    now_ms = int(now * 1000)
    return [c for c in candles if c.ts + tf_ms <= now_ms]
