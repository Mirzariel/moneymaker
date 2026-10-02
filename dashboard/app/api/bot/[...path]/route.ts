import { NextRequest, NextResponse } from "next/server";

// Proxy sisi server: token API TIDAK PERNAH dikirim ke browser.
export const dynamic = "force-dynamic";
export const runtime = "nodejs";

const GET_PATHS = new Set([
  "health",
  "status",
  "positions",
  "orders",
  "equity",
  "signals",
  "risk-events",
  "audit",
  "config",
]);
const POST_PATHS = new Set(["pause", "resume", "panic"]);

type Ctx = { params: Promise<{ path?: string[] }> };

function notFound() {
  return NextResponse.json({ detail: "not found" }, { status: 404 });
}

async function handle(req: NextRequest, ctx: Ctx, method: "GET" | "POST") {
  const { path } = await ctx.params;
  const segments = path ?? [];
  // Hanya satu segmen yang diizinkan, persis sama dengan allowlist.
  if (segments.length !== 1) return notFound();
  const name = segments[0];
  const allowed = method === "GET" ? GET_PATHS : POST_PATHS;
  if (!allowed.has(name)) return notFound();

  // Proteksi DNS rebinding: hanya terima Host lokal (atau yang diizinkan lewat DASHBOARD_ALLOWED_HOSTS).
  const hostname = (req.headers.get("host") ?? "").replace(/:\d+$/, "").toLowerCase();
  const allowedHosts = new Set(
    ["127.0.0.1", "localhost", "[::1]"].concat(
      (process.env.DASHBOARD_ALLOWED_HOSTS ?? "")
        .split(",")
        .map((h) => h.trim().toLowerCase())
        .filter(Boolean),
    ),
  );
  if (!allowedHosts.has(hostname)) {
    return NextResponse.json({ detail: "forbidden host" }, { status: 403 });
  }

  if (method === "POST") {
    // Proteksi CSRF: halaman web lain tidak boleh memicu pause/resume/panic.
    const origin = req.headers.get("origin");
    if (origin) {
      let originHost = "";
      try {
        originHost = new URL(origin).host;
      } catch {
        /* ignore */
      }
      if (originHost !== req.headers.get("host")) {
        return NextResponse.json({ detail: "forbidden origin" }, { status: 403 });
      }
    }
    const ct = req.headers.get("content-type") ?? "";
    if (!ct.toLowerCase().startsWith("application/json")) {
      return NextResponse.json(
        { detail: "content-type must be application/json" },
        { status: 415 },
      );
    }
  }

  const base = (process.env.BOT_API_URL ?? "http://127.0.0.1:8000").replace(/\/+$/, "");
  const url = `${base}/api/${name}${req.nextUrl.search}`;

  const headers: Record<string, string> = {
    Authorization: `Bearer ${process.env.BOT_API_TOKEN ?? ""}`,
    Accept: "application/json",
  };
  const init: RequestInit = {
    method,
    headers,
    cache: "no-store",
    signal: AbortSignal.timeout(method === "POST" ? 120_000 : 20_000),
  };
  if (method === "POST") {
    headers["Content-Type"] = "application/json";
    const body = await req.text();
    if (body) init.body = body;
  }

  let upstream: Response;
  try {
    upstream = await fetch(url, init);
  } catch {
    return NextResponse.json({ detail: "bot unreachable" }, { status: 502 });
  }

  const text = await upstream.text();
  return new NextResponse(text, {
    status: upstream.status,
    headers: {
      "Content-Type": upstream.headers.get("content-type") ?? "application/json",
      "Cache-Control": "no-store",
    },
  });
}

export function GET(req: NextRequest, ctx: Ctx) {
  return handle(req, ctx, "GET");
}

export function POST(req: NextRequest, ctx: Ctx) {
  return handle(req, ctx, "POST");
}
