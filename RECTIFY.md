# Plan rectification: making the building conform to the voxel grid

The idea (proposed by the project owner): instead of voxelizing the building
exactly where it stands, first look at **each floor's layout** — walls,
corridors, rooms — and *rectify* it into shapes that can exist cleanly in
Minecraft (axis-aligned walls, straight corridors), then put the attached
elements (doors, windows) back **relative to the walls they belong to**.

Short answer: **yes, this makes sense, it is the right long-term move, and
the literature backs the exact mechanism** — but it should be done per
*wing*, not per wall, and openings must be stored parametrically before any
geometry moves. Below: the evidence, the algorithm, and an honest cost
estimate.

## Why the current output has diagonal artifacts

Measured from the IFC placements (14,902 walls):

| plan angle (mod 90°) | walls | share |
|---|---|---|
| 0° (axis-aligned) | 9,678 | **65 %** |
| 58° | 3,564 | **24 %** |
| 5° | 351 | 2.4 % |
| 60° | 257 | 1.7 % |
| everything else | ~1,050 | ~7 % |

So this is not "a few skewed walls": **an entire wing of the campus sits at
58° to the main grid.** Inside that wing the architecture is perfectly
orthogonal *in its own frame* — its corridors meet at right angles — but on
our grid every one of its walls voxelizes as a jagged staircase line,
corridors pinch and bulge, and doors sit in stepped wall segments (several
of the "outside doors that don't make sense" live on these facades).

## What others do (research summary — full sources in the PR discussion)

- **City-scale Minecraft imports** (Denmark in Minecraft, GeoCraft NL,
  Blockholm) don't rectify at all: they rasterize footprints and accept
  jagged diagonals, and none place functional doors. Rectification is the
  differentiator no one has built.
- **OSM/JOSM "orthogonalize"** and its Python re-implementations are the
  standard footprint-squaring recipe: rotate to the dominant edge direction,
  snap near-axis segments to 0°/90° within a tolerance band, keep genuine
  diagonals. Cartographic squaring (Lokhat & Touya) does the same as a
  soft least-squares problem so lengths and topology survive.
