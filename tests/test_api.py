from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from moneymaker.api.app import create_app
from moneymaker.control import ControlService
from moneymaker.models import BotStatus

TOKEN = "test-token-1234567890"
H = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def client(engine):
    return TestClient(create_app(ControlService(engine), TOKEN))


def test_rejects_short_token(engine):
    with pytest.raises(ValueError):
        create_app(ControlService(engine), "short")


def test_auth_required(client):
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/status").status_code == 401
    assert client.get("/api/status", headers={"Authorization": "Bearer wrong"}).status_code == 401
    r = client.get("/api/status", headers=H)
    assert r.status_code == 200
    assert r.json()["status"] == "PAUSED" and r.json()["mode"] == "paper"


def test_pause_resume(client, engine):
    assert client.post("/api/resume", headers=H).json()["status"] == "RUNNING"
    assert engine.store.status == BotStatus.RUNNING
    assert client.post("/api/pause", headers=H, json={"reason": "x"}).json()["status"] == "PAUSED"


def test_panic_requires_confirm(client, engine):
    assert client.post("/api/panic", headers=H, json={"confirm": "yes"}).status_code == 400
    r = client.post("/api/panic", headers=H, json={"confirm": "PANIC"})
    assert r.status_code == 200
    assert set(r.json()) == {"cancelled", "sold", "errors", "leftover"}
    assert engine.store.status == BotStatus.PAUSED


def test_lists(client):
    for path in ["positions?status=all", "orders", "equity", "signals", "risk-events", "audit", "config"]:
        assert client.get(f"/api/{path}", headers=H).status_code == 200, path
