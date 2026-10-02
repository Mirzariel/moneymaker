"""Entrypoint.

    moneymaker run                 # start bot (+ Telegram, API, heartbeat if configured)
    moneymaker status|pause|resume # talk to the running bot (falls back to the DB)
    moneymaker panic               # cancel all orders + sell all bot positions, then PAUSE
    moneymaker check               # Phase-0 read-only exchange check
    moneymaker backtest BTC/USDT --days 180
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import signal
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import httpx

from .config import BotConfig, Secrets, load_config
from .control import ControlService
from .db import Store
from .engine import Engine
from .exchange.base import ExchangeClient
from .exchange.paper import PaperExchange
from .exchange.tokocrypto import TokocryptoClient
from .heartbeat import heartbeat_loop
from .notify.base import Notifier

log = logging.getLogger("moneymaker")


def setup_logging(db_path: str) -> None:
    log_path = Path(db_path).parent / "moneymaker.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout),
                                       RotatingFileHandler(log_path, maxBytes=5_000_000, backupCount=5)]
    for h in handlers:
        h.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def build_exchange(secrets: Secrets, cfg: BotConfig) -> ExchangeClient:
    if secrets.live_trading:
        if not (secrets.toko_api_key and secrets.toko_api_secret):
            raise SystemExit("LIVE_TRADING=true but TOKO_API_KEY / TOKO_API_SECRET are missing")
        return TokocryptoClient(secrets.toko_api_key, secrets.toko_api_secret, native_quotes=(cfg.quote,))
    return PaperExchange(TokocryptoClient(native_quotes=(cfg.quote,)), quote=cfg.quote,
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
        raise SystemExit(f"Database {mode!r} mismatch: it holds {stored!r} history. "
                         f"Use a separate DB_PATH for {mode} trading.")


def make_api_server(config):
    """uvicorn server that leaves SIGINT/SIGTERM handling to us."""
    import uvicorn

    class _Server(uvicorn.Server):
        @contextlib.contextmanager
        def capture_signals(self):
            yield

    return _Server(config)


async def run_bot(secrets: Secrets, cfg: BotConfig) -> None:
    store = Store(secrets.db_path)
    guard_mode(store, secrets.live_trading)
    ex = build_exchange(secrets, cfg)
    notifier = Notifier()
    engine = Engine(ex, store, notifier, cfg)
    control = ControlService(engine)

    tasks: list[asyncio.Task] = []
    tg = None
    server = None
    if secrets.telegram_bot_token and secrets.telegram_chat_id:
        from .notify.telegram import TelegramBot
        tg = TelegramBot(secrets.telegram_bot_token, secrets.telegram_chat_id, control)
        await tg.start()
        notifier.add(tg.send)
    else:
        log.warning("Telegram not configured: no phone notifications or remote panic")

    mode = "LIVE (real money)" if secrets.live_trading else "PAPER"
    await notifier.send(f"🤖 moneymaker started in {mode} mode · status {store.status.value}")
    if secrets.live_trading and store.status.value == "RUNNING":
        log.warning("LIVE mode resumed in RUNNING state from previous session")

    if secrets.api_token:
        import uvicorn

        from .api.app import create_app
        app = create_app(control, secrets.api_token)
        server = make_api_server(uvicorn.Config(app, host=secrets.api_host, port=secrets.api_port, log_level="warning"))
        tasks.append(asyncio.create_task(server.serve(), name="api"))
    else:
        log.warning("API_TOKEN not set: dashboard API disabled")
    if secrets.healthcheck_url:
        tasks.append(asyncio.create_task(heartbeat_loop(secrets.healthcheck_url, engine), name="heartbeat"))
    else:
        log.warning("HEALTHCHECK_URL not set: nobody will notice if this process dies")
    tasks.append(asyncio.create_task(engine.run(), name="engine"))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # Windows
            loop.add_signal_handler(sig, stop.set)
    try:
        await stop.wait()
    finally:
        log.info("shutting down (positions keep their exchange-side stops)")
        engine.stop()
        if server:
            server.should_exit = True
        for t in tasks:
            if t.get_name() == "heartbeat":
                t.cancel()
        # Let an in-flight cycle finish (orders are write-ahead logged, so a hard stop is still recoverable).
        pending = [t for t in tasks if not t.done()]
        if pending:
            await asyncio.wait(pending, timeout=15)
        for t in tasks:
            t.cancel()
        await notifier.send("⏹️ moneymaker stopped. Open positions remain protected by exchange stop-loss.")
        if tg:
            await tg.stop()
        await ex.close()
        store.close()


async def api_call(secrets: Secrets, method: str, path: str, body: dict | None = None) -> dict | None:
    if not secrets.api_token:
        return None
    host = "127.0.0.1" if secrets.api_host in ("0.0.0.0", "") else secrets.api_host
    try:
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.request(method, f"http://{host}:{secrets.api_port}/api/{path}", json=body,
                                headers={"Authorization": f"Bearer {secrets.api_token}"})
            r.raise_for_status()
            return r.json()
    except httpx.HTTPError:
        return None


async def offline_control(secrets: Secrets, cfg: BotConfig, action: str) -> dict:
    """Used when the bot process isn't running: act directly on the DB/exchange."""
    store = Store(secrets.db_path)
    ex = build_exchange(secrets, cfg)
    engine = Engine(ex, store, Notifier(), cfg)
    control = ControlService(engine)
    try:
        if action == "panic":
            return await control.panic("cli")
        if action == "pause":
            return await control.pause("cli")
        if action == "resume":
            return await control.resume("cli")
        return control.status()
    finally:
        await ex.close()
        store.close()


def cli() -> None:
    p = argparse.ArgumentParser(prog="moneymaker")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    sub.add_parser("status")
    sub.add_parser("pause")
    sub.add_parser("resume")
    sub.add_parser("panic")
    ck = sub.add_parser("check")
    ck.add_argument("--test-orders", metavar="SYMBOL", help="place+cancel a tiny far-away limit buy and stop sell")
    bt = sub.add_parser("backtest")
    bt.add_argument("symbols", nargs="+")
    bt.add_argument("--days", type=int, default=180)
    args = p.parse_args()

    secrets = Secrets()
    cfg = load_config(secrets.config_path)
    setup_logging(secrets.db_path)

    if args.cmd == "run":
        asyncio.run(run_bot(secrets, cfg))
    elif args.cmd in ("status", "pause", "resume", "panic"):
        if args.cmd == "panic":
            ans = input("Cancel ALL orders and SELL all bot positions at market? Type PANIC: ")
            if ans.strip() != "PANIC":
                print("aborted")
                return
        method = "GET" if args.cmd == "status" else "POST"
        body = {"confirm": "PANIC"} if args.cmd == "panic" else None
        res = asyncio.run(api_call(secrets, method, args.cmd, body))
        if res is None:
            print("(bot API not reachable – acting directly on DB/exchange)")
            res = asyncio.run(offline_control(secrets, cfg, args.cmd))
        print(json.dumps(res, indent=2, default=str))
    elif args.cmd == "check":
        from .check import run_check
        asyncio.run(run_check(secrets, cfg, args.test_orders))
    elif args.cmd == "backtest":
        from .backtest.runner import run_backtest_cli
        asyncio.run(run_backtest_cli(cfg, args.symbols, args.days))


if __name__ == "__main__":
    cli()
