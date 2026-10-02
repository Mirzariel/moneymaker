"""Startup reconciliation: make the DB agree with the exchange after a crash, sleep or restart."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..models import BotStatus

if TYPE_CHECKING:
    from ..engine import Engine

log = logging.getLogger(__name__)


async def reconcile(engine: "Engine") -> list[str]:
    store, ex = engine.store, engine.ex
    findings: list[str] = []
    await engine.refresh_markets()

    # 1. Orders written to the DB but whose exchange outcome we never learned.
    stuck = store.orders_with_status("pending", "unknown")
    for r in stuck:
        store.update_order(r["client_id"], status="unknown")
    stuck_entries = [r for r in stuck if r["purpose"] == "entry"]
    if stuck_entries:
        syms = ", ".join(sorted({r["symbol"] for r in stuck_entries}))
        findings.append(f"entry order(s) with unknown outcome: {syms} — check Tokocrypto order history")
        store.set_status(BotStatus.PAUSED, "reconcile: unknown entry orders")

    # 2. Every open position: stop filled while we were away? stop missing? re-protect.
    tickers = await ex.fetch_tickers()
    before = {p.id for p in store.open_positions()}
    await engine.manage_positions(tickers)
    after = {p.id for p in store.open_positions()}
    closed = before - after
    if closed:
        findings.append(f"{len(closed)} position(s) closed while offline (see trade history)")

    # 3. Assets the bot holds on the exchange but doesn't track (informational only; never touched).
    bal = await ex.fetch_balance()
    tracked = {p.symbol.split("/")[0] for p in store.open_positions()}
    untracked = [a for a, v in bal.total.items()
                 if v > 0 and a != engine.cfg.quote and a not in tracked and f"{a}/{engine.cfg.quote}" in engine.markets]
    if untracked:
        findings.append(f"untracked holdings (ignored by bot): {', '.join(sorted(untracked))}")

    store.audit("system", "reconcile", "; ".join(findings) or "clean")
    if findings:
        await engine.notify.send("🔁 Startup reconcile:\n- " + "\n- ".join(findings))
    return findings
