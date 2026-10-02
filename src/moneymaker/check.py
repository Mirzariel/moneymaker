"""Phase 0: verify what the bot assumes about Tokocrypto, using YOUR account. Read-only by default."""
from __future__ import annotations

import asyncio
import time

from .config import BotConfig, Secrets
from .exchange.tokocrypto import TokocryptoClient
from .models import BotStatus, Signal
from .risk.engine import RiskContext, RiskEngine


def _line(title: str) -> None:
    print(f"\n=== {title} " + "=" * max(0, 60 - len(title)))


async def run_check(secrets: Secrets, cfg: BotConfig, test_symbol: str | None = None) -> None:
    ex = TokocryptoClient(secrets.toko_api_key, secrets.toko_api_secret, native_quotes=(cfg.quote,))
    try:
        _line("Markets")
        markets = await ex.load_markets()
        by_quote: dict[str, int] = {}
        for m in markets.values():
            if m.active:
                by_quote[m.quote] = by_quote.get(m.quote, 0) + 1
        print("active spot markets per quote:", dict(sorted(by_quote.items(), key=lambda x: -x[1])[:8]))
        try:
            await ex.ex.fetch_tickers()
            print("api.binance.com (data for Binance-backed markets): reachable")
        except Exception as e:  # noqa: BLE001
            print(f"api.binance.com NOT reachable ({type(e).__name__}) – only native markets will have data")

        _line(f"Top {cfg.quote} markets by 24h volume")
        print("(native markets need 2 requests each – this can take a minute)")
        tickers = await ex.fetch_tickers()
        rows = [(t.quote_volume, s, t) for s, t in tickers.items()
                if s in markets and markets[s].quote == cfg.quote and markets[s].active]
        rows.sort(reverse=True, key=lambda r: r[0])
        print(f"{'symbol':<14}{'vol24h':>18}{'spread%':>9}{'minCost':>10}{'native':>8}{'stopLimit':>10}")
        for vol, s, t in rows[:25]:
            m = markets[s]
            print(f"{s:<14}{vol:>18,.0f}{t.spread_pct:>9.3f}{m.min_cost:>10.4g}{str(m.native):>8}"
                  f"{str(m.supports_stop_limit):>10}")
        tradeable = [(vol, s, t) for vol, s, t in rows if vol >= cfg.scanner.min_quote_volume_24h
                     and t.spread_pct <= cfg.scanner.max_spread_pct
                     and (markets[s].supports_stop_limit or cfg.risk.allow_bot_side_stop)]
        print(f"\n{len(tradeable)} {cfg.quote} markets are tradeable with your config "
              f"(vol >= {cfg.scanner.min_quote_volume_24h:,.0f}, spread <= {cfg.scanner.max_spread_pct}%, "
              f"stop {'exchange or bot' if cfg.risk.allow_bot_side_stop else 'exchange only'})")
        if not tradeable:
            print("  -> NOTHING to trade. Lower scanner.min_quote_volume_24h / raise max_spread_pct, or set "
                  "risk.allow_bot_side_stop: true if the stopLimit column is False.")

        if rows:
            s = rows[0][1]
            candles = await ex.fetch_ohlcv(s, cfg.timeframe, 5)
            print(f"\nOHLCV {s} {cfg.timeframe}: {len(candles)} candles, last close {candles[-1].close if candles else '-'}")

        equity = cfg.paper.starting_quote_balance
        if secrets.toko_api_key and secrets.toko_api_secret:
            _line("Account (private, read-only)")
            bal = await ex.fetch_balance()
            nonzero = {k: v for k, v in bal.total.items() if v > 0}
            print("non-zero balances:", nonzero or "none")
            equity = bal.total_of(cfg.quote) or equity
            try:
                fees = await ex.ex.fetch_trading_fees()
                sample = fees.get(rows[0][1]) if rows else None
                print("trading fees (API):", sample or fees)
            except Exception as e:  # noqa: BLE001
                print(f"trading fees not available via API ({type(e).__name__}).")
        else:
            print("\n(no API key in .env – skipping private checks)")
        print(f"config fees: buy {cfg.fees.buy_pct}% / sell {cfg.fees.sell_pct}% (all-in). "
              f"Compare with the fee page in your Tokocrypto account.")

        if tradeable:
            _sizing_preview(cfg, equity, [(s, t, markets[s]) for _, s, t in tradeable[:5]])
        if test_symbol and secrets.toko_api_key:
            await _test_orders(ex, test_symbol, markets, cfg.risk.min_order_quote)
    finally:
        await ex.close()


def _sizing_preview(cfg: BotConfig, equity: float, items) -> None:
    """Show what the risk engine would do with a typical signal (stop 3% below, target 6% above)."""
    _line(f"Order size preview for equity {equity:,.2f} {cfg.quote}")
    risk = RiskEngine(cfg.risk, cfg.fees, cfg.scanner.min_quote_volume_24h, cfg.scanner.max_spread_pct)
    now = time.time()
    for sym, t, m in items:
        sig = Signal(sym, "buy", t.ask, t.ask * 0.97, t.ask * 1.06, now, now + 60, "preview")
        ctx = RiskContext(now=now, status=BotStatus.RUNNING, equity=equity, quote_free=equity, exposure=0.0,
                          open_symbols=set(), day_start_equity=equity, day_pnl=0.0, consecutive_losses=0,
                          ticker=t, market=m, last_entry_ts=None)
        d = risk.evaluate(sig, ctx)
        if d.approved:
            print(f"{sym:<14} BUY {d.cost:,.2f} {cfg.quote} · {d.detail}" + (f" · {'; '.join(d.notes)}" if d.notes else ""))
        else:
            print(f"{sym:<14} REJECTED {d.rule}: {d.detail}")


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
