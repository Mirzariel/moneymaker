# Moneymaker Dashboard

Dashboard web (Next.js, diekspor jadi file statis) untuk mengisi API key, mengecek kesiapan, lalu memantau dan mengontrol bot: status, KPI, grafik equity, posisi, sinyal, risk events, audit log, serta Pause / Resume / PANIC.

## Cara pakai (pengguna)

UI **disajikan langsung oleh bot** (FastAPI) di <http://127.0.0.1:8000>. Tidak ada server Node yang perlu dijalankan. Buka lewat link login yang muncul di jendela terminal saat bot start (`/login?token=...`); link itu memasang cookie sesi HttpOnly. Tanpa cookie, halaman hanya menampilkan petunjuk untuk membuka link tersebut.

Alur: wizard Pengaturan (API Tokocrypto, Telegram, alarm) -> Cek kesiapan -> Mulai trading -> dashboard. Tombol **Pengaturan** dan **Cek kesiapan** tersedia di header dashboard.

## Pengembangan UI

```bash
npm install
node scripts/mock-bot.mjs    # mock bot di http://127.0.0.1:8000 (menyajikan out/ + API tiruan)
npm run build                # bangun ulang out/ setiap habis mengubah UI (mock membaca out/ tiap request)
```

Buka <http://127.0.0.1:8000/login?token=test-token-1234567890>. Mock mulai di mode setup (isi key apa saja; key yang mengandung "bad" ditolak); `MOCK_RUNNING=1` untuk langsung ke dashboard, `MOCK_MODE=live` untuk mode live.

(`npm run dev` tetap ada untuk Next dev server di :3000, tetapi UI memanggil `/api/...` di origin yang sama, jadi dev server tidak punya backend. Pakai mock di atas.)

## Setelah mengubah UI

```bash
npm run export     # next build + salin out/ ke ../src/moneymaker/web/
```

Lalu **commit folder `src/moneymaker/web/`** — itulah yang dipakai bot, sehingga pengguna tidak perlu Node.

## Keamanan

- Browser tidak pernah melihat token; autentikasi hanya cookie `HttpOnly; SameSite=Strict` dari backend.
- Semua request same-origin ke `/api/*`. Respons 401 menampilkan "Sesi tidak valid"; 409 pada `/api/status` berarti bot belum berjalan (mode setup).
- Bot hanya bind ke 127.0.0.1; jangan diekspos ke jaringan/internet.
