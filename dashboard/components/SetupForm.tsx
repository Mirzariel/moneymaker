"use client";

import { useState } from "react";
import { api, ApiError } from "@/lib/api";
import type {
  DetectChatResult,
  SetupInfo,
  SetupUpdate,
  TestExchangeResult,
  TestTelegramResult,
} from "@/lib/types";
import { fmtNum } from "@/lib/format";

const FIELD_LABELS: Record<string, string> = {
  toko_api_key: "Tokocrypto API Key",
  toko_api_secret: "Tokocrypto Secret Key",
  telegram_bot_token: "Telegram bot token",
  telegram_chat_id: "Telegram Chat ID",
  healthcheck_url: "Healthchecks.io ping URL",
};

type Secret = "toko_api_key" | "toko_api_secret" | "telegram_bot_token";
type Plain = "telegram_chat_id" | "healthcheck_url";
type Clearable = Secret | Plain;

const errText = (e: unknown) => (e instanceof ApiError ? e.message : String(e));
const cleanHint = (h: string | null | undefined) => (h ? h.replace(/^[….*•\s]+/, "") : "");

function ValueField({
  id,
  label,
  value,
  onChange,
  saved,
  savedPlaceholder,
  cleared,
  onClear,
  onUndoClear,
  secret = false,
  placeholder,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (v: string) => void;
  saved: boolean;
  savedPlaceholder?: string;
  cleared: boolean;
  onClear: () => void;
  onUndoClear: () => void;
  secret?: boolean;
  placeholder?: string;
}) {
  const [show, setShow] = useState(false);
  return (
    <div className="field">
      <div className="field-head">
        <label htmlFor={id}>{label}</label>
        {saved && !cleared && (
          <button type="button" className="linklike" onClick={onClear}>
            hapus
          </button>
        )}
        {cleared && (
          <button type="button" className="linklike" onClick={onUndoClear}>
            batal hapus
          </button>
        )}
      </div>
      <div className="input-row">
        <input
          id={id}
          type={secret && !show ? "password" : "text"}
          value={cleared ? "" : value}
          onChange={(e) => onChange(e.target.value)}
          disabled={cleared}
          placeholder={cleared ? "akan dihapus saat disimpan" : saved && savedPlaceholder ? savedPlaceholder : placeholder}
          autoComplete="off"
          autoCapitalize="off"
          autoCorrect="off"
          spellCheck={false}
        />
        {secret && (
          <button type="button" className="show-btn" onClick={() => setShow((v) => !v)} disabled={cleared}>
            {show ? "Sembunyikan" : "Tampilkan"}
          </button>
        )}
      </div>
    </div>
  );
}

