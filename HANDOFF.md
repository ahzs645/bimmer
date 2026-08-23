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

## Before trusting a rectified build, look at it

```sh
make rectify-preview IFC="<model.ifc>"
```

`docs/rectify_*_before_after.png` show this on UNBC already, but nothing
in the repo can regenerate them — they were committed with Phase 1 with
no generator. The wing computation reads wall placements only, so
`preview_rectify.py` runs the same function the engine runs and draws
the same comparison in seconds, on any IFC, after any engine change.
`--self-test` needs no model. RECTIFY.md.

## Walk it, don't just score it

```sh
make fixture           # build + convert a small test building, no model needed
make walk WORLD=out/fixture
```

Every other check here reports a number, and this file's own trap list
says why that is not enough: bugs pass the metric that shares their
assumption. `walk_voxels.py` renders what a player sees along the route
the audit would take — same movement model, from `walk_physics.py`, so
the walk and the score cannot disagree. A corridor that pinches shut is
visible in a frame and invisible in a percentage.

## Sweep the interior, not just one route

```sh
make inspect WORLD=out/fixture
scripts/inspect_interior.py out/unbc/blocks.csv --out out/inspect \
    --stair-views 4 --outside-views 4
```

Locates the cut-off pockets rather than counting them, separates the
roof from the interior (standing on the roof is standable and is not a
storey at 0% reachable), reports interior cells with open sky above
them, counts the interior cells that can see straight out sideways,
locates every place the player crosses from indoors to outdoors, and per
stairwell gives its position, its facing level by level, and how many
turns that adds up to.

**Say where, not how many.** Every number in that report was wrong once,
and in each case the count looked reasonable while the location gave it
away:

- *"22 stairwells bend"* counted any well holding two facings, including
  two straight flights that happened to touch. Read level by level, the
  real building has 21 wells that turn as they rise (one of them four
  half-flights through 16 levels) and 3 that hold two facings on a
  single landing.
- *"29,076 outdoor cells are reachable"* is a symptom with no fix
  attached. Clustered into crossings it is 191 openings, and the largest
  is 104 stand cells wide — a floor plate running out past its wall line,
  not a hole someone left in a door.
- *"holes in the envelope"* came from a rule that called a cell a hole
  when 3 of its 8 neighbouring columns were taller. That is also true of
  every cell along the foot of a tall wing, and the first-person views of
  those "holes" showed the campus and the horizon. See below.
- *"~2-3 stairwells per build are flagged ISOLATED, their floors served
  by seam corridors"* was a guess, and wrong for the one that was looked
  at. The 12-rise flight at (179, 228) had no landing plate at either
  end: every tread standable, the bottom tread's only neighbour the tread
  above it, and nothing but sky when you looked east off it. Its landings
  had been left behind by `--rectify` (see below); they are back, and it
  is connected (`docs/confirm_isolated_stair.png`).
- ***"I see a lot of gaps where I'm not supposed to see any."*** Nothing
  in the report said so. Instrumenting the raycaster (`render(...,
  diagnose=True)` returns why each pixel is empty) said 10.7% of interior
  stand cells in the rectified build had a clear horizontal line out of
  the model, against 1.6% in the faithful one. That is now the
  `see_through` check, and finding its cause is the next section.

## Why a rectified build leaked: walls turned, floors did not

`--rectify` rotates an off-grid wing onto the world grid. `wing_for_point`
decided which wing an element belonged to **from the element's centroid**.
That is right for a wall — small, wholly one side of the seam — and wrong
for a floor slab, which spans the wing AND the spine, so its centroid sits
outside the hull. Measured per hull on the real model:

| | walls that rotate | plates that rotate |
| --- | ---: | ---: |
| wing 0 (+32) | 91% | 25% |
| wing 2 (+32) | 90% | 50% |
| wing 4 (+32) | 98% | 59% |
| wing 5 (-5) | 97% | 25% |

So a wing's walls swung 32 or 58 degrees away and the floor they stood on
stayed exactly where it was: storeys of bare plate with no wall anywhere
in the column, and the wing's walls landing in the middle of somewhere
else. Wall voxels fell 3.4% and glass 5% while floor voxels did not move.

A rigid motion applied to a REGION has to cut whatever crosses the
region's boundary. `apply_wings_piecewise` does that on the triangles: a
mesh whose vertices disagree about their wing is subdivided below half a
metre and each triangle then goes wholly with the wing its own centroid
falls in. Aggregates (a stair and its flights, stringers and railings)
still move whole — half a stair placed correctly is worse than a whole
one placed loosely.

