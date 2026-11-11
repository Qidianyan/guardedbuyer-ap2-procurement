#!/usr/bin/env bash
# One-command local demo: installs the single runtime dependency if missing, then starts the web app.
# Offline by default (PayPal Orders v2 / Disputes v1 emulation, deterministic LLM fallbacks): no keys needed.
#   bash scripts/demo.sh            # http://127.0.0.1:8000
#   PORT=8080 bash scripts/demo.sh
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PYTHON:-python3}"
"$PY" -c 'import sys; assert sys.version_info >= (3, 10), "Python 3.10+ is required"' 
if ! "$PY" -c 'import cryptography' 2>/dev/null; then
  echo "Installing requirements (cryptography)..."
  "$PY" -m pip install -q -r requirements.txt
fi
exec "$PY" web/server.py --port "${PORT:-8000}" "$@"
