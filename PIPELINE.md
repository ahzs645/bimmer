# IFC → Minecraft voxel pipeline

Converts a BIM model (IFC) into Minecraft-ready voxel schematics with
**semantic block mapping** and **functional, openable doors**, plus an
interactive browser viewer. Tested end-to-end on the 80 MB UNBC campus model
(IFC2X3, ~41k elements, 13 storeys).

```
IFC ──ifc_to_voxels──▶ blocks.csv ──blocks_to_minecraft──▶ .schem / .litematic
                          │
                          ├──export_web──▶ web/data/<name>/  (3-D viewer)
                          └──render_voxels──▶ out/<name>/preview/*.png
```

Everything under `out/` and `web/data/` is **reproducible from the IFC**, so it
is git-ignored. The scripts, the viewer (`web/index.html` + `web/vendor/`), and
these docs are the only things kept in the repo.

---

## Quick start

```sh
make setup                 # create .venv (Python 3.11) + install deps  (once)
make p1                    # full pipeline at 1 m/block  -> out/unbc_1m
make p05                   # full pipeline at 0.5 m/block -> out/unbc_0p5m
make viewer                # open http://127.0.0.1:8765/
```

Or drive it directly (handles any IFC / resolution):

```sh
.venv/bin/python scripts/pipeline.py "My Model.ifc" --pitch 1.0 --name mymodel
```

Outputs per run (`out/<name>/`): `blocks.csv`, `<name>.schem`,
`<name>.litematic`, `summary.json`, `preview/*.png`, and viewer data in
`web/data/<name>/`.

---

## How the engine works (concepts)

The hard part is not "mesh → cubes" — it is turning *building semantics* into
*Minecraft semantics*. Key design decisions, with the reasoning:

### 1. Units: IfcOpenShell returns **metres**
The IFC file declares millimetres, but `ifcopenshell.geom` normalises geometry
to SI metres (verified empirically on this model: coords ≈ 90–260, not 90 000).
So `--pitch` is in metres and `--pitch 1.0` = **1 block per metre**. No manual
mm→m scaling needed.

### 2. Fast geometry extraction
Uses the multi-threaded `ifcopenshell.geom.iterator`, not a serial
`create_shape` loop. On this model: **~60 s vs. ~1 hr** for 39k shapes.

### 3. Exclude voids, or openings get re-filled
`IfcOpeningElement` (door/window cut-outs), `IfcSpace`, annotations, grids and
the site surface are **skipped**. If you voxelize them as solids you plug every
doorway and window back up. IfcOpenShell already subtracts openings from the
host wall, so the wall mesh arrives with the holes already in it.

### 4. Semantic block mapping + priority resolution
Each IFC class maps to a coarse *behaviour class*, each class to a Minecraft
block. Every class is voxelized onto **one shared integer lattice**; when two
classes land in the same cell, a **priority rule** decides the winner (solid
structure beats transparent glazing, so the building doesn't read see-through;
stairs beat *everything* solid, because stair flights run flush against
stairwell shaft walls and would otherwise lose their walking path to the wall
ring at coarse pitches). This mirrors the IfcVoxNet "global priority" approach.

| IFC type | class | Minecraft block |
|---|---|---|
| `IfcWall`, `IfcWallStandardCase` | wall | `white_concrete` |
| `IfcSlab`, `IfcCovering` | floor | `smooth_stone` |
| `IfcColumn`, `IfcBeam` | structure | `stone` |
| `IfcRoof` | roof | `deepslate_tiles` |
| `IfcStair*`, `IfcRamp*` | stair | `stone_bricks` |
| `IfcWindow`, `IfcPlate`, `IfcCurtainWall` | glass | `light_blue_stained_glass` |
| `IfcMember` (mullions) | frame | `gray_concrete` |
| `IfcMember` (stair stringers) | stair | `stone_bricks` |
| `IfcRailing` | railing | `oak_fence` *(with connection states — see below)* |
| `IfcDoor` | door | `oak_door` *(functional — see below)* |

`IfcMember` is disambiguated by its aggregation: members that decompose an
`IfcStair`/`IfcRamp` are stair stringers and voxelize with the staircase;
all others are treated as curtain-wall framing.

Railing fences carry explicit connection states
(`oak_fence[east=true,north=false,...]`, computed from neighbouring fences and
full-cube solids). This matters because saved worlds, schematic pastes and
prismarine-based renderers use the *stored* state — a bare `oak_fence` never
receives the in-game neighbour update that computes its arms, so it would
render as a row of disconnected posts.

### 5. Functional doors (the headline feature)
Each `IfcDoor` becomes a **real, openable Minecraft door**, not a solid block or
a bare gap:

1. The door's voxel footprint is read from its geometry (in shared-grid
   coordinates).
2. The **passage** is carved to air (each leaf column, through the wall depth,
   door-height tall) — and *only* the passage: carving the door's whole
   bounding box would also blow out the glazing and framing around wide
   curtain-wall / shop-front doors, leaving free-standing doors in holes.
3. A two-half `minecraft:oak_door` is placed at the threshold:
   - `facing` is derived from the **wall normal** (the thinner horizontal axis
     of the door footprint),
   - the door bottom is anchored to the walkable surface beside the opening
     that is **closest to the IFC sill height**, never more than 2 cells from
     it (a probed floor cell must have door-height headroom above it, so
     walls/mullions/glazing can't misanchor it; picking the *highest* nearby
     surface instead hoisted facade doors onto adjacent roof decks, and with a
     fully plugged doorway the only "surface" found is the roof on top of the
     wall — beyond the cap the IFC sill wins outright). All leaves of a door
     share one floor level so double doors never step,
   - `half=lower` at floor level + `half=upper` directly above,
   - `hinge`, `open`, `powered` states set so it pastes as a closed, working
     door; adjacent leaves get mirrored hinges so double doors meet in the
     middle.

