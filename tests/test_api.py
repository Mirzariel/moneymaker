"""Web API + first-run setup flow, in the same event loop via ASGI transport."""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import httpx
import pytest

from moneymaker.api.app import create_app
from moneymaker.envfile import bootstrap
from moneymaker.runtime import Runtime

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def root(tmp_path):
    shutil.copyfile(REPO / ".env.example", tmp_path / ".env.example")
    shutil.copyfile(REPO / "config.example.yaml", tmp_path / "config.example.yaml")
    bootstrap(tmp_path)
    with open(tmp_path / ".env", "a") as f:  # keep test databases out of the repo
        f.write(f"DB_PATH={tmp_path / 'bot.db'}\nPAPER_STATE_PATH={tmp_path / 'paper.json'}\n"
                f"CONFIG_PATH={tmp_path / 'config.yaml'}\n")
    return tmp_path


@pytest.fixture
async def setup(root, paper):
    rt = Runtime(root, exchange_factory=lambda s, c: paper)
    stop = asyncio.Event()
    task = asyncio.create_task(rt.supervise(stop))
    app = create_app(rt)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000")
    yield rt, client
    stop.set()
    await asyncio.wait_for(task, 20)
    await client.aclose()


async def login(rt, client):
    r = await client.get(f"/login?token={rt.api_token}")
    assert r.status_code == 302 and "mm_session" in r.headers["set-cookie"]
    assert "httponly" in r.headers["set-cookie"].lower() and "samesite=strict" in r.headers["set-cookie"].lower()


async def wait_state(rt, state, timeout=10.0):
    for _ in range(int(timeout / 0.05)):
        if rt.state == state:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"state {rt.state!r} != {state!r} (error: {rt.error})")


def test_bootstrap_creates_env_token_and_config(root):
    env = (root / ".env").read_text()
    token = next(line.split("=", 1)[1] for line in env.splitlines() if line.startswith("API_TOKEN="))
    assert len(token) >= 32
    assert (root / "config.yaml").exists()
    assert bootstrap(root) == []  # idempotent


def test_rejects_short_token(root, monkeypatch):
    rt = Runtime(root)
    monkeypatch.setattr(Runtime, "api_token", property(lambda self: "short"))
    with pytest.raises(ValueError):
        create_app(rt)


async def test_fresh_clone_setup_flow(setup, root):
    rt, c = setup
    await wait_state(rt, "setup")
    assert (await c.get("/api/session")).json() == {"authenticated": False}
    assert (await c.get("/api/setup")).status_code == 401
    assert (await c.get("/login?token=wrong-token-wrong-token")).status_code == 401

    await login(rt, c)
    assert (await c.get("/api/session")).json() == {"authenticated": True}
    info = (await c.get("/api/setup")).json()
    assert info["state"] == "setup" and info["live_trading"] is True
    assert set(info["missing"]) == {"toko_api_key", "toko_api_secret"}
    assert (await c.get("/api/status")).status_code == 409
    assert (await c.get("/api/positions")).json() == []

    secret = "SECRET-abcdefghijklmnop-1234"
    r = await c.post("/api/setup", json={"toko_api_key": "KEY-abcdefgh-9876", "toko_api_secret": secret,
                                         "telegram_chat_id": "12345"})
    assert r.status_code == 200 and r.json()["state"] == "starting"
    env = (root / ".env").read_text()
    assert "TOKO_API_KEY=KEY-abcdefgh-9876" in env and f"TOKO_API_SECRET={secret}" in env
    assert "# Tokocrypto API key" in env  # comments preserved
    await wait_state(rt, "running")

    info = (await c.get("/api/setup")).json()
    assert info["ready"] and info["fields"]["toko_api_key"]["hint"] == "…9876"
    assert secret not in (await c.get("/api/setup")).text
    st = (await c.get("/api/status")).json()
    assert st["status"] == "PAUSED"

    assert (await c.post("/api/resume", json={})).json()["status"] == "RUNNING"
    assert (await c.post("/api/pause", json={"reason": "x"})).json()["status"] == "PAUSED"
    assert (await c.post("/api/panic", json={"confirm": "no"})).status_code == 400
    rep = (await c.post("/api/panic", json={"confirm": "PANIC"})).json()
    assert set(rep) == {"cancelled", "sold", "errors", "leftover"}
    for path in ["positions?status=all", "orders", "equity", "signals", "risk-events", "audit", "config"]:
        assert (await c.get(f"/api/{path}")).status_code == 200, path


async def test_setup_validation_and_csrf(setup):
    rt, c = setup
    await login(rt, c)
    assert (await c.post("/api/setup", json={"telegram_chat_id": "abc"})).status_code == 400
    assert (await c.post("/api/setup", json={"healthcheck_url": "http://x"})).status_code == 400
    assert (await c.post("/api/setup", json={"toko_api_key": "a\nLIVE_TRADING=false"})).status_code in (400, 500)
    assert (await c.post("/api/pause", content=b"{}", headers={"content-type": "text/plain"})).status_code == 415
    r = await c.post("/api/pause", json={}, headers={"origin": "http://evil.example"})
    assert r.status_code == 403
    r = await c.get("/api/session", headers={"host": "evil.example:8000"})
    assert r.status_code == 403


async def test_bearer_token_for_cli(setup):
    rt, c = setup
    r = await c.get("/api/setup", headers={"authorization": f"Bearer {rt.api_token}"})
    assert r.status_code == 200
    assert (await c.get("/api/setup", headers={"authorization": "Bearer nope"})).status_code == 401


async def test_start_error_is_reported_and_recoverable(root, paper):
    calls = {"n": 0}

    def factory(s, c):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return paper

    with open(root / ".env", "a") as f:
        f.write("LIVE_TRADING=false\n")
    rt = Runtime(root, exchange_factory=factory)
    stop = asyncio.Event()
    task = asyncio.create_task(rt.supervise(stop))
    await wait_state(rt, "error")
    assert "boom" in rt.error
    await rt.apply_setup({})
    await wait_state(rt, "running")
    stop.set()
    await asyncio.wait_for(task, 20)
