from __future__ import annotations

import time

import pytest

from moneymaker.backtest.runner import backtest
from moneymaker.config import StrategyConfig
from moneymaker.models import Candle
from moneymaker.strategy.base import atr, closed_candles, ema
from moneymaker.strategy.trend_atr import TrendAtrStrategy

from .conftest import SYM, breakout_candles, make_cfg


def test_ema_and_atr_basics():
    assert ema([1, 1, 1], 2) == [1, 1, 1]
    cs = [Candle(i, 10, 11, 9, 10, 1) for i in range(30)]
    assert atr(cs, 14)[-1] == pytest.approx(2.0)


def test_breakout_produces_signal_with_sane_levels():
    s = TrendAtrStrategy(StrategyConfig())
    now = time.time()
    sig = s.entry_signal(SYM, breakout_candles(), now)
    assert sig is not None
    assert sig.stop < sig.entry < sig.take_profit
    assert sig.take_profit - sig.entry == pytest.approx(2 * (sig.entry - sig.stop))
    assert sig.expires_at == pytest.approx(now + 300)


def test_no_signal_in_downtrend():
    s = TrendAtrStrategy(StrategyConfig())
    cs = breakout_candles(start=150, end=110, last_close=109)
    assert s.entry_signal(SYM, cs, time.time()) is None


def test_closed_candles_drops_forming_candle():
    now = time.time()
    cs = breakout_candles(now=now)
    forming = Candle(cs[-1].ts + 3_600_000, 1, 1, 1, 1, 1)
    assert closed_candles(cs + [forming], "1h", now) == cs


async def test_paper_fees_and_locking(paper):
    o = await paper.market_buy(SYM, 100.0, "c1")
    assert o.fee_currency == "BTC"
    bal = await paper.fetch_balance()
    assert bal.total_of("USDT") == pytest.approx(900.0)
    held = bal.free_of("BTC")
    assert held == pytest.approx(100 / 124.031 * 0.999, rel=1e-3)
    await paper.stop_loss_limit_sell(SYM, held, 100.0, 99.0, "c2")
    assert (await paper.fetch_balance()).free_of("BTC") == pytest.approx(0.0, abs=1e-12)
    with pytest.raises(ValueError):
        await paper.market_sell(SYM, held, "c3")  # locked by the stop


async def test_paper_state_survives_restart(paper, market_data, tmp_path):
    from moneymaker.exchange.paper import PaperExchange
    await paper.market_buy(SYM, 50.0, "c1")
    p2 = PaperExchange(market_data, "USDT", 1000.0, 0.1, 0.1, 0.0, state_path=tmp_path / "paper.json")
    assert (await p2.fetch_balance()).total_of("USDT") == pytest.approx(950.0)


def _series(n=1500):
    """Deterministic up/down waves so the backtest has wins, losses and fees."""
    import math
    out, price = [], 100.0
    for i in range(n):
        drift = 0.004 * math.sin(i / 60) + 0.0005
        o = price
        price = price * (1 + drift + 0.003 * math.sin(i * 1.7))
        out.append(Candle(i * 3_600_000, o, max(o, price) * 1.003, min(o, price) * 0.997, price, 10))
    return out


def test_backtest_runs_net_of_fees():
    cfg = make_cfg()
    r = backtest(SYM, _series(), cfg)
    assert len(r.trades) > 0
    assert r.fees_paid > 0
    assert r.end_equity == pytest.approx(r.start_equity + sum(t.pnl for t in r.trades), rel=1e-9)
    assert 0 <= r.max_drawdown_pct < 100
    for t in r.trades:
        assert t.exit_ts >= t.entry_ts


def test_backtest_higher_fees_never_help():
    cheap, pricey = make_cfg(), make_cfg()
    pricey.fees.buy_pct = pricey.fees.sell_pct = 0.75
    pricey.risk.min_edge_fee_multiple = 0  # same trades, only fees differ
    cheap.risk.min_edge_fee_multiple = 0
    a, b = backtest(SYM, _series(), cheap), backtest(SYM, _series(), pricey)
    assert b.end_equity < a.end_equity
