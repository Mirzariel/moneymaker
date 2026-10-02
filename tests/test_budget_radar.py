"""Rate-limit budget (the 'Cek kesiapan never finishes' bug), strategy regressions, radar & regime filters."""
from __future__ import annotations

import asyncio
import math
import time

import pytest

from moneymaker.backtest.runner import resample
from moneymaker.config import BotConfig, MomentumConfig, StrategyConfig
from moneymaker.exchange.tokocrypto import TokocryptoClient
from moneymaker.market_sweeper import MarketSweeper, compute_regime
from moneymaker.models import Balance, Candle, Market, Ticker
from moneymaker.preflight import run_preflight
from moneymaker.config import Secrets
from moneymaker.strategy.base import prior_max, prior_mean
from moneymaker.strategy.momentum_surge import MomentumSurgeStrategy
from moneymaker.strategy.trend_atr import TrendAtrStrategy
from moneymaker.exchange.paper import StaticMarketData

from .conftest import SYM, breakout_candles
from .synth import series


# ---- request budget -------------------------------------------------------------------------------------
async def test_fetch_tickers_never_uses_bulk_endpoint(monkeypatch):
    c = TokocryptoClient()
    c._markets = {"BTC/IDR": Market("BTC/IDR", "BTC", "IDR", native=True),
                  "ETH/USDT": Market("ETH/USDT", "ETH", "USDT", native=False)}

    async def bulk(*a, **k):
        raise AssertionError("bulk ticker/24hr (cost 40 = 80 s wait) must never be called")

    async def one(symbol, params={}):
        return {"bid": 1, "ask": 2, "last": 1.5, "quoteVolume": 10}

    async def ob(symbol, limit=None):
        return {"bids": [[1, 1]], "asks": [[2, 1]]}

    monkeypatch.setattr(c.ex, "fetch_tickers", bulk)
    monkeypatch.setattr(c.ex, "fetch_ticker", one)
    monkeypatch.setattr(c.ex, "fetch_order_book", ob)
    c.set_volume_hint("BTC/IDR", 5e9)  # radar already knows the volume -> no klines request
    try:
        t = await c.fetch_tickers(["BTC/IDR", "ETH/USDT"])
        assert t["BTC/IDR"].quote_volume == 5e9 and t["ETH/USDT"].last == 1.5
    finally:
        await c.close()


class CountingClient:
    """Stand-in for TokocryptoClient that counts requests (each ~2 s on the real exchange)."""

    def __init__(self, delay: float = 0.0):
        self.requests = 0
        self.delay = delay
        bases = ["BTC", "ETH", "BNB", "SOL", "XRP", "DOGE", "ADA", "TRX", "LINK", "AVAX", "DOT", "LTC"]
        self.markets = {f"{b}/IDR": Market(f"{b}/IDR", b, "IDR", min_cost=0, native=True) for b in bases}
        self.markets.update({f"ALT{i}/IDR": Market(f"ALT{i}/IDR", f"ALT{i}", "IDR", native=True) for i in range(140)})

    async def _req(self):
        self.requests += 1
        if self.delay:
            await asyncio.sleep(self.delay)

    async def load_markets(self):
        await self._req()
        return self.markets

    async def ping_binance(self):
        await self._req()
        return True

    async def fetch_ticker(self, sym):
        await self._req()
        await self._req()  # native ticker = order book + volume
        return Ticker(sym, 99.9, 100.0, 100.0, 5e9)

    async def fetch_balance(self):
        await self._req()
        return Balance({"IDR": 100_000}, {"IDR": 100_000})

    async def close(self):
        pass


async def test_preflight_request_budget_independent_of_market_count():
    from moneymaker.config import load_config
    from pathlib import Path
    cfg = load_config(Path(__file__).resolve().parent.parent / "config.example.yaml")
    ex = CountingClient()
    r = await run_preflight(Secrets(toko_api_key="k", toko_api_secret="s", live_trading=True), cfg, ex)
    assert r["ok"], r
    assert r["candidates_checked"] == 12
    assert r["alts_available"] == 140
    # 12 majors x 2 + markets + ping + balance; NOT proportional to the 152 markets.
    assert ex.requests <= 30
    assert any(z["approved"] for z in r["sizing"])