Per-door **overrides** (for the handful of doors coarse voxels can't resolve —
split-level thresholds, odd curtain-wall vestibules): pass
`--overrides my.json` with

```json
{"doors": {"3cUkl32yn9qRSPvBJVyWYp": {"raise": 1},
           "0BTBFw6f90Nfh9rP1dlXr2": {"skip": true},
           "2O2Fr$t4X7Zf8NOew3FLOH": {"facing": "north", "leaves": 2}}}
```

Every run writes `out/<name>/doors.csv` (GlobalId → placed x/y/z, facing,
leaves, sill offset) so you can find the GlobalId of any misplaced door in the
world and pin it.

Block-states are carried through `blocks.csv` as
`minecraft:oak_door[facing=east,half=lower,...]` and preserved by **both** the
`.schem` (mcschematic) and `.litematic` (litemapy) writers — verified by
round-trip.

`--doors` modes: `functional` (default), `air` (just a passable gap), `solid`
(legacy: plug the opening with planks). Functional doors fit best around
`--pitch 1.0`, where a typical doorway is ≈ 1 wide × 2 tall = exactly one
Minecraft door.

### 6. Spiral staircases are synthesized, not voxelized
A voxelized spiral flight at 1 m/block is a jumpy blob pinched between the
stairwell shaft walls (treads stack in tight columns, so stair refinement
can't orient them). With `--spiral synth` (the default), each
`IfcStair.ShapeType == SPIRAL_STAIR` assembly is instead rebuilt from its
parameters: a centre newel column plus one tread per ring cell winding around
it, with the start/end angles, height and winding direction measured from the
real flight mesh. Every rise is an oriented stair block, so the spiral is
walkable without jumping; headroom above each tread is carved. Use
`--spiral voxel` to keep the raw voxelization.

### 7. Surface voxelization, **not** fill
`--fill` is off by default. IFC-derived meshes are usually **not watertight**, so
trimesh `.fill()` is unreliable (and would also fill interior rooms solid).
Surface voxelization gives walkable shells, which is what you want for a
building.

---

## Per-stage reference

| Script | Role | Key flags |
|---|---|---|
| `scripts/inspect_ifc.py` | fast probe: schema, units, storeys, element counts, geometry scale | — |
| `scripts/ifc_to_voxels.py` | **engine**: IFC → `blocks.csv` (semantic + functional doors) | `--pitch`, `--doors {functional,air,solid}`, `--spiral {synth,voxel}`, `--overrides`, `--fill`, `--threads` |
| `scripts/blocks_to_minecraft.py` | `blocks.csv` → `.schem` / `.litematic` (state-aware) | `--format {schem,litematic}`, `--minecraft-version` |
| `scripts/export_web.py` | `blocks.csv` → compact binary + meta for the viewer | `--name`, `--label`, `--pitch` |
| `scripts/render_voxels.py` | `blocks.csv` → iso / plan / elevation PNGs | `--iso-scale` |
| `scripts/pipeline.py` | runs all of the above + manifest | `--pitch`, `--name`, `--doors`, `--formats`, `--no-web`, `--no-preview` |
| `scripts/serve_viewer.sh` | serve the viewer locally | `[port]` |
| `scripts/mc_palette.py` | optional sRGB→Lab block matching (not default) | — |
| `scripts/probe_materials.py` | check IFC per-element material colours | — |

---

## Web viewer

`web/index.html` is a self-contained Three.js viewer (Three.js vendored in
`web/vendor/`, so it works offline). Features: dataset dropdown (resolutions),
per-category visibility toggles with live counts, a **height-slice slider** to
look inside floors, and orbit/zoom/pan. Run `make viewer` (or
`scripts/serve_viewer.sh`) and open <http://127.0.0.1:8765/>.

---

## Getting it into a Minecraft world

For small/medium builds: WorldEdit `//schem load <name>` then `//paste -a`
(skip air). For this model's **1 M-block 0.5 m** export, use
**FastAsyncWorldEdit (FAWE)** — its disk-backed clipboard handles million-block
pastes that vanilla WorldEdit OOMs on. Ramp limits with `//fast` + `//limit`,
and don't leave mid-paste. Build height in modern Java is Y −64…319 (384), so
pick a base Y that clears 319. `.litematic` + the Litematica mod is the
client-side alternative (per-chunk paste built in).

---

## "Is there a better toolkit?" — evaluation

Short answer: the closest specialised tool is **IfcOpenShell's
`voxelization_toolkit` / `voxec`** (robust integer-math IFC voxelizer with
per-class grids and morphological ops). But it (a) does **not** emit any
Minecraft format and (b) is **not pip/conda-installable on Apple Silicon**
(C++/CMake build; not published for `osx-arm64`). So it's a *concept reference*,
not a drop-in — we borrowed its per-class + priority ideas. **ObjToSchematic**
does texture→block matching and can emit functional structures, but is
GUI/web-only and mesh-only (loses IFC semantics). Full survey with citations and
confidence notes in [`PRIOR_ART.md`](PRIOR_ART.md). Net: an open, scriptable,
IFC-native Python pipeline that emits modern `.schem`/`.litematic` at building
scale is genuinely under-served prior art.

---

## Reproducibility / what's in git

**In the repo:** `scripts/`, `web/index.html`, `web/vendor/`, `Makefile`,
`requirements-pipeline.txt`, and the `*.md` docs.
**Git-ignored (regenerate with `make all`):** `out/`, `web/data/`, `*.ifc`,
`*.rvt`. To regenerate everything from scratch: `make setup && make all`.
