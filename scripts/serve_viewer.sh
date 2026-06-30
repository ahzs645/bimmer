#!/usr/bin/env bash
# Serve the interactive voxel viewer locally.
# Usage: scripts/serve_viewer.sh [port]
set -euo pipefail

PORT="${1:-8765}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WEB="$ROOT/web"
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"

# Free the port if something is already bound to it.
if lsof -ti tcp:"$PORT" >/dev/null 2>&1; then
  echo "Port $PORT busy; stopping the old server..."
  lsof -ti tcp:"$PORT" | xargs kill 2>/dev/null || true
  sleep 0.5
fi

echo "Serving $WEB at http://127.0.0.1:$PORT/"
echo "Open that URL in a browser. Ctrl-C to stop."
cd "$WEB"
exec "$PY" -m http.server "$PORT" --bind 127.0.0.1
