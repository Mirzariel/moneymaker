from __future__ import annotations

import time

import pytest

from moneymaker.models import BotStatus, Market, Signal, Ticker
from moneymaker.risk.engine import RiskContext, RiskEngine

from .conftest import make_cfg

SYM = "ETH/USDT"


def sig(entry=100.0, stop=97.0, tp=106.0, ttl=300, now=None) -> Signal:
    now = now or time.time()
    return Signal(SYM, "buy", entry, stop, tp, now, now + ttl, "test")


def ctx(**kw) -> RiskContext:
    base = dict(now=time.time(), status=BotStatus.RUNNING, equity=1000.0, quote_free=1000.0, exposure=0.0,
                open_symbols=set(), day_start_equity=1000.0, day_pnl=0.0, consecutive_losses=0,
                ticker=Ticker(SYM, bid=99.95, ask=100.05, last=100.0, quote_volume=10_000_000),
                market=Market(SYM, "ETH", "USDT", min_amount=0.001, min_cost=5.0), last_entry_ts=None)
    base.update(kw)
    return RiskContext(**base)


@pytest.fixture
def risk():
    cfg = make_cfg()
    return RiskEngine(cfg.risk, cfg.fees, min_quote_volume_24h=500_000, max_spread_pct=0.3)


def test_approves_and_sizes_by_risk(risk):
    d = risk.evaluate(sig(), ctx())
    assert d.approved, d
    # loss fraction ~ (100.05-97)/100.05 + 0.5% + 0.2% ~= 3.75%  -> 10 / 0.0375 ~= 266 -> capped at 20% = 200
    assert d.cost == pytest.approx(200.0)
    assert any("max_position_pct" in n for n in d.notes)


def test_uncapped_size_matches_risk_budget():
    cfg = make_cfg(max_position_pct=50.0)
    r = RiskEngine(cfg.risk, cfg.fees, 0, 1)
    d = r.evaluate(sig(), ctx())
    loss_frac = (100.05 - 97) / 100.05 + 0.005 + 0.002
    assert d.cost == pytest.approx(10 / loss_frac, rel=1e-6)
    assert d.cost * loss_frac == pytest.approx(10.0, rel=1e-6)  # exactly 1% of equity at risk


@pytest.mark.parametrize("override,rule", [
    ({"status": BotStatus.PAUSED}, "bot_status"),
    ({"open_symbols": {SYM}}, "already_in_position"),
    ({"open_symbols": {"A/USDT", "B/USDT", "C/USDT"}}, "max_open_positions"),
    ({"day_pnl": -31.0}, "max_daily_loss"),
    ({"consecutive_losses": 4}, "consecutive_losses"),
    ({"last_entry_ts": time.time() - 60}, "cooldown"),
    ({"market": Market(SYM, "ETH", "USDT", active=False)}, "market_inactive"),
    ({"market": Market(SYM, "ETH", "USDT", supports_stop_limit=False)}, "no_stop_limit"),
    ({"ticker": Ticker(SYM, bid=99.0, ask=101.0, last=100.0, quote_volume=1e7)}, "max_spread"),
    ({"ticker": Ticker(SYM, bid=99.95, ask=100.05, last=100.0, quote_volume=1000)}, "min_liquidity"),
    ({"ticker": Ticker(SYM, bid=101.95, ask=102.0, last=102.0, quote_volume=1e7)}, "entry_drift"),
    ({"quote_free": 3.0}, "below_min_notional"),
    ({"exposure": 600.0}, "below_min_notional"),
])
def test_rejections(risk, override, rule):
    d = risk.evaluate(sig(), ctx(**override))
    assert not d.approved
    assert d.rule == rule


def test_expired_signal_rejected(risk):
    d = risk.evaluate(sig(now=time.time() - 1000, ttl=300), ctx())
    assert d.rule == "signal_expired"


def test_stop_above_price_rejected(risk):
    d = risk.evaluate(sig(stop=100.5), ctx())
    assert d.rule == "stop_above_price"


def test_insufficient_edge_vs_fees():
    cfg = make_cfg()
    cfg.fees.buy_pct, cfg.fees.sell_pct = 0.2222, 0.4322  # Tokocrypto IDR taker, all-in
    r = RiskEngine(cfg.risk, cfg.fees, 0, 1)
    d = r.evaluate(sig(tp=101.9), ctx())  # ~1.85% target < 3 x 0.65% round trip
    assert d.rule == "insufficient_edge"


def test_never_exceeds_free_quote(risk):
    d = risk.evaluate(sig(), ctx(quote_free=50.0))
    assert d.approved
    assert d.cost <= 50.0 * 0.99 + 1e-9


def test_circuit_breakers(risk):
    assert risk.circuit_breaker(ctx(day_pnl=-29.0)) is None
    assert "daily loss" in risk.circuit_breaker(ctx(day_pnl=-30.0))
    assert "consecutive" in risk.circuit_breaker(ctx(consecutive_losses=4))
