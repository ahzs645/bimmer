# HANDOFF — state of the project and what to do next

Written for the next contributor (human or AI) to pick this up cold.
Read this first, then the doc it points to for any area you touch.

## What this repo is

An IFC (BIM) → Minecraft pipeline, hardened end-to-end on the UNBC campus
model (83 MB IFC2X3, ~41k elements, 13 storeys, 1 m/block), plus two
browser renderers deployed as a static GitHub Pages site. The engine is
**one file**: `scripts/ifc_to_voxels.py`. Everything else is QA, export,
renderers, and docs.

| doc | read it when |
|---|---|
| `PIPELINE.md` | you need to understand or run the conversion |
| `LESSONS.md` | you hit a weird artifact — it is probably catalogued (D1–D12 doors, S1–S14 stairs/floors/terrain) with root cause and fix |
| `ASSUMPTIONS.md` | you wonder whether behaviour X is a bug or a documented 1 m-voxel tradeoff |
| `RECTIFY.md` | anything about `--rectify` (wing rotation, push-apart, seam stitching, collision numbers) |
| `PRIOR_ART.md` | before researching alternatives — surveys exist for IFC→Minecraft, voxel escape-route analysis, and hole-filling |
| `REVITER.md` | anything about where the IFC comes from: the RVT→IFC→voxel transformation chain, the engine's input contract, and the Reviter (licence-free RVT decoder) join |
| `RENDERERS.md` / `BLOCKCRAFT.md` | the two web renderers and the Pages layout |

## Where the IFC comes from

`parsers/reviter` is a pinned git submodule holding **the parser** — a
clean-room RVT decoder — and this repo is **the interpreter**. They meet
at a file-level contract, not at code: `scripts/rvt_to_ifc.py` runs the
parser's CLI and grades its output with `check_ifc_contract.py`, and
nothing here imports its TypeScript. `make parser-setup` once, then
`make rvt RVT=...` runs the whole chain from the proprietary format.

Both halves are verifiable without the 67 MB model
(`rvt_to_ifc.py --self-test`, `check_ifc_contract.py --self-test`); what
neither establishes is whether the parser recovers *this* building
correctly. REVITER.md §3 is the measured answer, and its §4 is the
ranked work.

## The two builds

```sh
make setup                                  # once: .venv + deps
.venv/bin/python scripts/ifc_to_voxels.py <model.ifc> --pitch 1.0 --out-dir out/unbc_1m            # faithful
.venv/bin/python scripts/ifc_to_voxels.py <model.ifc> --pitch 1.0 --rectify --out-dir out/unbc_1m_rect  # rectified
```

Current audited state (2026-07, `scripts/audit_walkability.py`):

| | faithful | `--rectify` |
|---|---|---|
| interior floor reachable from entrance | 93 % | **96 %** |
| stairwells that climb + connect | 34–36 of ~36 (2–3 metric-flagged wells are corridor-served) | same |
| doors | 4,170 blocks, 0 orphans, 0 stepped | 4,212, 0 orphans, 1 stepped |
| wing walls clipping the spine | n/a | 27 (source IFC itself interlocks; was 104 rotation-only) |

**The deployed Pages site ships the faithful build.** Switching to
rectified is a one-line change in `.github/workflows/pages.yml`
(which snapshot gets committed) — it is a product decision, since
rectification visibly moves wings.

## Before converting an IFC you have not converted before

```sh
python3 scripts/check_ifc_contract.py <model.ifc>
```

The engine reads IFC *facts* — product class, `OverallWidth`, stair
aggregation, host relationships — and every one of them fails silently
when absent: the conversion succeeds and the building is subtly
unwalkable. This gate names what is missing and what it will do to the
world, in seconds, without meshing. `--self-test` runs it against
synthetic producers with no model needed. See REVITER.md.

## After ANY engine change, run this

```sh
.venv/bin/python scripts/verify_blocks.py out/unbc_1m        # door/fence/stair QA
.venv/bin/python scripts/audit_walkability.py out/unbc_1m/blocks.csv   # per-well climb + reachability
renderers/mcweb/run.sh export out/unbc_1m/blocks.csv /tmp/w && node renderers/mcweb/verify_save.js /tmp/w
```

Several past bugs passed the metric that shared their own wrong
assumption and only fell to (a) a *different* metric or (b) walking the
world in the renderer. Do not skip the audit. Do not trust a pass that
verifies itself.

## Known residuals (all measured, none blocking)

- **Door oddballs** (inherent to 1 m voxels, all pin-able via
  `--overrides` + `out/<name>/doors.csv`): ~72 doors entombed in wall
  mass (invisible in game), ~250 closet doors whose room was swallowed
  by wall rounding, ~100 free-standing in glazed facades, a handful of
  exterior doors still facing drops where no apron fit. A curated
  overrides JSON for the UNBC model would zero the visible ones.
- **~2–3 stairwells per build flagged ISOLATED** by the well metric —
  their floors are served by seam corridors; nothing player-relevant is
  behind them. Fix would be per-well base linkage; low value.
- **Rectified build**: 27 wing walls still clip the spine (the source
  model interlocks there — no rigid motion can separate them), and 1
  stepped door pair near a seam.
- **Roof-deck**: remaining plenum holes are covered with glass; if you
  dislike the look, the alternative is accepting open holes (the solid
  fill is forbidden — it crushes the space below; see LESSONS S12).

