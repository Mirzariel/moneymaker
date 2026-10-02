"""HTTP API + web UI. Bound to 127.0.0.1.

Auth: either `Authorization: Bearer <API_TOKEN>` (CLI) or an HttpOnly SameSite=Strict session cookie that
`/login?token=<API_TOKEN>` sets (the launcher opens that link). The browser UI never sees the token.
Host-header allowlist blocks DNS rebinding; POSTs must be JSON and same-origin (CSRF).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import math
import os
import time
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..preflight import run_preflight
from ..setup_tools import detect_chat_id, test_exchange, test_telegram

COOKIE = "mm_session"
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}


class PauseBody(BaseModel):
    reason: str = "manual (dashboard)"


class PanicBody(BaseModel):
    confirm: str


class SetupBody(BaseModel):
    toko_api_key: str | None = None
    toko_api_secret: str | None = None
    live_trading: bool | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    healthcheck_url: str | None = None


class ExchangeTestBody(BaseModel):
    toko_api_key: str | None = None
    toko_api_secret: str | None = None


class TelegramTestBody(BaseModel):
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None


def clean(x: Any) -> Any:
    """JSON can't carry NaN/Infinity (Starlette refuses them); market math can produce them."""
    if isinstance(x, float):
        return x if math.isfinite(x) else None
    if isinstance(x, dict):
        return {k: clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [clean(v) for v in x]
    return x


def _session_value(token: str) -> str:
    return hmac.new(token.encode(), b"moneymaker-session-v1", hashlib.sha256).hexdigest()


def create_app(runtime, allowed_hosts: set[str] | None = None) -> FastAPI:
    """`runtime` provides: api_token, secrets, state, control, store, setup_info(), apply_setup(), cfg."""
    if not runtime.api_token or len(runtime.api_token) < 16:
        raise ValueError("API_TOKEN must be set (>= 16 chars) to start the HTTP API")
    hosts = set(LOCAL_HOSTS) | (allowed_hosts or set()) | {
        h.strip().lower() for h in os.environ.get("DASHBOARD_ALLOWED_HOSTS", "").split(",") if h.strip()}
    app = FastAPI(title="moneymaker", version="0.2.0", docs_url=None, redoc_url=None, openapi_url=None)

    def token_ok(token: str) -> bool:
        return bool(token) and hmac.compare_digest(token.encode(), runtime.api_token.encode())

    def authenticated(request: Request) -> bool:
        header = request.headers.get("authorization", "")
        if header.startswith("Bearer ") and token_ok(header.removeprefix("Bearer ").strip()):
            return True
        cookie = request.cookies.get(COOKIE, "")
        return bool(cookie) and hmac.compare_digest(cookie, _session_value(runtime.api_token))

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = (request.headers.get("host") or "").lower()
        hostname = host.rsplit(":", 1)[0] if not host.endswith("]") else host
        if hostname not in hosts:
            return JSONResponse({"detail": "forbidden host"}, status_code=403)
        if request.method == "POST":
            origin = request.headers.get("origin")
            if origin and origin.split("://", 1)[-1].lower() != host:
                return JSONResponse({"detail": "forbidden origin"}, status_code=403)
            if not (request.headers.get("content-type") or "").lower().startswith("application/json"):
                return JSONResponse({"detail": "content-type must be application/json"}, status_code=415)
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    def auth(request: Request) -> None:
        if not authenticated(request):
            raise HTTPException(status_code=401, detail="unauthorized")

    def control():
        c = runtime.control
        if c is None:
            raise HTTPException(status_code=409, detail=f"bot is not running (state: {runtime.state})")
        return c

    def rows(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        store = runtime.store
        return store.rows(sql, params) if store is not None else []

    # ---- session -------------------------------------------------------------------------
    @app.get("/login")
    async def login(token: str = "") -> Any:
        if not token_ok(token):
            return HTMLResponse("<h3>Link login tidak valid.</h3><p>Jalankan start lagi dan pakai link yang "
                                "muncul di terminal.</p>", status_code=401)
        resp = RedirectResponse("/", status_code=302)
        resp.set_cookie(COOKIE, _session_value(runtime.api_token), httponly=True, samesite="strict",
                        max_age=30 * 86400, path="/")
        return resp

    @app.get("/api/session")
    async def session(request: Request) -> dict[str, Any]:
        return {"authenticated": authenticated(request)}

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        c = runtime.control
        return {"ok": True, "state": runtime.state,
                "last_cycle_at": c.engine.last_cycle_at if c else None, "server_time": time.time()}

    # ---- setup -----------------------------------------------------------------------------
    @app.get("/api/setup", dependencies=[Depends(auth)])
    async def get_setup() -> dict[str, Any]:
        return runtime.setup_info()

    @app.post("/api/setup", dependencies=[Depends(auth)])
    async def post_setup(body: SetupBody) -> dict[str, Any]:
        try:
            return await runtime.apply_setup(body.model_dump())
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @app.post("/api/setup/test-exchange", dependencies=[Depends(auth)])
    async def setup_test_exchange(body: ExchangeTestBody) -> dict[str, Any]:
        s = runtime.secrets
        return await test_exchange((body.toko_api_key or s.toko_api_key).strip(),
                                   (body.toko_api_secret or s.toko_api_secret).strip())

    @app.post("/api/setup/test-telegram", dependencies=[Depends(auth)])
    async def setup_test_telegram(body: TelegramTestBody) -> dict[str, Any]:
        s = runtime.secrets
        return await test_telegram((body.telegram_bot_token or s.telegram_bot_token).strip(),
                                   (body.telegram_chat_id or s.telegram_chat_id).strip())

    @app.post("/api/setup/detect-chat-id", dependencies=[Depends(auth)])
    async def setup_detect_chat_id(body: TelegramTestBody) -> dict[str, Any]:
        return await detect_chat_id((body.telegram_bot_token or runtime.secrets.telegram_bot_token).strip())

    @app.get("/api/preflight", dependencies=[Depends(auth)])
    async def preflight() -> dict[str, Any]:
        try:
            return clean(await asyncio.wait_for(run_preflight(runtime.secrets, runtime.cfg), timeout=165))
        except asyncio.TimeoutError:
            return {"ok": False, "error": "Cek kesiapan melebihi batas waktu (Tokocrypto lambat merespons). Coba lagi.",
                    "quote": runtime.cfg.quote, "binance_reachable": False, "markets_active": 0, "fees": {
                        "buy_pct": runtime.cfg.fees.buy_pct, "sell_pct": runtime.cfg.fees.sell_pct},
                    "equity": 0, "equity_source": "config", "top": [], "sizing": [], "warnings": [],
                    "candidates_checked": 0}

    # ---- learning model + market radar ----------------------------------------------------
    def engine():
        c = runtime.control
        if c is None:
            raise HTTPException(status_code=409, detail=f"bot is not running (state: {runtime.state})")
        return c.engine

    def model_payload(eng) -> dict[str, Any]:
        lc = eng.cfg.learning
        return {"learning_enabled": lc.enabled, "require_validated_edge": lc.require_validated_edge,
                "sleeves": eng.model.load(), "job": eng.trainer.job, "next_training_at": eng.next_training_at(),
                "live": {s: eng.model.live_stats(s) for s in ("majors", "alts")},
                "history": eng.store.get("model_history") or []}

    @app.get("/api/model", dependencies=[Depends(auth)])
    async def model() -> dict[str, Any]:
        return clean(model_payload(engine()))

    @app.post("/api/model/train", dependencies=[Depends(auth)])
    async def model_train() -> dict[str, Any]:
        eng = engine()
        eng.trainer.start(tuple(s for s in ("majors", "alts") if getattr(eng.cfg.sleeves, s).enabled))
        await asyncio.sleep(0)
        return clean(model_payload(eng))

    @app.get("/api/radar", dependencies=[Depends(auth)])
    async def radar() -> dict[str, Any]:
        return clean(engine().sweeper.as_dict())

    # ---- bot -------------------------------------------------------------------------------
    @app.get("/api/status", dependencies=[Depends(auth)])
    async def status() -> dict[str, Any]:
        return clean(control().status())

    @app.get("/api/positions", dependencies=[Depends(auth)])
    async def positions(status: Literal["open", "closed", "all"] = "open",
                        limit: int = Query(100, ge=1, le=1000)) -> list[dict[str, Any]]:
        where = "" if status == "all" else "WHERE status=?"
        params: tuple = () if status == "all" else (status,)
        out = rows(f"SELECT * FROM positions {where} ORDER BY id DESC LIMIT ?", (*params, limit))
        prices = runtime.control.engine.last_prices if runtime.control else {}
        for r in out:
            price = prices.get(r["symbol"])
            r["last_price"] = price
            r["unrealized_pnl"] = (r["amount"] * price - r["cost"]) if (price and r["status"] == "open") else None
        return out

    @app.get("/api/orders", dependencies=[Depends(auth)])
    async def orders(limit: int = Query(100, ge=1, le=1000)) -> list[dict[str, Any]]:
        return rows("SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,))

    @app.get("/api/equity", dependencies=[Depends(auth)])
    async def equity(hours: int = Query(168, ge=1, le=24 * 365)) -> list[dict[str, Any]]:
        out = rows("SELECT * FROM equity WHERE ts>=? ORDER BY ts", (time.time() - hours * 3600,))
        step = max(1, len(out) // 500)  # downsample to <= ~500 points
        return out[::step]

    @app.get("/api/signals", dependencies=[Depends(auth)])
    async def signals(limit: int = Query(100, ge=1, le=1000)) -> list[dict[str, Any]]:
        return rows("SELECT * FROM signals ORDER BY id DESC LIMIT ?", (limit,))

    @app.get("/api/risk-events", dependencies=[Depends(auth)])
    async def risk_events(limit: int = Query(100, ge=1, le=1000)) -> list[dict[str, Any]]:
        return rows("SELECT * FROM risk_events ORDER BY id DESC LIMIT ?", (limit,))

    @app.get("/api/audit", dependencies=[Depends(auth)])
    async def audit(limit: int = Query(100, ge=1, le=1000)) -> list[dict[str, Any]]:
        return rows("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))

    @app.get("/api/config", dependencies=[Depends(auth)])
    async def config() -> dict[str, Any]:
        return runtime.cfg.model_dump()

    @app.post("/api/pause", dependencies=[Depends(auth)])
    async def pause(body: PauseBody | None = None) -> dict[str, Any]:
        return await control().pause("dashboard", (body or PauseBody()).reason)

    @app.post("/api/resume", dependencies=[Depends(auth)])
    async def resume() -> dict[str, Any]:
        return await control().resume("dashboard")

    @app.post("/api/panic", dependencies=[Depends(auth)])
    async def panic(body: PanicBody) -> dict[str, Any]:
        if body.confirm != "PANIC":
            raise HTTPException(status_code=400, detail='send {"confirm": "PANIC"} to liquidate')
        return await control().panic("dashboard")

    # ---- web UI (static export of dashboard/) ----------------------------------------------
    if (WEB_DIR / "index.html").exists():
        app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    else:
        @app.get("/")
        async def no_ui() -> HTMLResponse:
            return HTMLResponse("<p>Web UI belum di-build. Jalankan <code>npm run export</code> di folder "
                                "dashboard/.</p>", status_code=503)

    return app
