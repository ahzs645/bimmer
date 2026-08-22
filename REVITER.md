# RVT → voxels without Revit: the Reviter path

`README.md` "Step 0" says the honest thing about the front of this pipeline:
RVT is proprietary, so open the model in Revit and export IFC yourself. That
step is the reason nobody can reproduce a build of the UNBC campus without an
Autodesk licence, and it is the reason the conversion is pinned to one IFC file
that no code in this repository produced.

[**Reviter**](https://github.com/ahzs645/reviter) is a clean-room RVT decoder
that reads the same building natively and writes IFC4. Both projects are built
on the *same three files* — the [shared Drive
folder](https://drive.google.com/drive/folders/1Dx_v2v6M1LI02E4sngLyoyVMdnohijLT)
holds the 67 MB RVT, the 80 MB Autodesk IFC and a 25.6 MB Autodesk GLB, and
Reviter's audits pin all three by SHA-256. They have never been joined.

This document works out what joining them takes: the exact transformation chain
from Revit's internal frame to a Minecraft block coordinate, what the voxel
engine actually reads out of an IFC, and — measured against Reviter's own dated
audits — which of those things a Reviter export supplies today.

> Every figure below is quoted from a dated run on **one building**, and is
> evidence for a decision rather than a standing fact. Both repositories are
> explicit about this; see `docs/validating-on-a-second-building.md` in Reviter.

---

## 1. The transformation chain

Six frames, and the conversion crosses all of them. Nothing here is a guess:
each row is what the code does.

| # | Stage | Units | Up | Origin |
|---|---|---|---|---|
| 0 | RVT internal | decimal feet | +Z | Revit project-internal |
| 1 | Reviter recovery | feet | +Z | unchanged; recorded as `result.origin` |
| 2 | Reviter IFC4 | metres (`× 0.3048`) | +Z | `origin` carried in an `IfcLocalPlacement` |
| 2′ | Autodesk IFC2X3 | metres | +Z | may be shared/survey coordinates |
| 3 | IfcOpenShell world coords | metres | +Z | file origin |
| 4 | Voxel lattice | cells of `--pitch` m | +Z | `all_min` — min corner over every class mesh **and** every door mesh |
| 5 | Minecraft | blocks | **+Y** | `origin_shift_xyz` |

The whole forward map, stages 3 → 5, is one line:

```text
g  = round((p_metres - world_bounds_min_m) / pitch_m)
mc = [g.x, g.z, -g.y] - origin_shift_xyz
```

`world_bounds_min_m`, `pitch_m` and `origin_shift_xyz` are all in
`out/<name>/summary.json`, and this branch adds a `voxel_transform` block that
states the formula in the file itself, so a consumer never has to rediscover it
from the engine source.

The `y ↔ z` swap negates the swapped horizontal axis on purpose. A bare swap has
determinant −1 and would mirror the building north↔south; IFC +Y is North, which
is Minecraft −Z, so `z = -g.y` is what keeps the handedness. The stair-facing
table `GRID_TO_FACING` is derived under exactly this map, which is why a change
to it would silently point every staircase the wrong way.

### The five traps

**Project-internal vs shared coordinates.** Reviter reads Revit's internal
frame; it does not apply a project base point or survey point. The Autodesk
exporter may have written shared coordinates. So *the two IFCs of this building
are not in the same place.* This is invisible to the voxelizer — stage 4
re-origins on `all_min` — and fatal to any element-by-element comparison
between an Autodesk build and a Reviter build. Reviter has already solved this
problem once, for the GLB: pair elements whose axis-aligned bounds agree to
0.01 ft on all three axes and let each pair vote for the translation. On
2026-08-13, **1,642 pairs landed in a single 0.01 ft bin** while the runner-up
held 122. The same method transfers to IFC↔IFC. Do not fit the offset from
bounding-box centres or footprint overlap; that entry records both being tried
and both being wrong by feet.

**The Y-up number that is not a Y-up file.** Reviter's 2026-08-02 validation
reports the export spanning "217.898923 × 19.400000 × 375.120452 metres", with
the 19.4 m height in the middle. The file is not Y-up: it writes storey
elevations on Z and extrusions along `IFCDIRECTION((0.,0.,1.))`. That triple is
web-ifc's axis convention, which is why the audit names the field
`spansWebIfcAxesMetres`. Anyone re-deriving a transform from the printed
numbers would insert a Y↔Z swap that is not needed and lay the building on its
side. `check_ifc_contract.py` measures the vertical axis from the file's own
coordinates for this reason.

**Schema drift in attribute names.** `IfcStair`'s shape enum is `ShapeType` in
IFC2X3 and `PredefinedType` in IFC4. The engine read only `ShapeType`, which
fails *silently* on IFC4 — `getattr` returns `None`, the loop skips, and every
spiral stair in the model quietly stops being rebuilt. The artifact is a jumpy,
wall-pinched blob in the stairwell, which reads as a voxelization limit rather
than a missed branch. Fixed on this branch (`stair_shape`), and covered by the
checker's self-test in both schemas. It is worth assuming this is not the only
such rename in a pipeline that now accepts two schemas.

**Packed-key aliasing.** `key(x,y,z) = x + X·y + plane·z` wraps out-of-range
coordinates onto real cells rather than failing. Every grid walker must
bounds-check. This one cost a 16 GB OOM hunt; see LESSONS S13.

**Rectification had no inverse.** `--rectify` inserts a per-wing rigid motion
between stages 3 and 4 — rotation about a seam-nearest pivot, then a
whole-metre push-apart — and it only ever reached stdout. A cell in a rectified
build therefore could not be traced back to the element it came from, which
blocks both A/B comparison and RECTIFY Phase 3's opening replay. This branch
writes the wing transforms (pivot, angle, shove, and the hull half-planes that
decide membership) into `summary.json`, so the rectified world is invertible.

---

## 2. What the engine actually reads

The engine is semantic, not geometric. ASSUMPTIONS.md states the governing
principle: at 1 m/block you are far past the resolution where geometry alone
preserves walkability, so anything the player walks on or through is driven by
IFC *facts*, with geometry only as a tie-breaker. That makes the input contract
short, specific, and mostly invisible when it is unmet.

| What | Where it is read | What a missing value does to the world |
|---|---|---|
| Product class (`is_a()`) | `SEMANTIC_CLASSES` | Falls to `other`: a solid grey cube with no walkability semantics |
| `IfcRelAggregates` on stairs | `extract()`, three separate passes | Stringers become curtain-wall frame; every flight becomes its own "well"; spiral synthesis never fires |
| `IfcStair` shape enum | spiral synthesis | A spiral stair voxelizes as an unclimbable blob |
| `IfcDoor.OverallWidth` | leaf count `round(w / pitch)` | Every double door in an entrance bank narrows to one leaf |
| Door body min-Z | sill anchor (D1) | Doors hoist onto roof decks — ~1,200 of them, before the fix |
| `IfcSlab` / `IfcCovering` / `IfcRoof` | the walkable surface | Storeys report as unreachable, and the audit blames doors |
| `FillsVoids → opening → wall` | RECTIFY Phase 3 | Phase 1 is fine; Phase 3 cannot replay openings onto moved walls |
| `Tag` | nothing yet — see §4 | The only identifier that survives a change of producer |

Run the gate before a 40-minute conversion:

```sh
python3 scripts/check_ifc_contract.py model.ifc --json out/contract.json
python3 scripts/check_ifc_contract.py --self-test     # no model needed
```

It reads attributes and relationships only — no meshing — so it finishes on an
80 MB model in seconds, and it reads the engine's own tables out of
`ifc_to_voxels.py` with `ast` so it cannot drift from the converter it checks.

---

## 3. Where a Reviter export stands today

From Reviter's independent-reader run of 2026-08-19 (38,978 products, read back
by an implementation sharing no code with the exporter) and the export
validation of 2026-08-02.

| Contract item | Autodesk IFC2X3 | Reviter IFC4 | Severity |
|---|---|---|---|
| Z-up, metres, valid IFC | yes | yes; IfcOpenShell and web-ifc both read it, `ifcopenshell.validate --rules` clean | — |
| Typed products | typed | typed; **571** `IfcBuildingElementProxy` (1.5%) fall to `other` | low |
| **Stair aggregation** | present | **absent** — spatial `IfcRelAggregates` only | **critical** |
| Spiral stair enum | `ShapeType` | `PredefinedType` (engine now reads both) | fixed |
| `OverallWidth` | Revit's own parameter | `max(bbox.width, bbox.depth)` — a diagonal, not a width | medium |
| Door bodies | native | 1,921 doors, **100.0% centre / 99.9% size** on the half-foot overlay | low |
| Floor plates | 107 tagged slabs | **94** slabs; "floor/landing recovery remains incomplete" | high |
| Host relationships | present | present — 1,932 persisted, none invented | low now |
| `Tag` | present | present, 41,709 with native `UniqueId` | — |
| `GlobalId` | Autodesk-derived | Reviter-derived — **the two do not match** | medium |
| Geometry provenance | not declared | declared per element: 84.3% native, 8.5% reconstructed, **7.2% (2,797) bounds fallback** | see below |

**Stair aggregation is the one that matters most.** Three engine passes key on
`element.Decomposes` and all three degrade silently without it: an `IfcMember`
inside a stair is a stringer and not a mullion (LESSONS S5); overlapping
assembly bounding boxes merge into one stairwell before the climb test (S3); and
`SPIRAL_STAIR` flights are routed to the synthesiser instead of the merged stair
class. A file with 108 flights and no containers passes every geometric check
and produces stairwells the walkability audit reports as isolated. Reviter's
2026-08-02 note calls wrapper aggregation something that "can be added later
without changing visible geometry" — true of the *picture*, and the exact
opposite of true for a world you have to walk through.

**The 2,797 bounds fallbacks need grading, not a percentage.** An
axis-aligned box is nearly harmless for a wall, because a wall *is* a box. It is
fatal for a stair: a stair's AABB is a solid cube that seals its own stairwell.
Same for a railing, which becomes a wall. The provenance property set already
carries what is needed to grade them; `check_ifc_contract.py` breaks the
fallbacks down by voxel class and fails when they land in
stair/railing/floor/roof. Reviter's own overlay already flags where to look:
`IfcStairFlight` scores **75.0%** on centre and size against the paired export,
against 99.8% for members and 100% for plates.

**The slab gap is smaller than 94-vs-107 suggests, and worth checking anyway.**
This building is modelled with few large plates — the CAD floor audit found
level 311 carries **5 actual Revit `Floors` slabs** — so the population is small
and a 12% deficit is a handful of plates, not a hole in every storey. But floors
are the walkable surface and the door sill anchor, so a missing plate presents
as an unreachable storey and gets blamed on doors. Measure it before building.

**Reviter's missing 13th storey does not matter here.** The voxel engine never
reads `IfcBuildingStorey`; it derives everything from geometry and the walkable
graph. Worth stating so nobody spends a week on it for this consumer's sake.

---

## 4. The plan, ranked

Each item is self-contained, and the order is by measured impact per unit of
work rather than by which repository it lives in.

1. **Baseline the Autodesk IFC** with `check_ifc_contract.py`. Zero risk, and
   it converts "the contract" from prose into a number for the one file that is
   known to work. Everything below is graded against it.

2. **Reviter: emit `IfcRelAggregates` for stairs.** The tree is already
   decoded — `Revit2027StairsElementAggregate` carries `runAndLandingIds`,
   `registeredRailingIds` and `supportIds`, and
   `Revit2027StairsRunAndLandingAggregate` carries `stringerIds` and its parent
   `stairsId`. `convert-element-geometry.ts` reads it and drops it: it never
   reaches `ConvertResult`, so the exporter cannot see it. Surfacing it and
   writing one `IfcRelAggregates` per assembly is the single change that moves
   the most, and it costs no new decoding.

3. **Reviter: derive `OverallWidth` from the host wall, not the bounding box.**
   `max(width, depth)` of an AABB is the larger horizontal extent, which for a
   door on the 58° wing is a diagonal and for a door with a modelled swing is
   the swing. The host relation is already resolved — the exporter uses it to
   write `IfcRelFillsElement` — so the wall's direction is in reach: project the
   door footprint onto the wall centreline. Grade the change by how many doors
   move across a `round(w / pitch)` boundary; the checker counts the fragile
   population already.

4. **bimmer: key `--overrides` on `Tag` as well as `GlobalId`.** Two producers
   derive GlobalIds their own way, so a curated overrides file written against
   the Autodesk export matches *nothing* in a Reviter export of the same
   building. Both write the Revit element id into `Tag`. This is small, and it
   is what makes backlog item 6 (the curated UNBC overrides file) an asset
   rather than a per-file artifact.

5. **Reviter: measure the slab gap, then close or label it.** Per storey, not
   in aggregate.

6. **Register the two IFCs against each other** with the size-pair voting
   method from 2026-08-13, then diff the two builds cell by cell. This is the
   experiment that makes the whole join worth doing: a native RVT recovery and
   Autodesk's own exporter, through an identical voxel engine, differing only
   where the recovery differs. Neither project can run it alone.

7. **Rooms as an oracle — the idea with no prior art.** Neither source carries
   room semantics: the RVT has `Rooms: 0` and the paired IFC `IfcSpace: 0`. But
   Reviter *derives* zones from recovered walls — 135 zones on level 311 from
   1,989 wall records at a 1.4 ft grid, in 0.45 s — and its exporter can already
   write reviewed rooms as `IfcSpace`. That is exactly the signal the voxel
   engine is missing. Today `unblock_door_passages` guesses whether a real room
   sits behind a door, and F2/F3 leave ~250 closet doors opening into rooms that
   wall rounding swallowed at 1 m. A room oracle computed at *full model
   resolution*, before rounding destroys it, tells the engine which of those
   rooms exist and should be re-opened, and gives `stitch_seams` a principled
   target: an unreachable island containing no room does not need a corridor.
   The engine already excludes `IfcSpace` from solids, so this is additive —
   it reads spaces without voxelizing them. PRIOR_ART found no prior art for
   opening-preserving BIM→voxel rectification; this is the same territory.

8. **RECTIFY Phase 3**, unchanged in priority, now with an invertible transform
   (§1) and — if item 7 lands — room boundaries to preserve as a constraint
   rather than a hope.

### Two things this does *not* solve

**Terrain.** The RVT retains 30 DWG filenames but no payloads, and the drawings
themselves — the Drive folder's *UNBC Floorplan* — are per-storey plans and a
whole-building underlay by their names, plus one roof. They are plan geometry,
not a ground surface. Backlog item 2 (stamp a continuous ground surface by
interpolating from ground-door sills and the building underside) remains the
route, and the ~147 exterior doors facing a drop (D9) stay until it is built.

**A second building.** Every threshold in Reviter and every rounding rule here
is fitted to this one model. Joining the two projects doubles the tooling on one
building; it does not test either of them. The contract checker is the part that
transfers — it makes "is this model convertible?" answerable for a file nobody
has seen.
