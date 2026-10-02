"use client";

import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "@/lib/api";
import type { PanicReport } from "@/lib/types";
import { fmtAmount } from "@/lib/format";

export function PanicModal({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [report, setReport] = useState<PanicReport | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !busy) onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [busy, onClose]);

  const submit = async () => {
    if (text !== "PANIC" || busy) return;
    setBusy(true);
    setError(null);
    try {
      const r = await api<PanicReport>("panic", { method: "POST", body: { confirm: "PANIC" } });
      setReport(r);
      onDone();
    } catch (e) {
      setError(e instanceof ApiError ? `${e.message} (HTTP ${e.status})` : String(e));
    } finally {
      setBusy(false);
    }
  };

  const leftover = report ? Object.entries(report.leftover ?? {}) : [];

  return (
    <div className="overlay" onMouseDown={(e) => e.target === e.currentTarget && !busy && onClose()}>
      <div className="modal" role="dialog" aria-modal="true" aria-labelledby="panic-title">
        <h2 id="panic-title" className="danger-text">
          PANIC – jual semua
        </h2>
        {!report ? (
          <>
            <p>
              Ini akan <strong>membatalkan semua order</strong> dan <strong>menjual semua posisi terbuka di harga market</strong>.
              Tindakan ini tidak bisa dibatalkan.
            </p>
            <label htmlFor="panic-input">
              Ketik <code>PANIC</code> untuk melanjutkan:
            </label>
            <input
              id="panic-input"
              ref={inputRef}
              value={text}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && submit()}
              autoComplete="off"
              autoCapitalize="off"
              spellCheck={false}
              disabled={busy}
              placeholder="PANIC"
            />
            {error && <p className="inline-error" role="alert">{error}</p>}
            <div className="modal-actions">
              <button onClick={onClose} disabled={busy}>
                Batal
              </button>
              <button className="btn-danger" onClick={submit} disabled={text !== "PANIC" || busy}>
                {busy ? "Memproses…" : "Jual semua sekarang"}
              </button>
            </div>
          </>
        ) : (
          <>
            <p>
              <strong>Laporan PANIC</strong>
            </p>
            <dl className="report">
              <dt>Order dibatalkan</dt>
              <dd>{report.cancelled}</dd>
              <dt>Terjual ({report.sold.length})</dt>
              <dd>{report.sold.length ? report.sold.join(", ") : "–"}</dd>
              <dt>Error ({report.errors.length})</dt>
              <dd className={report.errors.length ? "neg" : ""}>
                {report.errors.length ? (
                  <ul>
                    {report.errors.map((e, i) => (
                      <li key={i}>{e}</li>
                    ))}
                  </ul>
                ) : (
                  "–"
                )}
              </dd>
              <dt>Sisa aset (leftover)</dt>
              <dd>
                {leftover.length ? (
                  <ul>
                    {leftover.map(([a, v]) => (
                      <li key={a}>
                        {a}: {fmtAmount(v)}
                      </li>
                    ))}
                  </ul>
                ) : (
                  "–"
                )}
              </dd>
            </dl>
            <div className="modal-actions">
              <button onClick={onClose}>Tutup</button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
