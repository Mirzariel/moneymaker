"""Phase 0: verify what the bot assumes about Tokocrypto, using YOUR account. Read-only by default."""
from __future__ import annotations

import asyncio

from .config import BotConfig, Secrets
from .exchange.tokocrypto import TokocryptoClient
from .preflight import run_preflight


def _line(title: str) -> None:
    print(f"\n=== {title} " + "=" * max(0, 60 - len(title)))


async def run_check(secrets: Secrets, cfg: BotConfig, test_symbol: str | None = None) -> None:
    ex = TokocryptoClient(secrets.toko_api_key, secrets.toko_api_secret, native_quotes=(cfg.quote,))
    try:
        print("Checking Tokocrypto (native markets need 2 requests each – this can take a minute)…")
        r = await run_preflight(secrets, cfg, ex)
        if r["error"]:
            print(f"\nERROR: {r['error']}")
            return
        _line("Markets")
        print("active spot markets per quote:", r["markets_per_quote"])
        print("api.binance.com:", "reachable" if r["binance_reachable"] else "NOT reachable")

        _line(f"Top {cfg.quote} markets by 24h volume")
        print(f"{'symbol':<14}{'vol24h':>18}{'spread%':>9}{'minCost':>10}{'native':>8}{'stopLimit':>10}{'ok':>4}")
        for t in r["top"]:
            print(f"{t['symbol']:<14}{t['quote_volume']:>18,.0f}{t['spread_pct']:>9.3f}{t['min_cost']:>10.4g}"
                  f"{str(t['native']):>8}{str(t['stop_limit']):>10}{'✔' if t['tradeable'] else '✗':>4}")
        print(f"\n{r.get('tradeable_count', 0)} {cfg.quote} markets tradeable with your config")

        _line("Account")
        print("balances:", r["balances"] or ("none" if secrets.toko_api_key else "(no API key – skipped)"))
        print(f"fees in config: buy {cfg.fees.buy_pct}% / sell {cfg.fees.sell_pct}% (all-in)")

        if r["sizing"]:
            _line(f"Order size preview for equity {r['equity']:,.2f} {cfg.quote} ({r['equity_source']})")
            for z in r["sizing"]:
                if z["approved"]:
                    notes = f" · {'; '.join(z['notes'])}" if z["notes"] else ""
                    print(f"{z['symbol']:<14} BUY {z['cost']:,.2f} {cfg.quote} · {z['detail']}{notes}")
                else:
                    print(f"{z['symbol']:<14} REJECTED {z['rule']}: {z['detail']}")
        for w in r["warnings"]:
            print(f"⚠️  {w}")
        if test_symbol and secrets.toko_api_key:
            markets = await ex.load_markets()
            await _test_orders(ex, test_symbol, markets, cfg.risk.min_order_quote)
    finally:
        await ex.close()


async def _test_orders(ex: TokocryptoClient, symbol: str, markets, min_order_quote: float = 0.0) -> None:
    _line(f"Order permission test on {symbol}")
    m = markets[symbol]
    min_cost = max(m.min_cost, min_order_quote)
    t = await ex.fetch_ticker(symbol)
    print("This places orders FAR from the market price and cancels them right away. They should never fill.")
    if input("Type YES to continue: ").strip() != "YES":
        print("skipped")
        return
    price = ex.price_to_precision(symbol, t.bid * 0.5)
    amount = ex.amount_to_precision(symbol, max(min_cost * 1.2 / price, m.min_amount))
    o = await ex.ex.create_order(symbol, "limit", "buy", amount, price, {"clientOrderId": "mmcheckbuy"})
    print(f"limit buy placed: id={o['id']} {amount} @ {price}")
    await asyncio.sleep(1)
    await ex.cancel_order(str(o["id"]), symbol)
    print("limit buy cancelled ✔ (spot trading permission works)")

    if not m.supports_stop_limit:
        print("market metadata says STOP_LOSS_LIMIT is not supported here -> only a bot-side stop is possible")
        return
    base = m.base
    bal = await ex.fetch_balance()
    held = bal.free_of(base)
    stop = ex.price_to_precision(symbol, t.bid * 0.5)
    limit = ex.price_to_precision(symbol, t.bid * 0.49)
    amount = ex.amount_to_precision(symbol, max(min_cost * 1.2 / limit, m.min_amount))
    if held < amount:
        print(f"skip stop-loss test: need >= {amount} {base} free (have {held}). Buy a tiny amount first to test it.")
        return
    o = await ex.stop_loss_limit_sell(symbol, amount, stop, limit, "mmcheckstop")
    print(f"STOP_LOSS_LIMIT sell placed: id={o.id} trigger {stop} limit {limit}")
    await asyncio.sleep(1)
    fetched = await ex.fetch_order(o.id, symbol)
    print(f"fetched back: status={fetched.status} stop_price={fetched.stop_price}")
    await ex.cancel_order(o.id, symbol)
    print("stop-loss cancelled ✔ (exchange-side stops work – the core safety assumption holds)")
