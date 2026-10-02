"""Walk-forward learning: must accept real edges, refuse losing setups, never peek at test data,
and stop a sleeve when live results contradict the model. Plus trailing-stop safety in the executor."""
from __future__ import annotations

import time

import pytest

from moneymaker.backtest.runner import Costs, resample
from moneymaker.config import BotConfig
from moneymaker.db import Store
from moneymaker.engine import Engine
from moneymaker.exchange.paper import PaperExchange, StaticMarketData
from moneymaker.learning import MAJOR_GRID, evaluate_grid, majors_strategy, sleeve_result, split_index
from moneymaker.models import BotStatus, Candle, Market, Ticker
from moneymaker.notify.base import Notifier

from .synth import series

SYM = "BTC/IDR"


def learn_cfg() -> BotConfig:
    cfg = BotConfig()
    cfg.paper.slippage_pct = 0.0
    cfg.learning.history_days = 30
    cfg.sleeves.alts.enabled = False
    cfg.scanner.major_bases = ["BTC"]
    cfg.scanner.min_quote_volume_24h = 0
    return cfg


def staircase(n: int, seed: int) -> list[Candle]:
    """15m candles: a strongly trending market (synthetic edge for a breakout strategy)."""
    return series(n, 900, 0.0012, 0.004, seed=seed, start=1e9)


def downtrend(n: int, seed: int) -> list[Candle]:
    return series(n, 900, -0.0008, 0.004, seed=seed, start=1e9)


def grid_rows(cands15: list[Candle], cfg: BotConfig, tf: str = "1h"):
    s = {SYM: resample(cands15, tf)}
    return evaluate_grid(s, MAJOR_GRID, lambda p: majors_strategy(cfg, p, tf), lambda p: (tf, p["breakout_lookback"]),
                         Costs.from_cfg(cfg), cfg.learning.train_fraction)


def test_accepts_a_real_trend_edge():
    cfg = learn_cfg()
    res = sleeve_result(grid_rows(staircase(31 * 96, 1), cfg), cfg.learning.majors_min_pf, cfg, [SYM], "1h")
    assert res["valid"] and res["status"] == "ok", res["reason"]
    assert res["test"]["trades"] >= cfg.learning.min_test_trades


def test_refuses_a_losing_market():
    cfg = learn_cfg()
    res = sleeve_result(grid_rows(downtrend(31 * 96, 2), cfg), cfg.learning.majors_min_pf, cfg, [SYM], "1h")
    assert not res["valid"] and res["status"] == "no_edge"
    assert res["reason"].startswith("Belum ada keunggulan")


def test_parameter_choice_never_sees_test_data():
    cfg = learn_cfg()
    cs = staircase(31 * 96, 3)
    a = sleeve_result(grid_rows(cs, cfg), 1.15, cfg, [SYM], "1h")
    cut = split_index(resample(cs, "1h"), cfg.learning.train_fraction)
    cut_ts = resample(cs, "1h")[cut].ts
    crashed = [c if c.ts < cut_ts else Candle(c.ts, c.open * 0.5, c.high * 0.5, c.low * 0.5, c.close * 0.5, c.volume)
               for c in cs]  # wreck everything after the split (same timestamps)
    b = sleeve_result(grid_rows(crashed, cfg), 1.15, cfg, [SYM], "1h")
    assert a["params"] == b["params"]       # selection identical: it only used train data
    assert a["train"] == b["train"]
    assert a["test"] != b["test"]           # ...while the test result reflects the changed future


# ---- trainer + engine integration -----------------------------------------------------------------------
def make_engine(tmp_path, candles15: list[Candle]) -> tuple[Engine, StaticMarketData]:
    cfg = learn_cfg()
    md = StaticMarketData({SYM: Market(SYM, "BTC", "IDR", min_cost=0)})
    md.candles[SYM] = candles15
    md.set_price(SYM, candles15[-1].close, quote_volume=1e12)
    ex = PaperExchange(md, "IDR", 100_000, cfg.fees.buy_pct, cfg.fees.sell_pct, 0.0, state_path=tmp_path / "p.json")
    eng = Engine(ex, Store(tmp_path / "b.db"), Notifier(), cfg)
    eng.markets = md.markets
    return eng, md


async def test_trainer_applies_validated_model(tmp_path):
    eng, _ = make_engine(tmp_path, staircase(31 * 96, 1))
    assert eng.sleeve_block("majors")  # untrained -> no entries
    await eng.trainer.train(("majors",))
    assert eng.trainer.job["state"] == "done", eng.trainer.job
    m = eng.model.load()["majors"]
    assert m["valid"], m["reason"]
    assert len(m["per_timeframe"]) == 4
    assert eng.sleeve_block("majors") is None
    assert eng.strategies["majors"].timeframe == m["timeframe"]
    assert eng.strategies["majors"].cfg.breakout_lookback == m["params"]["breakout_lookback"]


