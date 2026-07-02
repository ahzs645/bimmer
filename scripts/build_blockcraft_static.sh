#!/usr/bin/env bash
# Build the SERVERLESS BlockCraft client: a fully static site (works on GitHub
# Pages) with the flat world + UNBC building generated in-browser — no Node
# server, no socket.io. First-person walkable, functional doors included.
#
# Usage:
#   scripts/build_blockcraft_static.sh          # build -> blockcraft/client/dist
#   scripts/build_blockcraft_static.sh serve    # build + serve on :3003 to test
#
# Prereqs: setup_blockcraft.py has generated building.json (any blocks.csv),
# and blockcraft/client deps are installed (npm install).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLIENT="$ROOT/blockcraft/client"

[ -f "$CLIENT/public/building.json" ] || {
  echo "building.json missing — run: .venv/bin/python scripts/setup_blockcraft.py out/unbc_1m/blocks.csv"
  exit 1
}
[ -d "$CLIENT/node_modules" ] || (cd "$CLIENT" && npm install)

echo "Building static serverless client..."
(cd "$CLIENT" && OFFLINE_MODE=1 npx webpack --mode=production)

echo
echo "Static site ready: $CLIENT/dist"
echo "Deploy: copy dist/ to any static host (GitHub Pages, Netlify, ...)."

if [ "${1:-}" = "serve" ]; then
  echo "Serving on http://127.0.0.1:3003/ (plain static server — like Pages)"
  cd "$CLIENT/dist" && python3 -m http.server 3003
fi