## The backlog, ranked (each is a self-contained project)

1. **0.5 m pitch deploy** (`make p05` already builds it): the
   industry-standard remedy (see PRIOR_ART addendum — Recast) that
   shrinks EVERY rounding artifact class at ~6× block count. Needs: the
   post-passes profiled at 1M cells (they are pure Python; the
   walkability BFS and stitcher will need either patience or numpy/
   scipy vectorization), a heavier world.zip, slower first page load.
2. **Real terrain** (aprons are a v1): stamp a continuous ground
   surface by interpolating heights from all ground-door sills and the
   building underside, instead of per-door cones. Also fixes
   BlockCraft's floating corners (its flat world starts at a fixed y).
3. **Rectify Phase 3** (see RECTIFY.md): per-storey wall-graph
   schematization with parametric opening replay. Research-grade;
   PRIOR_ART found no prior art — publishable.
4. **Light fixtures from IFC**: `light_ceilings` uses a blind 6 m grid;
   the model may carry `IfcLightFixture`/`IfcFlowTerminal` instances
   whose positions would place lanterns where the real lights are.
5. **Elevators**: `IfcTransportElement` currently voxelizes as "other"
   (solid). Map shafts to open cavities with ladders (or vine/water
   columns) between storey doors.
6. **Curated UNBC overrides file** to zero out the last door oddballs
   (mechanism exists; someone just needs to walk the list in
   `doors.csv` and write the JSON).
7. **Pin the minecraft-web-client release** in `pages.yml`
   (currently `releases/latest` — a breaking upstream release would
   break the site on next deploy).
8. **CI**: run `verify_blocks` + `audit_walkability` on a small fixture
   IFC in GitHub Actions (the TU Wien escape-route test models are
   freely licensed fixtures — see PRIOR_ART). `check_ifc_contract.py
   --self-test` needs no fixture at all and can go in today.
9. **Key `--overrides` on `Tag`, not just `GlobalId`** (REVITER.md §4).
   Two IFC producers derive GlobalIds their own way, so a curated
   overrides file matches nothing in another export of the same
   building; both write the Revit element id into `Tag`. This is what
   turns item 6 into an asset instead of a per-file artifact.
10. **Join with Reviter** — a licence-free RVT→IFC4 path, and with it
   the experiment neither project can run alone: the same building
   through the same voxel engine from a native recovery and from
   Autodesk's own exporter, differing only where the recovery differs.
   Ranked plan in REVITER.md §4.

## Traps that bit us (do not rediscover these)

- **Packed-key aliasing** (LESSONS S13): `key(x,y,z) = x + X·y + plane·z`
  silently wraps out-of-bounds coordinates onto real cells. Every grid
  walker MUST bounds-check. This cost a 16 GB OOM hunt.
- **Filling "holes" can destroy walkability below** (S12): any pass that
  adds cells must check the 2-cell headroom under it.
- **Saved worlds never run neighbour updates**: fence arms, stair
  shapes, door halves/hinges must be baked into block states at export.
- **Stats can share the bug's blind spot**: the door-hoisting bug passed
  its own metric. Verify with the independent audits + in-client walks.
- **In-client screenshot harness quirks**: only the FIRST teleport per
  page session is reliable (use one browser context per viewpoint), the
  player can fall through unloaded chunks at distant teleports (wait
  20 s+, spawn 1–2 blocks high), and `bot.look(yaw, pitch, true)` +
  `&setting=enableLighting:false` for inspection shots. Headless
  Chromium here cannot reach the internet — serve mcraft.fun through a
  local curl-backed relay (see RENDERERS.md).
- **Attribute names drift between IFC schemas**: `IfcStair`'s shape
  enum is `ShapeType` in IFC2X3 and `PredefinedType` in IFC4, and
  reading one spelling fails *silently* on the other — `getattr`
  returns None and spiral synthesis just stops. The UNBC export is
  IFC2X3; Reviter writes IFC4. Assume this is not the only rename.
- **This dev container rolls back between sessions**: `git fetch +
  checkout -B <branch> origin/<branch>` before touching anything, and
  push every finished step. `out/` is disposable; rebuild it.

## Web deployment (GitHub Pages)

`.github/workflows/pages.yml` publishes on push to `main` (or manual
dispatch, Settings → Pages → Source: GitHub Actions):

- `/` — minecraft-web-client (real vanilla renderer) auto-loading the
  committed `renderers/mcweb/unbc_world.zip`, no menu, debug HUD off.
- `/blockcraft/` — the serverless BlockCraft fallback built from
  `blockcraft/` + committed `building.json`.
- `/choose/` — a small landing page linking both renderers.

The committed snapshots ARE the deploy inputs (CI never sees the IFC):
after an engine change, regenerate both — world.zip via
`renderers/mcweb/run.sh export` + zip, building.json via
`scripts/setup_blockcraft.py` — and commit them.

## Local dev server (root)

```sh
npm run dev          # or: pnpm dev / node tools/dev-server.js
```

Serves a chooser page on http://localhost:8080 with both renderers:
minecraft-web-client is proxied from mcraft.fun with the local
world.zip injected (works in offline-ish sandboxes via curl), and
BlockCraft is served from `blockcraft/client/dist` (build it once with
`scripts/build_blockcraft_static.sh`). No dependencies — plain Node.
