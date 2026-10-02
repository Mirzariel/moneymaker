"""Connection tests used by the setup page. Messages are in Indonesian because they are shown to the user."""
from __future__ import annotations

from typing import Any

import ccxt.async_support as ccxt
import httpx

from .exchange.tokocrypto import TokocryptoClient

TELEGRAM_API = "https://api.telegram.org"


async def test_exchange(api_key: str, secret: str) -> dict[str, Any]:
    if not api_key or not secret:
        return {"ok": False, "error": "API key dan secret key wajib diisi."}
    ex = TokocryptoClient(api_key, secret, read_retries=1)
    try:
        bal = await ex.fetch_balance()
        return {"ok": True, "balances": {k: v for k, v in bal.total.items() if v > 0}}
    except ccxt.AuthenticationError as e:
        return {"ok": False, "error": f"API key / secret ditolak Tokocrypto. Cek lagi (salin tanpa spasi). ({e})"}
    except ccxt.PermissionDenied as e:
        return {"ok": False, "error": f"Izin API key kurang. Aktifkan Read + Spot Trading. ({e})"}
    except (ccxt.NetworkError, ccxt.ExchangeNotAvailable) as e:
        return {"ok": False, "error": f"Tidak bisa terhubung ke Tokocrypto. Cek internet. ({e})"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    finally:
        await ex.close()


def _mask_token(text: str, token: str) -> str:
    return text.replace(token, "***") if token else text


async def test_telegram(token: str, chat_id: str, client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    if not token or not chat_id:
        return {"ok": False, "error": "Isi bot token dan chat ID dulu."}
    own = client is None
    client = client or httpx.AsyncClient(timeout=15)
    try:
        r = await client.post(f"{TELEGRAM_API}/bot{token}/sendMessage",
                              json={"chat_id": chat_id, "text": "✅ moneymaker terhubung. Notifikasi akan muncul di sini."})
        data = r.json()
        if data.get("ok"):
            return {"ok": True}
        desc = data.get("description", f"HTTP {r.status_code}")
        if "chat not found" in desc.lower():
            desc += " – buka bot kamu di Telegram dan kirim /start dulu."
        return {"ok": False, "error": desc}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": _mask_token(f"{type(e).__name__}: {e}", token)}
    finally:
        if own:
            await client.aclose()


async def detect_chat_id(token: str, client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """Find the chat that most recently messaged the bot (user sends /start first)."""
    if not token:
        return {"ok": False, "error": "Isi bot token dulu."}
    own = client is None
    client = client or httpx.AsyncClient(timeout=15)
    try:
        r = await client.get(f"{TELEGRAM_API}/bot{token}/getUpdates", params={"limit": 50})
        data = r.json()
        if not data.get("ok"):
            desc = data.get("description", f"HTTP {r.status_code}")
            if r.status_code == 409:
                desc = "Bot Telegram sedang dipakai oleh moneymaker yang berjalan; chat ID yang tersimpan sudah aktif."
            return {"ok": False, "error": desc}
        for upd in reversed(data.get("result", [])):
            msg = upd.get("message") or upd.get("edited_message") or {}
            chat = msg.get("chat") or {}
            if chat.get("type") == "private" and chat.get("id") is not None:
                name = " ".join(x for x in [chat.get("first_name"), chat.get("last_name")] if x) or chat.get("username")
                return {"ok": True, "chat_id": str(chat["id"]), "name": name or ""}
        return {"ok": False, "error": "Belum ada pesan. Buka bot kamu di Telegram, kirim /start, lalu coba lagi."}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": _mask_token(f"{type(e).__name__}: {e}", token)}
    finally:
        if own:
            await client.aclose()
