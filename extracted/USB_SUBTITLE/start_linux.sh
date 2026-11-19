#!/usr/bin/env bash
# USB SUBTITLE launcher for Linux. Dependencies live on THIS computer (not on the USB), created once.
set -e
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="${XDG_DATA_HOME:-$HOME/.local/share}/usb_subtitle/venv"
PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info>=(3,9) else 1)' 2>/dev/null; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  echo "Python 3.9 or newer is required but was not found."
  echo "Install it with your package manager, e.g.:  sudo apt install python3 python3-venv python3-pip"
  exit 1
fi
if [ ! -x "$VENV/bin/python" ]; then
  echo "First run on this computer: creating environment (needs internet once)..."
  "$PY" -m venv "$VENV" || { echo "venv failed. On Debian/Ubuntu: sudo apt install python3-venv"; exit 1; }
fi
if [ ! -f "$VENV/.deps-ok" ] || [ "$HERE/app/requirements.txt" -nt "$VENV/.deps-ok" ]; then
  "$VENV/bin/python" -m pip install --upgrade pip >/dev/null 2>&1 || true
  "$VENV/bin/python" -m pip install -r "$HERE/app/requirements.txt" || { echo "Dependency install failed (internet needed once)."; exit 1; }
  touch "$VENV/.deps-ok"
fi
cd "$HERE/app"
exec "$VENV/bin/python" -m usb_subtitle "$@"
