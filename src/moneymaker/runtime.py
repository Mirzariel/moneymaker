"""Process supervisor: the web UI is always up; the trading bot is (re)started whenever settings change.

On a fresh clone there are no API keys yet, so the bot sits in "setup" state and the browser shows the
setup page. Saving the form rewrites `.env` and restarts the bot in-process.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Callable

from .config import BotConfig, Secrets, load_config
from .control import ControlService
from .db import Store
from .engine import Engine
from .envfile import update_env
from .exchange.base import ExchangeClient
from .exchange.paper import PaperExchange
from .exchange.tokocrypto import TokocryptoClient
from .heartbeat import heartbeat_loop
from .notify.base import Notifier

log = logging.getLogger("moneymaker.runtime")

# setup-page field -> .env key
SETUP_FIELDS = {
    "toko_api_key": "TOKO_API_KEY",
    "toko_api_secret": "TOKO_API_SECRET",
    "telegram_bot_token": "TELEGRAM_BOT_TOKEN",
    "telegram_chat_id": "TELEGRAM_CHAT_ID",
    "healthcheck_url": "HEALTHCHECK_URL",
}


def build_exchange(secrets: Secrets, cfg: BotConfig) -> ExchangeClient:
    if secrets.live_trading:
        if not (secrets.toko_api_key and secrets.toko_api_secret):
            raise RuntimeError("LIVE_TRADING=true but TOKO_API_KEY / TOKO_API_SECRET are missing")
        return TokocryptoClient(secrets.toko_api_key, secrets.toko_api_secret, native_quotes=(cfg.quote,),
                                rate_limit_ms=cfg.exchange_rate_limit_ms)
    return PaperExchange(TokocryptoClient(native_quotes=(cfg.quote,), rate_limit_ms=cfg.exchange_rate_limit_ms),
                         quote=cfg.quote,
                         starting_quote=cfg.paper.starting_quote_balance, buy_fee_pct=cfg.fees.buy_pct,
                         sell_fee_pct=cfg.fees.sell_pct, slippage_pct=cfg.paper.slippage_pct,
                         state_path=secrets.paper_state_path)


def guard_mode(store: Store, live: bool) -> None:
    """Refuse to mix paper and live history in one database."""
    mode = "live" if live else "paper"
    stored = store.get("mode")
    if stored is None:
        store.set("mode", mode)
    elif stored != mode:
        raise RuntimeError(f"Database mode mismatch: it holds {stored!r} history but the bot runs {mode!r}. "
                           f"Leave DB_PATH empty so each mode gets its own database.")


def _hint(value: str) -> str | None:
    return f"…{value[-4:]}" if len(value) >= 8 else ("…" if value else None)


class BotInstance:
    """One running bot: exchange, engine, Telegram, heartbeat."""

    def __init__(self, secrets: Secrets, cfg: BotConfig, exchange_factory: Callable[[Secrets, BotConfig], ExchangeClient]):
        self.secrets, self.cfg = secrets, cfg
        self.store = Store(secrets.database_path)
        self.ex: ExchangeClient | None = None
        self.notifier = Notifier()
        self.engine: Engine | None = None
        self.control: ControlService | None = None
        self.tg = None
        self.tasks: list[asyncio.Task] = []
        self._factory = exchange_factory

    async def start(self) -> None:
        s = self.secrets
        guard_mode(self.store, s.live_trading)
        self.ex = self._factory(s, self.cfg)
        self.engine = Engine(self.ex, self.store, self.notifier, self.cfg)
        self.control = ControlService(self.engine)
        if s.telegram_bot_token and s.telegram_chat_id:
            from .notify.telegram import TelegramBot
            try:
                self.tg = TelegramBot(s.telegram_bot_token, s.telegram_chat_id, self.control)
                await self.tg.start()
                self.notifier.add(self.tg.send)
            except Exception as e:  # noqa: BLE001 - trading must not depend on Telegram
                log.exception("telegram start failed")
                self.tg = None
                self.engine.last_error = f"Telegram tidak aktif: {e}"
        else:
            log.warning("Telegram not configured: no phone notifications or remote panic")
        mode = "LIVE (uang asli)" if s.live_trading else "PAPER (simulasi)"
        await self.notifier.send(f"🤖 moneymaker started · {mode} · status {self.store.status.value}")
        if s.healthcheck_url:
            self.tasks.append(asyncio.create_task(heartbeat_loop(s.healthcheck_url, self.engine), name="heartbeat"))
        else:
            log.warning("HEALTHCHECK_URL not set: nobody will notice if this process dies")
        self.tasks.append(asyncio.create_task(self.engine.run(), name="engine"))

    async def stop(self) -> None:
        if self.engine:
            self.engine.stop()
        for t in self.tasks:
            if t.get_name() == "heartbeat":
                t.cancel()
        # Let an in-flight cycle finish (orders are write-ahead logged, so a hard stop is still recoverable).
        pending = [t for t in self.tasks if not t.done()]
        if pending:
            await asyncio.wait(pending, timeout=15)
        for t in self.tasks:
            t.cancel()
        if self.engine:
            await self.notifier.send("⏹️ moneymaker stopped. Posisi terbuka tetap dilindungi stop-loss di exchange.")
        if self.tg:
            try:
                await self.tg.stop()
            except Exception:  # noqa: BLE001
                log.exception("telegram stop failed")
        if self.ex:
            await self.ex.close()
        self.store.close()


class Runtime:
    def __init__(self, root: Path | None = None,
                 exchange_factory: Callable[[Secrets, BotConfig], ExchangeClient] = build_exchange):
        self.root = root or Path.cwd()
        self.env_path = self.root / ".env"
        self.exchange_factory = exchange_factory
        self.state = "setup"  # setup | starting | running | error
        self.error: str | None = None
        self.bot: BotInstance | None = None
        self._reload = asyncio.Event()
        self.secrets = self.load_secrets()
        self.cfg = self._load_cfg()

    # ---- settings ----------------------------------------------------------------------
    def load_secrets(self) -> Secrets:
        return Secrets(_env_file=self.env_path)

    def _load_cfg(self) -> BotConfig:
        p = Path(self.secrets.config_path)
        return load_config(p if p.is_absolute() else self.root / p)

    @property
    def api_token(self) -> str:
        return self.secrets.api_token

    @property
    def control(self) -> ControlService | None:
        return self.bot.control if (self.bot and self.state == "running") else None

    @property
    def store(self) -> Store | None:
        return self.bot.store if self.bot else None

    def setup_info(self) -> dict[str, Any]:
        s = self.secrets
        missing = s.missing_for_start()
        return {
            "state": self.state,
            "error": self.error,
            "ready": not missing,
            "missing": missing,
            "live_trading": s.live_trading,
            "quote": self.cfg.quote,
            "fields": {
                "toko_api_key": {"set": bool(s.toko_api_key), "hint": _hint(s.toko_api_key)},
                "toko_api_secret": {"set": bool(s.toko_api_secret), "hint": None},
                "telegram_bot_token": {"set": bool(s.telegram_bot_token), "hint": _hint(s.telegram_bot_token)},
                "telegram_chat_id": {"set": bool(s.telegram_chat_id), "value": s.telegram_chat_id or None},
                "healthcheck_url": {"set": bool(s.healthcheck_url), "value": s.healthcheck_url or None},
            },
        }

    async def apply_setup(self, body: dict[str, Any]) -> dict[str, Any]:
        """Write changed fields to .env and restart the bot. None/missing = keep, "" = clear."""
        updates: dict[str, str] = {}
        for field, env_key in SETUP_FIELDS.items():
            v = body.get(field)
            if v is None:
                continue
            v = str(v).strip()
            if field == "telegram_chat_id" and v and not v.lstrip("-").isdigit():
                raise ValueError("Chat ID harus berupa angka.")
            if field == "healthcheck_url" and v and not v.startswith("https://"):
                raise ValueError("URL healthcheck harus diawali https://")
            updates[env_key] = v
        if body.get("live_trading") is not None:
            updates["LIVE_TRADING"] = "true" if body["live_trading"] else "false"
        if updates:
            update_env(self.env_path, updates)
        self.secrets = self.load_secrets()
        self.request_reload()
        self.state = "starting" if not self.secrets.missing_for_start() else "setup"
        return self.setup_info()

    def request_reload(self) -> None:
        self._reload.set()

    # ---- lifecycle ---------------------------------------------------------------------
    async def supervise(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            self._reload.clear()
            self.error = None
            try:
                self.secrets = self.load_secrets()
                self.cfg = self._load_cfg()
                if self.secrets.missing_for_start():
                    self.state = "setup"
                    log.info("waiting for setup: missing %s", ", ".join(self.secrets.missing_for_start()))
                else:
                    self.state = "starting"
                    self.bot = BotInstance(self.secrets, self.cfg, self.exchange_factory)
                    await self.bot.start()
                    self.state = "running"
            except Exception as e:  # noqa: BLE001
                log.exception("bot start failed")
                self.state, self.error = "error", f"{type(e).__name__}: {e}"
                await self._stop_bot()
            waiters = [asyncio.create_task(self._reload.wait()), asyncio.create_task(stop.wait())]
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            for w in waiters:
                w.cancel()
            await self._stop_bot()

    async def _stop_bot(self) -> None:
        if self.bot:
            bot, self.bot = self.bot, None
            try:
                await bot.stop()
            except Exception:  # noqa: BLE001
                log.exception("bot stop failed")
