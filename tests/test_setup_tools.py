from __future__ import annotations

import httpx
import pytest

from moneymaker.envfile import update_env
from moneymaker.setup_tools import detect_chat_id, test_exchange as check_exchange, test_telegram as check_telegram


def test_update_env_preserves_and_appends(tmp_path):
    p = tmp_path / ".env"
    p.write_text("# comment\nTOKO_API_KEY=old\nOTHER=1\n")
    update_env(p, {"TOKO_API_KEY": "new", "TELEGRAM_CHAT_ID": "42"})
    assert p.read_text() == "# comment\nTOKO_API_KEY=new\nOTHER=1\nTELEGRAM_CHAT_ID=42\n"
    with pytest.raises(ValueError):
        update_env(p, {"TOKO_API_KEY": "x\nLIVE_TRADING=false"})
    with pytest.raises(ValueError):
        update_env(p, {"bad key": "x"})


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_detect_chat_id_picks_latest_private_chat():
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path.endswith("/getUpdates")
        return httpx.Response(200, json={"ok": True, "result": [
            {"message": {"chat": {"id": 111, "type": "private", "first_name": "Old"}}},
            {"message": {"chat": {"id": -5, "type": "group", "title": "G"}}},
            {"message": {"chat": {"id": 222, "type": "private", "first_name": "Mirza", "last_name": "R"}}},
        ]})

    async with _client(handler) as c:
        r = await detect_chat_id("123:abc", c)
    assert r == {"ok": True, "chat_id": "222", "name": "Mirza R"}


async def test_detect_chat_id_without_messages():
    async with _client(lambda req: httpx.Response(200, json={"ok": True, "result": []})) as c:
        r = await detect_chat_id("123:abc", c)
    assert not r["ok"] and "/start" in r["error"]


async def test_telegram_test_message_and_errors():
    sent = {}

    def ok(req: httpx.Request) -> httpx.Response:
        sent["body"] = req.content
        return httpx.Response(200, json={"ok": True})

    async with _client(ok) as c:
        assert (await check_telegram("123:abc", "42", c)) == {"ok": True}
    assert b'"chat_id":"42"' in sent["body"].replace(b" ", b"")

    async with _client(lambda r: httpx.Response(400, json={"ok": False, "description": "Bad Request: chat not found"})) as c:
        r = await check_telegram("123:abc", "42", c)
    assert not r["ok"] and "/start" in r["error"]


async def test_exchange_test_requires_both_fields():
    assert not (await check_exchange("", "x"))["ok"]
