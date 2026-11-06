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
| `docs/confirm_rectify_ground_floor.png` | and what it leaves behind, in the same drawing | level 311 before, after, and after with its remaining clashes ringed (REVITER §2g) |
| `docs/confirm_left_behind.png` | the hull leaves whole categories behind | walls and curtain panels that stayed put and now cross the rooms that moved (REVITER §2g) |
| `docs/confirm_elastic_seam.png` | making the seam elastic does not save the joins | 169,116 touching pairs scored under rigid and five band widths, against the grid share rectification exists for (REVITER §2j) |
| `docs/confirm_elastic_plan.png` | and the drawing says so without the numbers | level 694 as recovered, rectified rigidly, and with a 5 m elastic band — corridors smeared into ribbons |
| `docs/confirm_playable_map.png` | the export is a map you can walk into | minecraft-web-client, first person: the entrance spawn facing its door, four room signs (06-270 Kitchen, 04-329 Rotunda, 07-150, 10-B514 Hydrology Lab) and a corridor with the unified floor |
| `docs/confirm_contact_claim.png` | the contact claim clears the hull edge and moves the boundary | 638 findings against 580, by distance from the hull edge and by category — one decode, ONE wings file, one flag apart (`--no-contact`) |

`make confirm WORLD=out/unbc_1m` regenerates the voxel ones from a build. The
last four come from the plan side: `make rectify-plan` for the SVGs, then
again with `--no-contact` for the ablation half of the contact-claim figure,
and `parsers/reviter/scripts/render-svg.ts` to rasterise (cairosvg cannot —
the plan's `var(--plan-wall)` fills parse as hex).

## The three builds, side by side

At 1 m, after the fixes above:

| | Autodesk faithful | Reviter faithful | Autodesk `--rectify` | Reviter `--rectify` |
|---|---:|---:|---:|---:|
| interior reachable | 36,813 / 39,409 = 93.4% | 36,794 / 39,190 = 93.9% | 37,662 / 39,528 = **95.3%** | 36,607 / 40,534 = 90.3% |
| cut off (largest pocket) | 2,596 (362) | 2,396 (77) | **1,866 (293)** | 3,927 (724) |
| holes in the envelope | 841 | **535** | 508 | 803 |
| sees straight out | 603 (1.6%) | **533 (1.4%)** | 792 (2.1%) | 892 (2.4%) |
| stairwells / turning / ISOLATED | 47 / 19 / **0** | 47 / 14 / 1 | 47 / 18 / 2 | 45 / — / — |
| seam-wall cells | — | — | 1,056 | 1,063 |
| contact claims | — | — | 2,870 | 2,612 |

**`--rectify` is now the more walkable build — on the Autodesk export.**
It was 90.1% reachable against the faithful build's 93.4% and stranded a
1,376-cell region; claiming by contact what the hull could not reach took
it to 95.3% with a largest pocket of 293. That is the first time squaring
the wings has paid for itself on the measure it was always sold on.

**It does not carry over to the recovery, and that is the open question.**
The same two fixes take the reviter build only from 89.2% to 90.3%, and
it still strands 3,927 cells against 1,866. The contact claim fires
almost identically (2,612 against 2,870), so the difference is upstream:
`wall_plan` reads 7,462 walls from the recovery against 14,902 from the
export, so the hulls are looser. The stranded cells cluster around
(16–22, 238–250) m — the +32 degree wing whose pivot the two decoders
agree on to within a metre (REVITER §2e), so the wing is the same and its
boundary is not.

`--rectify` finds the **same six wings in both files** — four at +32
degrees, one at -58, one at -5 — reading per-element placements from the
export and footprints out of the tessellation from the recovery. The
16-rise switchback survives rectification in both. REVITER §2e has the
per-wing detail; `docs/confirm_rectify_both_decoders.png` is the two
rectified plans side by side.

## Playable in Java Edition, with room names (2026-11)

The world now opens in vanilla Java Edition as a map, not only in the web
client, and it says where you are. Three changes, each checked first-person
in minecraft-web-client and by `verify_save.js`:

```sh
make p1            # build (engine change below included)
make signs         # room signs from ../openindoormaps/public/data/unbc-plans
make world         # Anvil save + zip, verified; `make snapshot` updates Pages
```

**The save was not a vanilla map.** `level.dat` had no `WorldGenSettings`,
and the ground only covered the footprint plus 4 blocks, so the rest of
every edge chunk was empty down to y=-64. Vanilla never regenerates a chunk
saved `Status: full`, so those columns are holes to the void, permanently:
**96,000 columns** in the previous committed snapshot. `export_anvil.js`
now fills every column of every saved chunk (bedrock, stone, dirt, grass)
and writes a flat generator with the same layers, so the terrain past the
save meets it at the same height. It also writes gamerules for a static map
(no mobs, daylight cycle, weather, fire spread or griefing; keepInventory;
spawnRadius 0) and spawns the player on the ground at the widest exterior
door with terrain in front of it, facing it, instead of on the roof.
`verify_save.js` fails a world with void columns or without world-gen
settings, so the old snapshot now fails it.

**Rooms have names.** The IFC has no `IfcSpace`, but openindoormaps
publishes the UNBC visitor plans with 1,391 room labels. `room_signs.py`
joins the frames through the doors: the plans carry each door's Revit
element id as `nativeElementId`, the IFC carries it as `IfcDoor.Tag`, and
`doors.csv` says where each IfcDoor landed. **1,791 doors pair up; the
fitted affine map puts them within 0.52 blocks RMS at a scale of 1.000
blocks per metre**, which is the check that the frames really agree (the
script refuses a fit worse than 2 blocks). Each level's height comes from
its own doors. 1,356 waxed standing signs are placed on floors at their
labels, facing the nearest door; 34 corridor labels are skipped on purpose
and one label has no floor under it. Signs have no collision box, so no
corridor narrows.

*Trap:* prismarine-chunk's `loadBlockEntities` keys entities by `x >> 4`
(the chunk index) instead of `x & 15`, so reading the save back through
it collapsed 1,356 signs to 353. The write was right. `verify_save.js`
counts block entities from the raw region NBT for that reason.

**Floors were speckled with wall concrete.** Walls are modelled from the
slab bottom and outrank the floor class, so 17% of all standing surfaces
(5,578 cells) and as many ceiling cells were white concrete in a smooth-stone
floor. First person, that is the most visible flaw in the building.
`unify_plate_surfaces` recolours a wall/column cell that shows as a surface
(air above or below) when at least two of its four neighbours are plate
blocks. A wall run, its lintels and parapets keep their wall block. It
recoloured 17,900 cells and changes **material only**: 177,288 blocks
before and after, reachability identical (33,506 / 36,032), door, fence and
stair QA identical. Wall concrete on walking surfaces went from 5,578 to
2,914.

## Terrain, stairs, door signs and the 0.5 m build (2026-11, second pass)

Measured on the faithful build. "Flights" and "drops" are
`scripts/audit_flights.py`: every connected run of tread stand cells that
climbs >= 2 must touch walkable floor at both ends, and an indoor floor
edge should not drop the player onto bare world ground. Run it with the
other audits after any engine change.

| 1 m | before | after |
| --- | ---: | ---: |
| interior reachable | 33,506 / 36,032 = 93% | **34,526 / 36,678 = 94%** |
| stairwells not climbable | 2 / 37 | **1 / 37** |
| flights that dead-end at the top | 10 / 41 | **4 / 37** |
| indoor edges dropping to bare world ground | 187 | **30** |
| door QA (orphans / stepped) | 0 / 0 | 0 / 0 |
| signs | 1,356 | **2,224** (1,356 floor + 868 beside doors) |

**Terrain (`stamp_terrain`).** Grade is read off the building: a column
holding the lowest storey of its 5 m neighbourhood has ground right under
it, the outside cell of each exterior door has ground at its sill, and the
padded grid edge (16 m, `--terrain-margin`) sits on the export's flat
plane. A Laplace solve fills everything else, and no column is ever filled
up to its own lowest block, so earth cannot enter a room. A canopy over
lower ground is not taken as grade, so the space under it stays open
(self-test). Door aprons still run first, but no longer put cones under
floors: they had been building dirt mounds inside rooms. `--no-terrain`
turns it off.

**Stairs.** Two engine bugs and one new pass:

- `rebuild_blocked_stairs` merged assemblies into a well whenever their
  boxes overlapped on both axes. A monumental stair's box (18 x 12 m,
  29 x 16 m) overlaps any tower inside it, so the 4.4 m feature stair at
  (38-55, 38-50) m took a tower's height and was rebuilt as a 12-rise
  straight run into open air. Wells now merge only at plan IoU >= 0.25:
  real pairs score 0.39-1.00, the false ones scored 0.03-0.20.
