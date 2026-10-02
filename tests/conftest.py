from __future__ import annotations

import math
import time

import pytest

from moneymaker.config import BotConfig
from moneymaker.db import Store
from moneymaker.engine import Engine
from moneymaker.exchange.paper import PaperExchange, StaticMarketData
from moneymaker.models import Candle, Market
from moneymaker.notify.base import Notifier

SYM = "BTC/USDT"


def make_cfg(**risk) -> BotConfig:
    cfg = BotConfig()
    cfg.quote = "USDT"
    cfg.fees.buy_pct = 0.1
    cfg.fees.sell_pct = 0.1
    cfg.paper.slippage_pct = 0.0
    cfg.scanner.min_quote_volume_24h = 1_000
    # Classic single-strategy setup: no learning gate, majors only, 1% risk.
    cfg.learning.require_validated_edge = False
    cfg.sleeves.alts.enabled = False
    cfg.sleeves.majors.risk_per_trade_pct = 1.0
    for k, v in risk.items():
        setattr(cfg.risk, k, v)
    return cfg


def breakout_candles(n: int = 220, start: float = 100.0, end: float = 120.0, last_close: float = 124.0,
                     now: float | None = None) -> list[Candle]:
    """Steady uptrend with a breakout on the last CLOSED candle, aligned to the current hour."""
    now = now or time.time()
    hour_ms = 3_600_000
    last_ts = (int(now * 1000) // hour_ms) * hour_ms - hour_ms  # last fully closed candle
    out = []
    for i in range(n):
        ts = last_ts - (n - 1 - i) * hour_ms
        base = start + (end - start) * i / (n - 1) + 0.3 * math.sin(i / 3)
        close = last_close if i == n - 1 else base
        open_ = out[-1].close if out else base
        out.append(Candle(ts, open_, max(open_, close) * 1.002, min(open_, close) * 0.998, close, 1000))
    return out


class Collect:
    def __init__(self):
        self.msgs: list[str] = []

    async def __call__(self, text: str) -> None:
        self.msgs.append(text)


@pytest.fixture
def market_data():
    md = StaticMarketData({SYM: Market(SYM, "BTC", "USDT", min_amount=0.0001, min_cost=5.0)})
    md.set_price(SYM, 124.0, spread_pct=0.05)
    md.candles[SYM] = breakout_candles()
    return md


@pytest.fixture
def cfg():
    return make_cfg()


@pytest.fixture
def paper(market_data, cfg, tmp_path):
    return PaperExchange(market_data, "USDT", 1000.0, cfg.fees.buy_pct, cfg.fees.sell_pct, cfg.paper.slippage_pct,
                         state_path=tmp_path / "paper.json")


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "bot.db")
    yield s
    s.close()


@pytest.fixture
def collected():
    return Collect()


@pytest.fixture
def engine(paper, store, cfg, collected):
    n = Notifier()
    n.add(collected)
    return Engine(paper, store, n, cfg)
