"""Which markets the bot may look at, and how they are categorised."""
from __future__ import annotations

from .config import BotConfig
from .models import Market

LEVERAGED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")


def static_exclusion(m: Market, cfg: BotConfig) -> str | None:
    """Reason a market can never be traded by this bot, or None."""
    sc = cfg.scanner
    if m.quote != cfg.quote:
        return "quote lain"
    if not m.active:
        return "tidak aktif"
    if m.base in sc.exclude_bases:
        return "stablecoin/wrapped"
    if m.base.endswith(LEVERAGED_SUFFIXES) and len(m.base) > 4:
        return "token leverage"
    if m.symbol in sc.blacklist:
        return "blacklist"
    if sc.whitelist and m.symbol not in sc.whitelist:
        return "bukan whitelist"
    if not m.supports_stop_limit and not cfg.risk.allow_bot_side_stop:
        return "tanpa stop-limit"
    return None


def category(m: Market, cfg: BotConfig) -> str:
    return "majors" if m.base in cfg.scanner.major_bases else "alts"


def tradeable_markets(markets: dict[str, Market], cfg: BotConfig) -> list[Market]:
    return [m for m in markets.values() if static_exclusion(m, cfg) is None]


def candidate_symbols(markets: dict[str, Market], cfg: BotConfig) -> list[str]:
    """Majors in configured priority order (BTC first) that exist and are allowed."""
    by_base = {m.base: m for m in tradeable_markets(markets, cfg) if category(m, cfg) == "majors"}
    return [by_base[b].symbol for b in cfg.scanner.major_bases if b in by_base]
