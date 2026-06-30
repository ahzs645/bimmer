#!/usr/bin/env bash
# Launch the patched BlockCraft (flat world + UNBC building): server + client.
# Usage: scripts/run_blockcraft.sh        (start)
#        scripts/run_blockcraft.sh stop   (stop)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BC="$ROOT/blockcraft"

if [ "${1:-}" = "stop" ]; then
  pkill -f "node app.js" 2>/dev/null || true
  pkill -f "webpack-dev-server" 2>/dev/null || true
  echo "stopped BlockCraft server + client"
  exit 0
fi

[ -d "$BC" ] || { echo "blockcraft/ missing — it should be tracked in this repo."; exit 1; }
[ -f "$BC/server/building.json" ] || { echo "Run: .venv/bin/python scripts/setup_blockcraft.py"; exit 1; }

# Fresh flat world each run (clears stale player edits)
mkdir -p "$BC/server/saves" "$BC/server/logs"
: > "$BC/server/saves/test.json" 2>/dev/null || true

echo "Starting server (:3002)…"
( cd "$BC/server" && nohup node app.js > /tmp/bc_server.log 2>&1 & echo $! > /tmp/bc_server.pid )
echo "Starting client (:3001)…"
( cd "$BC/client" && nohup npm start > /tmp/bc_client.log 2>&1 & echo $! > /tmp/bc_client.pid )

echo
echo "Waiting for client to compile…"
for i in $(seq 1 30); do
  sleep 2
  curl -s -o /dev/null -w "" http://127.0.0.1:3001/ 2>/dev/null && \
    curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:3001/ 2>/dev/null | grep -q 200 && break
done

cat <<EOF

BlockCraft is running.
  1. Open  http://localhost:3001
  2. Click "Direct Connect" (box is pre-filled with localhost:3001)
  3. You spawn on a flat world beside the UNBC building.

Logs: /tmp/bc_server.log  /tmp/bc_client.log
Stop: scripts/run_blockcraft.sh stop
EOF
