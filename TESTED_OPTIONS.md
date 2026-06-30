# Tested conversion options

Date tested: 2026-06-29  
Machine: macOS 26.5.1 on Apple Silicon M4 Pro

## Local source file

- `UNBC Model - 2026-06-27 - DRAFT.rvt`
- Size: 65 MB
- Direct open tests:
  - IfcOpenShell: failed with `Unable to parse IFC SPF header`
  - Trimesh: failed with `file_type 'rvt' not supported`

Conclusion: open-source mesh/IFC tooling does not read RVT directly. The RVT still needs Revit, Autodesk Platform Services, or a commercial RVT-capable converter before the open-source pipeline can start.

## IfcOpenShell Python path

Status: works after an IFC is available.

Test performed:

```sh
/opt/homebrew/bin/python3.11 -m venv /tmp/bim-to-mc-venv
/tmp/bim-to-mc-venv/bin/python -m pip install trimesh numpy scipy ifcopenshell mcschematic litemapy
/tmp/bim-to-mc-venv/bin/python scripts/ifc_to_mesh.py /tmp/bim-to-mc-tests/duplex.ifc out/tool-tests/duplex_from_ifc_cli.obj
```

Observed result on a public Duplex sample IFC:

- 295 IFC products total
- 286 products converted
- 27,740 mesh faces

This is the strongest local IFC-to-mesh option. The pip package did not install an `IfcConvert` CLI binary, so `scripts/ifc_to_mesh.py` is the local fallback.

## Mesh to voxel map

Status: works.

Test performed:

```sh
/tmp/bim-to-mc-venv/bin/python scripts/voxelize_mesh.py out/tool-tests/duplex_from_ifc.obj --pitch 1.0 --fill --out-dir out/tool-tests/duplex_voxels --max-json-voxels 1000
```

Observed result:

- 2,124 filled voxels
- Minecraft grid shape: 10 x 12 x 27
- Outputs written:
  - `out/tool-tests/duplex_voxels/blocks.csv`
  - `out/tool-tests/duplex_voxels/voxels.json`
  - `out/tool-tests/duplex_voxels/voxels.npz`
  - `out/tool-tests/duplex_voxels/preview_boxes.obj`

Note: `--fill` requires SciPy, now included in `requirements-pipeline.txt`.

## Minecraft schematic libraries

Status: works.

Test performed:

```sh
/tmp/bim-to-mc-venv/bin/python scripts/blocks_to_minecraft.py out/tool-tests/duplex_voxels/blocks.csv out/tool-tests/script_schematics/duplex_cli.schem --format schem
/tmp/bim-to-mc-venv/bin/python scripts/blocks_to_minecraft.py out/tool-tests/duplex_voxels/blocks.csv out/tool-tests/script_schematics/duplex_cli.litematic --format litematic
```

Observed result:

- `.schem` written with `mcschematic` and loaded back successfully in the smoke test.
- `.litematic` written with `litemapy` and loaded back successfully in the smoke test.

Recommendation: use `.schem`/`mcschematic` first. Use `.litematic` if the target workflow is Litematica.

## threed2vox

Repository: https://github.com/skairunner/threed2vox

Status: builds and runs, but less convenient than the Python pipeline.

Test performed:

```sh
cd /tmp/bim-to-mc-github-tests/threed2vox
cargo build --release
target/release/threed2vox --output "/Users/ahmadjalil/Downloads/bim to mc/out/tool-tests/threed2vox" --size 30 --version 1.16 --format schem "/Users/ahmadjalil/Downloads/bim to mc/out/tool-tests/duplex_from_ifc.obj"
```

Observed result:

- Build succeeded.
- Generated `out/tool-tests/threed2vox/duplex_from_ifc.schem`.
- The file is valid compressed NBT with `Width`, `Height`, `Length`, `Palette`, and `BlockData`.
- `mcschematic` could not load it back because it expects `WEOffsetX` metadata that `threed2vox` does not write.

Recommendation: useful backup direct OBJ-to-schematic converter, but not the primary path.

## ObjToSchematic

Repository: https://github.com/LucasDower/ObjToSchematic

Status: builds, but headless automation is brittle.

Test performed:

```sh
cd /tmp/bim-to-mc-github-tests/ObjToSchematic
npm install
npm run build
npm run headless
```

Observed result:

- `npm install` succeeded.
- `npm run build` succeeded.
- `npm run headless` failed under `ts-node` on `.atlas` module loading. Transpile-only mode then failed because Node tried to parse `.atlas` as JavaScript.

Recommendation: good manual/web UI option after OBJ export, especially for visual block palette assignment. Not recommended as our automated pipeline.

## APS / Revit-to-IFC GitHub projects

### autodesk-platform-services/aps-revit.ifc.scheduler

Repository: https://github.com/autodesk-platform-services/aps-revit.ifc.scheduler

Status: Release build works locally, actual conversion needs APS/ACC credentials.

Test performed:

```sh
cd /tmp/bim-to-mc-github-tests/aps-revit.ifc.scheduler
dotnet restore RevitToIfcScheduler.csproj
dotnet build RevitToIfcScheduler.csproj --no-restore -c Release
```

Observed result:

- Release build succeeded.
- Debug build failed in the React client `npm install` step because of old peer dependency conflicts.
- The app still needs Autodesk credentials, ACC/BIM360-hosted files, database configuration, and the APS workflow to perform actual RVT-to-IFC conversion.

### ADN-DevTech/aps-revit-ifc-exporter-appbundle

Repository: https://github.com/ADN-DevTech/aps-revit-ifc-exporter-appbundle

Status: relevant, but not buildable as-is on this Mac.

Observed result:

- Revit 2026 project restored.
- Build failed because Revit API / Revit IFC assemblies are referenced from Windows Revit install paths and were not present on this Mac.

Recommendation: use as APS app-bundle reference on a Windows/Revit/APS setup, not as a local Mac converter.

### simonmoreau/RevitToIFCApp

Repository: https://github.com/simonmoreau/RevitToIFCApp

Status: Revit bundle compiles for 2026, then fails at Windows PowerShell post-build packaging on this Mac.

Recommendation: useful reference, but newer Autodesk APS samples are a better starting point.

## Not useful for this project

- `City-of-Helsinki/mesh_to_schematic`: archived, Windows batch workflow, depends on vendor tools. Good historical reference only.
- `idryanov/2schematic`: handles Octomap/PCD, not OBJ/GLB/IFC/RVT.
- `Briiqn/obj2schem`: requires NVIDIA CUDA, so it is not suitable for this Apple Silicon Mac.

## Recommended tested path

Once the RVT is exported to IFC:

```sh
python3 scripts/ifc_to_mesh.py "UNBC Model.ifc" out/mesh/unbc.obj
python3 scripts/voxelize_mesh.py out/mesh/unbc.obj --pitch 1.0 --fill --out-dir out/voxels
python3 scripts/blocks_to_minecraft.py out/voxels/blocks.csv out/minecraft/unbc.schem --format schem
```

