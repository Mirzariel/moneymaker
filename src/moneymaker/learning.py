"""Walk-forward learning: pick strategy parameters from recent history, keep them only if they also work on
data that was NOT used to pick them, and stop trading a sleeve when live results contradict the model.

Per sleeve (majors = well-known coins with `trend_atr`, alts = smaller coins with `momentum_surge`):
  1. download history (majors: 15m, resampled to 15m/30m/1h/4h; alts: 1h)
  2. split every symbol by time: first `train_fraction` = train, rest = test
  3. evaluate a small parameter grid on TRAIN only, trades pooled across symbols
  4. choose by a robust score (profit factor x sample size - drawdown, averaged with grid neighbours,
     so a lucky isolated spike doesn't win)
  5. run the chosen parameters on TEST; the sleeve may trade only if TEST passes the thresholds.
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import math
import time
from typing import TYPE_CHECKING, Any, Callable

from .backtest.runner import Costs, SimTrade, fetch_history, metrics, resample, simulate
from .config import BotConfig, MomentumConfig, StrategyConfig
from .models import Candle
from .strategy.base import Strategy, atr, ema
from .strategy.momentum_surge import MomentumSurgeStrategy
from .strategy.trend_atr import TrendAtrStrategy

if TYPE_CHECKING:
    from .engine import Engine

log = logging.getLogger(__name__)

MAJOR_TIMEFRAMES = ["15m", "30m", "1h", "4h"]
MAJOR_GRID = {"atr_stop_mult": [1.5, 2.0, 3.0], "reward_risk": [1.5, 2.0, 3.0], "breakout_lookback": [10, 20, 40]}
ALT_GRID = {"volume_mult": [2.0, 3.0, 5.0], "atr_stop_mult": [1.5, 2.5], "trail_mult": [2.0, 3.0, 4.0],
            "breakout_lookback": [12, 24]}
POSITION_FRACTION = 0.5  # used only to turn per-trade returns into an equity curve for return/drawdown


def empty_sleeve(reason: str = "Belum dilatih") -> dict[str, Any]:
    return {"status": "untrained", "valid": False, "reason": reason, "timeframe": None, "params": None,
            "symbols": [], "trained_at": None, "train": None, "test": None, "per_timeframe": []}


# ---- strategy factories ---------------------------------------------------------------------------------
def majors_strategy(cfg: BotConfig, params: dict | None, timeframe: str | None) -> Strategy:
    sc = StrategyConfig(**{**cfg.strategy.model_dump(), **(params or {})})
    return TrendAtrStrategy(sc, timeframe or cfg.timeframe)


def alts_strategy(cfg: BotConfig, params: dict | None) -> Strategy:
    return MomentumSurgeStrategy(MomentumConfig(**{**cfg.momentum.model_dump(), **(params or {})}))


# ---- evaluation ------------------------------------------------------------------------------------------
def split_index(candles: list[Candle], fraction: float) -> int:
    if not candles:
        return 0
    cut = candles[0].ts + fraction * (candles[-1].ts - candles[0].ts)
    return next((i for i, c in enumerate(candles) if c.ts >= cut), len(candles))


def score(m) -> float:
    if m.trades == 0:
        return -1.0
    pf = 3.0 if m.profit_factor is None else min(m.profit_factor, 3.0)
    return (pf - 1.0) * min(1.0, m.trades / 30) - m.max_drawdown_pct / 100


def btc_entry_filter(btc: list[Candle], cfg: BotConfig) -> Callable[[int], bool]:
    """Backtest version of the live regime rule: block alt entries when BTC is below EMA50 and down > X% in 24h."""
    if len(btc) < 60:
        return lambda ts: True
    closes = [c.close for c in btc]
    e50 = ema(closes, 50)
    a = atr(btc, 14)
    blocked: dict[int, bool] = {}
    for i in range(25, len(btc)):
        change = (closes[i] / closes[i - 24] - 1) * 100
        extreme = a[i - 1] > 0 and (btc[i].high - btc[i].low) > cfg.regime.extreme_move_atr * a[i - 1]
        blocked[btc[i].ts] = extreme or (closes[i] < e50[i] and change < cfg.regime.alt_block_btc_change_24h_pct)
    return lambda ts: not blocked.get(ts, False)


def evaluate_grid(series: dict[str, list[Candle]], grid: dict[str, list], make: Callable[[dict], Strategy],
                  prepare_key: Callable[[dict], tuple], costs: Costs, train_fraction: float,
                  entry_filter: Callable[[int], bool] | None = None) -> list[dict]:
    """Return one row per parameter combination with pooled train/test trades (test kept separate)."""
    keys = list(grid)
    rows = []
    cache: dict[tuple, Any] = {}
    for combo in itertools.product(*(grid[k] for k in keys)):
        params = dict(zip(keys, combo))
        strat = make(params)
        train: list[SimTrade] = []
        test: list[SimTrade] = []
        for sym, candles in series.items():
            if len(candles) < strat.min_candles + 20:
                continue
            ck = (sym, *prepare_key(params))
            if ck not in cache:
                cache[ck] = strat.prepare(candles)
            ind = cache[ck]
            cut = split_index(candles, train_fraction)
            train += simulate(strat, ind, candles, strat.min_candles - 1, cut, costs, sym, entry_filter)
            test += simulate(strat, ind, candles, cut, len(candles), costs, sym, entry_filter)
        mtr = metrics(train, POSITION_FRACTION)
        rows.append({"params": params, "train": mtr, "test": metrics(test, POSITION_FRACTION),
                     "score": score(mtr), "idx": tuple(grid[k].index(params[k]) for k in keys)})
    # Robustness: average each score with its direct grid neighbours.
    by_idx = {r["idx"]: r for r in rows}
    for r in rows:
        neigh = []
        for d in range(len(keys)):
            for step in (-1, 1):
                j = list(r["idx"])
                j[d] += step
                if tuple(j) in by_idx:
                    neigh.append(by_idx[tuple(j)]["score"])
        r["robust"] = 0.5 * r["score"] + 0.5 * (sum(neigh) / len(neigh) if neigh else r["score"])
    return rows


def passes(m, min_pf: float, cfg: BotConfig) -> tuple[bool, str]:
    lc = cfg.learning
    if m.trades < lc.min_test_trades:
        return False, f"data uji hanya {m.trades} trade (< {lc.min_test_trades})"
    pf = math.inf if m.profit_factor is None else m.profit_factor
    if pf < min_pf:
        return False, f"profit factor uji {pf:.2f} < {min_pf}"
    if m.total_return_pct <= 0:
        return False, f"return uji {m.total_return_pct:+.2f}% (tidak untung)"
    if m.max_drawdown_pct >= lc.max_test_drawdown_pct:
        return False, f"drawdown uji {m.max_drawdown_pct:.1f}% terlalu besar"
    pf_txt = "tanpa trade rugi" if pf == math.inf else f"PF {pf:.2f}"
    return True, f"lolos uji: {pf_txt}, return {m.total_return_pct:+.2f}%, {m.trades} trade"


def sleeve_result(rows: list[dict], min_pf: float, cfg: BotConfig, symbols: list[str],
                  timeframe: str | None) -> dict:
    best = max(rows, key=lambda r: r["robust"])
    ok, reason = passes(best["test"], min_pf, cfg)
    if best["train"].trades == 0:
        ok, reason = False, "tidak ada sinyal di data latih"
    return {"status": "ok" if ok else "no_edge", "valid": ok,
            "reason": reason if ok else f"Belum ada keunggulan: {reason}",
            "timeframe": timeframe, "params": best["params"], "symbols": symbols, "trained_at": time.time(),
            "train": best["train"].as_dict(), "test": best["test"].as_dict(), "per_timeframe": []}


# ---- trainer ---------------------------------------------------------------------------------------------
class Trainer:
    def __init__(self, engine: "Engine"):
        self.engine = engine
        self.job: dict[str, Any] = {"state": "idle", "sleeve": None, "progress": {"done": 0, "total": 0, "label": ""},
                                    "started_at": None, "finished_at": None, "error": None}
        self._task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self, sleeves: tuple[str, ...] = ("majors", "alts")) -> None:
        if not self.running:
            self._task = asyncio.create_task(self.train(sleeves), name="train")

    def _progress(self, done: int, total: int, label: str) -> None:
        self.job["progress"] = {"done": done, "total": total, "label": label}

    async def train(self, sleeves: tuple[str, ...]) -> None:
        eng = self.engine
        self.job.update(state="running", started_at=time.time(), finished_at=None, error=None)
        try:
            for sleeve in sleeves:
                if not getattr(eng.cfg.sleeves, sleeve).enabled:
                    continue
                self.job["sleeve"] = sleeve
                result = await (self._train_majors() if sleeve == "majors" else self._train_alts())
                if result is not None:
                    eng.model.save_sleeve(sleeve, result)
                    eng.apply_model()
                    await eng.notify.send(f"🧠 Model {sleeve}: {result['reason']}")
            self.job.update(state="done", finished_at=time.time())
            self._progress(1, 1, "Selesai")
        except Exception as e:  # noqa: BLE001
            log.exception("training failed")
            self.job.update(state="error", finished_at=time.time(), error=f"{type(e).__name__}: {e}")
        finally:
            self.job["sleeve"] = None

    async def _download(self, symbols: list[str], timeframe: str, label: str) -> dict[str, list[Candle]]:
        eng, days = self.engine, self.engine.cfg.learning.history_days
        out: dict[str, list[Candle]] = {}
        for k, sym in enumerate(symbols):
            self._progress(k, len(symbols) + 1, f"{label}: unduh riwayat {sym} ({k + 1}/{len(symbols)})")
            try:
                candles = await fetch_history(eng.ex, sym, timeframe, days)
            except Exception as e:  # noqa: BLE001
                log.warning("history %s failed: %s", sym, e)
                continue
            if candles and (candles[-1].ts - candles[0].ts) / 86_400_000 >= days * 0.8:
                out[sym] = candles  # pairs listed after the start of the window are skipped (no full test period)
        return out

    async def _train_majors(self) -> dict | None:
        eng, cfg = self.engine, self.engine.cfg
        sweep = eng.sweeper.stats
        cands = [s for s in eng.majors_candidates()]
        cands.sort(key=lambda s: sweep[s].volume_24h if s in sweep else 0.0, reverse=True)
        symbols = cands[: cfg.learning.majors_symbols]
        if not symbols:
            return empty_sleeve("Tidak ada koin terkenal yang bisa dipakai")
        data15 = await self._download(symbols, "15m", "Koin terkenal")
        if not data15:
            return empty_sleeve("Riwayat harga tidak bisa diunduh")
        self._progress(len(symbols), len(symbols) + 1, "Koin terkenal: menguji 108 kombinasi parameter…")
        costs = Costs.from_cfg(cfg)

        def work() -> dict:
            all_rows = []
            per_tf = []
            for tf in MAJOR_TIMEFRAMES:
                series = {s: resample(c, tf) for s, c in data15.items()}
                rows = evaluate_grid(series, MAJOR_GRID, lambda p, tf=tf: majors_strategy(cfg, p, tf),
                                     lambda p: (tf, p["breakout_lookback"]), costs, cfg.learning.train_fraction)
                for r in rows:
                    r["timeframe"] = tf
                best = max(rows, key=lambda r: r["robust"])
                per_tf.append({"timeframe": tf, "params": best["params"], "train": best["train"].as_dict(),
                               "test": best["test"].as_dict(),
                               "passed": passes(best["test"], cfg.learning.majors_min_pf, cfg)[0]})
                all_rows += rows
            best = max(all_rows, key=lambda r: r["robust"])
            res = sleeve_result([best], cfg.learning.majors_min_pf, cfg, list(data15), best["timeframe"])
            res["per_timeframe"] = per_tf
            return res

        return await asyncio.to_thread(work)

    async def _train_alts(self) -> dict | None:
        eng, cfg = self.engine, self.engine.cfg
        for _ in range(60):  # wait for the first radar sweep (it knows which alts exist and are liquid)
            if eng.sweeper.sweeps_completed:
                break
            self._progress(0, 1, "Koin kecil: menunggu radar pasar selesai memindai…")
            await asyncio.sleep(10)
        f = cfg.alt_filters
        alts = [p for p in eng.sweeper.stats.values() if p.category == "alts" and p.volume_24h >= f.min_volume_24h
                and (p.age_days is None or p.age_days >= f.min_age_days)]
        alts.sort(key=lambda p: p.volume_24h, reverse=True)
        symbols = [p.symbol for p in alts[: cfg.learning.alts_symbols]]
        if not symbols:
            return empty_sleeve("Belum ada koin kecil yang lolos filter likuiditas/umur")
        btc_sym = f"BTC/{cfg.quote}"
        data = await self._download(symbols + [btc_sym], "1h", "Koin kecil")
        btc = data.pop(btc_sym, [])
        if not data:
            return empty_sleeve("Riwayat harga koin kecil tidak bisa diunduh")
        self._progress(len(symbols), len(symbols) + 1, "Koin kecil: menguji 36 kombinasi parameter…")
        costs = Costs.from_cfg(cfg)
        flt = btc_entry_filter(btc, cfg)

        def work() -> dict:
            rows = evaluate_grid(data, ALT_GRID, lambda p: alts_strategy(cfg, p),
                                 lambda p: (p["breakout_lookback"],), costs, cfg.learning.train_fraction, flt)
            return sleeve_result(rows, cfg.learning.alts_min_pf, cfg, list(data), cfg.momentum.timeframe)

        return await asyncio.to_thread(work)


# ---- model store + live monitor --------------------------------------------------------------------------
class ModelStore:
    def __init__(self, engine: "Engine"):
        self.engine = engine

    def load(self) -> dict[str, dict]:
        m = self.engine.store.get("model") or {}
        return {"majors": m.get("majors") or empty_sleeve(), "alts": m.get("alts") or empty_sleeve()}

    def save_sleeve(self, sleeve: str, data: dict) -> None:
        m = self.load()
        m[sleeve] = data
        self.engine.store.set("model", m)
        hist = self.engine.store.get("model_history") or []
        hist.insert(0, {"sleeve": sleeve, "status": data["status"], "reason": data["reason"],
                        "trained_at": data.get("trained_at"), "timeframe": data.get("timeframe"),
                        "params": data.get("params"), "test": data.get("test")})
        self.engine.store.set("model_history", hist[:20])
        self.engine.store.audit("learning", "model", f"{sleeve}: {data['reason']}")

    def set_status(self, sleeve: str, status: str, reason: str) -> None:
        m = self.load()
        m[sleeve].update(status=status, valid=status == "ok", reason=reason)
        self.engine.store.set("model", m)

    def live_stats(self, sleeve: str) -> dict:
        lc = self.engine.cfg.learning
        since = self.load()[sleeve].get("trained_at") or 0.0
        rows = self.engine.store.closed_positions(sleeve, since, lc.live_window)
        pnls = [r["pnl"] or 0.0 for r in rows]
        gains = sum(p for p in pnls if p > 0)
        losses = -sum(p for p in pnls if p < 0)
        streak = 0
        for p in pnls:  # newest first
            if p < 0:
                streak += 1
            else:
                break
        return {"trades": len(pnls), "profit_factor": (gains / losses) if losses > 0 else None,
                "win_rate": (sum(p > 0 for p in pnls) / len(pnls) * 100) if pnls else None,
                "consecutive_losses": streak, "status": self.load()[sleeve]["status"]}

    def degraded_reason(self, sleeve: str) -> str | None:
        lc = self.engine.cfg.learning
        s = self.live_stats(sleeve)
        if s["consecutive_losses"] >= lc.live_max_consecutive_losses:
            return f"{s['consecutive_losses']} trade live rugi berturut-turut"
        if s["trades"] >= lc.live_min_trades and s["profit_factor"] is not None \
                and s["profit_factor"] < lc.live_min_pf:
            return f"profit factor live {s['profit_factor']:.2f} dari {s['trades']} trade (< {lc.live_min_pf})"
        return None
