@echo off
rem moneymaker launcher (Windows). Double-click. First run installs everything into .venv.
setlocal
cd /d "%~dp0"
title moneymaker

set "PY="
where py >nul 2>nul && py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo Python 3.11 atau lebih baru belum terpasang.
  echo Install dari https://www.python.org/downloads/ dan CENTANG "Add python.exe to PATH".
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Menyiapkan lingkungan Python ^(sekali saja^)...
  %PY% -m venv .venv || goto fail
)
if not exist ".venv\.installed" (
  echo Menginstal dependensi ^(beberapa menit, sekali saja^)...
  ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
  echo.> ".venv\.installed"
)
rem Quick no-op when nothing changed; picks up new dependencies after a git pull.
".venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check -e . || goto fail

".venv\Scripts\python.exe" -m moneymaker run --open
echo.
echo moneymaker berhenti.
pause
exit /b 0

:fail
echo Instalasi gagal. Lihat pesan di atas.
pause
exit /b 1
