# bimmer — BIM/IFC → Minecraft voxel pipeline

Turn a real building's BIM model into Minecraft-ready voxel schematics, with
**semantic block mapping** (glass for glazing, concrete for walls, stone for
slabs…) and **functional, openable doors**, plus an interactive 3-D web viewer.

Built and tested end-to-end on the **UNBC campus model** (Revit → IFC2X3, 80 MB,
~41k elements, 13 storeys, ~218 × 375 × 19 m).

```
RVT ──Revit/APS──▶ IFC ──pipeline──▶ .schem / .litematic ──WorldEdit/FAWE──▶ Minecraft
                                  └──▶ interactive web viewer
```

## Quick start

```sh
make setup          # .venv (Python 3.11) + dependencies   (run once)
make p1             # full pipeline at 1 m/block   -> out/unbc_1m/*.schem
make viewer         # interactive viewer at http://127.0.0.1:8765/
```

That's it. See **[PIPELINE.md](PIPELINE.md)** for how it works, the block-mapping
table, functional doors, per-stage reference, and Minecraft import instructions.

## What's here

| Path | What |
|---|---|
| `scripts/pipeline.py` | one-command end-to-end driver |
| `scripts/ifc_to_voxels.py` | the engine: IFC → voxels (semantic + functional doors) |
| `scripts/blocks_to_minecraft.py` | voxels → `.schem` / `.litematic` (block-state aware) |
| `scripts/export_web.py`, `web/` | interactive Three.js viewer |
| `scripts/render_voxels.py` | static iso / plan / elevation PNG previews |
| `scripts/inspect_ifc.py` | fast structural probe of an IFC |
| `renderers/mcweb/` | export the building to a Java world save for the **minecraft-web-client** renderer (real doors/stairs/slabs/fences) |
| `Makefile` | `setup` / `p1` / `p05` / `all` / `viewer` / `clean` |
| **[PIPELINE.md](PIPELINE.md)** | full design + usage docs |
| **[BLOCKCRAFT.md](BLOCKCRAFT.md)** | walk the building in a browser (BlockCraft, flat world) |
| **[RENDERERS.md](RENDERERS.md)** | the two walkable browser renderers compared (BlockCraft vs minecraft-web-client) |
| **[PRIOR_ART.md](PRIOR_ART.md)** | researched survey of existing IFC/BIM→Minecraft work |
| `TESTED_OPTIONS.md` | log of tools evaluated while building this |

## Step 0: getting an IFC from the RVT

RVT is Autodesk's proprietary format; open-source tooling can't read it directly.
Export it to IFC first (then this pipeline takes over):

- **Revit desktop** — open the RVT, make a clean 3-D view, *Export → IFC*
  (IFC2x3 Coordination View is fine). Best option if you have Revit.
- **Autodesk Platform Services** — Model Derivative / Design Automation can
  export IFC in the cloud (needs APS credentials).
- **ODA / commercial converters** — can read RVT without Revit.

IFC is the right interchange format here because it preserves *what each element
is* (wall vs. glazing vs. door vs. slab) — which is exactly what drives the
semantic block mapping. A plain mesh export (OBJ/GLB/FBX) would collapse to a
single-material shell. (See PRIOR_ART.md for the format discussion.)

## Reproducibility

Everything under `out/` and `web/data/`, plus `*.ifc` / `*.rvt`, is git-ignored
because it's reproducible from the source model. Regenerate from scratch with
`make setup && make all`. The repo keeps code, the viewer, docs, and our
**BlockCraft fork** in [`/blockcraft`](blockcraft) (its `node_modules/`, build
output, and IFC-derived world data are git-ignored — see
[BLOCKCRAFT.md](BLOCKCRAFT.md)).

## Requirements

macOS/Linux, Python 3.11 (for IfcOpenShell wheels). Dependencies in
`requirements-pipeline.txt` (ifcopenshell, trimesh, numpy, scipy, mcschematic,
litemapy, pillow). `make setup` installs them.