async def test_no_edge_means_bot_stays_idle(tmp_path):
    eng, _ = make_engine(tmp_path, downtrend(31 * 96, 2))
    await eng.trainer.train(("majors",))
    m = eng.model.load()["majors"]
    assert m["status"] == "no_edge"
    assert eng.sleeve_block("majors") == m["reason"]
    eng.store.set_status(BotStatus.RUNNING)
    await eng.cycle()
    assert eng.store.open_positions() == []


async def test_training_error_is_reported(tmp_path):
    eng, md = make_engine(tmp_path, staircase(31 * 96, 1))

    async def boom(*a, **k):
        raise RuntimeError("network down")

    md.fetch_ohlcv = boom
    await eng.trainer.train(("majors",))
    # download failures are tolerated per symbol; with no data the sleeve says why
    assert eng.model.load()["majors"]["reason"] == "Riwayat harga tidak bisa diunduh"


async def test_live_monitor_degrades_and_retrains(tmp_path):
    eng, _ = make_engine(tmp_path, staircase(10, 1))
    eng.model.save_sleeve("majors", {"status": "ok", "valid": True, "reason": "lolos", "timeframe": "1h",
                                     "params": {"breakout_lookback": 20}, "symbols": [SYM],
                                     "trained_at": time.time() - 10, "train": None, "test": None,
                                     "per_timeframe": []})
    started = []
    eng.trainer.start = lambda sleeves=("majors", "alts"): started.append(sleeves)
    for _ in range(5):
        pid = eng.store.open_position(symbol=SYM, amount=1, entry_price=100, cost=100, stop_price=97,
                                      take_profit=106, fees=0, sleeve="majors")
        eng.store.close_position(pid, exit_price=96, proceeds=96, extra_fees=0, reason="stop_loss")
    await eng.check_live_performance()
    m = eng.model.load()["majors"]
    assert m["status"] == "degraded" and not m["valid"]
    assert started == [("majors",)]
    assert eng.sleeve_block("majors").startswith("Performa live menurun")


# ---- trailing stop in the executor (exchange-side stop replacement) ----------------------------------------
async def open_trailing(tmp_path):
    eng, md = make_engine(tmp_path, staircase(10, 1))
    md.set_price(SYM, 100.0, spread_pct=0.01)
    ex = eng.ex
    await ex.market_buy(SYM, 50_000, "b")
    amount = (await ex.fetch_balance()).free_of("BTC")
    pid = eng.store.open_position(symbol=SYM, amount=amount, entry_price=100, cost=50_000, stop_price=96,
                                  take_profit=112, fees=0, sleeve="alts", trailing=True, trail_mult=3, atr=1.0)
    assert await eng.executor.place_stop(pid, SYM, amount, 96) is not None
    return eng, md, ex, pid


async def test_trailing_stop_moves_up_on_exchange(tmp_path):
    eng, md, ex, pid = await open_trailing(tmp_path)
    [pos] = eng.store.open_positions()
    old_id = pos.stop_order_id
    md.set_price(SYM, 110.0, spread_pct=0.01)
    await eng.executor.update_trailing(pos, await ex.fetch_ticker(SYM))
    [pos2] = eng.store.open_positions()
    assert pos2.stop_price == pytest.approx(110.0 * (1 - 0.00005) - 3.0, rel=1e-3)  # highest - 3 x ATR
    assert ex.orders[old_id].status == "canceled"
    assert ex.orders[pos2.stop_order_id].status == "open"
    md.set_price(SYM, 105.0, spread_pct=0.01)  # price falls back: stop never moves down
    await eng.executor.update_trailing(pos2, await ex.fetch_ticker(SYM))
    assert eng.store.open_positions()[0].stop_price == pytest.approx(pos2.stop_price)


async def test_trailing_failed_replace_restores_old_stop(tmp_path):
    eng, md, ex, pid = await open_trailing(tmp_path)
    [pos] = eng.store.open_positions()
    real = ex.stop_loss_limit_sell
    calls = {"n": 0}

    async def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("exchange hiccup")
        return await real(*a, **k)

    ex.stop_loss_limit_sell = flaky
    md.set_price(SYM, 110.0, spread_pct=0.01)
    await eng.executor.update_trailing(pos, await ex.fetch_ticker(SYM))
    [pos2] = eng.store.open_positions()
    assert pos2.stop_price == pytest.approx(96)                # old level restored
    assert ex.orders[pos2.stop_order_id].status == "open"      # still protected on the exchange


async def test_trailing_both_failures_sell_position(tmp_path):
    eng, md, ex, pid = await open_trailing(tmp_path)
    [pos] = eng.store.open_positions()

    async def broken(*a, **k):
        raise RuntimeError("exchange down")

    ex.stop_loss_limit_sell = broken
    md.set_price(SYM, 110.0, spread_pct=0.01)
    await eng.executor.update_trailing(pos, Ticker(SYM, 110, 110.01, 110))
    assert eng.store.open_positions() == []  # never left without protection
