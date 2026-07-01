#!/usr/bin/env bash
# run.sh — drive the minecraft-web-client renderer for a bimmer voxel model.
#
# minecraft-web-client (https://github.com/zardoy/minecraft-web-client, MIT) is a
# browser Minecraft client that renders REAL vanilla block models — so our
# functional doors, stairs, slabs, glass and iron-bar railings render natively,
# with no engine patching (contrast ../../blockcraft, a cube-only fork).
#
# Pipeline:
#   blocks.csv  --export-->  world/ (Anvil save)  --pack-->  world.zip
#   then load world.zip in the client (hosted mcraft.fun, or a local build).
#
# Subcommands:
#   run.sh export <blocks.csv> [world-dir]   voxel CSV -> Anvil world save
#   run.sh verify [world-dir]                round-trip block-states via the client's loader
#   run.sh pack   [world-dir]                zip the save for drag-and-drop import
#   run.sh all    <blocks.csv> [world-dir]   export + verify + pack
#
# Loading the packed world:
#   * Hosted (no build):  open https://mcraft.fun  ->  Menu  ->  "Load Save / Open World"
#                         ->  drop world.zip. Renders instantly, walk with WASD,
#                         right-click doors to open them.
#   * Local build:        git clone https://github.com/zardoy/minecraft-web-client
#                         cd minecraft-web-client && pnpm i && pnpm start
#                         then load the same world.zip in the local tab.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cmd="${1:-help}"; shift || true

ensure_deps() {
  [ -d "$here/node_modules/prismarine-chunk" ] || (cd "$here" && npm install)
}

case "$cmd" in
  export)
    csv="${1:?usage: run.sh export <blocks.csv> [world-dir]}"
    world="${2:-$here/world}"
    ensure_deps
    node "$here/export_anvil.js" "$csv" --out "$world"
    ;;
  verify)
    world="${1:-$here/world}"
    ensure_deps
    node "$here/verify_save.js" "$world"
    ;;
  pack)
    world="${1:-$here/world}"; world="${world%/}"
    dir="$(cd "$(dirname "$world")" && pwd)"; base="$(basename "$world")"
    # zip name is relative to $dir (we cd into it), so use the basename only —
    # passing a path with dir components would resolve wrong from inside $dir.
    ( cd "$dir" && rm -f "$base.zip" && zip -qr "$base.zip" "$base" )
    echo "packed -> $dir/$base.zip"
    echo "load it at https://mcraft.fun  (Menu -> Open World -> drop the zip)"
    ;;
  all)
    csv="${1:?usage: run.sh all <blocks.csv> [world-dir]}"
    world="${2:-$here/world}"
    ensure_deps
    node "$here/export_anvil.js" "$csv" --out "$world"
    node "$here/verify_save.js" "$world"
    "$here/run.sh" pack "$world"
    ;;
  *)
    sed -n '2,30p' "$here/run.sh" | sed 's/^# \{0,1\}//'
    ;;
esac
