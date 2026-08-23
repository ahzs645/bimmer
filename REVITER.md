# RVT → voxels without Revit: the Reviter path

`README.md` "Step 0" says the honest thing about the front of this pipeline:
RVT is proprietary, so open the model in Revit and export IFC yourself. That
step is the reason nobody can reproduce a build of the UNBC campus without an
Autodesk licence, and it is the reason the conversion is pinned to one IFC file
that no code in this repository produced.

[**Reviter**](https://github.com/ahzs645/reviter) is a clean-room RVT decoder
that reads the same building natively and writes IFC4. Both projects are built
on the *same three files* — a 67 MB RVT, an 80 MB Autodesk IFC and a 25.6 MB
Autodesk GLB, which Reviter's audits pin by SHA-256. The sources are not in
either repository and are not linked from one: they are a third party's Revit
project, shared privately, and this repository is public. Ask the maintainer
for access.

They are joined now: Reviter is pinned as a git submodule at
`parsers/reviter`, and `scripts/pipeline.py` takes a `.rvt`. This document is
how that is wired and why — the exact transformation chain from Revit's internal
frame to a Minecraft block coordinate, what the voxel engine actually reads out
of an IFC, and — measured against Reviter's own dated audits — which of those
things a Reviter export supplies today.

> Every figure below is quoted from a dated run on **one building**, and is
> evidence for a decision rather than a standing fact. Both repositories are
> explicit about this; see `docs/validating-on-a-second-building.md` in Reviter.

---

## 0. How the two are wired

```
RVT ──[parsers/reviter]──▶ IFC ──[scripts/ifc_to_voxels.py]──▶ voxels ──▶ world
       the parser                  the interpreter
     TypeScript / Node            Python / IfcOpenShell
```

```sh
make parser-setup                    # submodule + its npm deps, once
make parser-check                    # preflight; needs no model
make rvt RVT="UNBC Model ... .rvt"    # stage 0 + the whole pipeline
```

### The seam is the contract, not the code

Nothing in this repository imports Reviter's TypeScript. `scripts/rvt_to_ifc.py`
runs its CLI as a subprocess and then grades the file it produced with
`check_ifc_contract.py`. That is the whole coupling, and it is deliberate.

Reviter is a clean-room decoder for a proprietary format. Its internals change
constantly — the record layouts, the ownership rules, the geometry replay — and
every one of its thresholds is fitted to this one building. Importing its
modules would mean a decoder improvement could break a Minecraft world, and that
neither project could be reasoned about alone. Holding it to a *file-level*
contract instead means the parser is free to change everything except the small,
checkable set of facts in §2, and a failure lands in one of two clearly-labelled
places: the parser wrote a bad file, or the engine misread a good one.

This is also why the submodule is pinned to a commit rather than tracked to a
branch. Reviter's own docs are emphatic that every figure is an observation from
a dated run; a voxel world inherits that. `rvt_to_ifc.py` writes a
`<out>.provenance.json` beside every IFC it produces, recording the parser
commit, the node version, and the SHA-256 of both the source RVT and the output.
"The stairs are wrong" is a different bug depending on which parser wrote the
IFC, and without the pin there is no way to tell which.

### Working with the pin

```sh
git clone --recurse-submodules ...          # or: git submodule update --init
git submodule update --remote parsers/reviter   # move to reviter/main's tip
make parser-check && make contract IFC=...      # then re-grade before trusting it
```

Moving the pin is a deliberate act with a test attached: convert the model
again, re-run the contract gate, and diff the resulting world. A parser bump
that changes the contract report is exactly the signal the pin exists to
produce.

> **The current pin predates the stair-assembly work.** It points at Reviter's
> `main`, which does not yet write `IfcRelAggregates` for stairs (§3), so a
> `make rvt` run today produces an IFC the contract gate fails on
> `stair_aggregation` — correctly, and by design: the gate is doing its job.
> The pin should move to Reviter's tip once that work lands on `main`, at which
> point re-run `make contract` and diff the world. Pinning to an unmerged
> branch instead would tie this repository to a ref that can be force-pushed or
> deleted, which is the one thing a pin exists to prevent.

### What is checkable without the model

The 67 MB RVT is not in either repository and never will be, so the joins that
can be verified without it are:

| | |
|---|---|
| `make fixture-recovery` | the fixture building converted twice — once Revit-shaped, once shaped the way this parser writes — and both walked |
| `python3 scripts/rvt_to_ifc.py --self-test` | version comparison, every preflight failure path, and a stub-parser round trip through the contract gate |
| `python3 scripts/rvt_to_ifc.py --check` | the real submodule, node version, and installed deps |
| `python3 scripts/check_ifc_contract.py --self-test` | five synthetic producers across IFC2X3 and IFC4 |

Measured on the fixture building, all three shapes of the same model through
the same pipeline:

| producer | walls read | wing found | interior reachable |
|---|---:|---|---:|
| Revit-shaped, per-element placements | 43 (65% on-grid) | 15 walls, +32° | 656/669 = 98% |
| Reviter's own exporter | 43 (65% on-grid) | 15 walls, +32° | 656/669 = 98% |
| recovery-shaped fixture | 43 (65% on-grid) | 15 walls, +32° | 656/669 = 98% |

Identical, including the 60-cell walk between the same two world coordinates —
which is only checkable because `summary.json` records the transform (§1), so
"the same place" survives the wing having moved. Before the placement fallback
the middle row read 100% on-grid, found no wing, and rectified nothing.

What none of this establishes is that the parser recovers *the UNBC building*
correctly. That needs the model, and the answer is §3.

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

## 2a. The gate, run on the real building

`check_ifc_contract.py` against the UNBC IFC (`adb85a6f…`), 22 seconds:

| check | result |
|---|---|
| schema | IFC2X3, 41,312 products |
| units | **millimetres** (0.001 m per file unit) — flagged, and correct: IfcOpenShell normalises, `--pitch` stays metres |
| up axis | Z-up |
| class coverage | 38,226 products, **0** in the catch-all `other` |
| doors | 1,912, **all** with `OverallWidth`; **35** within 0.1 cell of a leaf-count boundary |
| stairs | 104 containers, 123 flights, **all 123 aggregated**; 1 spiral |
| floors | 161 slabs + 46 coverings + 20 roofs across 13 storeys |
| openings | 1,820/1,932 resolve to a host (94.2%) |
| join key | 38,222 of 38,226 carry a Tag |

Three things this settles that had only been asserted:

- the file is in **millimetres**, not metres — the units check earns its place;
- **35 doors** sit close enough to a `round(width / pitch)` boundary that a
  small change in `OverallWidth` flips them between one leaf and two. That is
  the population §4's item 3 is about, and it is now a number;
- there **is** a `SPIRAL_STAIR` here, so the spiral synthesis is live code on
  this model — and it is exactly what a Reviter export cannot trigger, because
  its `PredefinedType` is `.NOTDEFINED.` (§3).

## 2b. Both producers, measured, on the same building

The RVT through `parsers/reviter` (156,668,898 bytes out — the byte count
Reviter's own 2026-08-19 audit records, reproduced here), and the Autodesk
export of the same model, both through the gate:

| | Autodesk IFC2X3 | Reviter IFC4 |
|---|---:|---:|
| products | 41,312 | 40,924 |
| unclassified (`other`) | **0** | 571 (1.5%) |
| doors | 1,912 | 1,921 |
| …within 0.1 cell of a leaf boundary | **35** | **394** |
| stair containers / flights / aggregated | 104 / 123 / **123** | 23 / 108 / **0** |
| spiral stairs | 1 | 0 |
| slabs | 161 | 94 |
| openings resolving to a host | 94.2% | **99.4%** |
| products with a Tag | 38,222 | **38,978 (all)** |
| bounds fallbacks in walkability-critical classes | n/a | **1** |
| verdict | WARN (units: mm) | **FAIL (stair aggregation)** |

Four things this changes:

- **394 doors sit on a leaf-count boundary, against 35 from Autodesk.** An
  eleven-fold difference, and it is exactly the `OverallWidth =
  max(bbox.width, bbox.depth)` defect in §4 item 3 — measured rather than
  argued. This is now the strongest evidence for that item, not the weakest.
- **0 of 108 flights are aggregated**, as predicted. 23 `IfcStair` containers
  do reach the file, so the products exist; nothing relates them to their
  flights. The fix on the Reviter branch targets exactly this, and the
  submodule pin predates it.
- **Only 1 of 2,797 bounds fallbacks lands in a walkability-critical class.**
  §3 treats those 2,797 as a risk needing grading; graded, they are almost
  entirely harmless here. That corrects the concern rather than confirming it.
- **Reviter wins two rows**: every product carries a Tag (Autodesk drops four),
  and 99.4% of openings resolve to a host against 94.2%. The recovery is ahead
  of the exporter on the relationships it does keep.

## 2c. Both Reviter gaps closed, measured on the RVT

The two blockers in §4 are implemented on Reviter's branch and run against the
real RVT. The gate read each result:

| | pinned parser | + stair aggregation | + door width | Autodesk |
|---|---:|---:|---:|---:|
| flights aggregated | **0 of 108** | 108 of 108 | 108 of 108 | 123 of 123 |
| doors on a leaf boundary | 394 | 394 | **9** | 35 |
| verdict | **FAIL** | WARN | WARN | WARN |

Both are now WARN on `class_coverage` alone — 571 products (1.5%) still reach
the file unclassified. That is the remaining item, and it is a category-recovery
question rather than an export one.

The door number is the surprise. 394 of 1,921 doors sat within a tenth of a cell
of a `round(width / pitch)` boundary; reading the width off the footprint's own
principal axis rather than an axis-aligned box takes it to 9, **fewer than the
Autodesk export's 35**. The recovery is now ahead of the exporter on that
measure as well as on Tags and host relationships.

The submodule pin still predates all of this (§0).

## 3. Where a Reviter export stands today

From Reviter's independent-reader run of 2026-08-19 (38,978 products, read back
by an implementation sharing no code with the exporter) and the export
validation of 2026-08-02.

| Contract item | Autodesk IFC2X3 | Reviter IFC4 | Severity |
|---|---|---|---|
| Z-up, metres, valid IFC | yes | yes; IfcOpenShell and web-ifc both read it, `ifcopenshell.validate --rules` clean | — |
| Typed products | typed | typed; **571** `IfcBuildingElementProxy` (1.5%) fall to `other` | low |
| **Stair aggregation** | present | **now written** — `IfcRelAggregates` per assembly, onto a representation-less `IfcStair` where the wrapper has no body | closed |
| Spiral stair enum | `ShapeType` | `PredefinedType`, but always `.NOTDEFINED.` — the recovery does not declare a stair's shape | medium |
| `OverallWidth` | Revit's own parameter | `max(bbox.width, bbox.depth)` — a diagonal, not a width | medium |
| Door bodies | native | 1,921 doors, **100.0% centre / 99.9% size** on the half-foot overlay | low |
| Floor plates | 107 tagged slabs | **94** slabs; "floor/landing recovery remains incomplete" | high |
| Host relationships | present | present — 1,932 persisted, none invented | low now |
| `Tag` | present | present, 41,709 with native `UniqueId` | — |
| `GlobalId` | Autodesk-derived | Reviter-derived — **the two do not match** | medium |
| Geometry provenance | not declared | declared per element: 84.3% native, 8.5% reconstructed, **7.2% (2,797) bounds fallback** | see below |

**Stair aggregation was the one that mattered most, and it is now written.**
Three engine passes key on `element.Decomposes` and all three degraded silently
without it: an `IfcMember` inside a stair is a stringer and not a mullion
(LESSONS S5); overlapping assembly bounding boxes merge into one stairwell
before the climb test (S3); and `SPIRAL_STAIR` flights are routed to the
synthesiser instead of the merged stair class. A file with 108 flights and no
containers passes every geometric check and produces stairwells the walkability
audit reports as isolated. Reviter's 2026-08-02 note calls wrapper aggregation
something that "can be added later without changing visible geometry" — true of
the *picture*, and the exact opposite of true for a world you have to walk
through.

Reviter now joins the tree from the run frames (each names its parent and its
stringers) and the `Stairs` element frame (which names the railings and
supports), and exports one `IfcRelAggregates` per assembly. The container
carries no representation, so it adds no voxels — the parts already draw the
stair.

Measured on a three-element fixture through this engine, with the single
`IfcRelAggregates` line removed as the control:

| | `solid_faces_by_class` | `per_class_voxels` |
|---|---|---|
| with the aggregate | `stair: 2, railing: 1` | `stair: 3, railing: 3` |
| without it | `stair: 1, frame: 1, railing: 1` | `stair: 3, frame: 3, railing: 3` |

The stringer moves out of curtain-wall frame and into the stair class, which is
the S5 artifact appearing and disappearing on one line of IFC.

**And `blocks_by_id` is identical in both runs** — worth dwelling on, because it
is the trap HANDOFF warns about in its general form. At 1 m the stringer
occupies the same cells as its flight, and `CLASS_PRIORITY` puts stair above
frame, so the final block list hides the error behind an overlap. A stringer
running clear of its flight would render as curtain-wall concrete with the block
histogram still looking correct. `solid_faces_by_class` sees it; `blocks_by_id`
does not. Do not regression-test this class of bug on the block list.

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

2. ~~**Reviter: emit `IfcRelAggregates` for stairs.**~~ **Done** —
   `lib/reviter/stair-assemblies.ts`, published on
   `ConvertResult.nativeStairAssemblies`. It needed no new decoding: the tree
   was decoded to place run geometry and dropped before anything could publish
   it. What remains from this item is the stair *shape*. `PredefinedType` is
   `.NOTDEFINED.`, so spiral synthesis still cannot fire on a Reviter IFC. The
   evidence exists — a run recovered by `revit-2027-spiral-stair-mesh` is a
   spiral by construction, because that replay only succeeds against matching
   inner/outer helix guides — but that decoder's identity does not reach the
   export manifest. Carrying it there is the work, and it is small.

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

**Terrain.** The RVT retains **30 DWG filenames and no payloads**, so the CAD
underlays are not recoverable from the model; the Drive folder has a *UNBC
Floorplan* folder that plausibly holds them, which is worth checking. Either
way the retained names are per-storey plans, one roof, and a whole-building
underlay — plan geometry, not a ground surface. Backlog item 2 (stamp a
continuous ground surface by interpolating from ground-door sills and the
building underside) remains the route, and the ~147 exterior doors facing a
drop (D9) stay until it is built.

**A second building.** Every threshold in Reviter and every rounding rule here
is fitted to this one model. Joining the two projects doubles the tooling on one
building; it does not test either of them. The contract checker is the part that
transfers — it makes "is this model convertible?" answerable for a file nobody
has seen.
