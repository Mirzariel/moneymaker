// Mock bot untuk uji dashboard. Tanpa dependency: `node scripts/mock-bot.mjs`
// Menyajikan hasil export statis (folder out/, jalankan `npm run build` dulu) di "/"
// plus API tiruan di "/api/*" pada origin yang sama (seperti bot asli).
// Login: buka http://127.0.0.1:8000/login?token=test-token-1234567890 (cookie sesi HttpOnly).
// Env: MOCK_MODE=paper|live (default paper), MOCK_PORT (default 8000)
//      MOCK_RUNNING=1 -> langsung mulai dalam keadaan "running" (lewati wizard setup)
import http from "node:http";
import { existsSync, readFileSync, statSync } from "node:fs";
import { dirname, extname, join, normalize, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const TOKEN = "test-token-1234567890";
const OUT_DIR = resolve(dirname(fileURLToPath(import.meta.url)), "../out");
const MIME = {
  ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
  ".json": "application/json", ".svg": "image/svg+xml", ".ico": "image/x-icon", ".png": "image/png",
  ".txt": "text/plain; charset=utf-8", ".woff2": "font/woff2", ".map": "application/json",
};
const PORT = Number(process.env.MOCK_PORT ?? 8000);
const MODE = process.env.MOCK_MODE === "live" ? "live" : "paper";
const now = () => Date.now() / 1000;
const K = 16000; // kurs kasar USDT -> IDR untuk data tiruan

const state = { status: "RUNNING", reason: "running", changed: now() - 3600 };
const startedAt = now();

// ---- state machine setup ----
const START_RUNNING = process.env.MOCK_RUNNING === "1";
const setupState = {
  state: START_RUNNING ? "running" : "setup", // setup | starting | running | error
  error: null,
  live_trading: MODE === "live",
  toko_api_key: START_RUNNING ? "demo-key-abcd" : "",
  toko_api_secret: START_RUNNING ? "demo-secret" : "",
  telegram_bot_token: "",
  telegram_chat_id: "",
  healthcheck_url: "",
};
let startTimer = null;

function missingFields() {
  const m = [];
  if (!setupState.toko_api_key) m.push("toko_api_key");
  if (!setupState.toko_api_secret) m.push("toko_api_secret");
  return m;
}
function setupObj() {
  const key = setupState.toko_api_key;
  const tok = setupState.telegram_bot_token;
  const missing = missingFields();
  return {
    state: setupState.state,
    error: setupState.error,
    ready: missing.length === 0,
    missing,
    live_trading: setupState.live_trading,
    quote: "IDR",
    fields: {
      toko_api_key: { set: !!key, hint: key ? key.slice(-4) : null },
      toko_api_secret: { set: !!setupState.toko_api_secret, hint: null },
      telegram_bot_token: { set: !!tok, hint: tok ? tok.slice(-4) : null },
      telegram_chat_id: { set: !!setupState.telegram_chat_id, value: setupState.telegram_chat_id || null },
      healthcheck_url: { set: !!setupState.healthcheck_url, value: setupState.healthcheck_url || null },
    },
  };
}
const isRunning = () => setupState.state === "running";

function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

function preflightObj() {
  const top = [
    { symbol: "BTC/IDR", quote_volume: 48_500_000_000, spread_pct: 0.04, native: true, stop_limit: true, tradeable: true },
    { symbol: "ETH/IDR", quote_volume: 21_300_000_000, spread_pct: 0.06, native: true, stop_limit: true, tradeable: true },
    { symbol: "SOL/IDR", quote_volume: 6_700_000_000, spread_pct: 0.11, native: false, stop_limit: true, tradeable: true },
    { symbol: "DOGE/IDR", quote_volume: 2_150_000_000, spread_pct: 0.18, native: false, stop_limit: true, tradeable: true },
    { symbol: "XRP/IDR", quote_volume: 1_420_000_000, spread_pct: 0.32, native: false, stop_limit: false, tradeable: false },
  ];
  return {
    ok: true,
    quote: "IDR",
    binance_reachable: true,
    markets_active: 38,
    fees: { buy_pct: 0.1, sell_pct: 0.1 },
    equity: 5_000_000,
    equity_source: "balance",
    top,
    sizing: [
      { symbol: "BTC/IDR", approved: true, cost: 1_250_000, rule: "ok", detail: "ok", notes: ["risiko 0,5% equity, stop 2%"] },
      { symbol: "ETH/IDR", approved: true, cost: 980_000, rule: "ok", detail: "ok", notes: [] },
      { symbol: "SOL/IDR", approved: true, cost: 410_000, rule: "ok", detail: "ok", notes: ["dibatasi max_position_pct"] },
      { symbol: "DOGE/IDR", approved: false, cost: 0, rule: "min_notional", detail: "nilai order Rp 8.200 di bawah minimum Tokocrypto Rp 20.000", notes: ["naikkan risk_per_trade_pct atau saldo"] },
    ],
    candidates_checked: 38,
    warnings: ["XRP/IDR tidak punya order stop-limit, jadi tidak bisa memakai stop-loss di bursa dan dilewati."],
  };
}

function statusObj() {
  return {
    status: state.status,
    status_reason: state.reason,
    status_changed_at: state.changed,
    mode: MODE,
    quote: "IDR",
    timeframe: "15m",
    last_cycle_at: now() - 42,
    last_error: null,
    open_positions: 2,
    equity: 10234.56 * K,
    quote_free: 7120.4 * K,
    exposure: 3114.16 * K,
    day_pnl: 87.31 * K,
    universe: ["BTC/IDR", "ETH/IDR", "SOL/IDR", "BNB/IDR"],
    server_time: now(),
    model: { majors: modelObj().sleeves.majors.status, alts: modelObj().sleeves.alts.status },
    regime: REGIME,
  };
}

const openPositions = [
  { id: 12, symbol: "BTC/IDR", status: "open", amount: 0.0231, entry_price: 64210.5 * K, cost: 1483.2 * K, stop_price: 62900 * K, take_profit: 67500 * K, stop_order_id: "stp-9981", opened_at: now() - 7200, closed_at: null, exit_price: null, proceeds: null, pnl: null, fees: 1.48 * K, exit_reason: null, last_price: 64890.1 * K, unrealized_pnl: 15.7 * K, sleeve: "majors", trailing: 1, highest: 65120 * K },
  { id: 13, symbol: "SOL/IDR", status: "open", amount: 10.5, entry_price: 155.42 * K, cost: 1631.9 * K, stop_price: 150.1 * K, take_profit: 168 * K, stop_order_id: null, opened_at: now() - 3000, closed_at: null, exit_price: null, proceeds: null, pnl: null, fees: 1.63 * K, exit_reason: null, last_price: 153.8 * K, unrealized_pnl: -17.0 * K, sleeve: "alts", trailing: 0, highest: 156.2 * K },
];

const reasons = ["take_profit", "stop_loss", "take_profit", "manual", "stop_loss"];
const syms = ["ETH/IDR", "BTC/IDR", "BNB/IDR", "SOL/IDR"];
const closedPositions = Array.from({ length: 12 }, (_, i) => {
  const entry = (100 + i * 7.3) * K;
  const win = i % 3 !== 1;
  const exit = entry * (win ? 1.021 : 0.985);
  const amount = 10;
  return {
    id: 11 - i, symbol: syms[i % syms.length], status: "closed", amount, entry_price: entry, cost: entry * amount,
    stop_price: entry * 0.98, take_profit: entry * 1.03, stop_order_id: null,
    opened_at: now() - 86400 * (i + 1), closed_at: now() - 86400 * (i + 1) + 5400, exit_price: exit,
    proceeds: exit * amount, pnl: (exit - entry) * amount - 1.2 * K, fees: 1.2 * K, exit_reason: reasons[i % reasons.length],
    last_price: null, unrealized_pnl: null, sleeve: i % 4 === 3 ? "alts" : "majors", trailing: 0, highest: null,
  };
});

function equitySeries(hours) {
  const n = Math.min(hours * 2, 336);
  const out = [];
  let eq = 9800 * K;
  for (let i = n; i >= 0; i--) {
    eq += (Math.sin(i / 9) * 22 + (Math.random() - 0.42) * 30) * K;
    const exposure = (2000 + Math.abs(Math.sin(i / 20)) * 1500) * K;
    out.push({ ts: now() - i * 1800, equity: Math.round(eq * 100) / 100, quote_free: Math.round((eq - exposure) * 100) / 100, exposure: Math.round(exposure * 100) / 100 });
  }
  return out;
}

const signals = Array.from({ length: 25 }, (_, i) => {
  const ok = i % 3 === 0;
  const sym = syms[i % syms.length];
  const entry = (100 + i * 11.7) * K;
  return {
    id: 300 - i, ts: now() - i * 1500, symbol: sym, side: "buy", entry, stop: entry * 0.98, take_profit: entry * 1.03,
    expires_at: now() - i * 1500 + 900, reason: "breakout 20-bar high + volume spike (ratio 2.1)",
    decision: ok ? "approved" : "rejected",
    decision_detail: ok ? "ok" : "max_exposure: 31,2% > 30% — signal ditolak karena batas exposure",
  };
});

const riskEvents = Array.from({ length: 8 }, (_, i) => ({
  id: 50 - i, ts: now() - i * 7000, symbol: i % 2 ? "ETH/IDR" : null,
  rule: ["max_exposure", "daily_loss_limit", "cooldown", "max_open_positions"][i % 4],
  detail: i % 2 ? { limit: 0.3, actual: 0.312 } : "batas tercapai",
  action: i % 3 ? "reject" : "pause",
}));

const audit = [
  { id: 9, ts: now() - 3600, actor: "dashboard", action: "resume", detail: "manual (dashboard)" },
  { id: 8, ts: now() - 7200, actor: "dashboard", action: "pause", detail: "manual (dashboard)" },
  { id: 7, ts: now() - 86400, actor: "bot", action: "start", detail: { mode: MODE, version: "0.1.0" } },
];

const config = {
  mode: MODE,
  exchange: { name: "binance", sandbox: MODE === "paper" },
  strategy: { timeframe: "15m", lookback: 20, volume_ratio: 1.8 },
  risk: { max_exposure_pct: 30, risk_per_trade_pct: 0.5, daily_loss_limit_pct: 2, max_open_positions: 3 },
  universe: ["BTC/IDR", "ETH/IDR", "SOL/IDR", "BNB/IDR"],
};


// ---- model / radar tiruan ----
const REGIME = {
  btc_symbol: "BTC/IDR", btc_change_24h_pct: 1.34, btc_below_ema50: false,
  alts_blocked: false, all_blocked: false, reason: "BTC di atas EMA50 dan pergerakan 24j wajar",
};
const m = (trades, wr, pf, ret, dd, avg) => ({ trades, win_rate: wr, profit_factor: pf, total_return_pct: ret, max_drawdown_pct: dd, avg_trade_pct: avg });
const trainedAt = now() - 5 * 3600;
const model = {
  learning_enabled: true,
  require_validated_edge: true,
  sleeves: {
    majors: {
      status: "ok", valid: true, reason: "Lolos uji: PF uji 1,62 dengan 38 trade (minimal 30), return uji positif.",
      timeframe: "1h", params: { breakout_lookback: 24, volume_ratio: 1.8, atr_stop_mult: 2.0, atr_tp_mult: 3.5, trail_atr_mult: 1.5 },
      symbols: ["BTC/IDR", "ETH/IDR", "SOL/IDR", "BNB/IDR", "XRP/IDR", "DOGE/IDR"], trained_at: trainedAt,
      train: m(112, 47.3, 1.41, 38.6, 9.2, 0.34), test: m(38, 52.6, 1.62, 14.8, 5.1, 0.39),
      per_timeframe: [
        { timeframe: "15m", params: { breakout_lookback: 32 }, train: m(260, 41.2, 1.08, 9.1, 14.0, 0.04), test: m(88, 38.6, 0.94, -3.2, 11.3, -0.04), passed: false },
        { timeframe: "30m", params: { breakout_lookback: 28 }, train: m(171, 44.4, 1.22, 18.4, 11.1, 0.11), test: m(61, 45.9, 1.12, 3.1, 8.2, 0.05), passed: false },
        { timeframe: "1h", params: { breakout_lookback: 24 }, train: m(112, 47.3, 1.41, 38.6, 9.2, 0.34), test: m(38, 52.6, 1.62, 14.8, 5.1, 0.39), passed: true },
        { timeframe: "4h", params: { breakout_lookback: 18 }, train: m(44, 45.5, 1.35, 21.0, 8.4, 0.48), test: m(14, 42.9, 1.05, 0.9, 6.0, 0.06), passed: false },
      ],
    },
    alts: {
      status: "no_edge", valid: false, reason: "Uji gagal: PF uji 0,82 (minimal 1,3) — tidak ada kombinasi parameter yang bertahan di data baru.",
      timeframe: "1h", params: null, symbols: ["PEPE/IDR", "WIF/IDR", "ARB/IDR", "INJ/IDR", "SUI/IDR", "TIA/IDR", "JUP/IDR", "ENA/IDR"], trained_at: trainedAt,
      train: m(74, 43.2, 1.18, 11.5, 15.7, 0.15), test: m(22, 36.4, 0.82, -6.4, 12.9, -0.29), per_timeframe: [],
    },
  },
  job: { state: "idle", sleeve: null, progress: { done: 0, total: 0, label: "" }, started_at: null, finished_at: trainedAt, error: null },
  next_training_at: now() + 19 * 3600,
  live: {
    majors: { trades: 7, profit_factor: 1.9, win_rate: 57.1, consecutive_losses: 1, status: "sehat" },
    alts: { trades: 0, profit_factor: null, win_rate: null, consecutive_losses: 0, status: "tidak aktif" },
  },
};
let trainStart = null;
const TRAIN_MS = 20_000;
const TRAIN_STEPS = ["Mengunduh data candle", "Mencari parameter majors", "Uji walk-forward majors", "Mencari parameter alts", "Uji walk-forward alts", "Menyimpan model"];
function modelObj() {
  if (trainStart != null) {
    const el = Date.now() - trainStart;
    if (el >= TRAIN_MS) {
      trainStart = null;
      model.job = { state: "done", sleeve: null, progress: { done: 60, total: 60, label: "Selesai" }, started_at: model.job.started_at, finished_at: now(), error: null };
      model.sleeves.majors.trained_at = model.sleeves.alts.trained_at = now();
      model.next_training_at = now() + 24 * 3600;
    } else {
      const done = Math.floor((el / TRAIN_MS) * 60);
      model.job.state = "running";
      model.job.progress = { done, total: 60, label: TRAIN_STEPS[Math.min(TRAIN_STEPS.length - 1, Math.floor((el / TRAIN_MS) * TRAIN_STEPS.length))] };
      model.job.sleeve = done < 30 ? "majors" : "alts";
    }
  }
  return model;
}
function startTraining() {
  if (trainStart == null) {
    trainStart = Date.now();
    model.job = { state: "running", sleeve: "majors", progress: { done: 0, total: 60, label: TRAIN_STEPS[0] }, started_at: now(), finished_at: null, error: null };
  }
  return modelObj();
}

const FAIL = [
  ["listing baru (12 hr)", { age_days: 12 }], ["sudah naik 45% (24j)", { change_24h_pct: 45.2, ext_atr: 7.4 }],
  ["spread 1.2%", {}], ["volume kecil", { volume_24h: 90_000_000 }], ["BTC melemah (alt diblok)", {}],
  ["volatilitas terlalu rendah", { atr_pct: 0.22 }],
];
const MAJ = ["BTC", "ETH", "BNB", "SOL", "XRP", "DOGE", "ADA", "TRX", "LINK", "AVAX", "DOT", "LTC"];
const ALT = ["PEPE", "WIF", "ARB", "INJ", "SUI", "TIA", "JUP", "ENA", "NEAR", "APT", "OP", "FET", "RNDR", "SEI", "STRK", "ONDO", "PYTH", "BONK", "FLOKI", "GALA", "AAVE", "UNI", "IMX", "TON"];
let seed = 7;
const rnd = () => ((seed = (seed * 16807) % 2147483647) / 2147483647);
const radarPairs = [...MAJ.map((b) => [b, "majors"]), ...ALT.map((b) => [b, "alts"])].map(([base, category], i) => {
  const maj = category === "majors";
  const failIdx = !maj && i % 3 === 0 ? (i / 3) % FAIL.length : -1;
  const vol = maj ? (0.3 + rnd() * 40) * 1e9 : (0.05 + rnd() * 3) * 1e9;
  const price = maj ? Math.pow(10, 3 + rnd() * 5) : Math.pow(10, rnd() * 5);
  const p = {
    symbol: `${base}/IDR`, base, category, last: price, change_24h_pct: (rnd() - 0.45) * (maj ? 7 : 16),
    volume_24h: vol, atr_pct: 0.5 + rnd() * (maj ? 1.5 : 3.5), rs_vs_btc: rnd() < 0.08 ? null : (rnd() - 0.5) * 8,
    age_days: rnd() < 0.1 ? null : Math.round(maj ? 400 + rnd() * 2500 : 60 + rnd() * 700), vol_surge: 0.4 + rnd() * 2.8,
    ext_atr: rnd() * 4, eligible: true, reason: "", updated_at: now() - Math.floor(rnd() * 240),
  };
  if (failIdx >= 0) { const [reason, patch] = FAIL[failIdx]; Object.assign(p, patch, { eligible: false, reason }); }
  return p;
});
radarPairs.find((p) => p.base === "SEI").eligible = false;
radarPairs.find((p) => p.base === "SEI").reason = "spread 1.2%";
const radar = {
  updated_at: now() - 95, sweeps_completed: 14, progress: { done: radarPairs.length, total: radarPairs.length },
  regime: REGIME, pairs: radarPairs,
};

function limited(arr, q) {
  const l = Number(q.get("limit"));
  return Number.isFinite(l) && l > 0 ? arr.slice(0, l) : arr;
}

function readBody(req) {
  return new Promise((resolve) => {
    let s = "";
    req.on("data", (c) => (s += c));
    req.on("end", () => resolve(s));
  });
}


function parseCookies(req) {
  const out = {};
  for (const part of String(req.headers.cookie ?? "").split(";")) {
    const i = part.indexOf("=");
    if (i > 0) out[part.slice(0, i).trim()] = part.slice(i + 1).trim();
  }
  return out;
}

function serveStatic(req, res, pathname) {
  let rel = decodeURIComponent(pathname);
  if (rel.endsWith("/")) rel += "index.html";
  let file = normalize(join(OUT_DIR, rel));
  if (!file.startsWith(OUT_DIR)) { res.writeHead(403); return res.end("Forbidden"); }
  if (!existsSync(file) && existsSync(file + ".html")) file += ".html";
  if (!existsSync(file) || statSync(file).isDirectory()) {
    const nf = join(OUT_DIR, "404.html");
    res.writeHead(404, { "Content-Type": MIME[".html"] });
    return res.end(existsSync(nf) ? readFileSync(nf) : "Not Found (jalankan `npm run build` dulu)");
  }
  res.writeHead(200, { "Content-Type": MIME[extname(file)] ?? "application/octet-stream", "Cache-Control": "no-store" });
  res.end(readFileSync(file));
}

function applySetup(body) {
  const names = ["toko_api_key", "toko_api_secret", "telegram_bot_token", "telegram_chat_id", "healthcheck_url"];
  for (const k of names) {
    if (body[k] === undefined || body[k] === null) continue;
    if (typeof body[k] !== "string") return `${k} harus string`;
    setupState[k] = body[k].trim();
  }
  if (body.live_trading !== undefined && body.live_trading !== null) {
    if (typeof body.live_trading !== "boolean") return "live_trading harus boolean";
    setupState.live_trading = body.live_trading;
  }
  if (setupState.healthcheck_url && !/^https?:\/\//.test(setupState.healthcheck_url)) return "healthcheck_url harus diawali http(s)://";
  return null;
}

function restartBot() {
  clearTimeout(startTimer);
  if (missingFields().length) { setupState.state = "setup"; return; }
  setupState.state = "starting";
  setupState.error = null;
  startTimer = setTimeout(() => {
    setupState.state = "running";
    state.status = "PAUSED"; state.reason = "menunggu persetujuan (Mulai trading)"; state.changed = now();
  }, 3000);
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url ?? "/", "http://127.0.0.1");
  const send = (code, obj, headers = {}) => {
    res.writeHead(code, { "Content-Type": "application/json", ...headers });
    res.end(JSON.stringify(obj));
  };

  if (req.method === "GET" && url.pathname === "/login") {
    if (url.searchParams.get("token") !== TOKEN) { res.writeHead(403, { "Content-Type": "text/plain" }); return res.end("token salah"); }
    res.writeHead(302, { "Set-Cookie": "mm_session=ok; HttpOnly; SameSite=Strict; Path=/", Location: "/" });
    return res.end();
  }

  if (!url.pathname.startsWith("/api/")) return serveStatic(req, res, url.pathname);

  const route = url.pathname.replace(/^\/api\//, "");
  const authed = parseCookies(req).mm_session === "ok";

  if (req.method === "GET" && route === "health") return send(200, { ok: true, last_cycle_at: now() - 42, server_time: now() });
  if (req.method === "GET" && route === "session") return send(200, { authenticated: authed });
  if (!authed) return send(401, { detail: "sesi tidak valid" });

  const q = url.searchParams;
  if (req.method === "GET") {
    switch (route) {
      case "setup": return send(200, setupObj());
      case "preflight": await sleep(2000); return send(200, preflightObj());
      case "status": return isRunning() ? send(200, statusObj()) : send(409, { detail: "bot belum berjalan (mode setup)" });
      case "positions": {
        if (!isRunning()) return send(200, []);
        const s = q.get("status") ?? "open";
        const base = s === "open" ? openPositions : s === "closed" ? closedPositions : [...openPositions, ...closedPositions];
        return send(200, limited(base, q));
      }
      case "orders": return send(200, []);
      case "equity": return send(200, isRunning() ? equitySeries(Number(q.get("hours") ?? 24)) : []);
      case "signals": return send(200, isRunning() ? limited(signals, q) : []);
      case "risk-events": return send(200, isRunning() ? limited(riskEvents, q) : []);
      case "audit": return send(200, isRunning() ? limited(audit, q) : []);
      case "model": return send(200, modelObj());
      case "radar": return send(200, { ...radar, updated_at: now() - 95 });
      case "config": return send(200, isRunning() ? config : {});
    }
  }
  if (req.method === "POST") {
    const raw = await readBody(req);
    let body = {};
    try { body = raw ? JSON.parse(raw) : {}; } catch { return send(422, { detail: "bad json" }); }
    if (route === "setup") {
      const err = applySetup(body);
      if (err) return send(400, { detail: err });
      restartBot();
      return send(200, setupObj());
    }
    if (route === "setup/test-exchange") {
      await sleep(800);
      const key = body.toko_api_key || setupState.toko_api_key;
      const secret = body.toko_api_secret || setupState.toko_api_secret;
      if (!key || !secret) return send(200, { ok: false, error: "API key dan secret key belum diisi" });
      if (key.toLowerCase().includes("bad")) return send(200, { ok: false, error: "Invalid API-key, IP, or permissions for action (code -2015)" });
      return send(200, { ok: true, balances: { IDR: 5_000_000, BTC: 0.00012, USDT: 12.5 } });
    }
    if (route === "setup/detect-chat-id") {
      await sleep(500);
      if (!(body.telegram_bot_token || setupState.telegram_bot_token)) return send(200, { ok: false, error: "Isi bot token dulu" });
      return send(200, { ok: true, chat_id: "123456789", name: "Budi Santoso" });
    }
    if (route === "setup/test-telegram") {
      await sleep(500);
      if (!(body.telegram_bot_token || setupState.telegram_bot_token)) return send(200, { ok: false, error: "Isi bot token dulu" });
      return send(200, { ok: true });
    }
    if (route === "model/train") return send(200, startTraining());
    if (!isRunning()) return send(409, { detail: "bot belum berjalan (mode setup)" });
    if (route === "pause") {
      state.status = "PAUSED"; state.reason = body.reason ?? "paused"; state.changed = now();
      audit.unshift({ id: audit.length + 10, ts: now(), actor: "dashboard", action: "pause", detail: state.reason });
      return send(200, statusObj());
    }
    if (route === "resume") {
      state.status = "RUNNING"; state.reason = "resumed"; state.changed = now();
      audit.unshift({ id: audit.length + 10, ts: now(), actor: "dashboard", action: "resume", detail: "" });
      return send(200, statusObj());
    }
    if (route === "panic") {
      if (body.confirm !== "PANIC") return send(400, { detail: "confirm must be PANIC" });
      state.status = "PAUSED"; state.reason = "PANIC"; state.changed = now();
      openPositions.length = 0;
      return send(200, { cancelled: 2, sold: ["BTC/IDR", "SOL/IDR"], errors: [], leftover: { DOGE: 0.00012 } });
    }
  }
  return send(404, { detail: "Not Found" });
});

server.listen(PORT, "127.0.0.1", () => {
  console.log(`mock bot listening on http://127.0.0.1:${PORT} (mode=${MODE})`);
  console.log(`login: http://127.0.0.1:${PORT}/login?token=${TOKEN}`);
});
