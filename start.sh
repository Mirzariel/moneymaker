#!/usr/bin/env bash
# moneymaker launcher (macOS / Linux). First run installs everything into .venv, then opens the browser.
set -euo pipefail
cd "$(dirname "$0")"

PY=""
for c in python3.13 python3.12 python3.11 python3 python; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then
  echo "Python 3.11 atau lebih baru belum terpasang."
  echo "macOS: install dari https://www.python.org/downloads/ (atau: brew install python@3.12)"
  echo "Linux: sudo apt install python3 python3-venv"
  read -r -p "Tekan Enter untuk keluar..." _ || true
  exit 1
fi

if [ ! -x .venv/bin/python ]; then
  echo "Menyiapkan lingkungan Python (sekali saja)..."
  "$PY" -m venv .venv
fi
# Reinstall only when dependencies changed.
if [ ! -f .venv/.installed ] || [ pyproject.toml -nt .venv/.installed ]; then
  echo "Menginstal dependensi..."
  .venv/bin/python -m pip install --quiet --upgrade pip
  .venv/bin/python -m pip install --quiet -e .
  touch .venv/.installed
fi

# Keep the Mac awake while the bot runs (the bot stops when the laptop sleeps).
if command -v caffeinate >/dev/null 2>&1 && [ "$(uname)" = "Darwin" ]; then
  exec caffeinate -dimsu .venv/bin/python -m moneymaker run --open
elif command -v systemd-inhibit >/dev/null 2>&1 && systemd-inhibit --what=idle true >/dev/null 2>&1; then
  exec systemd-inhibit --what=sleep:idle --why="moneymaker trading bot" .venv/bin/python -m moneymaker run --open
else
  exec .venv/bin/python -m moneymaker run --open
fi
