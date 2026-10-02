"""End-to-end scenarios against PaperExchange + StaticMarketData (no network)."""
from __future__ import annotations

import pytest

from moneymaker.control import ControlService
from moneymaker.db import Store
from moneymaker.engine import Engine
from moneymaker.exchange.paper import PaperExchange
from moneymaker.execution.reconcile import reconcile
from moneymaker.models import BotStatus
from moneymaker.notify.base import Notifier

from .conftest import SYM


async def open_one(engine: Engine):
    engine.store.set_status(BotStatus.RUNNING)
    await engine.cycle()
    positions = engine.store.open_positions()
    assert len(positions) == 1, engine.store.rows("SELECT * FROM signals")
    return positions[0]


async def test_fresh_install_is_paused_and_does_not_trade(engine):
    assert engine.store.status == BotStatus.PAUSED
    await engine.cycle()
    assert engine.store.open_positions() == []


async def test_entry_places_exchange_stop(engine, paper):
    pos = await open_one(engine)
    assert pos.symbol == SYM
    assert pos.stop_order_id is not None
    stop = paper.orders[pos.stop_order_id]
    assert stop.type == "stop_limit" and stop.status == "open"
    assert stop.amount == pytest.approx(pos.amount)
    assert stop.stop_price == pytest.approx(pos.stop_price, rel=1e-6)
    assert pos.cost <= 200.0 + 1e-6  # max_position_pct 20% of 1000
    bal = await paper.fetch_balance()
    assert bal.free_of("BTC") == pytest.approx(0.0, abs=1e-9)  # all base locked in the stop


async def test_stop_fill_closes_position_with_loss(engine, paper, market_data):
    pos = await open_one(engine)
    market_data.set_price(SYM, pos.stop_price * 0.999, spread_pct=0.01)  # between trigger and limit
    await engine.cycle()
    assert engine.store.open_positions() == []
    row = engine.store.rows("SELECT * FROM positions WHERE id=?", (pos.id,))[0]
    assert row["exit_reason"] == "stop_loss"
    assert row["pnl"] < 0
    # Loss must stay within the configured risk budget (1% of 1000 equity) plus a small tolerance.
    assert row["pnl"] > -10.5


async def test_gap_through_stop_limit_falls_back_to_market(engine, market_data):
    pos = await open_one(engine)
    market_data.set_price(SYM, pos.stop_price * 0.97)  # far below the stop-limit price
    await engine.cycle()
    row = engine.store.rows("SELECT * FROM positions WHERE id=?", (pos.id,))[0]
    assert row["status"] == "closed"
    assert row["exit_reason"] == "stop_gap_fallback"


async def test_take_profit(engine, market_data, paper):
    pos = await open_one(engine)
    market_data.set_price(SYM, pos.take_profit * 1.001)
    await engine.cycle()
    row = engine.store.rows("SELECT * FROM positions WHERE id=?", (pos.id,))[0]
    assert row["exit_reason"] == "take_profit"
    assert row["pnl"] > 0
    assert paper.orders[pos.stop_order_id].status == "canceled"


async def test_pause_blocks_entries_but_keeps_protection(engine, market_data):
    pos = await open_one(engine)
    control = ControlService(engine)
    await control.pause("test")
    market_data.set_price(SYM, pos.stop_price * 0.999, spread_pct=0.01)
    await engine.cycle()
    assert engine.store.open_positions() == []  # stop still handled while paused


async def test_panic_cancels_sells_and_stays_paused(engine, paper, store, tmp_path, collected):
    pos = await open_one(engine)
    control = ControlService(engine)
    report = await control.panic("test")
    assert report["cancelled"] >= 1
    assert report["errors"] == []
    assert store.status == BotStatus.PAUSED
    assert store.open_positions() == []
    assert paper.orders[pos.stop_order_id].status == "canceled"
    bal = await paper.fetch_balance()
    assert bal.total_of("BTC") == pytest.approx(0.0, abs=1e-9)
    assert "PANIC done" in collected.msgs[-1]
    # Restart: still paused, no new entries even though the breakout signal is still there.
    store2 = Store(tmp_path / "bot.db")
    eng2 = Engine(paper, store2, Notifier(), engine.cfg)
    await eng2.cycle()
    assert store2.status == BotStatus.PAUSED
    assert store2.open_positions() == []


async def test_daily_loss_breaker_auto_pauses(engine, market_data, store):
    await open_one(engine)
    # A losing trade closed earlier today: -40 on ~1000 equity breaches the 3% daily limit.
    pid = store.open_position(symbol="ETH/USDT", amount=1, entry_price=100, cost=100, stop_price=90,
                              take_profit=120, fees=0)
    store.close_position(pid, exit_price=60, proceeds=60, extra_fees=0, reason="test")
    await engine.cycle()
    assert store.status == BotStatus.PAUSED
    assert "circuit breaker" in store.get("bot_status_reason")


async def test_crash_recovery_stop_filled_while_offline(engine, paper, market_data, store, tmp_path, cfg):
    pos = await open_one(engine)
    # Process dies. While offline the stop fills on the exchange.
    market_data.set_price(SYM, pos.stop_price * 0.999, spread_pct=0.01)
    await paper.fetch_balance()  # exchange-side matching happens regardless of the bot
    paper2 = PaperExchange(market_data, "USDT", 1000.0, cfg.fees.taker_pct, 0.0, state_path=tmp_path / "paper.json")
    eng2 = Engine(paper2, Store(tmp_path / "bot.db"), Notifier(), cfg)
    findings = await reconcile(eng2)
    assert eng2.store.open_positions() == []
    assert any("closed while offline" in f for f in findings)


async def test_crash_recovery_replaces_missing_stop(engine, paper):
    pos = await open_one(engine)
    await paper.cancel_order(pos.stop_order_id, SYM)  # e.g. cancelled by hand in the app
    await reconcile(engine)
    [p] = engine.store.open_positions()
    assert p.stop_order_id and p.stop_order_id != pos.stop_order_id
    assert paper.orders[p.stop_order_id].status == "open"


async def test_unknown_entry_outcome_pauses(engine, store):
    store.set_status(BotStatus.RUNNING)
    store.insert_order(client_id="mmeX", symbol=SYM, side="buy", type="market", purpose="entry", cost=100)
    findings = await reconcile(engine)
    assert store.status == BotStatus.PAUSED
    assert any("unknown outcome" in f for f in findings)


async def test_cooldown_prevents_immediate_reentry(engine, market_data, store):
    pos = await open_one(engine)
    market_data.set_price(SYM, pos.take_profit * 1.001)
    await engine.cycle()  # take profit
    market_data.set_price(SYM, 124.0, spread_pct=0.05)
    engine._last_eval.clear()
    await engine.cycle()
    assert store.open_positions() == []
    assert store.rows("SELECT * FROM risk_events WHERE rule='cooldown'")
