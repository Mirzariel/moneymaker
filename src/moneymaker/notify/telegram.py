"""Telegram bot: notifications + remote control. Only the configured chat id is served."""
from __future__ import annotations

import logging
import time

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from ..control import ControlService

log = logging.getLogger(__name__)

HELP = (
    "/status – status bot & equity\n"
    "/positions – posisi terbuka\n"
    "/pnl – PnL hari ini & 7 hari\n"
    "/pause – stop entry baru (posisi tetap dilindungi stop-loss)\n"
    "/resume – lanjut trading\n"
    "/panic – cancel semua order + jual semua posisi (butuh konfirmasi)"
)


def _fmt_status(s: dict) -> str:
    age = f"{time.time() - s['last_cycle_at']:.0f}s lalu" if s.get("last_cycle_at") else "belum pernah"
    eq = f"{s['equity']:.2f}" if s.get("equity") is not None else "-"
    pnl = f"{s['day_pnl']:+.2f}" if s.get("day_pnl") is not None else "-"
    lines = [
        f"Status: {s['status']} ({s['mode']})",
        f"Alasan: {s.get('status_reason') or '-'}",
        f"Equity: {eq} {s['quote']} · PnL hari ini: {pnl}",
        f"Posisi terbuka: {s['open_positions']} · cycle terakhir: {age}",
    ]
    if s.get("last_error"):
        lines.append(f"Error terakhir: {s['last_error']}")
    return "\n".join(lines)


class TelegramBot:
    def __init__(self, token: str, chat_id: str, control: ControlService):
        self.chat_id = int(chat_id)
        self.control = control
        self.app = Application.builder().token(token).build()
        for name, fn in {"start": self.help, "help": self.help, "status": self.status, "positions": self.positions,
                         "pnl": self.pnl, "pause": self.pause, "resume": self.resume, "panic": self.panic}.items():
            self.app.add_handler(CommandHandler(name, self._guard(fn)))

    def _guard(self, fn):
        async def wrapped(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
            chat = update.effective_chat
            if chat is None or chat.id != self.chat_id:
                log.warning("ignored telegram command from chat %s", chat.id if chat else None)
                return
            await fn(update, ctx)
        return wrapped

    async def send(self, text: str) -> None:
        await self.app.bot.send_message(chat_id=self.chat_id, text=text[:4000])

    async def start(self) -> None:
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)

    async def stop(self) -> None:
        await self.app.updater.stop()
        await self.app.stop()
        await self.app.shutdown()

    # ---- commands ------------------------------------------------------------------------
    async def help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(HELP)

    async def status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(_fmt_status(self.control.status()))

    async def positions(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        eng = self.control.engine
        pos = self.control.store.open_positions()
        if not pos:
            await update.message.reply_text("Tidak ada posisi terbuka.")
            return
        lines = []
        for p in pos:
            price = eng.last_prices.get(p.symbol)
            upnl = f"{p.amount * price - p.cost:+.2f}" if price else "?"
            lines.append(f"{p.symbol}: {p.amount:.8g} @ {p.entry_price:.8g} · now {price or '?'} · uPnL {upnl}\n"
                         f"  stop {p.stop_price:.8g} ({'aktif' if p.stop_order_id else 'TIDAK ADA'}) · "
                         f"target {p.take_profit:.8g}")
        await update.message.reply_text("\n".join(lines))

    async def pnl(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        s = self.control.store
        week = s.realized_pnl_since(time.time() - 7 * 86400)
        n = s.rows("SELECT COUNT(*) AS n, SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) AS w FROM positions "
                   "WHERE status='closed' AND closed_at>=?", (time.time() - 7 * 86400,))[0]
        day = self.control.engine.last_day_pnl
        day_txt = "-" if day is None else f"{day:+.2f}"
        await update.message.reply_text(
            f"PnL hari ini (incl. unrealized): {day_txt}\n"
            f"Realized 7 hari: {week:+.2f} · trade: {n['n']} · menang: {n['w'] or 0}"
        )

    async def pause(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        reason = " ".join(ctx.args) if ctx.args else "manual (telegram)"
        await self.control.pause("telegram", reason)

    async def resume(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await self.control.resume("telegram")

    async def panic(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not ctx.args or ctx.args[0] != "CONFIRM":
            await update.message.reply_text(
                "⚠️ Ini akan cancel semua order dan MENJUAL semua posisi bot di harga market.\n"
                "Kirim: /panic CONFIRM"
            )
            return
        await self.control.panic("telegram")
