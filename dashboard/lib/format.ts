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

/** Jumlah uang dengan label quote, mis. "1.234,56 USDT" */
export function fmtMoney(n: number | null | undefined, quote: string, signed = false): string {
  if (n == null || !Number.isFinite(n)) return "–";
  const big = Math.abs(n) >= 1000;
  const s = fmtNum(Math.abs(n), big ? 0 : 2, big ? 0 : 2);
  const sign = n < 0 ? "-" : signed && n > 0 ? "+" : "";
  return `${sign}${s} ${quote}`;
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