| | before | after |
| --- | ---: | ---: |
| interior cells that can see straight out | 4,240 (10.7%) | **1,145 (3.2%)** |
| largest such cluster | 718 cells | **159** |
| holes in the envelope | 1,828 | **390** |
| floor holes the patcher had to fill | 1,840 | **675** |
| columns `cap_envelope` had to roof | 1,513 | **642** |
| stairwells ISOLATED | 2 | 2 (one of them a different, smaller well) |

Two things got slightly worse and are not hidden: reachable interior
share 90.4% to 90.1%, and the largest stranded pocket 1,143 cells to
1,381.

### And then the canyon it leaves

Cutting the plate cleanly does not make the gap go away — the gap is the
point, because the wing really has moved. What is left is two exposed
plate edges metres apart with nothing standing on them. Compared against
the faithful build cell by cell, **707 of the rectified build's 1,125
see-through cells are not open in the faithful build**, and they sit
within 4 m of a wing hull four times as often as interior cells do
generally. That is the seam.

`close_seam_walls` closes it: an exposed plate edge inside the seam band,
with headroom above it and a ceiling not far overhead, gets a wall from
the plate up to that ceiling. It runs after the stitcher (corridors have
to be cut before anything is built across them) and keeps two cells clear
of every corridor.

| | rectified | + seam walls |
| --- | ---: | ---: |
| sees straight out | 1,125 (3.2%) | **845 (2.4%)** |
| of those, NOT open in the faithful build | 707 | **431** |
| openings the faithful build also has, kept | 418 | 414 |
| interior cells | 35,034 | 34,644 |
| reachable share | 90.1% | 90.1% |
| cut off | 3,898 | 3,870 |
| entrance cells | 2,216 | 2,213 |
| cells walled | — | 1,147 |

`docs/confirm_seam_walls.png` is every one of those 1,147 cells in plan:
they trace the wing hull boundaries and nothing else.

**Two stricter rules were tried on the real model and rejected on their
numbers, so do not re-try them blind:**

| rule | leaks closed | what it cost |
| --- | ---: | --- |
| wall every indoors/outdoors boundary in the band | 398 | 9,842 cells written, 2,500 interior cells stranded, **326 entrances sealed** |
| the same, plus skip glazed and door columns | 373 | 3,492 written, 1,150 more cells cut off |
| count stair and structure mass as floor too | +43 | 1,000 more cells cut off |

Where the floor runs on past its roof, the missing skin is the source
model's business. A room is worth more than a metric.

### What is still open, and why

Of the 431 leaks the seam walls do not close, traced ray by ray to the
plate edge each one escapes over:

| | share | |
| --- | ---: | --- |
| escapes over a plate edge outside the seam band | 40% | 12–56 m from any hull; widening the band means having no band |
| no floor plate under the ray at all | 32% | the cell stands on something that is not a storey plate |
| in the band, but no ceiling above the edge | 26% | the top storey, where there is no ceiling to wall up to |
| in the band with a ceiling — the rule should have fired | 3% | corridor halo, most likely |

The faithful build is still the cleaner world on see-through (1.6%
against 2.4%), so rectification has not stopped costing anything — it has
stopped costing most of the envelope.

## Roof or hole: decide it by escape

Both are "a standable cell with open sky above it", and the same
question is asked in two places — `inspect_interior.outdoors_by_escape`,
and `ifc_to_voxels.cap_envelope`, which ADDS roof over what it decides
is a hole. Two rules were tried and both broke on the real building:

| rule | breaks on |
| --- | --- |
| by level — a level is outdoors when most of it is exposed | stepped roofs; called 53% of the interior holes |
| by neighbour count — exposed, but 3 neighbouring columns are taller | the foot of every tall mass; roofed over open terrace |

The rule now is **escape**: flood horizontally at the cell's own level
through columns that nothing covers, and ask whether that flood reaches
the edge of the model. Outdoors means you could leave without going
under a roof. A light well cannot; a terrace beside a tower can. Both
shapes are in `scripts/ifc_to_voxels.py --self-test`.

On the real building the old rule capped **5,330** columns in the
faithful build and **6,660** in the rectified one; the escape test caps
**887** and **1,513**. The rest was roof invented over open terrace,
with ceiling lanterns recessed into the underside of it.

## The claims, confirmed by looking

Every one of these was a number first, and looking changed two of them:

