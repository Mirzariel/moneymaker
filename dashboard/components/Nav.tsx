"use client";

export type View = "dashboard" | "radar" | "model" | "preflight" | "settings";

const TABS: [View, string][] = [
  ["dashboard", "Dashboard"],
  ["radar", "Radar pasar"],
  ["model", "Model"],
  ["preflight", "Cek kesiapan"],
  ["settings", "Pengaturan"],
];

export function Nav({ view, onChange }: { view: View; onChange: (v: View) => void }) {
  return (
    <nav className="nav" aria-label="Navigasi utama">
      <div className="nav-inner">
        <span className="brand">
          <span className="brand-mark" aria-hidden="true" />
          Moneymaker
        </span>
        <div className="tabs" role="tablist">
          {TABS.map(([k, label]) => (
            <button
              key={k}
              type="button"
              role="tab"
              aria-selected={view === k}
              className={`tab ${view === k ? "on" : ""}`}
              onClick={() => onChange(k)}
            >
              {label}
            </button>
          ))}
        </div>
      </div>
    </nav>
  );
}