- The rebuilt run only turned at the end of the well, so in a glazed tower
  with a landing every 3 cells it ran 6-rise flights straight past them
  and overshot the last one. It now also turns level with each landing
  plate in the well.
- `link_flight_tops` (new, last pass that moves cells): a flight whose top
  has no way off gets a straight 2-high passage, up to 3 m, to floor at the
  same height, one below or one above (with a stair). It never removes a
  door, cuts a slab someone stands on, pads a cell that is somebody's
  body or head room, or touches a stair's climbing envelope. **Every one
  of those guards was added because the build lost reachability without
  it**: one pad at (207,16,65) was the head room of a flight and cut
  2,602 cells of top-floor deck off. Diagonal passages are not used: the
  audit walks them, a player's box cannot. It links 1 flight at 1 m and 6
  at 0.5 m; the rest need more than a short straight cut.

**Door signs.** `room_signs.py` floods each labelled room's floor from its
label (own level, never through a door, capped at 600 m2) and hangs a wall
sign beside every door on that flood's edge, at eye height, facing out of
the room. 774 rooms at 1 m get no door sign because the engine sealed them
(no door survived rounding); at 0.5 m that is 506.

**0.5 m.** `make p05`, or `ifc_to_voxels.py --pitch 0.5`: **8.5 minutes,
2.76 M blocks** (1.75 M of them terrain), exported in 27 s to a 1.26 MB
zip, committed as `renderers/mcweb/unbc_world_0p5m.zip` (not deployed:
which one Pages serves is a product decision). What it took:

