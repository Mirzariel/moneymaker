"""Configuration.

Secrets and deployment switches come from environment variables / `.env`.
Trading parameters come from `config.yaml` (copy `config.example.yaml`).
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Secrets(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    toko_api_key: str = ""
    toko_api_secret: str = ""
    # Real orders are only ever sent when this is true AND the bot was resumed manually.
    live_trading: bool = False

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    api_token: str = ""
    api_host: str = "127.0.0.1"
    api_port: int = 8000

    healthcheck_url: str = ""

    config_path: str = "config.yaml"
    # Empty = data/live.db or data/paper.db depending on LIVE_TRADING (the bot never mixes the two).
    db_path: str = ""
    paper_state_path: str = "data/paper_state.json"

    @property
    def database_path(self) -> str:
        return self.db_path or ("data/live.db" if self.live_trading else "data/paper.db")

    def missing_for_start(self) -> list[str]:
        """Fields that must be filled in before the bot can trade."""
        missing = []
        if self.live_trading:
            if not self.toko_api_key:
                missing.append("toko_api_key")
            if not self.toko_api_secret:
                missing.append("toko_api_secret")
        return missing


class ScannerConfig(BaseModel):
    min_quote_volume_24h: float = 500_000.0
    max_spread_pct: float = 0.3
    max_symbols: int = 15
    refresh_minutes: int = 30
    whitelist: list[str] = Field(default_factory=list)
    blacklist: list[str] = Field(default_factory=list)
    # Stablecoin and leveraged-token bases are excluded by default.
    exclude_bases: list[str] = Field(
        default_factory=lambda: ["USDT", "USDC", "BUSD", "FDUSD", "TUSD", "DAI", "BIDR", "IDRT"]
    )


class StrategyConfig(BaseModel):
    name: Literal["trend_atr"] = "trend_atr"
    ema_fast: int = 20
    ema_slow: int = 50
    breakout_lookback: int = 20
    atr_period: int = 14
    atr_stop_mult: float = 2.0
    reward_risk: float = 2.0
    signal_ttl_sec: int = 300


class RiskConfig(BaseModel):
    risk_per_trade_pct: float = 1.0
    max_position_pct: float = 20.0
    max_open_positions: int = 3
    max_exposure_pct: float = 60.0
    max_daily_loss_pct: float = 3.0
    max_consecutive_losses: int = 4
    cooldown_minutes: int = 60
    # Reject if the live ask has run away from the signal's entry price by more than this.
    max_entry_drift_pct: float = 1.0
    min_edge_fee_multiple: float = 3.0
    # Stop-limit sell is placed this far below the trigger so it still fills on a fast drop.
    stop_limit_offset_pct: float = 0.5
    # Keep a small quote buffer so fees never make an order fail for insufficient balance.
    quote_buffer_pct: float = 1.0
    # Exchange minimum order in quote currency, used when the market metadata doesn't carry one
    # (Tokocrypto IDR pairs: Rp20.000). The larger of this and the market's own minimum applies.
    min_order_quote: float = 0.0
    # Size at least this multiple of the minimum, so the position can still be sold after a drop + fees.
    min_notional_headroom: float = 1.25
    # Small accounts: if the risk-based size is below the exchange minimum, bump it up to the minimum
    # as long as the trade then risks at most `max_risk_pct_on_bump` of equity. Otherwise reject.
    allow_min_size_bump: bool = False
    max_risk_pct_on_bump: float = 3.0
    # If a market can't hold a STOP_LOSS_LIMIT order on the exchange, allow trading it with a stop that
    # the bot enforces itself. That stop only works while the bot is running.
    allow_bot_side_stop: bool = False


class FeeConfig(BaseModel):
    """All-in cost per side in %, including tax and exchange levies. The bot uses market (taker) orders.

    Defaults: Tokocrypto IDR pairs, taker, from 18 Jun 2026 (fee 0.20% + ICEx 0.0222%, plus PPh 0.21% on
    sells under PMK 50/2025). Check the fee page in your account and update if your tier differs.
    """
    buy_pct: float = 0.2222
    sell_pct: float = 0.4322

    @property
    def round_trip_pct(self) -> float:
        return self.buy_pct + self.sell_pct


class PaperConfig(BaseModel):
    starting_quote_balance: float = 1000.0
    slippage_pct: float = 0.1


class BotConfig(BaseModel):
    quote: str = "IDR"
    timeframe: str = "1h"
    loop_interval_sec: int = 60
    timezone: str = "Asia/Jakarta"
    panic_scope: Literal["bot_positions", "all_non_quote"] = "bot_positions"
    scanner: ScannerConfig = Field(default_factory=ScannerConfig)
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    fees: FeeConfig = Field(default_factory=FeeConfig)
    paper: PaperConfig = Field(default_factory=PaperConfig)

    @model_validator(mode="after")
    def _sanity(self) -> "BotConfig":
        if self.strategy.ema_fast >= self.strategy.ema_slow:
            raise ValueError("strategy.ema_fast must be smaller than strategy.ema_slow")
        if not 0 < self.risk.risk_per_trade_pct <= 5:
            raise ValueError("risk.risk_per_trade_pct must be in (0, 5]")
        if self.risk.max_position_pct > self.risk.max_exposure_pct:
            raise ValueError("risk.max_position_pct cannot exceed risk.max_exposure_pct")
        if self.risk.max_risk_pct_on_bump > 10:
            raise ValueError("risk.max_risk_pct_on_bump above 10% per trade is not allowed")
        return self


def load_config(path: str | Path) -> BotConfig:
    p = Path(path)
    if not p.exists():
        return BotConfig()
    data = yaml.safe_load(p.read_text()) or {}
    return BotConfig.model_validate(data)
