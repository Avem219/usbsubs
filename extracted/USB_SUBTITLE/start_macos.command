#!/usr/bin/env bash
# USB SUBTITLE launcher for macOS (double-click in Finder, or run in Terminal).
set -e
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$HOME/Library/Application Support/USB_SUBTITLE/venv"
PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info>=(3,9) else 1)' 2>/dev/null; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  echo "Python 3.9+ is required. Install from https://www.python.org/downloads/macos/ (or: brew install python), then run this again."
  read -r -p "Press Enter to close..." _; exit 1
fi
if [ ! -x "$VENV/bin/python" ]; then
  echo "First run on this Mac: creating environment (needs internet once)..."
  "$PY" -m venv "$VENV"
fi
if [ ! -f "$VENV/.deps-ok" ] || [ "$HERE/app/requirements.txt" -nt "$VENV/.deps-ok" ]; then
  "$VENV/bin/python" -m pip install --upgrade pip >/dev/null 2>&1 || true
  "$VENV/bin/python" -m pip install -r "$HERE/app/requirements.txt" || { echo "Dependency install failed (internet needed once)."; read -r -p "Press Enter..." _; exit 1; }
  touch "$VENV/.deps-ok"
fi
cd "$HERE/app"
"$VENV/bin/python" -m usb_subtitle "$@"
