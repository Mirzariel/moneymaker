"""Readiness check shared by `moneymaker check` (CLI) and the setup page (/api/preflight).

Budget-aware: Tokocrypto via CCXT allows ~1 request / 2 s, so we only check the well-known coins
(one ticker each = order book + 24h volume) and stop at a hard deadline instead of hanging.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from .config import BotConfig, Secrets
from .exchange.tokocrypto import TokocryptoClient
from .models import BotStatus, Signal
from .risk.engine import RiskContext, RiskEngine
from .scanner import candidate_symbols, category, static_exclusion

log = logging.getLogger(__name__)
DEADLINE_S = 150.0


async def run_preflight(secrets: Secrets, cfg: BotConfig, ex: TokocryptoClient | None = None,
                        deadline_s: float = DEADLINE_S) -> dict[str, Any]:
    own = ex is None
    ex = ex or TokocryptoClient(secrets.toko_api_key, secrets.toko_api_secret, native_quotes=(cfg.quote,),
                                rate_limit_ms=cfg.exchange_rate_limit_ms)
    t0 = time.monotonic()
    res: dict[str, Any] = {
        "ok": False, "error": None, "quote": cfg.quote, "binance_reachable": False, "markets_active": 0,
        "markets_per_quote": {}, "fees": {"buy_pct": cfg.fees.buy_pct, "sell_pct": cfg.fees.sell_pct},
        "equity": cfg.paper.starting_quote_balance, "equity_source": "config", "balances": {},
        "top": [], "sizing": [], "warnings": [], "candidates_checked": 0, "tradeable_count": 0,
        "alts_available": 0,
    }
    try:
        log.info("preflight: loading markets")
        markets = await ex.load_markets()
        active = [m for m in markets.values() if m.active]
        res["markets_active"] = len(active)
        per_quote: dict[str, int] = {}
        for m in active:
            per_quote[m.quote] = per_quote.get(m.quote, 0) + 1
        res["markets_per_quote"] = dict(sorted(per_quote.items(), key=lambda x: -x[1])[:8])
        res["alts_available"] = sum(1 for m in active if static_exclusion(m, cfg) is None
                                    and category(m, cfg) == "alts")
        res["binance_reachable"] = await ex.ping_binance()

        cands = candidate_symbols(markets, cfg)
        if not cands:
            res["warnings"].append(f"Tidak ada koin terkenal dengan pair {cfg.quote} yang bisa ditrade.")
        rows = []
        for k, sym in enumerate(cands):
            if time.monotonic() - t0 > deadline_s - 20:
                res["warnings"].append(f"Waktu cek habis: baru {k} dari {len(cands)} koin dicek "
                                       f"(Tokocrypto membatasi ±1 request per 2 detik).")
                break
            log.info("preflight %d/%d %s", k + 1, len(cands), sym)
            try:
                t = await ex.fetch_ticker(sym)
            except Exception as e:  # noqa: BLE001
                log.warning("preflight %s failed: %s", sym, e)
                continue
            rows.append((sym, t))
        res["candidates_checked"] = len(rows)
        if cands and not rows:
            raise RuntimeError("Data harga tidak bisa diambil dari Tokocrypto (cek internet / coba lagi).")

        def tradeable(sym: str, t) -> bool:
            return t.quote_volume >= cfg.scanner.min_quote_volume_24h and t.spread_pct <= cfg.scanner.max_spread_pct

        rows.sort(key=lambda r: r[1].quote_volume, reverse=True)
        for sym, t in rows:
            m = markets[sym]
            res["top"].append({"symbol": sym, "quote_volume": t.quote_volume, "spread_pct": round(t.spread_pct, 4),
                               "native": m.native, "stop_limit": m.supports_stop_limit, "min_cost": m.min_cost,
                               "tradeable": tradeable(sym, t)})
        ok_rows = [(s, t) for s, t in rows if tradeable(s, t)]
        res["tradeable_count"] = len(ok_rows)
        if rows and not ok_rows:
            res["warnings"].append("Tidak ada koin terkenal yang lolos filter volume/spread. Turunkan "
                                   "scanner.min_quote_volume_24h atau naikkan scanner.max_spread_pct.")
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
    log.info("preflight finished in %.0fs (ok=%s)", time.monotonic() - t0, res["ok"])
    return res


def sizing_preview(cfg: BotConfig, equity: float, items) -> list[dict[str, Any]]:
    """What the risk engine would do with a typical majors signal (stop 3% below, target 6% above)."""
    risk = RiskEngine(cfg.risk, cfg.fees, cfg.scanner.min_quote_volume_24h, cfg.scanner.max_spread_pct)
    now = time.time()
    out = []
    for sym, t, m in items:
        sig = Signal(sym, "buy", t.ask, t.ask * 0.97, t.ask * 1.06, now, now + 60, "preview")
        ctx = RiskContext(now=now, status=BotStatus.RUNNING, equity=equity, quote_free=equity, exposure=0.0,
                          open_symbols=set(), day_start_equity=equity, day_pnl=0.0, consecutive_losses=0,
                          ticker=t, market=m, last_entry_ts=None, sleeve=cfg.sleeves.majors)
        d = risk.evaluate(sig, ctx)
        out.append({"symbol": sym, "approved": d.approved, "cost": d.cost, "rule": d.rule, "detail": d.detail,
                    "notes": d.notes})
    return out
