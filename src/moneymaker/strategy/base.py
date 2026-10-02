"""Strategy interface.

A strategy only *proposes*. It never sizes or sends orders; the risk engine decides.
A future AI/LLM filter plugs in the same way: it can veto or annotate a Signal, nothing more.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import Candle, Signal


class Strategy(ABC):
    name: str = "base"

    @property
    @abstractmethod
    def min_candles(self) -> int: ...

    @abstractmethod
    def entry_signal(self, symbol: str, candles: list[Candle], now: float) -> Signal | None:
        """`candles` are CLOSED candles only, oldest first."""

    @abstractmethod
    def should_exit(self, candles: list[Candle]) -> str | None:
        """Return an exit reason for an open long position, or None to hold."""


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


TIMEFRAME_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "2h": 7200, "4h": 14400,
                     "6h": 21600, "12h": 43200, "1d": 86400}


def closed_candles(candles: list[Candle], timeframe: str, now: float) -> list[Candle]:
    """Drop the still-forming last candle."""
    tf_ms = TIMEFRAME_SECONDS[timeframe] * 1000
    now_ms = int(now * 1000)
    return [c for c in candles if c.ts + tf_ms <= now_ms]
