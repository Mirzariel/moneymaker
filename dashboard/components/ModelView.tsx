"use client";

import { useEffect, useState } from "react";
import { api, ApiError } from "@/lib/api";
import type { LiveStats, Metrics, ModelInfo, SleeveModel } from "@/lib/types";
import { SLEEVE_TITLE, fmtNum, fmtPF, fmtPct, fmtRelative, fmtTime, pnlClass, sleeveStatus } from "@/lib/format";
import { usePoll } from "@/lib/usePoll";

function MetricsTable({ train, test }: { train: Metrics | null; test: Metrics | null }) {
  if (!train && !test) return <p className="muted pad">Belum ada hasil uji.</p>;
  const rows: { label: string; get: (m: Metrics) => string; cls?: (m: Metrics) => string }[] = [
    { label: "Jumlah trade", get: (m) => String(m.trades) },
    { label: "Win rate", get: (m) => fmtPct(m.win_rate, 1) },
    { label: "Profit factor", get: (m) => fmtPF(m.profit_factor) },
    { label: "Return total", get: (m) => fmtPct(m.total_return_pct, 2, true), cls: (m) => pnlClass(m.total_return_pct) },
    {
      label: "Max drawdown",
      get: (m) => (m.max_drawdown_pct > 0.005 ? fmtPct(-m.max_drawdown_pct, 2) : "0%"),
      cls: (m) => (m.max_drawdown_pct > 0.005 ? "neg" : ""),
    },
    { label: "Rata-rata / trade", get: (m) => fmtPct(m.avg_trade_pct, 3, true), cls: (m) => pnlClass(m.avg_trade_pct) },
  ];
  return (
    <div className="table-wrap">
      <table className="metrics">
        <thead>
          <tr>
            <th>Metrik</th>
            <th className="num">Latih (70%)</th>
            <th className="num hl">Uji (30%)</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.label}>
              <td>{r.label}</td>
              <td className={`num ${train && r.cls ? r.cls(train) : ""}`}>{train ? r.get(train) : "–"}</td>
              <td className={`num hl strong ${test && r.cls ? r.cls(test) : ""}`}>{test ? r.get(test) : "–"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Live({ s }: { s: LiveStats }) {
  return (
    <dl className="live-grid">
      <div>
        <dt>Trade live</dt>
        <dd>{s.trades}</dd>
      </div>
      <div>
        <dt>Profit factor</dt>
        <dd>{s.trades === 0 ? "–" : fmtPF(s.profit_factor)}</dd>
      </div>
      <div>
        <dt>Win rate</dt>
        <dd>{s.win_rate == null ? "–" : fmtPct(s.win_rate, 1)}</dd>
      </div>
      <div>
        <dt>Rugi beruntun</dt>
        <dd className={s.consecutive_losses >= 3 ? "neg" : ""}>{s.consecutive_losses}</dd>
      </div>
      <div>
        <dt>Status</dt>
        <dd>{s.status}</dd>
      </div>
    </dl>
  );
}

function SleeveCard({ id, m, live, nowMs }: { id: "majors" | "alts"; m: SleeveModel; live: LiveStats; nowMs: number }) {
  const st = sleeveStatus(m.status);
  return (
    <section className="card sleeve" aria-label={SLEEVE_TITLE[id]}>
      <div className="sleeve-head">
        <h2>{SLEEVE_TITLE[id]}</h2>
        {m.timeframe && <span className="tf-chip">{m.timeframe}</span>}
      </div>
      <div className={`status-big tone-${st.tone}`}>
        <span className="dot" aria-hidden="true" />
        {st.label}
      </div>
      <p className="reason">{m.reason || "–"}</p>

      {m.params && Object.keys(m.params).length > 0 && (
        <div className="chips" aria-label="Parameter">
          {Object.entries(m.params).map(([k, v]) => (
            <span key={k} className="chip">
              {k}: <b>{fmtNum(v, 4)}</b>
            </span>
          ))}
        </div>
      )}
      <p className="small muted">
        Koin dipakai: {m.symbols.length ? m.symbols.map((s) => s.replace("/IDR", "")).join(", ") : "–"}
      </p>
      <p className="small muted">
        Dilatih: {m.trained_at ? `${fmtRelative(m.trained_at, nowMs)} (${fmtTime(m.trained_at)})` : "belum pernah"}
      </p>

      <h3>Latih (70%) vs Uji (30%)</h3>
      <MetricsTable train={m.train} test={m.test} />
      <p className="small muted explain">
        Parameter dipilih dari 70% data awal, lalu diuji di 30% data terakhir yang tidak dipakai saat memilih.
      </p>

      {id === "majors" && m.per_timeframe.length > 0 && (
        <>
          <h3>Per timeframe</h3>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>TF</th>
                  <th className="num">PF latih</th>
                  <th className="num">PF uji</th>
                  <th className="num">Return uji</th>
                  <th className="num">Trade uji</th>
                  <th>Lolos</th>
                </tr>
              </thead>
              <tbody>
                {m.per_timeframe.map((t) => (
                  <tr key={t.timeframe}>
                    <td>
                      <strong>{t.timeframe}</strong>
                    </td>
                    <td className="num">{fmtPF(t.train.profit_factor)}</td>
                    <td className="num strong">{fmtPF(t.test.profit_factor)}</td>
                    <td className={`num ${pnlClass(t.test.total_return_pct)}`}>{fmtPct(t.test.total_return_pct, 2, true)}</td>
                    <td className="num">{t.test.trades}</td>
                    <td className={t.passed ? "pos strong" : "neg strong"}>
                      {t.passed ? "✓" : "✗"}
                      <span className="sr-only">{t.passed ? " lolos" : " tidak lolos"}</span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}

      <h3>Performa live</h3>
      <Live s={live} />
    </section>
  );
}

export function ModelView() {
  const [nowMs, setNowMs] = useState(() => Date.now());
  const [trainError, setTrainError] = useState<string | null>(null);
  const [posting, setPosting] = useState(false);
  const { data, error, setData } = usePoll<ModelInfo>("model", (d) => (d?.job.state === "running" ? 5000 : 30_000));

  useEffect(() => {
    const t = setInterval(() => setNowMs(Date.now()), 15_000);
    return () => clearInterval(t);
  }, []);

  const train = async () => {
    setPosting(true);
    setTrainError(null);
    try {
      setData(await api<ModelInfo>("model/train", { method: "POST", body: {} }));
    } catch (e) {
      setTrainError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setPosting(false);
    }
  };

  if (!data) {
    return error ? (
      <p className="inline-error" role="alert">
        {error}
      </p>
    ) : (
      <p className="muted">Memuat model…</p>
    );
  }

  const job = data.job;
  const running = job.state === "running";
  const pct = job.progress.total > 0 ? Math.min(100, Math.round((job.progress.done / job.progress.total) * 100)) : 0;
  const jobLabel = { idle: "Siap", running: "Sedang melatih", done: "Selesai", error: "Gagal" }[job.state];

  return (
    <div className="view">
      <header className="view-head">
        <h1>Model</h1>
        <p className="muted">
          Bot belajar parameter terbaik dari data historis, lalu hanya trading jika terbukti punya keunggulan.
        </p>
      </header>

      {!data.learning_enabled && <div className="note note-warn">Pembelajaran model dinonaktifkan di konfigurasi.</div>}

      <section className="card" aria-label="Pelatihan">
        <div className="job-head">
          <h2>Pelatihan model</h2>
          <span className={`pill ${job.state === "error" ? "pill-bad" : job.state === "done" ? "pill-ok" : running ? "pill-warn" : ""}`}>
            {jobLabel}
          </span>
          <button
            type="button"
            className="btn-primary job-btn"
            onClick={train}
            disabled={running || posting}
          >
            {running || posting ? "Melatih…" : "Latih ulang sekarang"}
          </button>
        </div>
        {(running || job.state === "done") && (
          <div className="progress-block">
            <div
              className="progress"
              role="progressbar"
              aria-valuemin={0}
              aria-valuemax={job.progress.total}
              aria-valuenow={job.progress.done}
            >
              <div className={`progress-bar ${running ? "active" : ""}`} style={{ width: `${pct}%` }} />
            </div>
            <div className="progress-meta">
              <span>{job.progress.label || "–"}</span>
              <span className="mono">
                {job.progress.done}/{job.progress.total}
              </span>
            </div>
          </div>
        )}
        {job.state === "error" && job.error && (
          <p className="inline-error" role="alert">
            {job.error}
          </p>
        )}
        {trainError && (
          <p className="inline-error" role="alert">
            {trainError}
          </p>
        )}
        <p className="small muted">
          Latihan otomatis berikutnya:{" "}
          {data.next_training_at ? `${fmtRelative(data.next_training_at, nowMs)} (${fmtTime(data.next_training_at)})` : "–"}
          {job.finished_at ? ` · terakhir selesai ${fmtRelative(job.finished_at, nowMs)}` : ""}
        </p>
      </section>

      <div className="sleeves">
        <SleeveCard id="majors" m={data.sleeves.majors} live={data.live.majors} nowMs={nowMs} />
        <SleeveCard id="alts" m={data.sleeves.alts} live={data.live.alts} nowMs={nowMs} />
      </div>

      <div className="note note-info honest">
        Lolos uji ≠ pasti untung. Bot hanya trade bila ada bukti di data terbaru, dan berhenti sendiri bila performa live
        memburuk.
      </div>
    </div>
  );
}
