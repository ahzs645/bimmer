# Assumptions the IFC → Minecraft conversion makes

Every conversion decision below is an assumption about the building or the
IFC file. When one is violated you get a *specific, recognizable* artifact —
listed here with the mitigation. Companion docs: [`LESSONS.md`](LESSONS.md)
(how each fix was found), [`PIPELINE.md`](PIPELINE.md) (how the engine works),
[`RECTIFY.md`](RECTIFY.md) (the plan-conformance proposal).

The governing principle (borrowed from Autodesk VASA and the voxel
escape-route literature): **at 1 m/block you are far past the resolution
where geometry alone preserves walkability** — published studies find
vertical links (stairs/steps) stop surviving voxelization above ~25 cm
cells. So anything the player *walks on or through* must be driven by IFC
semantics (sill heights, stair parameters, aggregation), with geometry only
as a tie-breaker.

## Units & grid

| # | Assumption | If violated | Mitigation |
|---|---|---|---|
| U1 | IfcOpenShell geometry arrives in **metres** (it normalises mm files) | Model 1000× too big/small | `inspect_ifc.py` prints the coordinate scale; checked once per model |
| U2 | One shared integer lattice for all classes; cell ownership resolved by `CLASS_PRIORITY` | Overlapping elements flicker per-run | Priorities are fixed and semantic: glass < railing < frame < roof < floor < structure < wall < stair |
| U3 | **1 block = 1 m** (default pitch) and the player is 2 blocks tall, steps up 1, jumps 1.25 | Nothing breaks, but every rounding artifact below scales with pitch | `--pitch 0.5` halves every artifact class at ~6× the block count |
| U4 | The building's dominant axes are grid-aligned | Walls/corridors at other angles voxelize as jagged staircase lines; rooms shrink/merge unpredictably | Measured on UNBC: 65 % of walls axis-aligned, 24 % at 58° (one whole wing) — see `RECTIFY.md` |

## Floors & walls

| # | Assumption | If violated | Mitigation |
|---|---|---|---|
| F1 | A floor slab is **≥ 1 voxel thick** after rounding; any slab 0.1–1.5 m thick becomes exactly 1 block (thicker slabs 2) | Landings 2 cells thick swallow door bottoms (lesson D9); split-level floors 30 cm apart merge into one level | Door sill probe scans ±2 cells; `--floor-slabs` turns thin plates into half-slabs |
| F2 | A wall is **≥ 1 voxel thick** after rounding; a 20 cm partition and a 90 cm shaft wall are both 1 block | Two parallel walls < ~1.5 m apart **merge into solid mass** — small closets/shafts/risers disappear entirely | Detected but not invented: ~256 doors on UNBC lead into swallowed rooms; they stay as "closet doors that don't open" (see D-list below) |
| F3 | Rooms the player should enter are **≥ ~1.5 m wide** in plan | Room voxel-plugs shut; its door becomes a dead-end | `unblock_door_passages` re-opens plugs ≤ 3 m deep when a real room exists behind |
| F4 | Storey floors are horizontal | Sloped slabs become 1-block terraces | Acceptable; ramps are on the backlog (synthesize stair runs from IfcRamp) |
| F5 | Stairwell slab openings are **≥ 1.5 m** across | The hole rounds **shut** and the stairwell dead-ends into a ceiling (this disconnected every upper floor of UNBC, lesson S8) | `carve_stair_headroom` pops floor/roof cells 1–3 above every stair block |

## Doors (all must hold for a door to convert cleanly)