| figure | claim | what the frame shows |
| --- | --- | --- |
| `docs/confirm_stairs_bend.png` | stairwells turn as they rise | a 16-step switchback at (195, 51) walked end to end: x reverses at y=5 and y=10, y never drops |
| `docs/confirm_envelope.png` | roof, hole, and the step between | sky in a box (a hole), a light well, and the crossings the player walks out through |
| `docs/confirm_see_through.png` | you could stand indoors and see the horizon | three columns of bare floor plate, and the same build after the fix above |
| `docs/confirm_isolated_stair.png` | one flight was unreachable, and why | its bottom tread looking east at open sky, then the same flight with its landing back |
| `docs/confirm_reviter_same_stair.png` | the clean-room decoder recovers the same building | that switchback walked in both worlds; 11 of 16 stand cells are the same cell (REVITER §2d) |
| `docs/confirm_seam_walls.png` | the seam walls close the tear and nothing else | all 1,147 added cells in plan, tracing the wing hull boundaries |
| `docs/confirm_rectify_both_decoders.png` | rectification is not an artefact of one exporter | the same six wings squared, from Autodesk placements and from recovered tessellation (REVITER §2e) |
| `docs/confirm_rectify_floor_plan.png` | the wings square up as a *building*, not as sticks | level 694 before and after, drawn by Reviter's own floor viewer (`make rectify-plan`, REVITER §2f) |

`make confirm WORLD=out/unbc_1m` regenerates all of them from a build.

## The three builds, side by side

At 1 m, after the fixes above:

| | Autodesk faithful | Reviter faithful | Autodesk `--rectify` | Reviter `--rectify` |
|---|---:|---:|---:|---:|
| interior reachable | 36,813 / 39,409 = 93.4% | 36,794 / 39,190 = **93.9%** | 35,054 / 38,924 = 90.1% | 35,327 / 39,601 = 89.2% |
| cut off (largest pocket) | 2,596 (362) | **2,396 (77)** | 3,870 (1,376) | 4,274 (779) |
| holes in the envelope | 841 | 535 | **390** | 526 |
| sees straight out | 603 (1.6%) | **533 (1.4%)** | 845 (2.4%) | 949 (2.7%) |
| outdoor reachable / openings | 29,760 / 182 | 26,310 / 149 | 29,770 / 169 | **23,421** / 171 |
| stairwells / turning / ISOLATED | 47 / 19 / **0** | 47 / 14 / 1 | 47 / 18 / 2 | 45 / 12 / 3 |
| seam-wall cells | — | — | 1,147 | 935 |

`--rectify` finds the **same six wings in both files** — four at +32
degrees, one at -58, one at -5 — reading per-element placements from the
export and footprints out of the tessellation from the recovery. The
16-rise switchback survives rectification in both. REVITER §2e has the
per-wing detail; `docs/confirm_rectify_both_decoders.png` is the two
rectified plans side by side.

## After ANY engine change, run this

```sh
.venv/bin/python scripts/ifc_to_voxels.py --self-test        # cap: light well vs terrace
.venv/bin/python scripts/inspect_interior.py --self-test     # pockets, holes, dog-leg climb
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
- **Stairwells flagged ISOLATED**: 0 in the faithful build, 1 with the
  recovered IFC, 2 with `--rectify`. The one that was walked — a 12-rise
  flight with no landing at either end — turned out to be the wing/plate
  defect above and is connected now; its top landing is still thin (that
  stand cell has one neighbour). Whatever remains is per-well base
  linkage at extract time; low value.
- **`--rectify` still leaks half again as much as the faithful build**:
  2.4% of interior cells can see straight out against 1.6%. The
  per-triangle assignment and `close_seam_walls` between them took the
  rectification-caused half of that from 707 cells to 431; the residual
  is broken down by cause in "What is still open, and why" above. Two
  stricter wall rules were measured and rejected there — read that table
  before writing a third.
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
- **The conversion was not reproducible**: `ifcopenshell.geom.iterator`
  yields shapes in thread-completion order, and several decisions here
  are taken by whichever element arrives first. Under load at
  `--threads 8`, two runs of one file differed by 129 of 6,058 blocks
  and one of them dropped a whole storey from 99% reachable to 0%.
  `extract()` now drains the iterator and processes in element order.
  Any new pass that depends on arrival order will reintroduce this.
- **Stats can share the bug's blind spot**: the door-hoisting bug passed
  its own metric. Verify with the independent audits + in-client walks.
- **`blocks_by_id` cannot see a misclassification that overlaps**: a
  stringer wrongly classed as curtain-wall frame occupies the same
  cells as its flight at 1 m, and `CLASS_PRIORITY` puts stair above
  frame, so the block histogram is byte-identical either way.
  `solid_faces_by_class` and `per_class_voxels` in summary.json show
  it. Measured on the stair-aggregate fix; see REVITER.md §3.
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
- **Not every producer writes per-element placements**: Reviter puts
  every product on ONE shared placement with world coordinates in the
  geometry, so anything reading `IfcLocalPlacement` sees the whole
  building at one point at zero degrees. That made `--rectify` a
  silent no-op on the RVT path — 100% axis-aligned, zero wings,
  success reported. `rectify.wall_plan` now falls back to footprints.
  Assume any other placement-based pass has the same hole.
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
