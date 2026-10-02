"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "@/lib/api";
import type { AuditEntry, EquityPoint, Position, RiskEvent, Signal, Status } from "@/lib/types";
import { fmtAge, fmtAmount, fmtDetail, fmtMoney, fmtPrice, fmtTime, pnlClass } from "@/lib/format";
import { DataTable, type Column } from "@/components/DataTable";
import { EquityChart } from "@/components/EquityChart";
import { PanicModal } from "@/components/PanicModal";

const POLL_MS = 10_000;
const STALE_CYCLE_SEC = 180;

type Banner = { kind: "unreachable" | "other"; text: string } | null;

function errMessage(e: unknown): string {
  return e instanceof ApiError ? e.message : String(e);
}

export function Dashboard({
  onOpenSettings,
  onOpenPreflight,
  onNotRunning,
}: {
  onOpenSettings: () => void;
  onOpenPreflight: () => void;
  onNotRunning: () => void;
}) {
  const [status, setStatus] = useState<Status | null>(null);
  const [statusAt, setStatusAt] = useState(0); // waktu lokal (ms) saat status diambil
  const [equity, setEquity] = useState<EquityPoint[] | null>(null);
  const [open, setOpen] = useState<Position[] | null>(null);
  const [closed, setClosed] = useState<Position[] | null>(null);
  const [signals, setSignals] = useState<Signal[] | null>(null);
  const [risk, setRisk] = useState<RiskEvent[] | null>(null);
  const [audit, setAudit] = useState<AuditEntry[] | null>(null);
  const [config, setConfig] = useState<unknown>(undefined);
  const [banner, setBanner] = useState<Banner>(null);
  const [now, setNow] = useState(() => Date.now());

  const [busy, setBusy] = useState<null | "pause" | "resume">(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [showPanic, setShowPanic] = useState(false);
  const inFlight = useRef(false);

  const refresh = useCallback(async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    try {
      const jobs = await Promise.allSettled([
        api<Status>("status"),
        api<EquityPoint[]>("equity?hours=168"),
        api<Position[]>("positions?status=open"),
        api<Position[]>("positions?status=closed&limit=20"),
        api<Signal[]>("signals?limit=20"),
        api<RiskEvent[]>("risk-events?limit=20"),
        api<AuditEntry[]>("audit?limit=20"),
        api<unknown>("config"),
      ]);
      const [st, eq, op, cl, sg, rk, au, cf] = jobs;

      if (st.status === "fulfilled") {
        setStatus(st.value);
        setStatusAt(Date.now());
      }
      if (eq.status === "fulfilled") setEquity(Array.isArray(eq.value) ? eq.value : []);
      if (op.status === "fulfilled") setOpen(Array.isArray(op.value) ? op.value : []);
      if (cl.status === "fulfilled") setClosed(Array.isArray(cl.value) ? cl.value : []);
      if (sg.status === "fulfilled") setSignals(Array.isArray(sg.value) ? sg.value : []);
      if (rk.status === "fulfilled") setRisk(Array.isArray(rk.value) ? rk.value : []);
      if (au.status === "fulfilled") setAudit(Array.isArray(au.value) ? au.value : []);
      if (cf.status === "fulfilled") setConfig(cf.value);

      const failed = jobs.filter((j): j is PromiseRejectedResult => j.status === "rejected");
      if (st.status === "rejected" && st.reason instanceof ApiError && st.reason.status === 409) {
        // bot belum jalan (mode setup) -> minta App memeriksa ulang /api/setup
        onNotRunning();
      }
      if (failed.some((j) => j.reason instanceof ApiError && j.reason.status === 0)) {
        setBanner({ kind: "unreachable", text: "Bot tidak terjangkau — pastikan jendela terminal bot masih terbuka." });
      } else if (failed.length) {
        setBanner({ kind: "other", text: `Gagal memuat sebagian data: ${errMessage(failed[0].reason)}` });
      } else {
        setBanner(null);
      }
    } finally {
      inFlight.current = false;
    }
  }, [onNotRunning]);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, POLL_MS);
    const tick = setInterval(() => setNow(Date.now()), 1000);
    return () => {
      clearInterval(t);
      clearInterval(tick);
    };
  }, [refresh]);

  const isLive = status?.mode === "live";
  const quote = status?.quote ?? "";

  const act = async (kind: "pause" | "resume") => {
    if (busy) return;
    if (kind === "resume") {
      const msg = isLive
        ? "PERINGATAN: bot dalam mode LIVE. Melanjutkan (Resume) berarti bot akan kembali membuka posisi dengan UANG ASLI.\n\nYakin ingin melanjutkan?"
        : "Lanjutkan (Resume) bot? Bot akan kembali membuka posisi baru.";
      if (!window.confirm(msg)) return;
    }
    setBusy(kind);
    setActionError(null);
    try {
      const s =
        kind === "pause"
          ? await api<Status>("pause", { method: "POST", body: { reason: "manual (dashboard)" } })
          : await api<Status>("resume", { method: "POST", body: {} });
      if (s && typeof s === "object" && "status" in s) {
        setStatus(s);
        setStatusAt(Date.now());
      }
      await refresh();
    } catch (e) {
      setActionError(`${kind === "pause" ? "Pause" : "Resume"} gagal: ${errMessage(e)}`);
    } finally {
      setBusy(null);
    }
  };

  // umur siklus terakhir, dikoreksi dengan waktu server agar tidak terpengaruh selisih jam
  let cycleAge: number | null = null;
  if (status && status.last_cycle_at != null) {
    cycleAge = status.server_time - status.last_cycle_at + (now - statusAt) / 1000;
  }
  const cycleWarn = status != null && (cycleAge == null || cycleAge > STALE_CYCLE_SEC);

  const posOpenCols: Column<Position>[] = [
    { header: "Symbol", render: (p) => <strong>{p.symbol}</strong> },
    { header: "Jumlah", className: "num", render: (p) => fmtAmount(p.amount) },
    { header: "Entry", className: "num", render: (p) => fmtPrice(p.entry_price) },
    { header: "Harga kini", className: "num", render: (p) => fmtPrice(p.last_price) },
    {
      header: "Unrealized PnL",
      className: "num",
      render: (p) => <span className={pnlClass(p.unrealized_pnl)}>{fmtMoney(p.unrealized_pnl, quote, true)}</span>,
    },
    { header: "Stop", className: "num", render: (p) => fmtPrice(p.stop_price) },
    { header: "TP", className: "num", render: (p) => fmtPrice(p.take_profit) },
    { header: "Dibuka", className: "nowrap", render: (p) => fmtTime(p.opened_at) },
  ];

  const posClosedCols: Column<Position>[] = [
    { header: "Symbol", render: (p) => <strong>{p.symbol}</strong> },
    { header: "Entry", className: "num", render: (p) => fmtPrice(p.entry_price) },
    { header: "Exit", className: "num", render: (p) => fmtPrice(p.exit_price) },
    {
      header: "PnL",
      className: "num",
      render: (p) => <span className={pnlClass(p.pnl)}>{fmtMoney(p.pnl, quote, true)}</span>,
    },
    { header: "Fees", className: "num", render: (p) => fmtMoney(p.fees, quote) },
    { header: "Alasan", render: (p) => p.exit_reason ?? "–" },
    { header: "Dibuka", className: "nowrap", render: (p) => fmtTime(p.opened_at) },
    { header: "Ditutup", className: "nowrap", render: (p) => fmtTime(p.closed_at) },
  ];

  const signalCols: Column<Signal>[] = [
    { header: "Waktu", className: "nowrap", render: (s) => fmtTime(s.ts) },
    { header: "Symbol", render: (s) => <strong>{s.symbol}</strong> },
    { header: "Side", render: (s) => s.side },
    { header: "Entry", className: "num", render: (s) => fmtPrice(s.entry) },
    { header: "Stop", className: "num", render: (s) => fmtPrice(s.stop) },
    { header: "TP", className: "num", render: (s) => fmtPrice(s.take_profit) },
    {
      header: "Keputusan",
      render: (s) => (
        <span className={`pill ${s.decision === "approved" ? "pill-ok" : s.decision === "rejected" ? "pill-bad" : ""}`}>
          {s.decision}
        </span>
      ),
    },
    {
      header: "Alasan",
      className: "wrap",
      render: (s) => [s.reason, fmtDetail(s.decision_detail)].filter(Boolean).join(" — ") || "–",
    },
  ];

  const riskCols: Column<RiskEvent>[] = [
    { header: "Waktu", className: "nowrap", render: (r) => fmtTime(r.ts) },
    { header: "Symbol", render: (r) => r.symbol ?? "–" },
    { header: "Rule", render: (r) => r.rule },
    { header: "Aksi", render: (r) => r.action ?? "–" },
    { header: "Detail", className: "wrap", render: (r) => fmtDetail(r.detail) || "–" },
  ];

  const auditCols: Column<AuditEntry>[] = [
    { header: "Waktu", className: "nowrap", render: (a) => fmtTime(a.ts) },
    { header: "Aktor", render: (a) => a.actor },
    { header: "Aksi", render: (a) => a.action },
    { header: "Detail", className: "wrap", render: (a) => fmtDetail(a.detail) || "–" },
  ];

  return (
    <>
      <header className="top">
        <div className="top-row">
          <h1>Moneymaker</h1>
          {status ? (
            <>
              <span className={`badge ${status.status === "RUNNING" ? "badge-ok" : "badge-warn"}`}>{status.status}</span>
              <span className={`badge ${isLive ? "badge-bad" : "badge-neutral"}`}>
                {isLive ? "LIVE – uang asli" : "paper"}
              </span>
            </>
          ) : (
            <span className="badge badge-neutral">{banner ? "tidak terhubung" : "memuat…"}</span>
          )}
          <div className="top-actions">
            <button onClick={onOpenPreflight}>Cek kesiapan</button>
            <button onClick={onOpenSettings}>Pengaturan</button>
          </div>
        </div>
        {status && (
          <div className="meta">
            {status.status_reason && <span>{status.status_reason}</span>}
            <span className={cycleWarn ? "neg strong" : "muted"}>
              {cycleAge == null ? "siklus terakhir: belum pernah" : `siklus terakhir ${fmtAge(cycleAge)} lalu`}
              {cycleWarn && cycleAge != null ? " (terlambat!)" : ""}
            </span>
            <span className="muted">
              {status.timeframe} · {status.universe.length} pair
            </span>
          </div>
        )}
        {status?.last_error && (
          <p className="inline-error" role="alert">
            Error terakhir: {status.last_error}
          </p>
        )}
      </header>

      {banner && (
        <div className={`banner banner-${banner.kind}`} role="alert">
          {banner.text}
        </div>
      )}

      <section className="kpis" aria-label="Ringkasan">
        <Kpi label="Equity" value={fmtMoney(status?.equity, quote)} />
        <Kpi label="Saldo bebas" value={fmtMoney(status?.quote_free, quote)} />
        <Kpi label="Exposure" value={fmtMoney(status?.exposure, quote)} />
        <Kpi label="PnL hari ini" value={fmtMoney(status?.day_pnl, quote, true)} cls={pnlClass(status?.day_pnl)} />
        <Kpi label="Posisi terbuka" value={status ? String(status.open_positions) : "–"} />
      </section>

      <section className="card" aria-label="Kontrol">
        <h2>Kontrol</h2>
        <div className="controls">
          <button onClick={() => act("pause")} disabled={!status || busy !== null || status.status === "PAUSED"}>
            {busy === "pause" ? "Memproses…" : "Pause"}
          </button>
          <button onClick={() => act("resume")} disabled={!status || busy !== null || status.status === "RUNNING"}>
            {busy === "resume" ? "Memproses…" : "Resume"}
          </button>
          <button className="btn-danger" onClick={() => setShowPanic(true)} disabled={!status || busy !== null}>
            PANIC – jual semua
          </button>
        </div>
        {actionError && (
          <p className="inline-error" role="alert">
            {actionError}
          </p>
        )}
      </section>

      <section className="card">
        <h2>Equity (168 jam terakhir)</h2>
        <EquityChart points={equity} quote={quote} />
      </section>

      <section className="card">
        <h2>Posisi terbuka</h2>
        <DataTable columns={posOpenCols} rows={open} empty="Tidak ada posisi terbuka." />
      </section>

      <section className="card">
        <h2>Trade tertutup terbaru</h2>
        <DataTable columns={posClosedCols} rows={closed} empty="Belum ada trade tertutup." />
      </section>

      <section className="card">
        <h2>Sinyal terbaru</h2>
        <DataTable columns={signalCols} rows={signals} empty="Belum ada sinyal." />
      </section>

      <section className="card">
        <h2>Risk events</h2>
        <DataTable columns={riskCols} rows={risk} empty="Belum ada risk event." />
      </section>

      <section className="card">
        <h2>Audit log</h2>
        <DataTable columns={auditCols} rows={audit} empty="Belum ada audit log." />
      </section>

      <details className="card config">
        <summary>Konfigurasi</summary>
        <pre>{config === undefined ? "Memuat…" : JSON.stringify(config, null, 2)}</pre>
      </details>

      <footer className="muted foot">
        Diperbarui otomatis tiap 10 detik{statusAt ? ` · terakhir ${fmtTime(statusAt / 1000)}` : ""}
      </footer>

      {showPanic && <PanicModal onClose={() => setShowPanic(false)} onDone={refresh} />}
    </>
  );
}

function Kpi({ label, value, cls = "" }: { label: string; value: string; cls?: string }) {
  return (
    <div className="kpi">
      <div className="kpi-label">{label}</div>
      <div className={`kpi-value ${cls}`}>{value}</div>
    </div>
  );
}