- **Out of memory in voxelization.** trimesh subdivides a whole class mesh
  before rounding a vertex. `surface_voxel_centers` voxelizes in face
  batches; the lattice is `round(v / pitch)` and subdivision is per face,
  so the result is identical (checked at 1, 0.5 and 0.25 m). It peaks
  around 3 GB. *Do not run two builds at once here*: the session limit is
  below what `free` reports, and the 0.5 m build was killed three times
  running beside a 1 m one.
- **The stitcher.** It re-walked every stand cell and re-labelled every
  island for each of up to 68 links: 40+ minutes and still going. It now
  extends the walk from each new corridor and re-walks every 8 links.
  Never re-walking was measured too, and lost 518 reachable cells at 1 m,
  because carving can remove a floor. 2.5 min at 0.5 m; at 1 m, 33
  corridors and 34,524 reachable against 34,523 before.
- **Cells that meant metres.** At 0.5 m `light_ceilings` looked 3-4 cells
  up for a ceiling in an 8-cell storey and would have lit nothing; door
  aprons, stitch span, island size and the room-sign search radius halved
  too. All scale with pitch now; player-scale numbers (headroom, steps)
  stay in blocks. Identical at 1 m.
- **An open transom over every door.** The doorway is cut 4 cells tall and
  a Minecraft door is 2, so every door had a 2-cell hole above it into
  the next room. `cap_stacked_doors` closes it with the wall beside it
  (7,070 cells).

| 0.5 m | |
| --- | ---: |
| interior reachable | **100,355 / 103,744 = 97%** |
| stairwells not climbable | 7 / 64 |
| flights that dead-end at the top | 26 / 67 |
| door QA | 0 orphans, 0 stepped; 5 sunk, 1 floating, 1 without threshold (1 m: 6 / 0 / 10) |
| lanterns / signs | 4,729 / 2,922 |

