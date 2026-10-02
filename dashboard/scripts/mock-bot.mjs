// Mock bot API untuk uji dashboard. Tanpa dependency: `node scripts/mock-bot.mjs`
// Env: MOCK_MODE=paper|live (default paper), MOCK_PORT (default 8000)
import http from "node:http";

const TOKEN = "test-token-1234567890";
const PORT = Number(process.env.MOCK_PORT ?? 8000);
const MODE = process.env.MOCK_MODE === "live" ? "live" : "paper";
const now = () => Date.now() / 1000;

const state = { status: "RUNNING", reason: "running", changed: now() - 3600 };
const startedAt = now();

function statusObj() {
  return {
    status: state.status,
    status_reason: state.reason,
    status_changed_at: state.changed,
    mode: MODE,
    quote: "USDT",
    timeframe: "15m",
    last_cycle_at: now() - 42,
    last_error: null,
    open_positions: 2,
    equity: 10234.56,
    quote_free: 7120.4,
    exposure: 3114.16,
    day_pnl: 87.31,
    universe: ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT"],
    server_time: now(),
  };
}

const openPositions = [
  { id: 12, symbol: "BTC/USDT", status: "open", amount: 0.0231, entry_price: 64210.5, cost: 1483.2, stop_price: 62900, take_profit: 67500, stop_order_id: "stp-9981", opened_at: now() - 7200, closed_at: null, exit_price: null, proceeds: null, pnl: null, fees: 1.48, exit_reason: null, last_price: 64890.1, unrealized_pnl: 15.7 },
  { id: 13, symbol: "SOL/USDT", status: "open", amount: 10.5, entry_price: 155.42, cost: 1631.9, stop_price: 150.1, take_profit: 168, stop_order_id: null, opened_at: now() - 3000, closed_at: null, exit_price: null, proceeds: null, pnl: null, fees: 1.63, exit_reason: null, last_price: 153.8, unrealized_pnl: -17.0 },
];

const reasons = ["take_profit", "stop_loss", "take_profit", "manual", "stop_loss"];
const syms = ["ETH/USDT", "BTC/USDT", "BNB/USDT", "SOL/USDT"];
const closedPositions = Array.from({ length: 12 }, (_, i) => {
  const entry = 100 + i * 7.3;
  const win = i % 3 !== 1;
  const exit = entry * (win ? 1.021 : 0.985);
  const amount = 10;
  return {
    id: 11 - i, symbol: syms[i % syms.length], status: "closed", amount, entry_price: entry, cost: entry * amount,
    stop_price: entry * 0.98, take_profit: entry * 1.03, stop_order_id: null,
    opened_at: now() - 86400 * (i + 1), closed_at: now() - 86400 * (i + 1) + 5400, exit_price: exit,
    proceeds: exit * amount, pnl: (exit - entry) * amount - 1.2, fees: 1.2, exit_reason: reasons[i % reasons.length],
    last_price: null, unrealized_pnl: null,
  };
});

function equitySeries(hours) {
  const n = Math.min(hours * 2, 336);
  const out = [];
  let eq = 9800;
  for (let i = n; i >= 0; i--) {
    eq += Math.sin(i / 9) * 22 + (Math.random() - 0.42) * 30;
    const exposure = 2000 + Math.abs(Math.sin(i / 20)) * 1500;
    out.push({ ts: now() - i * 1800, equity: Math.round(eq * 100) / 100, quote_free: Math.round((eq - exposure) * 100) / 100, exposure: Math.round(exposure * 100) / 100 });
  }
  return out;
}

const signals = Array.from({ length: 25 }, (_, i) => {
  const ok = i % 3 === 0;
  const sym = syms[i % syms.length];
  const entry = 100 + i * 11.7;
  return {
    id: 300 - i, ts: now() - i * 1500, symbol: sym, side: "buy", entry, stop: entry * 0.98, take_profit: entry * 1.03,
    expires_at: now() - i * 1500 + 900, reason: "breakout 20-bar high + volume spike (ratio 2.1)",
    decision: ok ? "approved" : "rejected",
    decision_detail: ok ? "ok" : "max_exposure: 31.2% > 30% — signal ditolak karena batas exposure",
  };
});

const riskEvents = Array.from({ length: 8 }, (_, i) => ({
  id: 50 - i, ts: now() - i * 7000, symbol: i % 2 ? "ETH/USDT" : null,
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
  universe: ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT"],
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

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url ?? "/", "http://127.0.0.1");
  const send = (code, obj) => {
    res.writeHead(code, { "Content-Type": "application/json" });
    res.end(JSON.stringify(obj));
  };
  const route = url.pathname.replace(/^\/api\//, "");
  if (!url.pathname.startsWith("/api/")) return send(404, { detail: "Not Found" });

  if (req.method === "GET" && route === "health") {
    return send(200, { ok: true, last_cycle_at: now() - 42, server_time: now() });
  }
  if (req.headers.authorization !== `Bearer ${TOKEN}`) return send(401, { detail: "invalid token" });

  const q = url.searchParams;
  if (req.method === "GET") {
    switch (route) {
      case "status": return send(200, statusObj());
      case "positions": {
        const s = q.get("status") ?? "open";
        const base = s === "open" ? openPositions : s === "closed" ? closedPositions : [...openPositions, ...closedPositions];
        return send(200, limited(base, q));
      }
      case "orders": return send(200, []);
      case "equity": return send(200, equitySeries(Number(q.get("hours") ?? 24)));
      case "signals": return send(200, limited(signals, q));
      case "risk-events": return send(200, limited(riskEvents, q));
      case "audit": return send(200, limited(audit, q));
      case "config": return send(200, config);
    }
  }
  if (req.method === "POST") {
    const raw = await readBody(req);
    let body = {};
    try { body = raw ? JSON.parse(raw) : {}; } catch { return send(422, { detail: "bad json" }); }
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
      return send(200, { cancelled: 2, sold: ["BTC/USDT", "SOL/USDT"], errors: [], leftover: { DOGE: 0.00012 } });
    }
  }
  return send(404, { detail: "Not Found" });
});

server.listen(PORT, "127.0.0.1", () => {
  console.log(`mock bot listening on http://127.0.0.1:${PORT} (mode=${MODE}), token=${TOKEN}`);
});
