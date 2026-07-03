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

## Recommendation

1. Do **Phase 1 + openings replay** next: it is a contained change (one
   rotation per wing + parametric door records), it fixes the 58° wing —
   a quarter of the building — and it reuses every existing pass untouched
   (each wing is just a normal orthogonal building in its own frame).
2. Keep Phase 3 as the research goal; publish it — the survey found **no
   prior art** for functional-opening-preserving rectification of BIM into
   voxel worlds.
3. Regardless of phase, adopt the VASA walkability invariant as the
   acceptance gate (already done for stairwells as of this branch).