async def test_preflight_hits_deadline_with_clear_message():
    cfg = BotConfig()
    ex = CountingClient(delay=0.05)
    t0 = time.monotonic()
    r = await run_preflight(Secrets(), cfg, ex, deadline_s=20.3)  # leaves 0.3 s for tickers
    assert time.monotonic() - t0 < 5
    assert r["candidates_checked"] < 12
    assert any("Waktu cek habis" in w for w in r["warnings"])


async def test_engine_fetches_candles_once_per_candle(engine, market_data):
    from moneymaker.models import BotStatus
    engine.store.set_status(BotStatus.RUNNING)
    engine.cfg.sleeves.majors.enabled = True
    engine.markets = {SYM: market_data.markets[SYM]}
    engine.cfg.scanner.major_bases = ["BTC"]
    engine.cfg.quote = "USDT"
    await engine.cycle()
    after_first = market_data.ohlcv_calls
    assert after_first >= 1
    await engine.cycle()
    await engine.cycle()
    # Position management may read the same cached candles; no new requests within the same candle.
    assert market_data.ohlcv_calls == after_first


# ---- strategy refactor regressions ----------------------------------------------------------------------
def test_prior_max_and_mean_match_naive():
    vals = [3.0, 1.0, 4.0, 1.0, 5.0, 9.0, 2.0, 6.0, 5.0, 3.0]
    pm, pa = prior_max(vals, 3), prior_mean(vals, 3)
    for i in range(len(vals)):
        if i < 3:
            assert math.isnan(pm[i]) and math.isnan(pa[i])
        else:
            assert pm[i] == max(vals[i - 3:i])
            assert pa[i] == pytest.approx(sum(vals[i - 3:i]) / 3)


def test_entry_at_equals_entry_signal_on_every_prefix():
    s = TrendAtrStrategy(StrategyConfig())
    cs = breakout_candles(n=260)
    ind = s.prepare(cs)
    for i in range(s.min_candles - 1, len(cs)):
        a = s.entry_at(ind, i, SYM, 0)
        b = s.entry_signal(SYM, cs[: i + 1], 0)
        assert (a is None) == (b is None)
        if a:
            assert a.stop == pytest.approx(b.stop) and a.take_profit == pytest.approx(b.take_profit)