So 0.5 m is the more walkable building, with cleaner doors, and the worse
one for stairs: the rebuild lays one rise per cell, and the landing logic
was tuned at 1 m. That is the next thing to fix before deploying it.
First person it also shows doors standing in open facade at the top
storeys, sky behind them: the known "doors free-standing in glazed facades"
class, which halving the pitch does not fix.

## After ANY engine change, run this

```sh
.venv/bin/python scripts/ifc_to_voxels.py --self-test        # cap: light well vs terrace
.venv/bin/python scripts/inspect_interior.py --self-test     # pockets, holes, dog-leg climb
.venv/bin/python scripts/verify_blocks.py out/unbc_1m        # door/fence/stair QA
.venv/bin/python scripts/audit_walkability.py out/unbc_1m/blocks.csv   # per-well climb + reachability
.venv/bin/python scripts/audit_flights.py out/unbc_1m/blocks.csv       # dead-ended flights, drops to bare ground
renderers/mcweb/run.sh export out/unbc_1m/blocks.csv /tmp/w && node renderers/mcweb/verify_save.js /tmp/w
# or, signs included: make signs && make world
```

Several past bugs passed the metric that shared their own wrong
assumption and only fell to (a) a *different* metric or (b) walking the
world in the renderer. Do not skip the audit. Do not trust a pass that
verifies itself.

Run on the current `--rectify` build, after the piecewise wings, the seam
walls, the escape-based capping and the contact claim:

| check | result |
| --- | --- |
| `verify_blocks` | 4,190 door blocks, **0** orphan halves, **0** un-mirrored hinges, 1 stepped pair; 8 sunk and 9 floating of 2,095; 1,030 fences all with connection states; 554 stair blocks with 47 corner shapes |
| `audit_walkability` | **95%** interior reachable, 2 of 36 wells not climbable |
| Anvil round trip | 390 chunks; doors, stairs, fences and glazing all **PASS** |

`audit_walkability` and `inspect_interior` count different denominators
(34,742/36,542 against 37,662/39,528) and land on the same 95%. Two
metrics that do not share an assumption agreeing is the point of running
both.

The weak storey is the top one: y=16 is 70% reachable where every other
level is 87–98%.

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
- **The contact claim costs 161 holes to buy 2,608 reachable cells.**
  Envelope holes go 390 to 508 with `adjacency_claims` on. Traced cell by
  cell: 65 of the 161 new ones genuinely lost their cover — a small roof
  or floor piece the claim took with its wing — and 96 were never covered
  and changed classification. Against +2,608 reachable interior cells and
  −2,004 cut off, that is the trade, and it is worth taking. Levels 6, 7
  and 13 hold 109 of the 118 net increase.
- **The seam residue cannot be closed by extending walls, and the
  measurement that says so is cheap.** Extending a cut run along its
  own axis reaches its moved half in 6% of cases (median gap 25 m — a
  wing rotates, so its far end is carried away rather than cut).
  Gating the contact claim on the wall's angle family instead, which
  63% of the residue argued for, makes it monotonically worse:
  580 → 639 → 715 → 805 → 847 as the gated reach goes 9 → 30 m,
  because every element claimed beyond 6 m costs two to four broken
  joins. Both were built or probed, measured, and reverted (REVITER
  §2i, LESSONS S23).
- **And the non-rigid transform those pointed to is built, and also
  does not pay.** A smoothstep ramp across the hull boundary, summing
  each wing's displacement so the field is continuous everywhere and
  exact inside. Scored on all 169,116 pairs that touched in the source
  model, rigid hull-only breaks 2,229 and keeps 60.3% of wall runs on
  the grid; elastic breaks 5,009 → 14,158 as the band goes 5 → 80 m,
  with on-grid falling to 22.1% and p99 strain at 244%. Strain is the
  displacement over the band, and a wing's far end has moved tens of
  metres — so a strained join breaks just as a torn one does (REVITER
  §2j, LESSONS S24). **Kept behind `--band-metres`, off by default**,
  for the one question the plan cannot answer: a plate STRETCHED across
  a seam is walkable where a torn one is a canyon, so it may still buy
  reachable interior. That needs the transform ported into
  `ifc_to_voxels.py` and is the next thing to try.
