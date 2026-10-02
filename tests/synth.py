"""Deterministic synthetic candles for tests."""
from __future__ import annotations

import random
import time

from moneymaker.models import Candle


def series(n: int, tf_s: int, mu: float, sigma: float, seed: int, start: float = 100.0,
           volume: float = 1000.0, now: float | None = None) -> list[Candle]:
    """Random walk with drift `mu` per candle, aligned so the last candle has just closed."""
    rng = random.Random(seed)
    now = now or time.time()
    last_open = (int(now) // tf_s) * tf_s - tf_s
    first = last_open - (n - 1) * tf_s
    out, price = [], start
    for i in range(n):
        o = price
        c = o * (1 + rng.gauss(mu, sigma))
        h = max(o, c) * (1 + abs(rng.gauss(0, sigma / 2)))
        low = min(o, c) * (1 - abs(rng.gauss(0, sigma / 2)))
        out.append(Candle((first + i * tf_s) * 1000, o, h, low, c, volume * (1 + abs(rng.gauss(0, 0.3)))))
        price = c
    return out
