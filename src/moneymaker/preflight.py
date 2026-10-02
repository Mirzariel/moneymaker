"""Readiness check shared by `moneymaker check` (CLI) and the setup page (/api/preflight)."""
from __future__ import annotations

import time
from typing import Any

from .config import BotConfig, Secrets
from .exchange.tokocrypto import TokocryptoClient
from .models import BotStatus, Signal
from .risk.engine import RiskContext, RiskEngine


async def run_preflight(secrets: Secrets, cfg: BotConfig, ex: TokocryptoClient | None = None) -> dict[str, Any]:
    own = ex is None
    ex = ex or TokocryptoClient(secrets.toko_api_key, secrets.toko_api_secret, native_quotes=(cfg.quote,))
    res: dict[str, Any] = {
        "ok": False, "error": None, "quote": cfg.quote, "binance_reachable": False, "markets_active": 0,
        "markets_per_quote": {}, "fees": {"buy_pct": cfg.fees.buy_pct, "sell_pct": cfg.fees.sell_pct},
        "equity": cfg.paper.starting_quote_balance, "equity_source": "config", "balances": {},
        "top": [], "sizing": [], "warnings": [],
    }
    try:
        markets = await ex.load_markets()
        active = [m for m in markets.values() if m.active]
        res["markets_active"] = len(active)
        per_quote: dict[str, int] = {}
        for m in active:
            per_quote[m.quote] = per_quote.get(m.quote, 0) + 1
        res["markets_per_quote"] = dict(sorted(per_quote.items(), key=lambda x: -x[1])[:8])
        try:
            await ex.ex.fetch_tickers()
            res["binance_reachable"] = True
        except Exception:  # noqa: BLE001
            res["warnings"].append("api.binance.com tidak bisa diakses dari internetmu: hanya market native "
                                   "(mis. pair IDR) yang punya data.")

        tickers = await ex.fetch_tickers()
        rows = sorted(((t.quote_volume, s, t) for s, t in tickers.items()
                       if s in markets and markets[s].quote == cfg.quote and markets[s].active),
                      key=lambda r: r[0], reverse=True)

        def tradeable(s: str, t) -> bool:
            m = markets[s]
            return (t.quote_volume >= cfg.scanner.min_quote_volume_24h and t.spread_pct <= cfg.scanner.max_spread_pct
                    and (m.supports_stop_limit or cfg.risk.allow_bot_side_stop)
                    and m.base not in cfg.scanner.exclude_bases)

        for vol, s, t in rows[:25]:
            m = markets[s]
            res["top"].append({"symbol": s, "quote_volume": vol, "spread_pct": round(t.spread_pct, 4),
                               "native": m.native, "stop_limit": m.supports_stop_limit,
                               "min_cost": m.min_cost, "tradeable": tradeable(s, t)})
        ok_rows = [(s, t) for _, s, t in rows if tradeable(s, t)]
        res["tradeable_count"] = len(ok_rows)
        if not rows:
            res["warnings"].append(f"Tidak ada market {cfg.quote} yang datanya terbaca.")
        elif not ok_rows:
            res["warnings"].append("Tidak ada market yang lolos filter. Turunkan scanner.min_quote_volume_24h "
                                   "atau naikkan scanner.max_spread_pct di config.yaml.")
        if any(not markets[s].supports_stop_limit for s, _ in ok_rows):
            res["warnings"].append("Sebagian market tidak menerima stop-loss di exchange: stop dijaga bot, "
                                   "jadi laptop harus tetap menyala selama ada posisi.")

        if secrets.toko_api_key and secrets.toko_api_secret:
            bal = await ex.fetch_balance()
            res["balances"] = {k: v for k, v in bal.total.items() if v > 0}
            if bal.total_of(cfg.quote) > 0:
                res["equity"], res["equity_source"] = bal.total_of(cfg.quote), "balance"
            elif secrets.live_trading:
                res["warnings"].append(f"Saldo {cfg.quote} kamu 0. Deposit dulu supaya bot bisa membeli.")

        res["sizing"] = sizing_preview(cfg, res["equity"], [(s, t, markets[s]) for s, t in ok_rows[:5]])
        if res["sizing"] and not any(r["approved"] for r in res["sizing"]):
            res["warnings"].append("Dengan saldo dan config sekarang semua order contoh DITOLAK – lihat alasannya.")
        res["ok"] = True
    except Exception as e:  # noqa: BLE001
        res["error"] = f"{type(e).__name__}: {e}"
    finally:
        if own:
            await ex.close()
    return res


def sizing_preview(cfg: BotConfig, equity: float, items) -> list[dict[str, Any]]:
    """What the risk engine would do with a typical signal (stop 3% below, target 6% above)."""
    risk = RiskEngine(cfg.risk, cfg.fees, cfg.scanner.min_quote_volume_24h, cfg.scanner.max_spread_pct)
    now = time.time()
    out = []
    for sym, t, m in items:
        sig = Signal(sym, "buy", t.ask, t.ask * 0.97, t.ask * 1.06, now, now + 60, "preview")
        ctx = RiskContext(now=now, status=BotStatus.RUNNING, equity=equity, quote_free=equity, exposure=0.0,
                          open_symbols=set(), day_start_equity=equity, day_pnl=0.0, consecutive_losses=0,
                          ticker=t, market=m, last_entry_ts=None)
        d = risk.evaluate(sig, ctx)
        out.append({"symbol": sym, "approved": d.approved, "cost": d.cost, "rule": d.rule, "detail": d.detail,
                    "notes": d.notes})
    return out
