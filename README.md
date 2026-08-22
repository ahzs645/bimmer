# bimmer — BIM/IFC → Minecraft voxel pipeline

Turn a real building's BIM model into Minecraft-ready voxel schematics, with
**semantic block mapping** (glass for glazing, concrete for walls, stone for
slabs…) and **functional, openable doors**, plus an interactive 3-D web viewer.

Built and tested end-to-end on the **UNBC campus model** (Revit → IFC2X3, 80 MB,
~41k elements, 13 storeys, ~218 × 375 × 19 m).

```
RVT ──parsers/reviter──▶ IFC ──pipeline──▶ .schem / .litematic ──WorldEdit/FAWE──▶ Minecraft
     (or Revit/APS)                     └──▶ interactive web viewer
      the parser            the interpreter
```

The front half is [**Reviter**](https://github.com/ahzs645/reviter), a
clean-room RVT decoder pinned here as a git submodule, so the whole chain runs
without a Revit licence. The two halves meet at a **file-level contract** rather
than at code — see [REVITER.md](REVITER.md).

## Quick start

```sh
git clone --recurse-submodules https://github.com/ahzs645/bimmer
make setup          # .venv (Python 3.11) + dependencies   (run once)
make p1             # full pipeline at 1 m/block   -> out/unbc_1m/*.schem
make viewer         # interactive viewer at http://127.0.0.1:8765/
```

Starting from the `.rvt` instead of an IFC (no Revit needed):

```sh
make parser-setup   # check out parsers/reviter + install its deps (run once)
make parser-check   # preflight, needs no model
make rvt RVT="UNBC Model ... .rvt"
```

That's it. See **[PIPELINE.md](PIPELINE.md)** for how it works, the block-mapping
table, functional doors, per-stage reference, and Minecraft import instructions.

## What's here

| Path | What |
|---|---|
| `parsers/reviter` | **the parser** — Reviter, a clean-room RVT decoder, as a pinned submodule |
| `scripts/rvt_to_ifc.py` | stage 0: RVT → IFC through the parser, gated by the contract check |
| `scripts/pipeline.py` | one-command end-to-end driver (accepts `.rvt` or `.ifc`) |
| `scripts/ifc_to_voxels.py` | the engine: IFC → voxels (semantic + functional doors) |
| `scripts/blocks_to_minecraft.py` | voxels → `.schem` / `.litematic` (block-state aware) |
| `scripts/export_web.py`, `web/` | interactive Three.js viewer |
| `scripts/render_voxels.py` | static iso / plan / elevation PNG previews |
| `scripts/inspect_ifc.py` | fast structural probe of an IFC |
| `scripts/check_ifc_contract.py` | does this IFC carry what the engine reads? (gate a model before converting it) |
| `scripts/rectify.py` | Phase-1 plan rectification: which wings sit off the grid, and the rigid motion that squares each one |
| `scripts/preview_rectify.py` | see that rectification as a before/after plan, from wall placements alone (seconds, no conversion) |
| `renderers/mcweb/` | export the building to a Java world save for the **minecraft-web-client** renderer (real doors/stairs/slabs/fences) |
| `Makefile` | `setup` / `p1` / `p05` / `all` / `viewer` / `clean` |
| **[HANDOFF.md](HANDOFF.md)** | **start here to continue this work**: current audited state, known residuals, ranked backlog, and the traps that already bit us |
| **[PIPELINE.md](PIPELINE.md)** | full design + usage docs |
| **[REVITER.md](REVITER.md)** | RVT → voxels **without Revit**: the frame-by-frame transformation chain, the engine's IFC input contract, and what a native RVT recovery supplies today |
| **[BLOCKCRAFT.md](BLOCKCRAFT.md)** | walk the building in a browser (BlockCraft, flat world) |
| **[RENDERERS.md](RENDERERS.md)** | the two walkable browser renderers compared (BlockCraft vs minecraft-web-client) |
| **[LESSONS.md](LESSONS.md)** | failure catalog (doors/stairs/railings): symptom → root cause → fix, the verification workflow, player behaviour, open items |
| **[ASSUMPTIONS.md](ASSUMPTIONS.md)** | every assumption the conversion makes (floor/wall thickness, door preconditions, stair physics) and the artifact you get when one is violated |
| **[RECTIFY.md](RECTIFY.md)** | plan-rectification proposal: conform angled wings to the voxel grid, then replay doors/windows parametrically |
| **[PRIOR_ART.md](PRIOR_ART.md)** | researched survey of existing IFC/BIM→Minecraft work |
| `TESTED_OPTIONS.md` | log of tools evaluated while building this |

## See it in a browser

**Deployed:** enable GitHub Pages (Settings → Pages → Source: *GitHub
Actions*); `.github/workflows/pages.yml` publishes on every push to `main`:
the site root boots straight into the building in **minecraft-web-client**,
`/blockcraft/` is the lightweight fallback, `/choose/` is a landing page
linking both.

**Locally, one command** (plain Node, no dependencies):

```sh
npm run dev        # or: pnpm dev
# chooser  -> http://localhost:8080
# mcweb    -> http://localhost:8091   (mcraft.fun proxied, local world.zip)
# blockcraft -> http://localhost:8092 (build once: scripts/build_blockcraft_static.sh)
```

## Step 0: getting an IFC from the RVT

RVT is Autodesk's proprietary format; open-source tooling can't read it directly,
so something has to produce an IFC before this pipeline takes over.

**The built-in route** is [Reviter](https://github.com/ahzs645/reviter), pinned
as a submodule at `parsers/reviter` — a clean-room RVT decoder that reads the
model natively and writes IFC4, no licence involved. It is built on the same
UNBC sources as this repository. `make rvt` runs it and hands the result to the
pipeline; `scripts/pipeline.py` also takes a `.rvt` directly.

Other routes, all of which produce an IFC this pipeline reads the same way:

- **Revit desktop** — open the RVT, make a clean 3-D view, *Export → IFC*
  (IFC2x3 Coordination View is fine). Best option if you have Revit.
- **Autodesk Platform Services** — Model Derivative / Design Automation can
  export IFC in the cloud (needs APS credentials).
- **ODA / commercial converters** — can read RVT without Revit.

Whichever you use, grade the result before converting it — the engine reads IFC
*semantics*, and a file missing them converts without error into a subtly
unwalkable world:

```sh
make contract IFC="model.ifc"      # or: python3 scripts/check_ifc_contract.py model.ifc
```

**[REVITER.md](REVITER.md)** has the frame-by-frame transformation chain, the
full input contract, and how the parser/interpreter split is wired.

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
