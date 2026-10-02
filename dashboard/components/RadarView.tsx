"use client";

import { useMemo, useState } from "react";
import type { RadarInfo, RadarPair } from "@/lib/types";
import { fmtCompactMoney, fmtNum, fmtPct, fmtPrice, fmtRelative, fmtTime, pnlClass, regimeInfo } from "@/lib/format";
import { usePoll } from "@/lib/usePoll";

type Filter = "all" | "majors" | "alts" | "eligible";
type SortKey =
  | "symbol"
  | "category"
  | "last"
  | "change_24h_pct"
  | "volume_24h"
  | "atr_pct"
  | "rs_vs_btc"
  | "age_days"
  | "vol_surge"
  | "eligible";

const COLS: { key: SortKey; label: string; num?: boolean }[] = [
  { key: "symbol", label: "Symbol" },
  { key: "category", label: "Kategori" },
  { key: "last", label: "Harga", num: true },
  { key: "change_24h_pct", label: "24j %", num: true },
  { key: "volume_24h", label: "Volume 24j", num: true },
  { key: "atr_pct", label: "Volatilitas", num: true },
  { key: "rs_vs_btc", label: "vs BTC", num: true },
  { key: "age_days", label: "Umur", num: true },
  { key: "vol_surge", label: "Lonjakan vol", num: true },
  { key: "eligible", label: "Status" },
];

function fmtAgeDays(d: number | null): string {
  if (d == null) return "-";
  return d >= 365 ? `${fmtNum(d / 365, 1)} th` : `${Math.round(d)} hr`;
}

function sortValue(p: RadarPair, k: SortKey): number | string | null {
  if (k === "eligible") return p.eligible ? 1 : 0;
  return p[k] as number | string | null;
}

