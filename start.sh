#!/usr/bin/env bash
# Run Bank Reco on this computer without Docker.  Usage:  ./start.sh
# Needs Python 3.11 or newer (macOS: `brew install python@3.12`, or python.org installer).
set -euo pipefail
cd "$(dirname "$0")"

PY=""
for c in python3.13 python3.12 python3.11 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)'; then PY="$c"; break; fi
done
[ -z "$PY" ] && { echo "Python 3.11+ not found. Install it from python.org and run again."; exit 1; }

if [ ! -d .venv ]; then
  echo "First run: setting up (about a minute)…"
  "$PY" -m venv .venv
  .venv/bin/pip install --quiet --upgrade pip
  .venv/bin/pip install --quiet -r requirements.txt
fi

[ -f .env ] && set -a && . ./.env && set +a
export DATA_DIR="${DATA_DIR:-$PWD/data}"
PORT="${PORT:-8077}"
echo ""
echo "  Bank Reco is running →  http://localhost:$PORT"
echo "  Data is stored in      $DATA_DIR"
echo "  Press Ctrl+C to stop."
echo ""
( sleep 2; command -v open >/dev/null && open "http://localhost:$PORT" ) >/dev/null 2>&1 &
exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "$PORT"
