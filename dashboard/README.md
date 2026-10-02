# Moneymaker Dashboard

Dashboard web kecil (Next.js) untuk memantau dan mengontrol bot trading crypto: status, KPI, grafik equity, posisi, sinyal, risk events, audit log, serta tombol Pause / Resume / PANIC.

## Menjalankan

1. Salin contoh env lalu isi token yang sama dengan `API_TOKEN` di bot:

   ```bash
   cp .env.example .env.local
   # edit .env.local
   #   BOT_API_URL=http://127.0.0.1:8000
   #   BOT_API_TOKEN=<token-bot-kamu>
   ```

2. Install dan jalankan:

   ```bash
   npm install
   npm run dev                 # mode development  -> http://127.0.0.1:3000
   # atau
   npm run build && npm run start   # mode produksi -> http://127.0.0.1:3000
   ```

   (`next start` menampilkan peringatan karena `output: "standalone"`; tetap berfungsi. Untuk Docker, image memakai `node server.js`.)

3. Uji tanpa bot asli: `node scripts/mock-bot.mjs` (token mock: `test-token-1234567890`, set `MOCK_MODE=live` untuk mode live).

## Docker (opsional)

```bash
docker build -t moneymaker-dashboard .
docker run --rm -p 127.0.0.1:3000:3000 \
  -e BOT_API_URL=http://host.docker.internal:8000 -e BOT_API_TOKEN=... moneymaker-dashboard
```

Gunakan `-p 127.0.0.1:3000:3000` agar tetap hanya bisa diakses dari laptop sendiri.

## Catatan keamanan

- **Token API hanya ada di sisi server.** Browser hanya memanggil `/api/bot/*` pada server Next.js; route handler di `app/api/bot/[...path]/route.ts` yang menambahkan header `Authorization: Bearer ...`. Token tidak pernah dikirim ke browser (jangan memakai awalan `NEXT_PUBLIC_`).
- Proxy hanya meneruskan path yang di-allowlist (GET: health, status, positions, orders, equity, signals, risk-events, audit, config; POST: pause, resume, panic). Selain itu 404. Request POST harus `application/json` dan, bila ada header `Origin`, harus sama dengan host dashboard (proteksi CSRF dari situs lain).
- Server dashboard **hanya bind ke 127.0.0.1** (script `dev`/`start`). Dashboard ini tidak punya login sendiri, jadi jangan diekspos ke jaringan/internet.
- Jangan commit `.env.local` (sudah ada di `.gitignore`).

## Akses dari host lain
Proxy hanya menerima Host `127.0.0.1`, `localhost`, `[::1]` (proteksi DNS rebinding). Kalau perlu nama host lain (mis. Tailscale), set `DASHBOARD_ALLOWED_HOSTS=nama-host` di `.env.local`.
