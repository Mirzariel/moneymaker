# moneymaker — bot trading spot pribadi untuk Tokocrypto

Bot ini memantau pasar, mencari sinyal, memeriksa setiap sinyal lewat risk engine, lalu (kalau lolos) membeli di pasar spot Tokocrypto. **Setiap posisi langsung diberi stop-loss yang tersimpan di exchange**, jadi tetap terlindungi meskipun laptop mati atau tidur. Bisa dikontrol dari HP (Telegram) dan dari dashboard web lokal, termasuk tombol darurat **PANIC** (cancel semua order + jual semua posisi).

> ⚠️ Arsitektur yang aman ≠ profit. Strategi bawaan (`trend_atr`) adalah baseline untuk menguji pipeline, **bukan** edge yang terbukti. Jalankan paper trading minimal 2–4 minggu dan backtest net fee sebelum memakai uang asli.

## Alur

```
Scanner → Strategy → Signal → RiskEngine (bisa REJECT) → Executor → Tokocrypto (live) / PaperExchange
                                   ↑                                   ↓
              ControlService (pause/resume/panic) ← Telegram · Dashboard · CLI
                                   ↓
                     SQLite (status, order, posisi, audit) · heartbeat → healthchecks.io
```

| Modul | File |
|---|---|
| Config (`.env` + `config.yaml`) | `src/moneymaker/config.py` |
| Wrapper Tokocrypto (CCXT) / paper trading | `src/moneymaker/exchange/` |
| Strategi | `src/moneymaker/strategy/trend_atr.py` |
| Risk engine | `src/moneymaker/risk/engine.py` |
| Eksekusi, stop-loss di exchange, rekonsiliasi | `src/moneymaker/execution/` |
| Pause / resume / PANIC | `src/moneymaker/control.py` |
| Telegram, API, heartbeat | `notify/telegram.py`, `api/app.py`, `heartbeat.py` |
| Backtest | `src/moneymaker/backtest/runner.py` |
| Dashboard (Next.js) | `dashboard/` |

## Fitur keamanan

- **Default PAUSED + paper.** Instalasi baru tidak trading sampai kamu `/resume`. Uang asli hanya kalau `LIVE_TRADING=true` **dan** kamu resume manual.
- **Stop-loss di exchange** (STOP_LOSS_LIMIT) setelah tiap entry. Gagal pasang stop berarti posisi langsung dijual. Kalau harga anjlok melewati batas limit (gap), bot menjual di harga market.
- **Risk engine** dengan aturan berikut (semua bisa diatur di `config.yaml`):
  - Risiko maksimal 1% equity per trade (sudah termasuk fee)
  - Posisi maksimal 20%, maksimal 3 posisi, exposure total maksimal 60%
  - Batas spread dan likuiditas
  - Cooldown, signal expiry, dan cek price drift
  - Target profit minimal 3× fee pulang-pergi
  - Minimum notional dari exchange
- **Circuit breaker.** Rugi harian ≥ 3% atau kalah 4× berturut-turut membuat bot auto-pause dan mengirim notifikasi.
- **PANIC.** Urutannya: status PAUSED disimpan *duluan*, lalu cancel semua order per simbol, jual semua posisi, verifikasi saldo, dan kirim laporan. Setelah restart bot tetap PAUSED.
- **Rekonsiliasi saat start.** Bot mendeteksi stop yang terisi saat offline, memasang ulang stop yang hilang, dan auto-pause kalau ada order dengan hasil tak jelas. Tidak ada trading sebelum rekonsiliasi berhasil.
- **Heartbeat ke healthchecks.io.** Kalau laptop atau bot mati, kamu tetap dapat alert (bot yang mati tidak bisa mengabari sendiri).
- **API dan dashboard hanya di `127.0.0.1`.** Akses pakai token, token tidak pernah sampai ke browser, dan ada proteksi CSRF + DNS rebinding.
- **Database paper dan live tidak bisa tercampur.** Bot menolak start kalau mode tidak cocok dengan DB.

## Setup (laptop)

```bash
python3.11 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env
cp config.example.yaml config.yaml
pytest                     # harus semua hijau
```

### 1. API key Tokocrypto
Buat API key di Tokocrypto dengan izin **Read + Spot Trading saja. JANGAN aktifkan Withdraw.** Isi `TOKO_API_KEY` dan `TOKO_API_SECRET` di `.env`. Nanti kalau sudah pakai VPS, aktifkan IP whitelist.