export function SetupForm({
  setup,
  onSaved,
  wizard = false,
}: {
  setup: SetupInfo;
  onSaved: (s: SetupInfo) => void;
  wizard?: boolean;
}) {
  const f = setup.fields;
  const [secrets, setSecrets] = useState<Record<Secret, string>>({
    toko_api_key: "",
    toko_api_secret: "",
    telegram_bot_token: "",
  });
  const [plain, setPlain] = useState<Record<Plain, string>>({
    telegram_chat_id: f.telegram_chat_id.value ?? "",
    healthcheck_url: f.healthcheck_url.value ?? "",
  });
  const [cleared, setCleared] = useState<Partial<Record<Clearable, boolean>>>({});
  const [live, setLive] = useState(setup.live_trading);

  const [exchRes, setExchRes] = useState<TestExchangeResult | null>(null);
  const [exchBusy, setExchBusy] = useState(false);
  const [tgRes, setTgRes] = useState<{ ok: boolean; text: string } | null>(null);
  const [tgBusy, setTgBusy] = useState<null | "detect" | "test">(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  const starting = setup.state === "starting";

  const setSecret = (k: Secret) => (v: string) => setSecrets((s) => ({ ...s, [k]: v }));
  const setPlainV = (k: Plain) => (v: string) => setPlain((s) => ({ ...s, [k]: v }));
  const clearOps = (k: Clearable) => ({
    cleared: !!cleared[k],
    onClear: () => {
      setCleared((c) => ({ ...c, [k]: true }));
      if (k in plain) setPlainV(k as Plain)("");
      else setSecret(k as Secret)("");
    },
    onUndoClear: () => {
      setCleared((c) => ({ ...c, [k]: false }));
      if (k === "telegram_chat_id") setPlainV(k)(f.telegram_chat_id.value ?? "");
      if (k === "healthcheck_url") setPlainV(k)(f.healthcheck_url.value ?? "");
    },
  });

  // kolom yang masih kosong (menurut bot), dikurangi yang sedang diketik
  const typed = (k: string): boolean => {
    if (cleared[k as Clearable]) return false;
    if (k in secrets) return secrets[k as Secret].trim() !== "";
    if (k in plain) return plain[k as Plain].trim() !== "";
    return false;
  };
  const remaining = new Set(setup.missing.filter((k) => !typed(k)));
  if (cleared.toko_api_key) remaining.add("toko_api_key");
  if (cleared.toko_api_secret) remaining.add("toko_api_secret");

  const buildPayload = (): SetupUpdate => {
    const p: SetupUpdate = {};
    (["toko_api_key", "toko_api_secret", "telegram_bot_token"] as Secret[]).forEach((k) => {
      if (cleared[k]) p[k] = "";
      else if (secrets[k].trim()) p[k] = secrets[k].trim();
    });
    (["telegram_chat_id", "healthcheck_url"] as Plain[]).forEach((k) => {
      const saved = f[k].value ?? "";
      const v = plain[k].trim();
      if (cleared[k]) p[k] = "";
      else if (v !== "" && v !== saved) p[k] = v;
    });
    if (live !== setup.live_trading) p.live_trading = live;
    return p;
  };

  const testExchange = async () => {
    setExchBusy(true);
    setExchRes(null);
    try {
      const body: { toko_api_key?: string; toko_api_secret?: string } = {};
      if (secrets.toko_api_key.trim()) body.toko_api_key = secrets.toko_api_key.trim();
      if (secrets.toko_api_secret.trim()) body.toko_api_secret = secrets.toko_api_secret.trim();
      setExchRes(await api<TestExchangeResult>("setup/test-exchange", { method: "POST", body, timeoutMs: 60_000 }));
    } catch (e) {
      setExchRes({ ok: false, error: errText(e) });
    } finally {
      setExchBusy(false);
    }
  };

  const detectChat = async () => {
    setTgBusy("detect");
    setTgRes(null);
    try {
      const body: { telegram_bot_token?: string } = {};
      if (secrets.telegram_bot_token.trim()) body.telegram_bot_token = secrets.telegram_bot_token.trim();
      const r = await api<DetectChatResult>("setup/detect-chat-id", { method: "POST", body, timeoutMs: 40_000 });
      if (r.ok && r.chat_id) {
        setCleared((c) => ({ ...c, telegram_chat_id: false }));
        setPlainV("telegram_chat_id")(String(r.chat_id));
        setTgRes({ ok: true, text: `Terdeteksi: ${r.name ?? "chat"} (ID ${r.chat_id})` });
      } else {
        setTgRes({ ok: false, text: r.error ?? "Chat tidak ditemukan. Kirim /start ke bot kamu, lalu coba lagi." });
      }
    } catch (e) {
      setTgRes({ ok: false, text: errText(e) });
    } finally {
      setTgBusy(null);
    }
  };

  const testTelegram = async () => {
    setTgBusy("test");
    setTgRes(null);
    try {
      const body: { telegram_bot_token?: string; telegram_chat_id?: string } = {};
      if (secrets.telegram_bot_token.trim()) body.telegram_bot_token = secrets.telegram_bot_token.trim();
      if (plain.telegram_chat_id.trim()) body.telegram_chat_id = plain.telegram_chat_id.trim();
      const r = await api<TestTelegramResult>("setup/test-telegram", { method: "POST", body, timeoutMs: 40_000 });
      setTgRes(r.ok ? { ok: true, text: "Pesan tes terkirim. Cek Telegram kamu." } : { ok: false, text: r.error ?? "Gagal mengirim." });
    } catch (e) {
      setTgRes({ ok: false, text: errText(e) });
    } finally {
      setTgBusy(null);
    }
  };

  const save = async () => {
    if (saving || starting || remaining.size > 0) return;
    if (live && !setup.live_trading) {
      if (!window.confirm("PERINGATAN: mode LIVE akan memakai UANG ASLI di akun Tokocrypto kamu.\n\nYakin ingin mengaktifkan?")) return;
    }
    setSaving(true);
    setSaveError(null);
    try {
      const r = await api<SetupInfo>("setup", { method: "POST", body: buildPayload(), timeoutMs: 60_000 });
      setSecrets({ toko_api_key: "", toko_api_secret: "", telegram_bot_token: "" });
      setPlain({ telegram_chat_id: r.fields.telegram_chat_id.value ?? "", healthcheck_url: r.fields.healthcheck_url.value ?? "" });
      setCleared({});
      setLive(r.live_trading);
      onSaved(r);
    } catch (e) {
      setSaveError(errText(e));
    } finally {
      setSaving(false);
    }
  };

  const balances = exchRes?.balances ? Object.entries(exchRes.balances) : [];

  return (
    <form
      className="setup"
      onSubmit={(e) => {
        e.preventDefault();
        save();
      }}
    >
      <section className="card step">
        <h2>
          <span className="step-n">1</span> Tokocrypto API
        </h2>
        <div className="note note-info">
          Buat di <strong>Tokocrypto → Akun → API Management</strong>. Centang HANYA <em>Read</em> dan{" "}
          <em>Spot Trading</em>. JANGAN centang <strong>Withdraw</strong>.
        </div>
        <ValueField
          id="toko_api_key"
          label="API Key"
          value={secrets.toko_api_key}
          onChange={setSecret("toko_api_key")}
          saved={f.toko_api_key.set}
          savedPlaceholder={`tersimpan (…${cleanHint(f.toko_api_key.hint)})`}
          {...clearOps("toko_api_key")}
        />
        <ValueField
          id="toko_api_secret"
          label="Secret Key"
          secret
          value={secrets.toko_api_secret}
          onChange={setSecret("toko_api_secret")}
          saved={f.toko_api_secret.set}
          savedPlaceholder="tersimpan"
          {...clearOps("toko_api_secret")}
        />

        <label className="toggle">
          <input type="checkbox" checked={live} onChange={(e) => setLive(e.target.checked)} />
          <span className="toggle-ui" aria-hidden="true" />
          <span className="toggle-text">Pakai uang asli (LIVE)</span>
        </label>
        {live ? (
          <p className="note note-bad">Order sungguhan memakai saldo Tokocrypto kamu.</p>
        ) : (
          <p className="note note-neutral">Mode simulasi: harga asli, order pura-pura.</p>
        )}

        <div className="btn-row">
          <button type="button" onClick={testExchange} disabled={exchBusy}>
            {exchBusy ? "Menguji…" : "Tes koneksi"}
          </button>
        </div>
        {exchRes && exchRes.ok && (
          <div className="result result-ok" role="status">
            <strong>Koneksi berhasil.</strong>
            {balances.length > 0 ? (
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Aset</th>
                      <th className="num">Saldo</th>
                    </tr>
                  </thead>
                  <tbody>
                    {balances.map(([a, v]) => (
                      <tr key={a}>
                        <td>{a}</td>
                        <td className="num">{fmtNum(v, 8)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <span> Tidak ada saldo.</span>
            )}
          </div>
        )}
        {exchRes && !exchRes.ok && (
          <p className="inline-error" role="alert">
            {exchRes.error ?? "Koneksi gagal."}
          </p>
        )}
      </section>

      <details className="card step optional">
        <summary>Notifikasi (opsional, bisa nanti)</summary>
        <p className="note note-neutral">Bot tetap jalan tanpa ini — semua bisa dipantau dari dashboard.</p>
      <section className="sub">
        <h3>Telegram</h3>
        <p className="help">
          Buat bot di <strong>@BotFather</strong> → salin token. Lalu buka bot kamu dan kirim <code>/start</code>,
          kemudian klik <strong>Deteksi otomatis</strong>.
        </p>
        <ValueField
          id="telegram_bot_token"
          label="Bot token"
          secret
          value={secrets.telegram_bot_token}
          onChange={setSecret("telegram_bot_token")}
          saved={f.telegram_bot_token.set}
          savedPlaceholder={`tersimpan (…${cleanHint(f.telegram_bot_token.hint)})`}
          {...clearOps("telegram_bot_token")}
        />
        <ValueField
          id="telegram_chat_id"
          label="Chat ID"
          value={plain.telegram_chat_id}
          onChange={setPlainV("telegram_chat_id")}
          saved={f.telegram_chat_id.set}
          {...clearOps("telegram_chat_id")}
        />
        <div className="btn-row">
          <button type="button" onClick={detectChat} disabled={tgBusy !== null}>
            {tgBusy === "detect" ? "Mendeteksi…" : "Deteksi otomatis"}
          </button>
          <button type="button" onClick={testTelegram} disabled={tgBusy !== null}>
            {tgBusy === "test" ? "Mengirim…" : "Kirim pesan tes"}
          </button>
        </div>
        {tgRes && (
          <p className={tgRes.ok ? "result result-ok" : "inline-error"} role={tgRes.ok ? "status" : "alert"}>
            {tgRes.text}
          </p>
        )}
      </section>

      <section className="sub">
        <h3>Alarm kalau bot mati (Healthchecks.io)</h3>
        <p className="help">
          Daftar gratis di{" "}
          <a href="https://healthchecks.io" target="_blank" rel="noopener noreferrer">
            https://healthchecks.io
          </a>
          , buat satu check, lalu tempel <em>ping URL</em>-nya di sini. Kamu akan diberi tahu kalau bot berhenti
          mengirim sinyal hidup.
        </p>
        <ValueField
          id="healthcheck_url"
          label="Ping URL"
          value={plain.healthcheck_url}
          onChange={setPlainV("healthcheck_url")}
          saved={f.healthcheck_url.set}
          placeholder="https://hc-ping.com/…"
          {...clearOps("healthcheck_url")}
        />
      </section>

      </details>

      <section className="card step">
        <h2>
          <span className="step-n">2</span> Simpan &amp; jalankan
        </h2>
        {remaining.size > 0 && (
          <p className="note note-warn">
            Masih perlu diisi: {[...remaining].map((k) => FIELD_LABELS[k] ?? k).join(", ")}.
          </p>
        )}
        {setup.state === "error" && setup.error && (
          <p className="inline-error" role="alert">
            Bot gagal berjalan: {setup.error}
          </p>
        )}
        {saveError && (
          <p className="inline-error" role="alert">
            {saveError}
          </p>
        )}
        <div className="btn-row">
          <button type="submit" className="btn-primary" disabled={saving || starting || remaining.size > 0}>
            {saving ? "Menyimpan…" : starting ? "Menjalankan bot…" : wizard ? "Simpan & jalankan" : "Simpan"}
          </button>
          {starting && (
            <span className="busy">
              <span className="spinner" aria-hidden="true" /> Menjalankan bot…
            </span>
          )}
        </div>
        {!wizard && <p className="help">Setelah disimpan, bot dijalankan ulang dengan pengaturan baru.</p>}
      </section>
    </form>
  );
}
