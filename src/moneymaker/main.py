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
from .envfile import bootstrap
from .notify.base import Notifier

log = logging.getLogger("moneymaker")


def setup_logging(db_path: str) -> None:
    log_path = Path(db_path).parent / "moneymaker.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    for stream in (sys.stdout, sys.stderr):  # Windows consoles can't print emoji in cp1252
        with contextlib.suppress(Exception):
            stream.reconfigure(errors="replace")
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout),
                                       RotatingFileHandler(log_path, maxBytes=5_000_000, backupCount=5,
                                                           encoding="utf-8")]
    for h in handlers:
        h.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def keep_awake() -> None:
    """Windows: stop the system from sleeping while this process runs (macOS/Linux: start.sh does it)."""
    if sys.platform == "win32":
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        with contextlib.suppress(Exception):
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)


def make_api_server(config):
    """uvicorn server that leaves SIGINT/SIGTERM handling to us."""
    import uvicorn

    class _Server(uvicorn.Server):
        @contextlib.contextmanager
        def capture_signals(self):
            yield

    return _Server(config)


async def run_app(open_browser: bool = False) -> None:
    """Web UI + bot supervisor. Works on a fresh clone: without API keys it serves the setup page."""
    import uvicorn

    from .api.app import create_app
    from .runtime import Runtime

    runtime = Runtime()
    s = runtime.secrets
    app = create_app(runtime)
    server = make_api_server(uvicorn.Config(app, host=s.api_host, port=s.api_port, log_level="warning"))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # Windows: Ctrl+C raises KeyboardInterrupt instead
            loop.add_signal_handler(sig, stop.set)

    server_task = asyncio.create_task(server.serve(), name="api")
    supervisor = asyncio.create_task(runtime.supervise(stop), name="supervisor")
    for _ in range(100):
        if server.started or server_task.done():
            break
        await asyncio.sleep(0.1)
    if server_task.done():
        stop.set()
        await supervisor
        raise SystemExit(f"Web server failed to start on port {s.api_port} (already running?).")

    host = "127.0.0.1" if s.api_host in ("0.0.0.0", "") else s.api_host
    url = f"http://{host}:{s.api_port}/login?token={s.api_token}"
    print("\n" + "=" * 70)
    print("  moneymaker berjalan. Buka dashboard di browser lewat link ini:")
    print(f"  {url}")
    print("  (link ini berisi kode akses – jangan dibagikan)")
    print("  Tutup jendela ini / tekan Ctrl+C untuk berhenti.")
    print("=" * 70 + "\n")
    keep_awake()
    if open_browser:
        import webbrowser
        with contextlib.suppress(Exception):
            webbrowser.open(url)
    try:
        await stop.wait()
    finally:
        log.info("shutting down (positions keep their exchange-side stops)")
        stop.set()
        await supervisor
        server.should_exit = True
        await asyncio.wait([server_task], timeout=10)


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
    from .runtime import build_exchange
    store = Store(secrets.database_path)
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
    rp = sub.add_parser("run")
    rp.add_argument("--open", action="store_true", help="open the dashboard in the browser")
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

    if args.cmd == "run":
        created = bootstrap(Path.cwd())
        if created:
            print(f"first run: created {', '.join(created)}")
    secrets = Secrets()
    cfg = load_config(secrets.config_path)
    setup_logging(secrets.database_path)

    if args.cmd == "run":
        try:
            asyncio.run(run_app(open_browser=args.open))
        except KeyboardInterrupt:
            print("stopped")
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
