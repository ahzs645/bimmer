# Rendering the building in BlockCraft (walkable, flat world)

## Serverless mode — static site / GitHub Pages (no Node server)

The client can now run **without any server**: an in-browser "local server"
(`client/src/offline/LocalServer.js`) emulates the socket protocol against the
same flat-world + building generator, RLE chunk codec and block registry
(`blockcraft/shared/registry.js`) the Node server uses. Single-player,
first-person, creative/fly, functional doors — all client-side.

```sh
.venv/bin/python scripts/setup_blockcraft.py out/unbc_1m/blocks.csv  # building.json
scripts/build_blockcraft_static.sh serve   # build dist/ + test on :3003
```

`blockcraft/client/dist/` is then a plain static site — host it anywhere. On
**GitHub Pages** it deploys as the **`/blockcraft/` subpage** of the project
site (the root is the higher-fidelity minecraft-web-client renderer — see
RENDERERS.md → *GitHub Pages layout*): enable Pages (Settings → Pages →
Source: *GitHub Actions*) and run the **“Deploy renderers to GitHub Pages”**
workflow (`.github/workflows/pages.yml`; it also auto-runs on pushes to `main`
touching `blockcraft/`). The committed
`blockcraft/client/public/building.json` snapshot is what gets deployed, so CI
never needs the IFC. Notes:

- `SharedArrayBuffer` needs cross-origin isolation; static hosts can't set
  COOP/COEP headers, so `index.html` loads the vendored MIT
  `coi-serviceworker.min.js` shim (no-op when headers are already present).
- Dev equivalent without a build: `npm start` + open
  `http://localhost:3001/?offline=1`.
- Multiplayer mode below still works exactly as before — offline mode only
  engages via the `OFFLINE_MODE=1` build flag or `?offline=1`.