export function RadarView() {
  const { data, error } = usePoll<RadarInfo>("radar", () => 30_000);
  const [filter, setFilter] = useState<Filter>("all");
  const [q, setQ] = useState("");
  const [sort, setSort] = useState<{ key: SortKey | null; dir: 1 | -1 }>({ key: null, dir: -1 });

  const rows = useMemo(() => {
    if (!data) return [];
    const needle = q.trim().toLowerCase();
    let r = data.pairs.filter((p) => {
      if (filter === "majors" && p.category !== "majors") return false;
      if (filter === "alts" && p.category !== "alts") return false;
      if (filter === "eligible" && !p.eligible) return false;
      return !needle || p.symbol.toLowerCase().includes(needle) || p.base.toLowerCase().includes(needle);
    });
    r = [...r].sort((a, b) => {
      if (sort.key == null) {
        if (a.eligible !== b.eligible) return a.eligible ? -1 : 1;
        return b.volume_24h - a.volume_24h;
      }
      const va = sortValue(a, sort.key);
      const vb = sortValue(b, sort.key);
      if (va == null && vb == null) return 0;
      if (va == null) return 1; // null selalu di bawah
      if (vb == null) return -1;
      const c = typeof va === "string" ? va.localeCompare(vb as string) : (va as number) - (vb as number);
      return c * sort.dir;
    });
    return r;
  }, [data, filter, q, sort]);

  if (!data) {
    return error ? (
      <p className="inline-error" role="alert">
        {error}
      </p>
    ) : (
      <p className="muted">Memuat radar…</p>
    );
  }

  const rg = regimeInfo(data.regime);
  const { done, total } = data.progress;
  const pct = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0;
  const btc = data.regime.btc_change_24h_pct;
  const eligibleCount = data.pairs.filter((p) => p.eligible).length;

  const toggleSort = (k: SortKey) =>
    setSort((s) => (s.key === k ? { key: k, dir: (s.dir * -1) as 1 | -1 } : { key: k, dir: k === "symbol" || k === "category" ? 1 : -1 }));

  const filters: [Filter, string][] = [
    ["all", "Semua"],
    ["majors", "Koin terkenal"],
    ["alts", "Alt"],
    ["eligible", "Lolos filter saja"],
  ];

  return (
    <div className="view">
      <header className="view-head">
        <h1>Radar pasar</h1>
        <p className="muted">Pemindaian semua pair IDR di latar belakang. Bot hanya mempertimbangkan yang lolos filter.</p>
      </header>

      <div className="radar-top">
        <section className={`card regime tone-${rg.tone}`} aria-label="Regime BTC">
          <div className="regime-title">
            <span className="dot" aria-hidden="true" />
            {rg.label}
          </div>
          <p className="reason">{data.regime.reason || "–"}</p>
          <p className="small muted">
            {data.regime.btc_symbol} 24j:{" "}
            <b className={`mono ${pnlClass(btc)}`}>{fmtPct(btc, 2, true)}</b>
            {data.regime.btc_below_ema50 ? " · di bawah EMA50" : " · di atas EMA50"}
          </p>
        </section>
        <section className="card sweep" aria-label="Sapuan radar">
          <div className="sweep-row">
            <span className="muted">Progres sapuan</span>
            <span className="mono">
              {done}/{total}
            </span>
          </div>
          <div className="progress" role="progressbar" aria-valuemin={0} aria-valuemax={total} aria-valuenow={done}>
            <div className={`progress-bar ${total > 0 && done < total ? "active" : ""}`} style={{ width: `${pct}%` }} />
          </div>
          <p className="small muted">
            Sapuan selesai: <b className="mono">{data.sweeps_completed}</b> · diperbarui{" "}
            {data.updated_at ? `${fmtRelative(data.updated_at)} (${fmtTime(data.updated_at)})` : "belum"}
          </p>
          <p className="small muted">
            {data.pairs.length} pair · <span className="pos">{eligibleCount} lolos</span>
          </p>
        </section>
      </div>

      {data.pairs.length === 0 ? (
        <div className="card empty">Radar sedang memindai pasar pertama kali… ±5 menit</div>
      ) : (
        <section className="card" aria-label="Daftar pair">
          <div className="toolbar">
            <div className="chips" role="group" aria-label="Filter">
              {filters.map(([k, label]) => (
                <button
                  key={k}
                  type="button"
                  className={`chip-btn ${filter === k ? "on" : ""}`}
                  aria-pressed={filter === k}
                  onClick={() => setFilter(k)}
                >
                  {label}
                </button>
              ))}
            </div>
            <input
              type="search"
              className="search"
              placeholder="Cari symbol…"
              aria-label="Cari symbol"
              value={q}
              onChange={(e) => setQ(e.target.value)}
            />
          </div>
          <div className="table-wrap tall">
            <table className="radar">
              <thead>
                <tr>
                  {COLS.map((c) => {
                    const active = sort.key === c.key;
                    return (
                      <th
                        key={c.key}
                        className={c.num ? "num" : ""}
                        aria-sort={active ? (sort.dir === 1 ? "ascending" : "descending") : "none"}
                      >
                        <button type="button" className="th-btn" onClick={() => toggleSort(c.key)}>
                          {c.label}
                          <span className="arrow" aria-hidden="true">
                            {active ? (sort.dir === 1 ? "▲" : "▼") : ""}
                          </span>
                        </button>
                      </th>
                    );
                  })}
                </tr>
              </thead>
              <tbody>
                {rows.map((p) => (
                  <tr key={p.symbol} className={p.eligible ? "" : "dimrow"}>
                    <td>
                      <strong>{p.symbol}</strong>
                    </td>
                    <td>
                      <span className={`pill ${p.category === "majors" ? "pill-info" : ""}`}>
                        {p.category === "majors" ? "terkenal" : "alt"}
                      </span>
                    </td>
                    <td className="num mono">{fmtPrice(p.last)}</td>
                    <td className={`num mono ${pnlClass(p.change_24h_pct)}`}>{fmtPct(p.change_24h_pct, 2, true)}</td>
                    <td className="num mono">{fmtCompactMoney(p.volume_24h, "IDR")}</td>
                    <td className="num mono">{fmtPct(p.atr_pct, 2)}</td>
                    <td className={`num mono ${pnlClass(p.rs_vs_btc)}`}>{fmtPct(p.rs_vs_btc, 2, true)}</td>
                    <td className="num mono">{fmtAgeDays(p.age_days)}</td>
                    <td className="num mono">{fmtNum(p.vol_surge, 1, 1)}×</td>
                    <td>
                      {p.eligible ? (
                        <span className="pill pill-ok">lolos</span>
                      ) : (
                        <span className="pill" title={p.reason}>
                          {p.reason || "ditolak"}
                        </span>
                      )}
                    </td>
                  </tr>
                ))}
                {rows.length === 0 && (
                  <tr>
                    <td colSpan={COLS.length} className="muted">
                      Tidak ada pair yang cocok.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </section>
      )}
    </div>
  );
}
