"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, onUnauthorized } from "@/lib/api";
import type { SetupInfo } from "@/lib/types";
import { Dashboard } from "@/components/Dashboard";
import { SetupForm } from "@/components/SetupForm";
import { PreflightView } from "@/components/PreflightView";

type Session = "loading" | "yes" | "no" | "invalid";
type View = "dashboard" | "preflight";

function SessionCard({ title }: { title: string }) {
  return (
    <main className="center-screen">
      <div className="card center-card">
        <h1>{title}</h1>
        <p>
          Buka dashboard lewat link yang muncul di jendela terminal (atau jalankan start lagi). Link berisi kode akses
          pribadi – jangan dibagikan.
        </p>
      </div>
    </main>
  );
}

export function App() {
  const [session, setSession] = useState<Session>("loading");
  const [setup, setSetup] = useState<SetupInfo | null>(null);
  const [setupError, setSetupError] = useState<string | null>(null);
  const [view, setView] = useState<View>("dashboard");
  const [showSettings, setShowSettings] = useState(false);
  const pendingStart = useRef(false);

  useEffect(() => onUnauthorized(() => setSession((s) => (s === "yes" ? "invalid" : s))), []);

  useEffect(() => {
    let alive = true;
    api<{ authenticated: boolean }>("session")
      .then((r) => alive && setSession(r.authenticated ? "yes" : "no"))
      .catch(() => alive && setSession("no"));
    return () => {
      alive = false;
    };
  }, []);

  const loadSetup = useCallback(async () => {
    try {
      const s = await api<SetupInfo>("setup");
      setSetup(s);
      setSetupError(null);
    } catch (e) {
      if (!(e instanceof ApiError && e.status === 401)) {
        setSetupError(e instanceof ApiError ? e.message : String(e));
      }
    }
  }, []);

  // poll /api/setup: 3 dtk saat "starting", selain itu 10 dtk
  const state = setup?.state;
  useEffect(() => {
    if (session !== "yes") return;
    loadSetup();
    const t = setInterval(loadSetup, state === "starting" ? 3000 : 10_000);
    return () => clearInterval(t);
  }, [session, state, loadSetup]);

  // setelah user menyimpan: begitu bot "running" -> otomatis ke Cek kesiapan
  useEffect(() => {
    if (!pendingStart.current || !setup) return;
    if (setup.state === "running") {
      pendingStart.current = false;
      setShowSettings(false);
      setView("preflight");
    } else if (setup.state === "error" || setup.state === "setup") {
      pendingStart.current = false;
      setShowSettings(false);
    }
  }, [setup]);

  const onSaved = useCallback((s: SetupInfo) => {
    pendingStart.current = true;
    setSetup(s);
  }, []);

  if (session === "loading") {
    return (
      <main className="center-screen">
        <p className="muted">Memuat…</p>
      </main>
    );
  }
  if (session === "no") return <SessionCard title="Dashboard Moneymaker" />;
  if (session === "invalid") return <SessionCard title="Sesi tidak valid" />;

  if (!setup) {
    return (
      <main className="center-screen">
        {setupError ? (
          <p className="inline-error" role="alert">
            {setupError}
          </p>
        ) : (
          <p className="muted">Memuat…</p>
        )}
      </main>
    );
  }

  const running = setup.state === "running" && setup.ready;

  if (!running) {
    return (
      <main className="container narrow">
        <header className="top">
          <h1>Moneymaker</h1>
          <p className="muted">Selamat datang. Isi pengaturan di bawah, lalu bot siap dijalankan.</p>
        </header>
        <SetupForm setup={setup} onSaved={onSaved} wizard />
      </main>
    );
  }

  if (view === "preflight") {
    return (
      <main className="container narrow">
        <PreflightView
          live={setup.live_trading}
          onBack={() => setView("dashboard")}
          onStarted={() => setView("dashboard")}
        />
      </main>
    );
  }

  return (
    <main className="container">
      <Dashboard
        onOpenSettings={() => setShowSettings(true)}
        onOpenPreflight={() => setView("preflight")}
        onNotRunning={loadSetup}
      />
      {showSettings && (
        <div className="overlay" onMouseDown={(e) => e.target === e.currentTarget && setShowSettings(false)}>
          <div className="modal modal-wide" role="dialog" aria-modal="true" aria-labelledby="settings-title">
            <div className="modal-head">
              <h2 id="settings-title">Pengaturan</h2>
              <button onClick={() => setShowSettings(false)} aria-label="Tutup">
                Tutup
              </button>
            </div>
            <SetupForm setup={setup} onSaved={onSaved} />
          </div>
        </div>
      )}
    </main>
  );
}