def test_resample_15m_to_1h_and_4h():
    cs = series(16 * 10, 900, 0.0, 0.01, seed=3)
    h1 = resample(cs, "1h")
    first_full = next(i for i, c in enumerate(cs) if (c.ts // 3_600_000) * 3_600_000 == c.ts)
    grp = cs[first_full:first_full + 4]
    c0 = next(c for c in h1 if c.ts == grp[0].ts)
    assert (c0.open, c0.close) == (grp[0].open, grp[-1].close)
    assert c0.high == max(c.high for c in grp) and c0.low == min(c.low for c in grp)
    assert c0.volume == pytest.approx(sum(c.volume for c in grp))
    assert all(c.ts % 14_400_000 == 0 for c in resample(cs, "4h"))


# ---- momentum surge + trailing in the simulator ---------------------------------------------------------
def pump_series() -> list[Candle]:
    base = series(200, 3600, 0.001, 0.004, seed=7, start=100)
    out = list(base)
    t = out[-1].ts
    p = out[-1].close
    surge = Candle(t + 3_600_000, p, p * 1.035, p * 0.999, p * 1.03, 8000)  # breakout on 8x volume
    out.append(surge)
    p = surge.close
    for k in range(25):  # strong run-up
        t += 3_600_000
        nxt = p * 1.012
        out.append(Candle(t + 3_600_000, p, nxt * 1.003, p * 0.997, nxt, 2000))
        p = nxt
    for k in range(10):  # then a sharp drop
        t += 3_600_000
        nxt = p * 0.97
        out.append(Candle(t + 3_600_000, p, p * 1.001, nxt * 0.998, nxt, 3000))
        p = nxt
    return out


def test_momentum_surge_trailing_rides_the_run():
    from moneymaker.backtest.runner import Costs, simulate
    cfg = MomentumConfig(volume_mult=3.0, max_extension_atr=10.0)
    s = MomentumSurgeStrategy(cfg)
    cs = pump_series()
    ind = s.prepare(cs)
    sig = s.entry_at(ind, 200, "ALT/IDR", 0)
    assert sig is not None and sig.trailing
    trades = simulate(s, ind, cs, 199, len(cs), Costs(0.002222, 0.004322, 0.0, 0.005, 3.0), "ALT/IDR")
    assert len(trades) == 1
    t = trades[0]
    fixed_target = (sig.take_profit / t.entry - 1)  # what a fixed 3R exit would have made (gross)
    assert t.reason == "trailing_stop"
    assert t.ret > fixed_target  # trailing captured more of the run than the fixed target
    assert t.ret > 0.15


# ---- radar ----------------------------------------------------------------------------------------------
def radar_fixture():
    md = StaticMarketData({})
    cfg = BotConfig()
    cfg.alt_filters.min_volume_24h = 1_000_000

    def add(sym, cs):
        b, q = sym.split("/")
        md.markets[sym] = Market(sym, b, q)
        md.candles[sym] = cs

    add("BTC/IDR", series(800, 3600, 0.0, 0.004, seed=1, start=1e9))
    add("ETH/IDR", series(800, 3600, 0.0, 0.004, seed=2, start=5e7))
    add("GOOD/IDR", series(800, 3600, 0.0005, 0.006, seed=3, start=1000, volume=5000))
    add("NEW/IDR", series(200, 3600, 0.0, 0.006, seed=4, start=1000, volume=5000))
    add("LOW/IDR", series(800, 3600, 0.0, 0.006, seed=5, start=10, volume=10))
    pumped = series(800, 3600, 0.0, 0.003, seed=6, start=1000, volume=5000)
    for k in range(24):
        c = pumped[-24 + k]
        f = 1 + 0.02 * (k + 1)
        pumped[-24 + k] = Candle(c.ts, c.open * f, c.high * f, c.low * f, c.close * f, c.volume)
    add("PUMP/IDR", pumped)
    add("USDT/IDR", series(800, 3600, 0.0, 0.0005, seed=8, start=16000, volume=1e6))
    return md, cfg


async def test_radar_filters_with_reasons_and_budget():
    md, cfg = radar_fixture()
    sw = MarketSweeper(md, cfg, lambda: md.markets)
    await sw.sweep_once()
    st = sw.stats
    assert "USDT/IDR" not in st  # stablecoins are never swept
    # one request per pair per sweep + a one-time age probe for alts that pass the volume filter (GOOD, NEW, PUMP)
    assert len(st) == 6 and md.ohlcv_calls == 6 + 3
    assert st["GOOD/IDR"].eligible and st["GOOD/IDR"].category == "alts"
    assert st["ETH/IDR"].category == "majors"
    assert st["NEW/IDR"].reason.startswith("listing baru")
    assert st["LOW/IDR"].reason == "volume kecil"
    assert "sudah naik" in st["PUMP/IDR"].reason
    assert sw.top_alts(5) == ["GOOD/IDR"]
    await sw.sweep_once()  # second sweep: no new probes
    assert md.ohlcv_calls == 9 + 6


def test_regime_blocks_alts_when_btc_weak():
    cfg = BotConfig()
    falling = series(200, 3600, -0.003, 0.002, seed=9, start=1e9)
    r = compute_regime(falling, cfg)
    assert r["btc_below_ema50"] and r["alts_blocked"] and not r["all_blocked"]
    calm = series(200, 3600, 0.0005, 0.002, seed=10, start=1e9)
    assert not compute_regime(calm, cfg)["alts_blocked"]
