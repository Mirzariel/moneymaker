const pad = (n: number) => String(n).padStart(2, "0");

/** epoch DETIK -> "YYYY-MM-DD HH:mm" (waktu lokal) */
export function fmtTime(ts: number | null | undefined): string {
  if (ts == null || !Number.isFinite(ts)) return "–";
  const d = new Date(ts * 1000);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function fmtNum(n: number | null | undefined, maxFrac = 2, minFrac = 0): string {
  if (n == null || !Number.isFinite(n)) return "–";
  return n.toLocaleString("id-ID", { minimumFractionDigits: minFrac, maximumFractionDigits: maxFrac });
}

/** Jumlah uang dengan label quote, mis. "1.234,56 USDT"; untuk IDR: "Rp 25.000" */
export function fmtMoney(n: number | null | undefined, quote: string, signed = false): string {
  if (n == null || !Number.isFinite(n)) return "–";
  // Rupiah has no meaningful cents: always whole numbers. Other quotes keep 2 decimals below 1000.
  const big = Math.abs(n) >= 1000 || quote === "IDR";
  const s = fmtNum(Math.abs(n), big ? 0 : 2, big ? 0 : 2);
  const sign = n < 0 ? "-" : signed && n > 0 ? "+" : "";
  return quote === "IDR" ? `${sign}Rp ${s}` : `${sign}${s} ${quote}`;
}

/** Nilai besar ringkas, mis. "Rp 4,5 M" (miliar) / "Rp 450 jt" untuk IDR */
export function fmtCompactMoney(n: number | null | undefined, quote: string): string {
  if (n == null || !Number.isFinite(n)) return "–";
  if (quote !== "IDR") {
    const s = n.toLocaleString("en-US", { notation: "compact", maximumFractionDigits: 1 });
    return `${s} ${quote}`;
  }
  const a = Math.abs(n);
  if (a >= 1e12) return `Rp ${fmtNum(n / 1e12, 1)} T`;
  if (a >= 1e9) return `Rp ${fmtNum(n / 1e9, 1)} M`;
  if (a >= 1e6) return `Rp ${fmtNum(n / 1e6, 0)} jt`;
  return fmtMoney(n, quote);
}

/** Harga: lebih banyak desimal untuk nilai kecil */
export function fmtPrice(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "–";
  const a = Math.abs(n);
  const frac = a >= 1000 ? 2 : a >= 1 ? 4 : a >= 0.01 ? 6 : 8;
  return fmtNum(n, frac);
}

export function fmtAmount(n: number | null | undefined): string {
  return fmtNum(n, 8);
}

export function fmtAge(sec: number): string {
  const s = Math.max(0, Math.round(sec));
  if (s < 120) return `${s}s`;
  if (s < 7200) return `${Math.floor(s / 60)}m ${s % 60}s`;
  return `${Math.floor(s / 3600)}j ${Math.floor((s % 3600) / 60)}m`;
}

export function pnlClass(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n) || n === 0) return "";
  return n > 0 ? "pos" : "neg";
}

export function fmtDetail(v: unknown): string {
  if (v == null) return "";
  return typeof v === "string" ? v : JSON.stringify(v);
}

/** epoch DETIK -> "3 mnt lalu" / "2 j lalu" / "5 hr lalu" (atau "dalam ..." untuk masa depan) */
export function fmtRelative(ts: number | null | undefined, nowMs = Date.now()): string {
  if (ts == null || !Number.isFinite(ts)) return "–";
  const diff = nowMs / 1000 - ts;
  const a = Math.abs(diff);
  let t: string;
  if (a < 60) t = `${Math.round(a)} dtk`;
  else if (a < 3600) t = `${Math.round(a / 60)} mnt`;
  else if (a < 86400) t = `${Math.round(a / 3600)} j`;
  else t = `${Math.round(a / 86400)} hr`;
  return diff >= 0 ? `${t} lalu` : `dalam ${t}`;
}

export function fmtPct(n: number | null | undefined, digits = 2, signed = false): string {
  if (n == null || !Number.isFinite(n)) return "–";
  return `${signed && n > 0 ? "+" : ""}${fmtNum(n, digits, digits)}%`;
}

export function fmtPF(n: number | null | undefined): string {
  return n == null || !Number.isFinite(n) ? "∞" : fmtNum(n, 2, 2);
}

export const SLEEVE_TITLE: Record<string, string> = { majors: "Koin terkenal", alts: "Koin kecil / alt" };

export const SLEEVE_STATUS: Record<string, { tone: "ok" | "warn" | "bad" | "neutral"; label: string; short: string }> = {
  ok: { tone: "ok", label: "Edge tervalidasi ✓ — boleh trading", short: "tervalidasi" },
  no_edge: { tone: "warn", label: "Belum ada keunggulan — bot diam (aman)", short: "belum ada edge" },
  degraded: { tone: "bad", label: "Performa live menurun — entry dijeda, latih ulang", short: "menurun" },
  untrained: { tone: "neutral", label: "Belum dilatih", short: "belum dilatih" },
};
export function sleeveStatus(s: string | null | undefined) {
  return SLEEVE_STATUS[s ?? "untrained"] ?? SLEEVE_STATUS.untrained;
}

export function regimeInfo(r: { alts_blocked: boolean; all_blocked: boolean } | null | undefined) {
  if (!r) return { tone: "neutral" as const, label: "Regime: menunggu data", short: "menunggu" };
  if (r.all_blocked) return { tone: "bad" as const, label: "Semua entry ditahan: BTC bergerak ekstrem", short: "semua ditahan" };
  if (r.alts_blocked) return { tone: "warn" as const, label: "Alt diblok: BTC melemah", short: "alt diblok" };
  return { tone: "ok" as const, label: "Pasar normal", short: "normal" };
}
