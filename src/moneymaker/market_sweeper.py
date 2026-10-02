"""Market radar: slowly sweeps every pair in the quote currency and keeps a snapshot per pair.

One klines request per pair per sweep (Tokocrypto allows ~1 request / 2 s), so ~150 pairs take ~5 minutes.
The snapshot feeds the alt sleeve (which coins are eligible), the BTC regime filter and the dashboard radar.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict, dataclass
from typing import Callable

from .config import BotConfig
from .exchange.base import MarketData
from .models import Candle, Market
from .scanner import category, static_exclusion
from .strategy.base import atr, ema

log = logging.getLogger(__name__)
REFRESH_CANDLES = 80  # recent 1h candles per pair per sweep


@dataclass
class PairStat:
    symbol: str
    base: str
    category: str
    last: float
    change_24h_pct: float
    volume_24h: float
    atr_pct: float
    rs_vs_btc: float | None
    age_days: float | None    # None = older than the history we fetch (>= ~31 days)
    vol_surge: float          # last closed candle volume / average of the previous 24
    ext_atr: float | None     # (close - EMA20) / ATR
    eligible: bool
    reason: str
    updated_at: float


def pair_stats(sym: str, m: Market, candles: list[Candle], cfg: BotConfig, age_days: float | None,
               btc_change: float | None) -> PairStat:
    cat = category(m, cfg)
    now = time.time()
    if len(candles) < 26:
        return PairStat(sym, m.base, cat, candles[-1].close if candles else 0.0, 0.0, 0.0, 0.0, None, age_days,
                        0.0, None, False, "data kurang", now)
    last = candles[-1]
    day = candles[-24:]
    volume = sum(c.volume * c.close for c in day)
    change = (last.close / candles[-25].close - 1) * 100 if candles[-25].close > 0 else 0.0
    a = atr(candles, 14)[-1]
    e20 = ema([c.close for c in candles], 20)[-1]
    prev = candles[-25:-1]
    avg_vol = sum(c.volume for c in prev) / len(prev)
    surge = last.volume / avg_vol if avg_vol > 0 else 0.0
    ext = (last.close - e20) / a if a > 0 else None
    rs = None if btc_change is None or m.base == "BTC" else change - btc_change
    reason = eligibility_reason(cat, volume, change, ext, age_days, cfg)
    return PairStat(sym, m.base, cat, last.close, change, volume, a / last.close * 100 if last.close else 0.0, rs,
                    age_days, surge, ext, reason == "", reason or "lolos", now)


def eligibility_reason(cat: str, volume: float, change: float, ext: float | None, age_days: float | None,
                       cfg: BotConfig) -> str:
    if cat == "majors":
        return "" if volume >= cfg.scanner.min_quote_volume_24h else "volume kecil"
    f = cfg.alt_filters
    if age_days is not None and age_days < f.min_age_days:
        return f"listing baru ({age_days:.0f} hr)"
    if volume < f.min_volume_24h:
        return "volume kecil"
    if change > f.max_change_24h_pct:
        return f"sudah naik {change:.0f}% (24j)"
    if ext is not None and ext > f.max_extension_atr:
        return "terlalu jauh di atas EMA20"
    return ""


def compute_regime(btc: list[Candle], cfg: BotConfig) -> dict:
    out = {"btc_symbol": f"BTC/{cfg.quote}", "btc_change_24h_pct": None, "btc_below_ema50": False,
           "alts_blocked": False, "all_blocked": False, "reason": "data BTC belum ada"}
    if len(btc) < 60:
        return out
    closes = [c.close for c in btc]
    change = (closes[-1] / closes[-25] - 1) * 100
    below = closes[-1] < ema(closes, 50)[-1]
    a = atr(btc, 14)
    last = btc[-1]
    extreme = a[-2] > 0 and (last.high - last.low) > cfg.regime.extreme_move_atr * a[-2]
    alts_blocked = below and change < cfg.regime.alt_block_btc_change_24h_pct
    reason = ("BTC bergerak ekstrem: semua entry ditahan" if extreme else
              f"BTC melemah ({change:+.1f}% 24j, di bawah EMA50): alt diblok" if alts_blocked else "pasar normal")
    out.update(btc_change_24h_pct=round(change, 2), btc_below_ema50=below, alts_blocked=alts_blocked or extreme,
               all_blocked=extreme, reason=reason)
    return out


class MarketSweeper:
    def __init__(self, ex: MarketData, cfg: BotConfig, markets: Callable[[], dict[str, Market]]):
        self.ex = ex
        self.cfg = cfg
        self.markets = markets
        self.stats: dict[str, PairStat] = {}
        self.regime: dict | None = None
        self.sweeps_completed = 0
        self.progress = {"done": 0, "total": 0}
        self.updated_at: float | None = None
        self._first_ts: dict[str, int | None] = {}  # first candle ms if younger than the probe window, else None
        self._btc_candles: list[Candle] = []

    def eligible(self, cat: str) -> list[PairStat]:
        return [p for p in self.stats.values() if p.category == cat and p.eligible]

    def top_alts(self, n: int) -> list[str]:
        """Eligible alts, strongest relative to BTC first, then by volume."""
        alts = self.eligible("alts")
        alts.sort(key=lambda p: ((p.rs_vs_btc if p.rs_vs_btc is not None else -1e9), p.volume_24h), reverse=True)
        return [p.symbol for p in alts[:n]]

    def as_dict(self) -> dict:
        return {"updated_at": self.updated_at, "sweeps_completed": self.sweeps_completed, "progress": self.progress,
                "regime": self.regime, "pairs": [asdict(p) for p in self.stats.values()]}

    async def _candles(self, sym: str) -> list[Candle]:
        candles = await self.ex.fetch_ohlcv(sym, "1h", REFRESH_CANDLES)
        now_ms = time.time() * 1000
        return [c for c in candles if c.ts + 3_600_000 <= now_ms]  # closed only

    async def _probe_age(self, sym: str) -> None:
        """Ask for candles starting (min_age + 2) days ago: if the first one starts later, the pair is younger.
        Works whatever the exchange's max candles per request is. One request per pair, cached."""
        days = self.cfg.alt_filters.min_age_days + 2
        since = int((time.time() - days * 86400) * 1000)
        first = await self.ex.fetch_ohlcv(sym, "1h", 5, since=since)
        self._first_ts[sym] = first[0].ts if first and first[0].ts > since + 2 * 3_600_000 else None

    def _age_days(self, sym: str) -> float | None:
        first = self._first_ts.get(sym)
        return None if first is None else (time.time() * 1000 - first) / 86_400_000

    async def sweep_once(self) -> None:
        markets = self.markets()
        syms = sorted((s for s, m in markets.items() if static_exclusion(m, self.cfg) is None),
                      key=lambda s: (markets[s].base != "BTC", s))
        btc_sym = f"BTC/{self.cfg.quote}"
        if btc_sym in markets and btc_sym not in syms:
            syms.insert(0, btc_sym)
        self.progress = {"done": 0, "total": len(syms)}
        btc_change = None
        for sym in syms:
            try:
                candles = await self._candles(sym)
                if sym == btc_sym:
                    self._btc_candles = candles
                    self.regime = compute_regime(candles, self.cfg)
                    btc_change = self.regime.get("btc_change_24h_pct")
                st = pair_stats(sym, markets[sym], candles, self.cfg, self._age_days(sym), btc_change)
                if st.category == "alts" and sym not in self._first_ts \
                        and st.volume_24h >= self.cfg.alt_filters.min_volume_24h:
                    await self._probe_age(sym)  # only for alts that pass the cheap filters
                    st = pair_stats(sym, markets[sym], candles, self.cfg, self._age_days(sym), btc_change)
                self.stats[sym] = st
                if st.volume_24h > 0:
                    self.ex.set_volume_hint(sym, st.volume_24h)
            except Exception as e:  # noqa: BLE001 - one pair failing must not stop the sweep
                log.warning("radar %s failed: %s", sym, e)
            self.progress = {"done": self.progress["done"] + 1, "total": len(syms)}
        self.sweeps_completed += 1
        self.updated_at = time.time()
        log.info("radar sweep %d done: %d pairs, %d alts eligible, regime: %s", self.sweeps_completed, len(syms),
                 len(self.eligible("alts")), (self.regime or {}).get("reason"))

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            if self.markets():
                try:
                    await self.sweep_once()
                except Exception:  # noqa: BLE001
                    log.exception("radar sweep failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.cfg.scanner.sweep_interval_minutes * 60
                                       if self.sweeps_completed else 5)
            except asyncio.TimeoutError:
                pass

    def btc_candles(self) -> list[Candle]:
        return self._btc_candles
