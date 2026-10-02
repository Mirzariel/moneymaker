"""Risk engine: the last gate before any order. It can only shrink or reject, never enlarge."""
from __future__ import annotations

from dataclasses import dataclass, field

from ..config import FeeConfig, RiskConfig
from ..models import BotStatus, Market, Signal, Ticker


@dataclass
class RiskContext:
    now: float
    status: BotStatus
    equity: float                 # quote value of quote balance + bot positions
    quote_free: float
    exposure: float               # quote value of open bot positions
    open_symbols: set[str]
    day_start_equity: float
    day_pnl: float                # realized today + current unrealized
    consecutive_losses: int
    ticker: Ticker | None = None
    market: Market | None = None
    last_entry_ts: float | None = None


@dataclass
class Decision:
    approved: bool
    rule: str = ""
    detail: str = ""
    cost: float = 0.0             # quote to spend
    notes: list[str] = field(default_factory=list)

    @staticmethod
    def reject(rule: str, detail: str) -> "Decision":
        return Decision(False, rule, detail)


class RiskEngine:
    def __init__(self, cfg: RiskConfig, fees: FeeConfig, min_quote_volume_24h: float, max_spread_pct: float):
        self.cfg = cfg
        self.fee = fees.taker_pct / 100
        self.min_volume = min_quote_volume_24h
        self.max_spread = max_spread_pct

    # ---- account-level breakers -----------------------------------------------------
    def daily_loss_breached(self, ctx: RiskContext) -> bool:
        if ctx.day_start_equity <= 0:
            return False
        return ctx.day_pnl <= -ctx.day_start_equity * self.cfg.max_daily_loss_pct / 100

    def circuit_breaker(self, ctx: RiskContext) -> str | None:
        """Return a reason when the bot must auto-pause."""
        if self.daily_loss_breached(ctx):
            return (f"daily loss {ctx.day_pnl:.2f} exceeds {self.cfg.max_daily_loss_pct}% "
                    f"of day-start equity {ctx.day_start_equity:.2f}")
        if ctx.consecutive_losses >= self.cfg.max_consecutive_losses:
            return f"{ctx.consecutive_losses} consecutive losing trades"
        return None

    # ---- per-signal gate ----------------------------------------------------------------
    def evaluate(self, sig: Signal, ctx: RiskContext) -> Decision:
        c = self.cfg
        if ctx.status != BotStatus.RUNNING:
            return Decision.reject("bot_status", f"bot is {ctx.status.value}")
        if sig.side != "buy":
            return Decision.reject("spot_long_only", "v1 only opens long spot positions")
        if ctx.now > sig.expires_at:
            return Decision.reject("signal_expired", f"expired {ctx.now - sig.expires_at:.0f}s ago")
        if sig.symbol in ctx.open_symbols:
            return Decision.reject("already_in_position", sig.symbol)
        if len(ctx.open_symbols) >= c.max_open_positions:
            return Decision.reject("max_open_positions", f"{len(ctx.open_symbols)} >= {c.max_open_positions}")
        if self.daily_loss_breached(ctx):
            return Decision.reject("max_daily_loss", f"day pnl {ctx.day_pnl:.2f}")
        if ctx.consecutive_losses >= c.max_consecutive_losses:
            return Decision.reject("consecutive_losses", str(ctx.consecutive_losses))
        if ctx.last_entry_ts is not None and ctx.now - ctx.last_entry_ts < c.cooldown_minutes * 60:
            return Decision.reject("cooldown", f"last entry {(ctx.now - ctx.last_entry_ts) / 60:.0f} min ago")

        m, t = ctx.market, ctx.ticker
        if m is None or not m.active:
            return Decision.reject("market_inactive", sig.symbol)
        if not m.supports_stop_limit:
            return Decision.reject("no_stop_limit", "market does not accept STOP_LOSS_LIMIT orders")
        if t is None or t.ask <= 0 or t.bid <= 0:
            return Decision.reject("no_quote", "missing bid/ask")
        if t.spread_pct > self.max_spread:
            return Decision.reject("max_spread", f"spread {t.spread_pct:.3f}% > {self.max_spread}%")
        if t.quote_volume < self.min_volume:
            return Decision.reject("min_liquidity", f"24h volume {t.quote_volume:.0f} < {self.min_volume:.0f}")

        price = t.ask
        if price > sig.entry * (1 + c.max_entry_drift_pct / 100):
            return Decision.reject("entry_drift", f"ask {price:.6g} ran >{c.max_entry_drift_pct}% above {sig.entry:.6g}")
        if sig.stop >= price:
            return Decision.reject("stop_above_price", f"stop {sig.stop:.6g} >= ask {price:.6g}")
        round_trip_fee = 2 * self.fee
        expected_move = (sig.take_profit - price) / price
        if expected_move < c.min_edge_fee_multiple * round_trip_fee:
            return Decision.reject("insufficient_edge",
                                   f"target {expected_move * 100:.2f}% < {c.min_edge_fee_multiple}x fees "
                                   f"({round_trip_fee * 100:.2f}%)")

        # ---- sizing: lose at most risk_per_trade_pct of equity if the stop fills at its limit, after fees
        notes: list[str] = []
        loss_frac = (price - sig.stop) / price + c.stop_limit_offset_pct / 100 + round_trip_fee
        cost = ctx.equity * c.risk_per_trade_pct / 100 / loss_frac
        caps = {
            "max_position_pct": ctx.equity * c.max_position_pct / 100,
            "max_exposure_pct": ctx.equity * c.max_exposure_pct / 100 - ctx.exposure,
            "quote_free": ctx.quote_free * (1 - c.quote_buffer_pct / 100),
        }
        for name, cap in caps.items():
            if cost > cap:
                notes.append(f"resized by {name}: {cost:.2f} -> {max(cap, 0):.2f}")
                cost = cap
        # Headroom so the post-fee amount still clears the exchange minimum for the stop order.
        min_cost = max(m.min_cost, m.min_amount * price) * 1.1
        if cost <= 0 or cost < min_cost:
            return Decision.reject("below_min_notional", f"size {max(cost, 0):.2f} < min {min_cost:.2f}")
        return Decision(True, "approved", f"cost {cost:.2f}, risk/unit {loss_frac * 100:.2f}%", cost=cost,
                        notes=notes)
