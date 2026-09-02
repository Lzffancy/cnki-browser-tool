#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

echo
echo " CNKI Local Research Assistant - one-click setup"
echo " ------------------------------------------------"
echo

# 1. locate python3
PYTHON="$(command -v python3 || command -v python || true)"
if [ -z "$PYTHON" ]; then
  echo "[ERROR] Python 3 not found. Install python3 first."
  exit 1
fi
echo "[1/4] Using Python: $PYTHON"

# 2. venv
VPY="backend/.venv/bin/python"
if [ ! -x "$VPY" ]; then
  echo "[2/4] Creating virtualenv at backend/.venv ..."
  "$PYTHON" -m venv backend/.venv
else
  echo "[2/4] Virtualenv already exists, skip."
fi

# 3. deps (http mode needs none; install only to enable --mode mcp later)
echo "[3/4] Installing dependencies ..."
"$VPY" -m pip install --quiet --upgrade pip || true
"$VPY" -m pip install --quiet -r backend/requirements.txt || \
  echo "         [WARN] dependency install failed; HTTP mode still works without 'mcp'."

# 4. start server (http mode) in background; log to backend/bridge.log
echo "[4/4] Starting bridge server (http://127.0.0.1:8765) ..."
nohup "$VPY" backend/bridge_server.py --mode http > backend/bridge.log 2>&1 &
echo "         server PID: $!"

# 5. health check
sleep 2
echo
echo " Health check (http://127.0.0.1:8765/health):"
curl -s http://127.0.0.1:8765/health || echo "         [WARN] cannot reach server; see backend/bridge.log"
echo
echo " ------------------------------------------------------------"
echo " NEXT STEPS (manual, cannot be automated):"
echo "  1. Open chrome://extensions -> enable 'Developer mode'"
echo "     -> 'Load unpacked' -> select the 'extension' folder."
echo "  2. Log in to CNKI (https://kns.cnki.net) and keep a CNKI tab open."
echo "  3. Re-check: curl http://127.0.0.1:8765/health"
echo "     -> confirm \"extension.connected\" becomes true (within ~30s)."
echo " ------------------------------------------------------------"
echo
