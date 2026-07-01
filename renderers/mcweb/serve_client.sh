#!/usr/bin/env bash
# serve_client.sh — wire up and launch a LOCAL minecraft-web-client that boots
# straight into our exported world (no menu, no drag-and-drop).
#
# It clones the upstream client next to the bimmer repo (once), installs deps,
# applies the Node >=24 SlowBuffer shim its legacy jwa dep needs, serves the
# world save same-origin, and opens the browser straight into the building.
#
#   renderers/mcweb/serve_client.sh [world.zip]
#
# Env overrides:
#   MCWEB_CLIENT   client checkout dir   (default: ../minecraft-web-client, sibling of the repo)
#   MCWEB_PORT     dev server port       (default: 3000)
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$here/../.." && pwd)"
CLIENT="${MCWEB_CLIENT:-$(cd "$repo_root/.." && pwd)/minecraft-web-client}"
PORT="${MCWEB_PORT:-3000}"
WORLD_ZIP="${1:-$repo_root/out/unbc_1m/world.zip}"

command -v pnpm >/dev/null || { echo "pnpm is required (npm i -g pnpm)"; exit 1; }
[ -f "$WORLD_ZIP" ] || { echo "world zip not found: $WORLD_ZIP  (run 'make mcweb' or run.sh export+pack first)"; exit 1; }

# 1. clone the client once
if [ ! -d "$CLIENT/.git" ]; then
  echo "Cloning minecraft-web-client -> $CLIENT"
  git clone --depth 1 https://github.com/zardoy/minecraft-web-client "$CLIENT"
fi

# 2. install deps once
if [ ! -d "$CLIENT/node_modules/prismarine-chunk" ]; then
  echo "Installing client deps (pnpm) ..."
  ( cd "$CLIENT" && pnpm install )
fi

# 3. Node >=24 removed buffer.SlowBuffer; its legacy jwa dep crashes data-prep.
#    Guard it (idempotent — also covered by a committed pnpm patch on our clone).
bect="$(ls "$CLIENT"/node_modules/.pnpm/buffer-equal-constant-time@*/node_modules/buffer-equal-constant-time/index.js 2>/dev/null | head -1 || true)"
if [ -n "$bect" ] && ! grep -q 'Node >=24' "$bect"; then
  perl -0pi -e "s/require\('buffer'\)\.SlowBuffer;/require('buffer').SlowBuffer || { prototype: {} }; \/\/ Node >=24 removed SlowBuffer/" "$bect"
  echo "Applied SlowBuffer shim to $bect"
fi

# 4. serve the world save same-origin from the dev server's public dir
mkdir -p "$CLIENT/public"
cp "$WORLD_ZIP" "$CLIENT/public/world.zip"
echo "World: $WORLD_ZIP -> $CLIENT/public/world.zip"

# 5. free ports (proxy on 8080 + rsbuild dev on $PORT)
for p in "$PORT" 8080; do lsof -ti tcp:"$p" 2>/dev/null | xargs kill 2>/dev/null || true; done

# 6. open the browser straight into the building once the server is up. The
#    ?map= query param loads the save on boot; a config.json default is read too
#    late (the app runs its load check before config.json finishes fetching), so
#    the query string is the reliable trigger — no menu, no drag-and-drop.
URL="http://localhost:$PORT/?map=/world.zip"
if command -v open >/dev/null 2>&1; then
  ( for _ in $(seq 1 150); do curl -sf "http://localhost:$PORT/" >/dev/null 2>&1 && break; sleep 1; done; open "$URL" ) &
fi
echo "Starting client -> $URL  (boots straight into the building)"
cd "$CLIENT" && exec pnpm start