- **Each wall was counted twice.** `by_type` returns subtypes, so
  `by_type("IfcWall") + by_type("IfcWallStandardCase")` doubled 7,381 of
  7,521 walls — the "14,902" in every document here. It moved no hull
  directly; it doubled `n`, the share-derived thresholds, and `overlap()`
  against a fixed shove penalty. Fixed and rebuilt: reachable 95.3% →
  **96.2%**, cut off 1,866 → **1,496**, nothing measurably worse.
- **The floor-plate gap was never real.** "94 slabs against 107" was a
  count of `IfcSlab` entities, which no two producers write the same way.
  Joined on `Tag` across the floor classes the recovery has 182 elements
  against Autodesk's 172, Revit Floors are 68/68, and 99.92% of standable
  surface is reproduced within half a metre. REVITER §3 severity high →
  closed.
- **An A/B whose halves came from different inputs.** The first
  contact-claim table paired a `--no-contact` run against a run with
  the flag on — and different `wings.json` files, one computed from the
  Autodesk IFC and one from the recovered IFC. Both are legitimate
  inputs, which is exactly why the mismatch was invisible: every number
  looked reasonable. Corrected, the improvement is 638 → 580, not
  638 → 526. An ablation has to name its fixed inputs, not just its
  flag.
- **A `ConvertResult` holds geometry in two frames 87 m apart, and which
  one you touch decides whether `origin` comes off.** `meshes` are
  written raw by `export-ifc.ts` with `origin` on the shared placement,
  so a hull computed from that IFC has to subtract it (`rectify-walk.ts`
  does); `elementBounds` — and the `solid`/`loops`/`boundsFeet` the plan
  is drawn from — already sit in the consumer's frame and must not
  (`rectifyForPlan` passes zero). Both mistakes have been made here,
  including "fixing" the plan path that was right and withdrawing a
  correct table for a day. Verify a frame against the model's own bbox,
  not against the drawing looking plausible.
- **A wing has to be inferred, so its boundary breaks joins somewhere.**
  The model carries no wing structure to use instead — one `IfcBuilding`,
  thirteen storeys, no zones and no element assemblies — so the wing is a
  hull of wall angles plus a contact claim, and joins break at whichever
  boundary is outermost. `adjacency_claims` moved that boundary from the
  hull edge (321 findings within 2 m of it) to its own reach (401 at
  5–10 m, 118 beyond). Widening `reach_m` moves it again; it does not
  remove it, and §2i/§2j measured two attempts to remove it instead.
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

1. **Stairs at 0.5 m**: 7/64 wells not climbable and 26/67 flights
   dead-ended, against 1/37 and 4/37 at 1 m. The rebuilt runs and
   `link_flight_tops` were developed on the 1 m build; walk the 0.5 m
   wells (coordinates from `audit_walkability.py`) before tuning.
   Then deploy 0.5 m: it is otherwise the better world (97% reachable).
2. **The last dead-ended flights and the one unclimbable 1 m well** at
   x 119-125, z 388-389 (a switchback squeezed into a 2-cell shaft whose
   landing rounded into wall mass). Per-well work; slice it with the
   column printer used for the ones above, it is quick to read.
3. **Rectify Phase 3** (see RECTIFY.md): per-storey wall-graph
   schematization with parametric opening replay. Research-grade;
   PRIOR_ART found no prior art — publishable.
4. **Light fixtures from IFC**: `light_ceilings` uses a blind 6 m grid.
   Checked: this export has **no** `IfcLightFixture`, `IfcFlowTerminal`,
   `IfcSpace` or `IfcTransportElement` at all, so lights and elevators
   need another source (the RVT through Reviter, or the plans).
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
