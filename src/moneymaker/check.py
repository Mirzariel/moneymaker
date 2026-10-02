"""Phase 0: verify what the bot assumes about Tokocrypto, using YOUR account. Read-only by default."""
from __future__ import annotations

import asyncio

from .config import BotConfig, Secrets
from .exchange.tokocrypto import TokocryptoClient


def _line(title: str) -> None:
    print(f"\n=== {title} " + "=" * max(0, 60 - len(title)))


async def run_check(secrets: Secrets, cfg: BotConfig, test_symbol: str | None = None) -> None:
    ex = TokocryptoClient(secrets.toko_api_key, secrets.toko_api_secret)
    try:
        _line("Markets")
        markets = await ex.load_markets()
        by_quote: dict[str, int] = {}
        for m in markets.values():
            if m.active:
                by_quote[m.quote] = by_quote.get(m.quote, 0) + 1
        print("active spot markets per quote:", dict(sorted(by_quote.items(), key=lambda x: -x[1])[:8]))

        _line(f"Top {cfg.quote} markets by 24h volume")
        tickers = await ex.fetch_tickers()
        rows = [(t.quote_volume, s, t) for s, t in tickers.items()
                if s in markets and markets[s].quote == cfg.quote and markets[s].active]
        rows.sort(reverse=True, key=lambda r: r[0])
        print(f"{'symbol':<14}{'vol24h':>16}{'spread%':>9}{'minCost':>10}{'stopLimit':>10}")
        for vol, s, t in rows[:25]:
            m = markets[s]
            print(f"{s:<14}{vol:>16,.0f}{t.spread_pct:>9.3f}{m.min_cost:>10.4g}{str(m.supports_stop_limit):>10}")
        passing = [s for vol, s, t in rows if vol >= cfg.scanner.min_quote_volume_24h
                   and t.spread_pct <= cfg.scanner.max_spread_pct]
        print(f"\n{len(passing)} {cfg.quote} markets pass the scanner filters "
              f"(vol >= {cfg.scanner.min_quote_volume_24h:,.0f}, spread <= {cfg.scanner.max_spread_pct}%)")
        native = [s for s, m in markets.items() if m.quote == cfg.quote and m.active and s not in tickers]
        if native:
            print(f"{len(native)} {cfg.quote} markets have no 24h ticker via the API (native markets) and are skipped")

        if rows:
            s = rows[0][1]
            candles = await ex.fetch_ohlcv(s, cfg.timeframe, 5)
            print(f"\nOHLCV {s} {cfg.timeframe}: {len(candles)} candles, last close {candles[-1].close if candles else '-'}")

        if not (secrets.toko_api_key and secrets.toko_api_secret):
            print("\n(no API key in .env – skipping private checks)")
            return

        _line("Account (private, read-only)")
        bal = await ex.fetch_balance()
        nonzero = {k: v for k, v in bal.total.items() if v > 0}
        print("non-zero balances:", nonzero or "none")
        try:
            fees = await ex.ex.fetch_trading_fees()
            sample = fees.get(rows[0][1]) if rows else None
            print("trading fees (API):", sample or fees)
        except Exception as e:  # noqa: BLE001
            print(f"trading fees not available via API ({type(e).__name__}).")
        print(f"config assumes taker fee {cfg.fees.taker_pct}% – CHECK your real fee tier on tokocrypto.com "
              f"and update `fees` in config.yaml")

        if test_symbol:
            await _test_orders(ex, test_symbol, markets)
    finally:
        await ex.close()


async def _test_orders(ex: TokocryptoClient, symbol: str, markets) -> None:
    _line(f"Order permission test on {symbol}")
    m = markets[symbol]
    t = await ex.fetch_ticker(symbol)
    print("This places orders FAR from the market price and cancels them right away. They should never fill.")
    if input("Type YES to continue: ").strip() != "YES":
        print("skipped")
        return
    price = ex.price_to_precision(symbol, t.bid * 0.5)
    amount = ex.amount_to_precision(symbol, max(m.min_cost * 1.2 / price, m.min_amount))
    o = await ex.ex.create_order(symbol, "limit", "buy", amount, price, {"clientOrderId": "mmcheckbuy"})
    print(f"limit buy placed: id={o['id']} {amount} @ {price}")
    await asyncio.sleep(1)
    await ex.cancel_order(str(o["id"]), symbol)
    print("limit buy cancelled ✔ (spot trading permission works)")

    base = m.base
    bal = await ex.fetch_balance()
    held = bal.free_of(base)
    stop = ex.price_to_precision(symbol, t.bid * 0.5)
    limit = ex.price_to_precision(symbol, t.bid * 0.49)
    amount = ex.amount_to_precision(symbol, max(m.min_cost * 1.2 / limit, m.min_amount))
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