| # | Assumption | If violated | Mitigation |
|---|---|---|---|
| D1 | The IFC **sill height is authoritative** for the door's floor level | Probing "nearest walkable surface" hoists facade doors onto roof decks (~1,200 on UNBC before the fix) | Anchor = walkable surface *closest to sill*, capped at ±2 cells |
| D2 | The door sits **in a wall** — solid cells flank the opening | Curtain-wall doors have glazing thinner than a voxel beside them → "free-standing door" in a gap (the **outside doors that look wrong**: 115 on UNBC, all in open/diagonal glass facades) | Glazing anchor pass pulls glass down/bridges ≤ 3 cells; the rest are pin-able via `--overrides`; a real fix needs finer pitch or rectification |
| D3 | The wall normal is detectable — one side of the door has **open room cells at sill level** | Deep-framed doors guess the wrong axis and place a storey too high | Openness probing decides the axis; mesh extents only break ties |
| D4 | The opening is **≥ 1 cell wide and ≥ 2 tall** at the chosen pitch | Door can't fit; opening plugs | At 1 m a standard 0.9×2.1 m door is exactly 1×2 — the design point of the whole pipeline |
| D5 | `OverallWidth` tells the leaf count (≤ 1.2 m single, else double) | Missing attribute → geometry fallback may split badly | Overrides: `{"leaves": 2}` |
| D6 | Each door serves **its own opening** | Multiple IfcDoors stacked in one opening corrupt the column (`lower,lower,upper`) | One shared bottom per opening + unpaired-half cleanup |
| D7 | Adjacent doors on one wall at the same IFC sill are on **one floor** | Independent probes step them 1 apart | Harmonization: same-wall same-sill doors snap to one bottom |
| D8 | A row of adjacent leaves is **one bank** and hinges mirror pairwise | Un-mirrored double doors | Hinge mirroring over runs. **NB the door banks you noticed** (runs of 3, 4, even 6 leaves on UNBC) are *real*: entrance vestibules with several IfcDoors side by side each contributing leaves. 6 leaves in a row is the correct conversion of a 3-double-door entrance bank — odd-looking but faithful. Genuine merge errors would show as odd leaf counts or stepped runs; QA counts both (currently 0) |
| D9 | There is **floor on both sides** of the passage | Exterior doors at slab edges open onto a drop — the model has **no site terrain**, so ground-floor exits face 1–3 m of air (more of the **outside doors that don't make sense**: ~147 on UNBC) | Left as-is deliberately (pads would float); fix belongs in a terrain-stamping pass |
| D10 | The passage behind the leaf is **≤ 3 m of carveable solid** (wall/floor/structure) before open space | Door opens onto bare concrete | `unblock_door_passages` carves through; runs deeper than 3 m are treated as swallowed rooms (F2/F3) |

## Stairs & railings

| # | Assumption | If violated | Mitigation |
|---|---|---|---|
| S1 | Stair walkability comes from **IfcStair semantics**, not voxelized treads (a 1 m voxelized flight has 1 m risers — unclimbable by definition) | Jumpy blobs, blocked shafts | Oriented stair blocks with vanilla corner shapes; spiral synthesis from measured parameters; switchback rebuild for failed wells |
| S2 | A stairwell is climbable iff a path of stand-cells exists with **Δy ≤ +1 per move, 2-air headroom, and jump clearance when the step is a full cube** (vanilla physics) | The climb test passes something the game blocks — this is exactly how the terraced lobby stair and the wall-pierced fire-escape runs shipped broken | The rebuild trigger and its post-verify now use these rules; rebuilt-run headroom clears walls/structure too |
| S3 | Scissor/dog-leg pairs share one shaft | Two rebuilds fight over one well | Overlapping assembly bboxes merge into one well before the climb test |
| S4 | Railings are ~1–1.2 m guardrails | 2-high fence walls; fence towers on treads | Fences collapse to 1 block, never stand on stair blocks, and carry explicit arm states |
| S5 | `IfcMember` inside a stair assembly is a stringer, not a mullion | Stringers render as curtain-wall concrete | Disambiguated by `Decomposes` aggregation |

## What is *not* assumed / out of scope

- **No terrain**: the IFC has no site surface we trust; the world floats on a
  flat ground plane. Exterior doors therefore face air (D9).
- **No lighting**: no IfcLightFixture mapping yet; interiors are dark under
  vanilla lighting (use `&setting=enableLighting:false`).
- **No elevators**: `IfcTransportElement` maps to "other" (solid shaft).
- **Watertightness**: never assumed — surface voxelization only, `--fill` off.

## The audit trail

Every assumption has a check: `scripts/verify_blocks.py` (doors, fences,
stair states), the dead-end door probe, and the stairwell climb audit
(vanilla-physics BFS per well + global reachability from the entrance).
Run them after any pipeline change — several past bugs were invisible to
the metric that shared their assumption and only fell to the in-client walk.
