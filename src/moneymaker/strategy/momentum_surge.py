"""S2 — momentum surge for smaller coins ("alts" sleeve).

Small coins move in bursts. We only want the start of a burst that has real buying behind it,
and we let winners run with a trailing stop instead of a fixed target.

Entry on a closed candle when ALL hold:
  * close breaks above the highest high of the previous `breakout_lookback` candles
  * volume >= volume_mult x average volume of the previous `volume_avg_period` candles (real demand)
  * close > EMA(fast) > EMA(slow)                     (already trending, not a dead-cat bounce)
  * close <= EMA(fast) + max_extension_atr x ATR      (not chasing a vertical candle)
  * 24-candle change <= max_change_24h_pct            (didn't already pump)
Initial stop = close - atr_stop_mult x ATR; then trailing: highest_since_entry - trail_mult x ATR(entry).
`take_profit` is only nominal (3R) for the fee-edge check; the trailing stop decides the exit.
"""
from __future__ import annotations

import math

from ..config import MomentumConfig
from ..models import Candle, Signal
from .base import Indicators, Strategy, atr, ema, prior_max, prior_mean


class MomentumSurgeStrategy(Strategy):
    name = "momentum_surge"
    trailing = True

    def __init__(self, cfg: MomentumConfig):
        self.cfg = cfg
        self.timeframe = cfg.timeframe

    @property
    def min_candles(self) -> int:
        c = self.cfg
        return max(c.ema_slow * 2, c.breakout_lookback + 2, c.volume_avg_period + 2, c.atr_period * 3, 26)

    def prepare(self, candles: list[Candle]) -> Indicators:
        closes = [c.close for c in candles]
        vols = [c.volume for c in candles]
        return {
            "ts": [c.ts for c in candles],
            "close": closes,
            "volume": vols,
            "ema_fast": ema(closes, self.cfg.ema_fast),
            "ema_slow": ema(closes, self.cfg.ema_slow),
            "atr": atr(candles, self.cfg.atr_period),
            "prior_high": prior_max([c.high for c in candles], self.cfg.breakout_lookback),
            "vol_avg": prior_mean(vols, self.cfg.volume_avg_period),
        }

    def entry_at(self, ind: Indicators, i: int, symbol: str, now: float) -> Signal | None:
        c = self.cfg
        if i < self.min_candles - 1:
            return None
        close, fast, slow, a = ind["close"][i], ind["ema_fast"][i], ind["ema_slow"][i], ind["atr"][i]
        ph, vavg, vol = ind["prior_high"][i], ind["vol_avg"][i], ind["volume"][i]
        if math.isnan(ph) or math.isnan(vavg) or a <= 0 or vavg <= 0:
            return None
        change_24 = (close / ind["close"][i - 24] - 1) * 100 if i >= 24 and ind["close"][i - 24] > 0 else 0.0
        if not (close > ph and vol >= c.volume_mult * vavg and close > fast > slow
                and close <= fast + c.max_extension_atr * a and change_24 <= c.max_change_24h_pct):
            return None
        stop = close - c.atr_stop_mult * a
        if stop <= 0:
            return None
        return Signal(
            symbol=symbol, side="buy", entry=close, stop=stop, take_profit=close + 3 * (close - stop),
            created_at=now, expires_at=now + c.signal_ttl_sec, candle_ts=ind["ts"][i],
            reason=f"surge vol x{vol / vavg:.1f} breakout>{ph:.6g} 24j {change_24:+.1f}% atr={a:.6g}",
            sleeve="alts", trailing=True, trail_mult=c.trail_mult, atr=a, timeframe=self.timeframe,
        )
