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
    db_path: str = "data/moneymaker.db"
    paper_state_path: str = "data/paper_state.json"


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


class FeeConfig(BaseModel):
    # CCXT lists 0.75% for Tokocrypto. Verify your real fee with `moneymaker check` and update.
    taker_pct: float = 0.75
    maker_pct: float = 0.75


class PaperConfig(BaseModel):
    starting_quote_balance: float = 1000.0
    slippage_pct: float = 0.1


class BotConfig(BaseModel):
    quote: str = "USDT"
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
        return self


def load_config(path: str | Path) -> BotConfig:
    p = Path(path)
    if not p.exists():
        return BotConfig()
    data = yaml.safe_load(p.read_text()) or {}
    return BotConfig.model_validate(data)
