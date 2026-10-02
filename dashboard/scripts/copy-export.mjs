// Salin hasil `next build` (folder out/) ke src/moneymaker/web/ supaya disajikan oleh bot.
import { cpSync, existsSync, mkdirSync, rmSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const src = resolve(here, "../out");
const dest = resolve(here, "../../src/moneymaker/web");

if (!existsSync(resolve(src, "index.html"))) {
  console.error(`out/index.html tidak ditemukan di ${src}. Jalankan "next build" dulu.`);
  process.exit(1);
}
rmSync(dest, { recursive: true, force: true });
mkdirSync(dest, { recursive: true });
cpSync(src, dest, { recursive: true });
console.log(`Disalin: ${src} -> ${dest}`);
