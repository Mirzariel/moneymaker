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
    # Stablecoin, wrapped and leveraged-token bases are excluded by default.
    exclude_bases: list[str] = Field(
        default_factory=lambda: ["USDT", "USDC", "BUSD", "FDUSD", "TUSD", "DAI", "BIDR", "IDRT", "USDP", "PYUSD",
                                 "USDE", "EURI", "AEUR", "WBTC", "WETH", "WBETH", "BETH", "STETH"]
    )
    # Well-known coins ("majors" sleeve). Everything else that passes the alt filters is an "alt".
    major_bases: list[str] = Field(
        default_factory=lambda: ["BTC", "ETH", "BNB", "SOL", "XRP", "DOGE", "ADA", "TRX", "LINK", "AVAX", "DOT",
                                 "LTC", "TON", "SUI", "BCH", "NEAR"]
    )
    # Market radar: how often to re-sweep all pairs, and how many top alts the strategy looks at.
    sweep_interval_minutes: int = 15
    alt_candidates: int = 10


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


class MomentumConfig(BaseModel):
    """Alt sleeve strategy `momentum_surge` (defaults; the learning model overrides them when validated)."""
    timeframe: str = "1h"
    ema_fast: int = 20
    ema_slow: int = 50
    breakout_lookback: int = 24
    volume_avg_period: int = 24
    volume_mult: float = 3.0
    atr_period: int = 14
    atr_stop_mult: float = 2.0
    trail_mult: float = 3.0
    max_extension_atr: float = 3.0
    max_change_24h_pct: float = 30.0
    signal_ttl_sec: int = 300


class AltFilterConfig(BaseModel):
    min_volume_24h: float = 300_000_000.0  # in quote currency (Rp300 juta)
    max_spread_pct: float = 0.6
    min_age_days: float = 30.0             # fresh listings often dump
    max_change_24h_pct: float = 30.0       # don't chase coins that already ran
    max_extension_atr: float = 3.0         # price far above EMA20 = late
    min_depth_multiple: float = 5.0        # asks within depth_range_pct must cover 5x our order
    depth_range_pct: float = 1.0


class RegimeConfig(BaseModel):
    """BTC drives the market: alts usually fall harder when BTC weakens."""
    alt_block_btc_change_24h_pct: float = -3.0
    extreme_move_atr: float = 2.5


class SleeveConfig(BaseModel):
    enabled: bool = True
    max_positions: int = 1
    risk_per_trade_pct: float = 2.0
    max_position_quote: float | None = None


class SleevesConfig(BaseModel):
    majors: SleeveConfig = Field(default_factory=lambda: SleeveConfig(risk_per_trade_pct=2.0))
    alts: SleeveConfig = Field(default_factory=lambda: SleeveConfig(risk_per_trade_pct=1.0,
                                                                    max_position_quote=30_000.0))


class LearningConfig(BaseModel):
    enabled: bool = True
    # No entries in a sleeve until its parameters passed the out-of-sample test.
    require_validated_edge: bool = True
    relearn_days: float = 7.0
    history_days: int = 120
    majors_symbols: int = 4
    alts_symbols: int = 30
    train_fraction: float = 0.7
    majors_min_pf: float = 1.15
    alts_min_pf: float = 1.3
    min_test_trades: int = 8
    max_test_drawdown_pct: float = 15.0
    # Live monitor: pause a sleeve when its recent live trades contradict the model.
    live_window: int = 15
    live_min_trades: int = 8
    live_min_pf: float = 0.8
    live_max_consecutive_losses: int = 5


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
    momentum: MomentumConfig = Field(default_factory=MomentumConfig)
    alt_filters: AltFilterConfig = Field(default_factory=AltFilterConfig)
    regime: RegimeConfig = Field(default_factory=RegimeConfig)
    sleeves: SleevesConfig = Field(default_factory=SleevesConfig)
    learning: LearningConfig = Field(default_factory=LearningConfig)
    # Override CCXT's request spacing for Tokocrypto (ms). None = CCXT default (2000 ms, safe).
    exchange_rate_limit_ms: int | None = None

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
