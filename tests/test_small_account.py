"""Rp100k account: trades must actually happen, within the risk caps."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from moneymaker.config import load_config
from moneymaker.db import Store
from moneymaker.engine import Engine
from moneymaker.exchange.paper import PaperExchange, StaticMarketData
from moneymaker.exchange.tokocrypto import TokocryptoClient
from moneymaker.models import BotStatus, Market, Signal, Ticker
from moneymaker.notify.base import Notifier
from moneymaker.risk.engine import RiskContext, RiskEngine

from .conftest import Collect, breakout_candles

PRESET = Path(__file__).resolve().parent.parent / "config.example.yaml"
SYM = "BTC/IDR"
PRICE = 1_500_000_000.0  # BTC in IDR


def idr_market(stop_limit: bool = True) -> Market:
    return Market(SYM, "BTC", "IDR", min_amount=0.00000001, min_cost=0.0, supports_stop_limit=stop_limit, native=True)


def ctx(equity=100_000.0, **kw) -> RiskContext:
    base = dict(now=time.time(), status=BotStatus.RUNNING, equity=equity, quote_free=equity, exposure=0.0,
                open_symbols=set(), day_start_equity=equity, day_pnl=0.0, consecutive_losses=0,
                ticker=Ticker(SYM, bid=PRICE * 0.9995, ask=PRICE, last=PRICE, quote_volume=5e9),
                market=idr_market(), last_entry_ts=None)
    base.update(kw)
    return RiskContext(**base)


def sig(stop_pct: float, tp_pct: float = 10.0) -> Signal:
    now = time.time()
    return Signal(SYM, "buy", PRICE, PRICE * (1 - stop_pct / 100), PRICE * (1 + tp_pct / 100), now, now + 300, "t")


@pytest.fixture
def preset():
    return load_config(PRESET)


def test_preset_loads_with_verified_fees(preset):
    assert preset.quote == "IDR"
    assert preset.fees.round_trip_pct == pytest.approx(0.6544)
    assert preset.risk.min_order_quote == 20_000


@pytest.mark.parametrize("stop_pct", [2.0, 3.0, 5.0, 8.0])
def test_rp100k_typical_signals_are_traded(preset, stop_pct):
    r = RiskEngine(preset.risk, preset.fees, 0, 1)
    d = r.evaluate(sig(stop_pct), ctx())
    assert d.approved, d
    assert 25_000 <= d.cost <= 50_000
    loss_frac = stop_pct / 100 + 0.005 + 0.006544
    assert d.cost * loss_frac <= 100_000 * 0.03 + 1  # never more than 3% of equity at risk


def test_bump_to_minimum_only_within_risk_cap(preset):
    r = RiskEngine(preset.risk, preset.fees, 0, 1)
    d = r.evaluate(sig(8.0), ctx())
    assert d.approved and any("bumped" in n for n in d.notes)
    d = r.evaluate(sig(15.0, tp_pct=40), ctx())  # Rp25k at a 15% stop risks ~4% > 3%
    assert d.rule == "below_min_notional"


def test_no_bump_when_disabled(preset):
    preset.risk.allow_min_size_bump = False
    r = RiskEngine(preset.risk, preset.fees, 0, 1)
    assert r.evaluate(sig(8.0), ctx()).rule == "below_min_notional"


def test_second_position_and_exposure_cap(preset):
    r = RiskEngine(preset.risk, preset.fees, 0, 1)
    d = r.evaluate(sig(3.0), ctx(exposure=48_000, quote_free=52_000, open_symbols={"ETH/IDR"}))
    assert d.approved and d.cost <= 42_000
    d = r.evaluate(sig(3.0), ctx(exposure=70_000, quote_free=30_000, open_symbols={"ETH/IDR"}))
    assert d.rule == "below_min_notional"  # only Rp20k of exposure room left


def test_no_exchange_stop_rejected_unless_allowed(preset):
    preset.risk.allow_bot_side_stop = False
    r = RiskEngine(preset.risk, preset.fees, 0, 1)
    assert r.evaluate(sig(3.0), ctx(market=idr_market(stop_limit=False))).rule == "no_stop_limit"


# ---- end to end with a Rp100k paper account on a market without exchange stops -------------------------
@pytest.fixture
def small(tmp_path, preset):
    preset.paper.slippage_pct = 0.0
    md = StaticMarketData({SYM: idr_market(stop_limit=False)})
    candles = breakout_candles(start=1.2e9, end=1.45e9, last_close=1.5e9)
    md.candles[SYM] = candles
    md.set_price(SYM, 1.5e9, spread_pct=0.05, quote_volume=5e9)
    ex = PaperExchange(md, "IDR", 100_000, preset.fees.buy_pct, preset.fees.sell_pct, 0.0,
                       state_path=tmp_path / "p.json")
    msgs = Collect()
    n = Notifier()
    n.add(msgs)
    eng = Engine(ex, Store(tmp_path / "b.db"), n, preset)
    eng.store.set_status(BotStatus.RUNNING)
    return eng, md, ex, msgs


async def test_rp100k_end_to_end_bot_side_stop(small):
    eng, md, ex, msgs = small
    await eng.cycle()
    [pos] = eng.store.open_positions()
    assert pos.stop_order_id is None  # no exchange stop possible on this market
    assert 25_000 <= pos.cost <= 50_000
    assert any("dijaga BOT" in m for m in msgs.msgs)
    md.set_price(SYM, pos.stop_price * 0.998, spread_pct=0.05, quote_volume=5e9)
    await eng.cycle()
    row = eng.store.rows("SELECT * FROM positions WHERE id=?", (pos.id,))[0]
    assert row["exit_reason"] == "stop_loss_bot"
    assert -3_000 < row["pnl"] < 0
    bal = await ex.fetch_balance()
    assert bal.total_of("IDR") == pytest.approx(100_000 + row["pnl"], abs=1)


async def test_paper_applies_asymmetric_fees(small):
    _, md, ex, _ = small
    await ex.market_buy(SYM, 50_000, "b")
    held = (await ex.fetch_balance()).free_of("BTC")
    await ex.market_sell(SYM, held, "s")
    idr = (await ex.fetch_balance()).total_of("IDR")
    bid, ask = md.tickers[SYM].bid, md.tickers[SYM].ask
    expected = 100_000 - 50_000 + 50_000 / ask * (1 - 0.002222) * bid * (1 - 0.004322)
    assert idr == pytest.approx(expected, abs=20)  # 1e-8 BTC amount rounding = Rp15
    assert 100_000 - idr == pytest.approx(50_000 * 0.0065 + 50_000 * 0.0005, abs=30)  # fees + spread


# ---- Tokocrypto native-market tickers (no network) ------------------------------------------------------
async def test_native_ticker_built_from_order_book_and_klines(monkeypatch):
    c = TokocryptoClient()
    c._markets = {SYM: idr_market(), "ETH/USDT": Market("ETH/USDT", "ETH", "USDT")}

    async def blocked(*a, **k):
        raise RuntimeError("api.binance.com blocked")

    async def order_book(symbol, limit=None):
        return {"bids": [[100.0, 1]], "asks": [[101.0, 1]]}

    async def ohlcv(symbol, timeframe, since=None, limit=None):
        return [[i * 3_600_000, 100, 101, 99, 100, 2.0] for i in range(25)]

    monkeypatch.setattr(c.ex, "fetch_tickers", blocked)
    monkeypatch.setattr(c.ex, "fetch_order_book", order_book)
    monkeypatch.setattr(c.ex, "fetch_ohlcv", ohlcv)
    try:
        t = await c.fetch_tickers([SYM])
        assert t[SYM].bid == 100 and t[SYM].ask == 101
        assert t[SYM].quote_volume == pytest.approx(24 * 2.0 * 100)
        all_t = await c.fetch_tickers(None)  # binance blocked, native still returned
        assert set(all_t) == {SYM}
        assert (await c.fetch_ticker(SYM)).last == pytest.approx(100.5)
    finally:
        await c.close()