This makes the voxelized UNBC building **walkable in a browser** using a
**modified fork of [BlockCraft](https://github.com/ChiefElite/blockcraft-public)**
(a WebGL voxel game — Three.js client + Node server) on a **flat (superflat)
world**. Our fork lives in [`/blockcraft`](blockcraft) and is committed to this
repo; see [Licensing & our changes](#licensing--our-changes).

## TL;DR

```sh
# 1. install BlockCraft deps  (once; node_modules is git-ignored)
( cd blockcraft/server && npm install )
( cd blockcraft/client && npm install three && npm install )

# 2. generate the building data (door textures + building.json from a blocks.csv)
.venv/bin/python scripts/setup_blockcraft.py out/unbc_0p5m/blocks.csv

# 3. run it (server :3002 + client :3001)
scripts/run_blockcraft.sh
```

Then open **http://localhost:3001**, click **Direct Connect** (pre-filled with
`localhost:3001`). You spawn in **creative, hovering above the building looking
down**. Stop with `scripts/run_blockcraft.sh stop`.

## How it works

BlockCraft generates terrain on demand in a worker thread
(`server/worker.js` → `WorldGeneration.generateCell`) and stores blocks 1-indexed
via a `world.blockId[name]` registry (16³ cells). There is **no flat-world option
and no save format that disables terrain**, so we replace the generator:

1. **`scripts/export_blockcraft.py`** converts our `blocks.csv` into
   `blockcraft/server/building.json` — block coordinates grouped by BlockCraft
   block name, centred on the origin, with the floor just above the grass.
   It remaps our Minecraft palette to blocks BlockCraft actually ships:

   | our block | BlockCraft block |
   |---|---|
   | `white_concrete` (walls) | `hardened_clay_stained_white` |
   | `smooth_stone` / `stone` | `stone` |
   | `gray_concrete` (mullions) | `hardened_clay_stained_gray` |
   | `light_blue_stained_glass` (glazing) | `glass_light_blue` |
   | `iron_bars` (railings) | `glass` |
   | `stone_bricks` (stairs) | `stonebrick` |
   | `deepslate_tiles` (roof) | `stone_andesite` |
   | `oak_door` (doors) | **`door_x` / `door_z`** — custom **functional, openable, flat** door blocks we added, oriented by the IFC door facing (see below) |

2. **Our changes are baked into the tracked `/blockcraft` fork** (see the git
   history for the exact diff vs. upstream):
   - `server/modules/WorldGeneration.js` — replaces `generateCell` to lay a flat
     world (bedrock y=0, dirt, grass at y=4) and stamp the building per cell
     (loaded once from `building.json`, names→ids via the registry).
   - `server/modules/Server.js` — default game mode `creative`; registers four new
     door blocks `door_x`/`door_z` (closed, solid) and `door_x_open`/`door_z_open`
     (open, passable), one pair per facing axis.
   - `server/app.js` — spawn point.
   - `client/src/input/PointerLock.js` — start the camera looking downward.
   - `client/.../Player.ts` — spawn in **creative & flying** above the building
     centre; door collision (`*_open` walk-through) and **right-click to
     open/close** (toggles both halves).
   - `client/.../TextureManager.js`, `client/.../voxel-worker.js` — register the
     door textures, render doors as thin flat panels, mark them see-through.

3. **`scripts/setup_blockcraft.py`** regenerates the door textures
   (`make_door_textures.py`) and `building.json` (`export_blockcraft.py`) — the
   only two IFC-derived, non-committed pieces. Idempotent.

### Creative mode & functional doors

You spawn in **creative mode, already flying, hovering above the building's
centre looking down** — fly around freely (no fall damage). Every doorway has a
real **door** rendered as a **thin flat panel** (not a cube): it starts closed
(solid); **right-click it to open** (it becomes walk-through) and right-click
again to close. Both halves toggle together.

BlockCraft ships no door block, so we added our own (Apache fork). Properties,
all derived from the IFC so they look right at any resolution:
- **Facing** comes from the IFC door (not guessed), so doors never cross/overlap.
- **Leaf count** comes from `OverallWidth` (pitch-aware), so a 0.9 m door is
  single and a 1.8 m door is double — no spurious extra leaves.
- **Height** is a real ~2 m (pitch-aware), **anchored to the floor** (not sunk,
  not filling the whole opening), and the texture tiles seamlessly so a multi-cell
  door reads as one door, not several stacked.
- **Closed** doors render in the opaque pass (write depth → no see-through), and
  are **solid**; **open** doors are see-through and **walk-through**.
- BlockCraft is **cube-only** — there is no sloped stair/slab block, so stairs
  are stepped `stonebrick` cubes (finer pitch = smoother steps).

The client is otherwise standard; webpack's dev server proxies `/socket.io` to
the Node server and sets the COOP/COEP headers the voxel worker needs.

## Verified

A socket.io probe (`join` → `requestChunk` → decode cells) confirms: ground
cells contain only flat bedrock/dirt/grass plus building blocks; cells away from
and above the ground are pure air (truly flat); door blocks appear in the
doorways; and the server logs `[bimmer] loaded building: <N> blocks (skipped 0
unknown)` (~169k at 1 m, ~1.0 M at 0.5 m).

## Notes / limits

- Resolution is chosen by which `blocks.csv` you pass to `setup_blockcraft.py`:
  - **1 m** (`out/unbc_1m/blocks.csv`, ~169k blocks) — light, fast, chunky steps.
  - **0.5 m** (`out/unbc_0p5m/blocks.csv`, ~1.0 M blocks) — finer steps/doors,
    but ~6× heavier to render and the building is 2× larger (spans z ≈ ±750), so
    the spawn point in the patch is tuned for it. Currently the live world uses
    0.5 m. BlockCraft is cube-only, so stairs are stepped `stonebrick` cubes
    (no sloped stair block exists in the engine); finer pitch = smoother steps.
- The building is large (≈218×376 m); fly/move around to load more chunks.
- `/blockcraft` is **tracked** in this repo (our fork). Only its `node_modules/`,
  `client/dist/`, `server/building.json`, and `server/saves/` are git-ignored
  (deps + generated). Helper scripts: `scripts/export_blockcraft.py`,
  `scripts/make_door_textures.py`, `scripts/setup_blockcraft.py`,
  `scripts/run_blockcraft.sh`.

## Licensing & our changes

BlockCraft is licensed under **Apache License 2.0**
([`blockcraft/LICENSE`](blockcraft/LICENSE)). This repo contains a **modified
fork** of it. Per the license, the original `LICENSE` is retained and our
modifications are noted here: world generation replaced with a flat world that
stamps the UNBC building; default game mode set to creative with an overhead
spawn and downward initial camera; and a custom functional flat **door** block
family (`door_x`/`door_z` + open variants) added across the server block
registry, client texture manager, voxel mesher, and player collision/interaction.
Upstream project: <https://github.com/ChiefElite/blockcraft-public>.
