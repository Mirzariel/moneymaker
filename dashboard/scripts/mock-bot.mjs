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
  };
}

const openPositions = [
  { id: 12, symbol: "BTC/IDR", status: "open", amount: 0.0231, entry_price: 64210.5 * K, cost: 1483.2 * K, stop_price: 62900 * K, take_profit: 67500 * K, stop_order_id: "stp-9981", opened_at: now() - 7200, closed_at: null, exit_price: null, proceeds: null, pnl: null, fees: 1.48 * K, exit_reason: null, last_price: 64890.1 * K, unrealized_pnl: 15.7 * K },
  { id: 13, symbol: "SOL/IDR", status: "open", amount: 10.5, entry_price: 155.42 * K, cost: 1631.9 * K, stop_price: 150.1 * K, take_profit: 168 * K, stop_order_id: null, opened_at: now() - 3000, closed_at: null, exit_price: null, proceeds: null, pnl: null, fees: 1.63 * K, exit_reason: null, last_price: 153.8 * K, unrealized_pnl: -17.0 * K },
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
    last_price: null, unrealized_pnl: null,
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