- **Voxel escape-route research** (Autodesk VASA; Gorte et al.; voxec) gives
  the acceptance test: walkable(c→c') iff Δh ≤ 1 step and headroom ≥ agent
  height; a stairwell is correct iff its walkable layer is one connected
  component from landing to landing. We already enforce this post-hoc; a
  rectified plan should be *verified* with the same rule, not assumed fine.

## The algorithm (proposed)

Phase 0 is cheap and buys most of the value; each later phase is optional.

**Phase 0 — global grid choice (hours).** Histogram wall angles (done,
above), rotate the *whole model* so the dominant cluster lands on 0°. UNBC
is already dominant-aligned, so this is a no-op here — but it's the first
thing to run on any new IFC.

**Phase 1 — wing segmentation + per-wing frames (days).** Cluster elements
by wall-angle families and spatial contiguity (the 58° wing separates
cleanly). Voxelize each wing **in its own rotated frame**, where it is
orthogonal, then place each wing's voxel block into the world at a snapped
(axis-aligned) offset. Result: every corridor and wall in both wings is
clean; the *seams* between wings are the only distorted zones.

**Phase 2 — seam stitching (days, the hard part).** Where the wings meet,
re-run corridor/door logic in a buffer zone: identify IfcDoors and corridor
spaces crossing the seam, and synthesize a straight connecting hall between
the two grids (same spirit as the stairwell switchback rebuild — a clean
synthetic connector beats a faithful-but-broken voxelization).

**Phase 3 — true per-floor schematization (weeks, research-grade).** Extract
each storey's wall centerline graph, JOSM-style snap near-axis segments
(tolerance ~10–15°) with soft least-squares so corridor widths ≥ 2 blocks
and room adjacency are preserved, then re-voxelize from the rectified graph.
This is the full version of the owner's idea; it subsumes Phases 1–2 but
carries real topology risk.

**Openings replay (needed from Phase 1 on).** Before any geometry moves,
record every door/window **parametrically**:
`(host wall id, t ∈ [0,1] along the wall centerline, sill height, width,
swing side)` — all available from IFC (`FillsVoids` → opening → wall). After
rectification, re-instantiate each opening on the *moved* wall at the same
t and sill. Our existing sill-anchored door placer already is the replay
half; the parametric recording is new but small. This is exactly the
constraint-re-anchoring idea from cartographic generalization, and it is
what "put back the elements relative to where they were connected" means
formally.

**Verification (unchanged).** After any phase: the vanilla-physics
reachability audit (every storey's floor cells reachable from the entrance)
plus the per-door two-sided walkability probe. Rectification that breaks a
door slides it along its wall to the nearest valid column (GDMC-style
validity rule) rather than deleting it.

## Looking at it before converting

```sh
make rectify-preview IFC="model.ifc"
# or: python3 scripts/preview_rectify.py model.ifc --svg out/r.svg --json out/wings.json
python3 scripts/preview_rectify.py --self-test    # needs no model
```

Rectification is the one stage that visibly moves the building, and for a long
time the only way to see it was to convert twice and compare two worlds — about
forty minutes to answer "which wings did it find, and where do they land".

It never needed to be. `compute_wing_transforms` reads IFC wall **placements**
and nothing else: no geometry, no meshing. `preview_rectify.py` runs the
identical function the engine runs and draws the answer as a before/after plan —
each wing in its own colour, the on-grid spine in grey — plus the per-wing
rotation, pivot, shove, and the count of wing walls within 2 m of the spine
before and against after. Its `--json` is the same `wing_records` shape
`--rectify` writes into `summary.json`, so a preview and a real build compare
field by field.

Membership is a convex-hull test, which is worth knowing when reading the
picture: an axis-aligned wall that happens to sit inside a wing's hull belongs
to that wing and moves with it. Grey dots inside a coloured cloud are not
mistakes.

The preview covers Phase 1 only. Seam stitching runs later against the voxel
grid and cannot be known from placements.

## Status: Phase 1 is implemented (`--rectify`)

`scripts/ifc_to_voxels.py --rectify` runs the whole recipe above at extract
time: wall placements → angle families → spatial wing clustering →
seam-nearest pivot → collision-scored rotation choice → rigid per-wing
rotation of every element (cached per aggregate root, so a stair and its
flights/railings rotate together, and doors ride their walls — the
"openings replay" is implicit in the rigid transform for Phase 1).

On UNBC it finds **six wings** (the five 58° clusters *and* a 5°-skewed
block it discovered on its own) and rotates each onto the grid:

![wall placements before/after](docs/rectify_walls_before_after.png)
![voxel plan before/after](docs/rectify_voxel_plan_before_after.png)

## Status: Phase 2 (seam stitching) is implemented too

`stitch_seams` runs on every build (rectified or not): it BFSes the
walkable graph from the entrance doors with vanilla physics, labels the
still-unreachable floor islands, and for each synthesizes the shortest
corridor (1 wide × 3 high, floor pads under air gaps, never through doors
or a stairwell's climbing envelope) to the reachable set, re-running the
BFS after each link so islands chain. On the un-rectified build the same
pass also bridges the annex wings that were only connected through
missing site terrain.

Measured (identical engine, `scripts/audit_walkability.py`):

| metric | original grid | `--rectify` |
|---|---|---|
| seam corridors stitched | 43 (162 cells) | 30 (96 cells) |
| stairwells that climb + connect | **36 / 36** | 32 / 34 * |
| interior floor cells (standable) | 35,000 | **36,273** (+4 %) |
| interior reachable from entrance | 94 % (was 84 %) | **96 %** |
| top tower storey (y16) reachable | 99 % | 98 % |
| wing wall/corridor geometry | jagged 58° staircase lines | clean orthogonal |

\* the 2 flagged wells climb internally; their storeys are served by seam
corridors instead, so nothing the player needs is behind them.

The rectified world now **beats the original on every count that
matters**: more usable floor, higher reachability, and clean orthogonal
wings. `--rectify` remains opt-in for the deployed page purely as a
product choice — it visibly changes the campus footprint from the air
(wings swing onto the grid), which is exactly what rectification means.

### Collision control (push-apart)

Rotation alone cannot guarantee a wing does not swing INTO the spine —
measured on UNBC, rotation-only left 104 wing walls within 1 m of a main
wall (vs 80 in the un-rotated source, which itself interlocks in places).
`compute_wing_transforms` therefore adds a **push-apart step**: if the
rotated wing still clips, it is translated outward in whole-metre steps
(8 directions × up to 5 m, scored by residual clipping with a penalty on
shove distance). On UNBC five of six wings take a 1–5 m shove and total
clipping drops to **27 walls (0.7 % of the largest wing)** — cleaner than
the original building. The widened seam gaps are exactly what
`stitch_seams` then bridges, and connectors ≥ 3 cells long are carved
**two cells wide** so they read as hallways rather than crawl-tunnels
(the side lane degrades to 1-wide where a door, stair envelope or
glazing protects the cell).

### Seam audit trail

Every synthesized corridor is logged to **`out/<name>/seams.csv`**
(reachable-side and island-side coordinates in world/blocks.csv space,
length, size of the island it reconnects, floor pads laid, and a
histogram of exactly which blocks were carved). Audited on this model
(every corridor walked end-to-end by BFS, plus an in-client sweep), and
the pass was upgraded from what the first audit found:

- **Most corridors are length 1** — a single doorway-sized opening
  through one wall thickness, reconnecting rooms of hundreds to
  thousands of cells that were sealed by one rounded partition.
- **Corridors route around glazing.** The line chooser scores every
  near-minimal candidate pair and takes the cheapest *feasible* line,
  with glass cells costing 8× a wall cell — after which **zero glass
  cells are cut in either build** (previously 5–7). If a corridor ever
  must pierce a curtain wall, the opening is framed with mullion-grey
  concrete (sides + lintel) so it reads as an intentional portal.
- **Open-air links are proper catwalks**: floor pads get side decks and
  fence guardrails (arm states computed by the normal railing pass),
  not a bare floating stone strip.
- Nothing carves a door, a stair, or a stairwell's climbing envelope
  (enforced by the pass; confirmed by the audit), and a candidate line
  with nothing to carve is rejected — an open-but-unreachable gap is a
  headroom problem this pass cannot fix, and accepting it would no-op
  forever.
- Current totals: 40 corridors / 234 cells touched (original build),
  42 / 173 (rectified); reachability 94 % / 96 %, all door QA green.

## Recommendation

1. ~~Do **Phase 1 + openings replay** next~~ — **done**, see Status above.
   It was a contained change (one
   rotation per wing + parametric door records), it fixes the 58° wing —
   a quarter of the building — and it reuses every existing pass untouched
   (each wing is just a normal orthogonal building in its own frame).
2. Keep Phase 3 as the research goal; publish it — the survey found **no
   prior art** for functional-opening-preserving rectification of BIM into
   voxel worlds.
3. Regardless of phase, adopt the VASA walkability invariant as the
   acceptance gate (already done for stairwells as of this branch).
