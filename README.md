# BIM to voxel map notes

Source model in this folder:

```text
UNBC Model - 2026-06-27 - DRAFT.rvt
```

The practical pipeline is:

```text
RVT -> IFC or GLB/OBJ -> voxel grid -> Minecraft-oriented block coordinates
```

Recommended local setup:

```sh
/opt/homebrew/bin/python3.11 -m venv .venv
. .venv/bin/activate
pip install -r requirements-pipeline.txt
```

Python 3.11 is recommended on this Mac because binary wheels are available for IfcOpenShell.

## RVT conversion options

RVT is Autodesk Revit's native proprietary format, so the first step is the hard part. The open-source tools start working after the model is exported to IFC or a mesh format.

Recommended options:

1. Revit desktop, if available
   - Open the RVT in the matching or newer Revit version.
   - Create a clean 3D view with only the categories you want.
   - Export to IFC for BIM structure, or FBX for a mesh-oriented path.
   - IFC is usually the best interchange format for the next automated step.

2. Autodesk Platform Services
   - Model Derivative can translate RVT files to derivatives and can export IFC, but custom IFC settings and view-specific exports are limited.
   - Revit Automation API can run a custom Revit app bundle and export IFC with more control. It needs Autodesk developer credentials.

3. Commercial converters
   - ODA BimRv SDK can work with Revit files without the native Revit application and supports Revit-to-IFC workflows.
   - CAD Exchanger and similar converter products advertise RVT import/export to IFC/glTF/OBJ-style formats.

Avoid uploading the RVT to random online converters unless the model is safe to share externally.

## GitHub projects worth using

Checked on 2026-06-29.

### RVT to IFC

- [Autodesk/revit-ifc](https://github.com/Autodesk/revit-ifc): Autodesk's open-source Revit IFC exporter add-in. This is useful if you have Revit desktop or are building a Revit/APS export workflow. It does not, by itself, give us a standalone RVT reader.
- [ADN-DevTech/aps-revit-ifc-exporter-appbundle](https://github.com/ADN-DevTech/aps-revit-ifc-exporter-appbundle): Autodesk sample app bundle for APS Design Automation. Best GitHub starting point if we want cloud export with Revit IFC options.
- [autodesk-platform-services/aps-revit.ifc.scheduler](https://github.com/autodesk-platform-services/aps-revit.ifc.scheduler): Autodesk APS sample that converts ACC/BIM360-hosted RVT files to IFC through Model Derivative. Useful pattern if the RVT is already in Autodesk Construction Cloud.
- [simonmoreau/RevitToIFCApp](https://github.com/simonmoreau/RevitToIFCApp): Forge/APS web app that uploads RVT and downloads IFC. Useful as a reference app, but it still needs Autodesk developer credentials.

### IFC to mesh

- [IfcOpenShell/IfcOpenShell](https://github.com/IfcOpenShell/IfcOpenShell): Best open-source project after the RVT is converted to IFC. It includes `IfcConvert` for IFC-to-OBJ/glTF/DAE/STL-style conversion workflows.

### Mesh to Minecraft or voxel formats

- [LucasDower/ObjToSchematic](https://github.com/LucasDower/ObjToSchematic): Visual OBJ-to-Minecraft converter. It supports Minecraft schematic-style outputs and is probably the fastest manual path after exporting OBJ.
- [Sloimayyy/mcschematic](https://github.com/Sloimayyy/mcschematic): Python library for writing Java Edition Minecraft schematic files. Good target for extending `scripts/voxelize_mesh.py` from CSV output to `.schem`.
- [SmylerMC/litemapy](https://github.com/SmylerMC/litemapy): Python library for reading/writing Litematica `.litematic` files. Good target if the desired output is for the Litematica mod.
- [Briiqn/obj2schem](https://github.com/Briiqn/obj2schem): OBJ-to-Sponge V3 schematic converter with texture color matching. Requires NVIDIA CUDA, so it is less portable on this Mac.
- [Zarbuz/FileToVox](https://github.com/Zarbuz/FileToVox): Converts many formats, including OBJ and schematic, to MagicaVoxel `.vox`. Useful for voxel preview/intermediate workflows, not the direct Minecraft path.
- [City-of-Helsinki/mesh_to_schematic](https://github.com/City-of-Helsinki/mesh_to_schematic): Archived MIT-licensed mesh-to-schematic tool. Interesting reference because it targets colored Minecraft schematics from 3D meshes, but it is not maintained.
- [skairunner/threed2vox](https://github.com/skairunner/threed2vox): Older 3D-model-to-Minecraft converter. Useful reference only.
- [idryanov/2schematic](https://github.com/idryanov/2schematic): Very old LGPL 3D-to-schematic converter. Useful reference only.

For this project, the most practical GitHub-backed path is:

```text
RVT -> IFC with Autodesk/revit-ifc or APS app bundle
IFC -> OBJ/GLB with IfcOpenShell
OBJ/GLB -> block CSV with scripts/voxelize_mesh.py
block CSV -> .schem/.litematic with mcschematic or litemapy
```

## IFC to mesh

Once you have an IFC file, use IfcOpenShell's `IfcConvert`:

```sh
IfcConvert "UNBC Model.ifc" "UNBC Model.glb"
```

or:

```sh
IfcConvert "UNBC Model.ifc" "UNBC Model.obj"
```

`GLB` is compact and convenient. `OBJ` is easy to inspect and is widely supported.

If `IfcConvert` is not installed, the tested Python path in this folder is:

```sh
python3 scripts/ifc_to_mesh.py "UNBC Model.ifc" "out/mesh/unbc.obj"
```

## Mesh to voxel map

This repo includes a local voxelizer:

```sh
python3 scripts/voxelize_mesh.py "UNBC Model.glb" --pitch 1.0 --out-dir out/voxels
```

Outputs:

- `voxels.npz`: compact numpy arrays for downstream scripts.
- `voxels.json`: metadata and voxel coordinates.
- `blocks.csv`: Minecraft-oriented block coordinates where vertical mesh `Z` is mapped to Minecraft `Y`.
- `preview_boxes.obj`: an optional preview mesh made from voxel cubes.

Use `--pitch` to control scale. If the mesh is in meters, `--pitch 1.0` means one voxel per meter. For Minecraft, that usually means one block per meter.

Useful flags:

```sh
python3 scripts/voxelize_mesh.py "UNBC Model.glb" --pitch 0.5 --fill --block minecraft:stone --out-dir out/voxels
```

Use a larger pitch first. Building-scale models can produce millions of voxels at fine resolution.

## Blocks to Minecraft schematic

After voxelization:

```sh
python3 scripts/blocks_to_minecraft.py out/voxels/blocks.csv out/minecraft/unbc.schem --format schem
```

or for Litematica:

```sh
python3 scripts/blocks_to_minecraft.py out/voxels/blocks.csv out/minecraft/unbc.litematic --format litematic
```
