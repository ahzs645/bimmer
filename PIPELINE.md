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
structure beats transparent glazing, so the building doesn't read see-through).
This mirrors the IfcVoxNet "global priority" approach.

| IFC type | class | Minecraft block |
|---|---|---|
| `IfcWall`, `IfcWallStandardCase` | wall | `white_concrete` |
| `IfcSlab`, `IfcCovering` | floor | `smooth_stone` |
| `IfcColumn`, `IfcBeam` | structure | `stone` |
| `IfcRoof` | roof | `deepslate_tiles` |
| `IfcStair*`, `IfcRamp*` | stair | `stone_bricks` |
| `IfcWindow`, `IfcPlate`, `IfcCurtainWall` | glass | `light_blue_stained_glass` |
| `IfcMember` (mullions) | frame | `gray_concrete` |
| `IfcRailing` | railing | `iron_bars` *(functional)* |
| `IfcDoor` | door | `oak_door` *(functional — see below)* |

### 5. Functional doors (the headline feature)
Each `IfcDoor` becomes a **real, openable Minecraft door**, not a solid block or
a bare gap:

1. The door's voxel footprint is read from its geometry (in shared-grid
   coordinates).
2. The whole opening footprint is **carved to air** → the doorway is walkable.
3. A two-half `minecraft:oak_door` is placed at the threshold:
   - `facing` is derived from the **wall normal** (the thinner horizontal axis
     of the door footprint),
   - `half=lower` at floor level + `half=upper` directly above,
   - `hinge`, `open`, `powered` states set so it pastes as a closed, working door.

Block-states are carried through `blocks.csv` as
`minecraft:oak_door[facing=east,half=lower,...]` and preserved by **both** the
`.schem` (mcschematic) and `.litematic` (litemapy) writers — verified by
round-trip.

`--doors` modes: `functional` (default), `air` (just a passable gap), `solid`
(legacy: plug the opening with planks). Functional doors fit best around
`--pitch 1.0`, where a typical doorway is ≈ 1 wide × 2 tall = exactly one
Minecraft door.

### 6. Surface voxelization, **not** fill
`--fill` is off by default. IFC-derived meshes are usually **not watertight**, so
trimesh `.fill()` is unreliable (and would also fill interior rooms solid).
Surface voxelization gives walkable shells, which is what you want for a
building.

---

## Per-stage reference

| Script | Role | Key flags |
|---|---|---|
| `scripts/inspect_ifc.py` | fast probe: schema, units, storeys, element counts, geometry scale | — |
| `scripts/ifc_to_voxels.py` | **engine**: IFC → `blocks.csv` (semantic + functional doors) | `--pitch`, `--doors {functional,air,solid}`, `--fill`, `--threads` |
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
