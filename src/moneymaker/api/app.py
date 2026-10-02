"""HTTP API for the dashboard. Bind to 127.0.0.1 only; every route except /api/health needs the bearer token."""
from __future__ import annotations

import hmac
import time
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel

from ..control import ControlService


class PauseBody(BaseModel):
    reason: str = "manual (dashboard)"


class PanicBody(BaseModel):
    confirm: str


def create_app(control: ControlService, api_token: str) -> FastAPI:
    if not api_token or len(api_token) < 16:
        raise ValueError("API_TOKEN must be set (>= 16 chars) to start the HTTP API")
    app = FastAPI(title="moneymaker", version="0.1.0")
    store = control.store
    engine = control.engine

    def auth(request: Request) -> None:
        header = request.headers.get("authorization", "")
        token = header.removeprefix("Bearer ").strip()
        if not hmac.compare_digest(token.encode(), api_token.encode()):
            raise HTTPException(status_code=401, detail="unauthorized")

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "last_cycle_at": engine.last_cycle_at, "server_time": time.time()}

    @app.get("/api/status", dependencies=[Depends(auth)])
    async def status() -> dict[str, Any]:
        return control.status()

    @app.get("/api/positions", dependencies=[Depends(auth)])
    async def positions(status: Literal["open", "closed", "all"] = "open",
                        limit: int = Query(100, ge=1, le=1000)) -> list[dict[str, Any]]:
        where = "" if status == "all" else "WHERE status=?"
        params: tuple = () if status == "all" else (status,)
        rows = store.rows(f"SELECT * FROM positions {where} ORDER BY id DESC LIMIT ?", (*params, limit))
        for r in rows:
            price = engine.last_prices.get(r["symbol"])
            r["last_price"] = price
            r["unrealized_pnl"] = (r["amount"] * price - r["cost"]) if (price and r["status"] == "open") else None
        return rows

    @app.get("/api/orders", dependencies=[Depends(auth)])
    async def orders(limit: int = Query(100, ge=1, le=1000)) -> list[dict[str, Any]]:
        return store.rows("SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,))

    @app.get("/api/equity", dependencies=[Depends(auth)])
    async def equity(hours: int = Query(168, ge=1, le=24 * 365)) -> list[dict[str, Any]]:
        since = time.time() - hours * 3600
        rows = store.rows("SELECT * FROM equity WHERE ts>=? ORDER BY ts", (since,))
        step = max(1, len(rows) // 500)  # downsample to <= ~500 points
        return rows[::step]

    @app.get("/api/signals", dependencies=[Depends(auth)])
    async def signals(limit: int = Query(100, ge=1, le=1000)) -> list[dict[str, Any]]:
        return store.rows("SELECT * FROM signals ORDER BY id DESC LIMIT ?", (limit,))

    @app.get("/api/risk-events", dependencies=[Depends(auth)])
    async def risk_events(limit: int = Query(100, ge=1, le=1000)) -> list[dict[str, Any]]:
        return store.rows("SELECT * FROM risk_events ORDER BY id DESC LIMIT ?", (limit,))

    @app.get("/api/audit", dependencies=[Depends(auth)])
    async def audit(limit: int = Query(100, ge=1, le=1000)) -> list[dict[str, Any]]:
        return store.rows("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))

    @app.get("/api/config", dependencies=[Depends(auth)])
    async def config() -> dict[str, Any]:
        return engine.cfg.model_dump()

    @app.post("/api/pause", dependencies=[Depends(auth)])
    async def pause(body: PauseBody | None = None) -> dict[str, Any]:
        return await control.pause("dashboard", (body or PauseBody()).reason)

    @app.post("/api/resume", dependencies=[Depends(auth)])
    async def resume() -> dict[str, Any]:
        return await control.resume("dashboard")

    @app.post("/api/panic", dependencies=[Depends(auth)])
    async def panic(body: PanicBody) -> dict[str, Any]:
        if body.confirm != "PANIC":
            raise HTTPException(status_code=400, detail='send {"confirm": "PANIC"} to liquidate')
        return await control.panic("dashboard")

    return app
