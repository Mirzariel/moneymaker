"use client";

import { useEffect, useState } from "react";
import { api, ApiError } from "@/lib/api";
import type { ModelInfo, Preflight, PreflightMarket, PreflightSizing } from "@/lib/types";
import { fmtCompactMoney, fmtMoney, fmtNum } from "@/lib/format";
import { DataTable, type Column } from "@/components/DataTable";

type Row<T> = T & { id: string };

export function PreflightView({
  live,
  onBack,
  onStarted,
}: {
  live: boolean;
  onBack: () => void;
  onStarted: () => void;
}) {
  const [data, setData] = useState<Preflight | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);

  const [model, setModel] = useState<ModelInfo | null>(null);
  useEffect(() => {
    api<ModelInfo>("model")
      .then(setModel)
      .catch(() => setModel(null));
  }, []);
  const noEdge = model != null && !model.sleeves.majors.valid && !model.sleeves.alts.valid;

  const run = async () => {
    setLoading(true);
    setError(null);
    setData(null);
    try {
      setData(await api<Preflight>("preflight", { timeoutMs: 180_000 }));
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  };

  const start = async () => {
    if (starting) return;
    if (live && !window.confirm("PERINGATAN: bot dalam mode LIVE dan akan membuka posisi dengan UANG ASLI.\n\nMulai trading sekarang?")) return;
    setStarting(true);
    setStartError(null);
    try {
      await api("resume", { method: "POST", body: {} });
      onStarted();
    } catch (e) {
      setStartError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setStarting(false);
    }
  };

  const q = data?.quote ?? "";
  const marketCols: Column<Row<PreflightMarket>>[] = [
    { header: "Symbol", render: (m) => <strong>{m.symbol}</strong> },
    { header: "Volume 24j", className: "num", render: (m) => fmtCompactMoney(m.quote_volume, q) },
    { header: "Spread %", className: "num", render: (m) => fmtNum(m.spread_pct, 3, 2) },
    { header: "Native", render: (m) => (m.native ? "ya" : "tidak") },
    { header: "Stop-limit", render: (m) => (m.stop_limit ? "ya" : "tidak") },
    {
      header: "Bisa ditrade",
      render: (m) => (
        <span className={m.tradeable ? "pos strong" : "neg strong"}>
          {m.tradeable ? "✓" : "✗"}
          <span className="sr-only">{m.tradeable ? " ya" : " tidak"}</span>
        </span>
      ),
    },
  ];
  const sizingCols: Column<Row<PreflightSizing>>[] = [
    { header: "Symbol", render: (s) => <strong>{s.symbol}</strong> },
    {
      header: "Hasil",
      render: (s) =>
        s.approved ? (
          <span className="pos strong">BUY {fmtMoney(s.cost, q)}</span>
        ) : (
          <span className="neg strong">Ditolak: {s.rule}</span>
        ),
    },
    {
      header: "Keterangan",
      className: "wrap",
      render: (s) => (
        <>
          {!s.approved && s.detail && <div className="neg">{s.detail}</div>}
          {s.notes?.length > 0 && <div className="muted">{s.notes.join("; ")}</div>}
          {s.approved && !s.notes?.length && "–"}
        </>
      ),
    },
  ];

  const top = data?.top.map((m) => ({ ...m, id: m.symbol })) ?? [];
  const sizing = data?.sizing.map((s) => ({ ...s, id: s.symbol })) ?? [];

  return (
    <>
      <header className="top">
        <div className="top-row">
          <h1>Cek kesiapan</h1>
          <div className="top-actions">
            <button type="button" className="linklike" onClick={onBack}>
              Kembali ke dashboard
            </button>
          </div>
        </div>
        <p className="muted">
          Memeriksa market Tokocrypto dan simulasi ukuran order sebelum bot mulai trading. Belum ada order yang dikirim.
        </p>
      </header>

      <section className="card">
        <div className="btn-row">
          <button type="button" className="btn-primary" onClick={run} disabled={loading}>
            {data || error ? "Cek ulang" : "Jalankan cek"}
          </button>
          {loading && (
            <span className="busy" role="status">
              <span className="spinner" aria-hidden="true" /> Mengecek market Tokocrypto… (bisa sampai 2 menit)
            </span>
          )}
        </div>
        {error && (
          <p className="inline-error" role="alert">
            {error}
          </p>
        )}
      </section>

      {data && !data.ok && (
        <p className="inline-error" role="alert">
          Cek gagal: {data.error ?? "alasan tidak diketahui"}
        </p>
      )}

      {data && data.warnings.length > 0 && (
        <div className="warns">
          {data.warnings.map((w, i) => (
            <div key={i} className="note note-warn" role="alert">
              {w}
            </div>
          ))}
        </div>
      )}

      {data && (
        <section className="card">
          <h2>Ringkasan</h2>
          <dl className="summary">
            <dt>Mata uang quote</dt>
            <dd>{data.quote}</dd>
            <dt>Equity</dt>
            <dd>
              {fmtMoney(data.equity, data.quote)}{" "}
              <span className="muted">({data.equity_source === "balance" ? "dari saldo akun" : "dari konfigurasi"})</span>
            </dd>
            <dt>Fee beli / jual</dt>
            <dd>
              {fmtNum(data.fees.buy_pct, 3)}% / {fmtNum(data.fees.sell_pct, 3)}%
            </dd>
            <dt>api.binance.com terjangkau</dt>
            <dd className={data.binance_reachable ? "pos strong" : "neg strong"}>{data.binance_reachable ? "ya" : "tidak"}</dd>
            <dt>Market bisa ditrade</dt>
            <dd>{data.markets_active}</dd>
            {data.candidates_checked != null && (
              <>
                <dt>Dicek</dt>
                <dd>Dicek: {data.candidates_checked} koin</dd>
              </>
            )}
          </dl>
        </section>
      )}

      {data && (
        <section className="card">
          <h2>Market teratas</h2>
          <DataTable columns={marketCols} rows={top} empty="Tidak ada market." />
        </section>
      )}

      {data && (
        <section className="card">
          <h2>Simulasi ukuran order</h2>
          <DataTable columns={sizingCols} rows={sizing} empty="Tidak ada simulasi." />
        </section>
      )}

      <section className="card start-card">
        <button type="button" className="btn-primary btn-big" onClick={start} disabled={starting || !data?.ok}>
          {starting ? "Memulai…" : "Mulai trading"}
        </button>
        {noEdge && (
          <p className="note note-warn">
            Model belum punya keunggulan tervalidasi — setelah mulai, bot akan diam sampai model lolos uji (lihat tab
            Model).
          </p>
        )}
        {!data?.ok && <p className="help">Jalankan cek kesiapan dulu sebelum mulai trading.</p>}
        {startError && (
          <p className="inline-error" role="alert">
            {startError}
          </p>
        )}
        <button type="button" className="linklike" onClick={onBack}>
          Kembali ke dashboard
        </button>
      </section>
    </>
  );
}