### 2. Fase 0 — cek asumsi exchange
```bash
moneymaker check                         # read-only: market, volume, spread, saldo, fee
moneymaker check --test-orders BTC/USDT  # opsional: pasang & cancel order jauh dari harga
```
Yang harus kamu pastikan dari output-nya:
- **Fee aktual** akunmu. CCXT mencatat 0.75%/sisi. Ubah `fees.taker_pct` di `config.yaml` sesuai fee asli, karena ini sangat menentukan apakah strategi bisa profit.
- Pilih `quote` (USDT atau IDR) yang punya market likuid paling banyak.
- Kolom `stopLimit` harus `True` untuk pair yang mau ditrade.
- Tes stop-loss (`--test-orders`) butuh sedikit koin base di akun. Ini membuktikan asumsi keamanan utama.

### 3. Backtest
```bash
moneymaker backtest BTC/USDT ETH/USDT SOL/USDT --days 365
```
Lihat return, profit factor (> 1.2 baru menarik), max drawdown, dan bandingkan dengan buy & hold. Kalau hasilnya negatif setelah fee, **jangan live**. Ubah parameter atau strategi dulu.

### 4. Telegram
1. Buat bot lewat @BotFather, lalu isi `TELEGRAM_BOT_TOKEN`.
2. Kirim pesan ke @userinfobot untuk tahu chat id-mu, lalu isi `TELEGRAM_CHAT_ID`. Bot hanya melayani chat id ini.
3. Perintah yang tersedia: `/status`, `/positions`, `/pnl`, `/pause`, `/resume`, `/panic` (lalu `/panic CONFIRM`).

### 5. Heartbeat
Buat check gratis di https://healthchecks.io (period 5 menit, grace 10 menit), hubungkan ke Telegram atau email, lalu isi `HEALTHCHECK_URL`.

### 6. Jalankan (paper)
```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"   # isi API_TOKEN di .env
moneymaker run
```
Kirim `/resume` dari Telegram. Untuk dashboard, lihat `dashboard/README.md` (`npm install && npm run build && npm run start`, lalu buka http://127.0.0.1:3000).

Atau semuanya lewat Docker (auto-restart):
```bash
docker compose up -d --build     # config.yaml HARUS sudah ada sebelum ini
```

### Perintah CLI
```bash
moneymaker status | pause | resume
moneymaker panic          # minta ketik PANIC; pakai API bot yang sedang jalan, atau langsung kalau bot mati
```

## Laptop harus tetap menyala
Bot berhenti kalau laptop sleep, mati, atau internet putus. Posisi tetap terlindungi stop-loss di exchange, tapi tidak ada entry atau take-profit selama mati. Cara mencegah sleep:
- **Windows:** Settings → System → Power → Sleep = *Never* (saat dicolok). Tutup layar: Control Panel → Power Options → "When I close the lid" = *Do nothing*.
- **macOS:** `caffeinate -dimsu moneymaker run`, atau System Settings → Battery → Options → "Prevent automatic sleeping…".
- **Linux:** `systemd-inhibit --what=sleep:idle moneymaker run`.

## Pindah ke VPS
1. Salin repo, `.env`, dan `config.yaml` ke VPS (Ubuntu + Docker).
2. Jalankan `docker compose up -d --build`.
3. Aktifkan IP whitelist API key di Tokocrypto (IP statis VPS).
4. Dashboard tetap bind ke localhost. Akses lewat SSH tunnel (`ssh -L 3000:127.0.0.1:3000 vps`) atau Tailscale. **Jangan buka port ke publik.**

## Checklist go-live (uang asli)
- [ ] `pytest` hijau
- [ ] `moneymaker check` sudah dijalankan; fee asli sudah masuk config; tes stop-loss berhasil
- [ ] Backtest net fee positif di beberapa pair dan periode
- [ ] Paper trading ≥ 2–4 minggu tanpa error kritis; PANIC dan pause sudah dicoba
- [ ] Heartbeat alert sudah dites (matikan bot → alert masuk)
- [ ] Modal kecil yang siap hilang
- [ ] `DB_PATH` baru untuk live (mis. `data/live.db`), `LIVE_TRADING=true`, restart, lalu `/resume`

## Yang belum diketahui / batasan
- Bot belum pernah diuji terhadap API Tokocrypto sungguhan (sandbox pengembangan tidak punya akses). Fase 0 di laptopmu adalah validasi pertamanya.
- Market "native" Tokocrypto (bukan yang dilayani Binance) tidak punya ticker 24 jam lewat API, jadi otomatis dilewati scanner.
- Take-profit dan exit strategi dijalankan oleh bot, bukan oleh exchange. Kalau laptop mati, hanya stop-loss yang aktif.
- Pajak kripto dipotong exchange per transaksi dan tidak dimodelkan terpisah. Masukkan ke `fees.taker_pct` kalau ingin konservatif.
