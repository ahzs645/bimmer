#!/usr/bin/env python3
"""Semantic-aware IFC -> voxel converter with functional Minecraft blocks.

What this does beyond a plain mesh voxelizer:

* Uses ifcopenshell.geom.iterator (multi-threaded), not a serial create_shape
  loop -- ~Ncores faster on large models.
* Excludes non-solid products (IfcOpeningElement voids, IfcSpace, annotations)
  so door/window openings are NOT re-filled as solid blocks.
* Maps each IFC element category to a sensible Minecraft block (glass for
  glazing, concrete for walls, smooth stone for slabs, ...) on a shared integer
  voxel grid, resolving overlaps with a per-class priority rule.
* FUNCTIONAL DOORS: every IfcDoor becomes a real, openable `minecraft:*_door`
  (two halves, oriented to the wall) sitting in a walk-through opening, instead
  of a solid block plugging the doorway. IfcRailing -> iron_bars.

Geometry note: IfcOpenShell returns vertices in METRES regardless of the file's
display unit, so --pitch is in metres (pitch=1.0 -> 1 block per metre).
Functional doors fit best around pitch=1.0 (a typical doorway is ~1 wide x 2
tall = exactly one Minecraft door).
"""

from __future__ import annotations

import argparse
import csv
import json
import multiprocessing
from collections import Counter, defaultdict
from pathlib import Path

import ifcopenshell
import ifcopenshell.geom
import ifcopenshell.util.unit
import numpy as np
import trimesh

# IFC element type -> coarse semantic class
SEMANTIC_CLASSES = {
    "IfcWindow": "glass",
    "IfcPlate": "glass",          # curtain-wall infill panels are usually glazing
    "IfcCurtainWall": "glass",
    "IfcMember": "frame",         # curtain-wall mullions / framing
    "IfcRailing": "railing",
    "IfcWall": "wall",
    "IfcWallStandardCase": "wall",
    "IfcColumn": "structure",
    "IfcBeam": "structure",
    "IfcSlab": "floor",
    "IfcCovering": "floor",
    "IfcRoof": "roof",
    "IfcStair": "stair",
    "IfcStairFlight": "stair",
    "IfcRamp": "stair",
    "IfcRampFlight": "stair",
    # IfcDoor is handled specially (functional door), not as a solid class.
}

# Products that must NOT become solid voxels
EXCLUDE_TYPES = {
    "IfcOpeningElement",  # subtractive voids (doors/windows)
    "IfcSpace",           # room volumes
    "IfcAnnotation",
    "IfcGrid",
    "IfcSite",            # often a huge topo surface; skip by default
}

# Special elements handled outside the solid-voxelization path
DOOR_TYPES = {"IfcDoor"}

CLASS_BLOCKS = {
    "glass": "minecraft:light_blue_stained_glass",
    "frame": "minecraft:gray_concrete",
    "railing": "minecraft:iron_bars",
    "wall": "minecraft:white_concrete",
    "structure": "minecraft:stone",
    "floor": "minecraft:smooth_stone",
    "roof": "minecraft:deepslate_tiles",
    "stair": "minecraft:stone_bricks",
    "other": "minecraft:light_gray_concrete",
}

# When several classes land in one cell, the one LATER in this list wins.
# Solid/structural beats transparent; stairs beat floors/roofs so a staircase
# isn't hidden by the slab voxels it overlaps.
CLASS_PRIORITY = [
    "glass", "railing", "frame", "roof", "floor", "stair", "structure", "wall", "other",
]

# Minecraft block id used for functional doors (must be a *_door)
DOOR_BLOCK = "minecraft:oak_door"


def class_for(ifc_type: str) -> str:
    return SEMANTIC_CLASSES.get(ifc_type, "other")


def extract(model, threads: int):
    """Iterate geometry once. Returns (solid meshes by class, list of door meshes, stats)."""
    settings = ifcopenshell.geom.settings()
    settings.set("use-world-coords", True)

    iterator = ifcopenshell.geom.iterator(settings, model, threads)
    if not iterator.initialize():
        raise RuntimeError("Geometry iterator failed to initialize (no geometry?)")

    verts_by_class: dict[str, list[np.ndarray]] = defaultdict(list)
    faces_by_class: dict[str, list[np.ndarray]] = defaultdict(list)
    offset_by_class: dict[str, int] = defaultdict(int)
    door_meshes: list[dict] = []  # [{verts, width_m}] in world metres
    unit_scale = ifcopenshell.util.unit.calculate_unit_scale(model)  # file unit -> metres
    type_counts: Counter = Counter()
    excluded_counts: Counter = Counter()
    processed = 0

    while True:
        shape = iterator.get()
        ifc_type = model.by_id(shape.id).is_a()
        geom = shape.geometry
        v = np.asarray(geom.verts, dtype=np.float64).reshape((-1, 3))
        f = np.asarray(geom.faces, dtype=np.int64).reshape((-1, 3))

        if ifc_type in EXCLUDE_TYPES:
            excluded_counts[ifc_type] += 1
        elif ifc_type in DOOR_TYPES:
            if len(v):
                w = getattr(model.by_id(shape.id), "OverallWidth", None)
                door_meshes.append({"verts": v, "width_m": (w * unit_scale) if w else None})
                type_counts[ifc_type] += 1
                processed += 1
        elif len(v) and len(f):
            cls = class_for(ifc_type)
            verts_by_class[cls].append(v)
            faces_by_class[cls].append(f + offset_by_class[cls])
            offset_by_class[cls] += len(v)
            type_counts[ifc_type] += 1
            processed += 1

        if not iterator.next():
            break

    meshes: dict[str, trimesh.Trimesh] = {}
    for cls in verts_by_class:
        V = np.concatenate(verts_by_class[cls], axis=0)
        F = np.concatenate(faces_by_class[cls], axis=0)
        meshes[cls] = trimesh.Trimesh(vertices=V, faces=F, process=False)

    stats = {
        "processed_products": processed,
        "type_counts": dict(type_counts.most_common()),
        "excluded_counts": dict(excluded_counts),
        "solid_faces_by_class": {c: int(len(m.faces)) for c, m in meshes.items()},
        "door_elements": len(door_meshes),
    }
    return meshes, door_meshes, stats


def voxelize_solids(meshes, door_verts, pitch, fill):
    """Voxelize each class on a shared mesh-index grid; resolve overlaps by priority.

    Returns winner: packed_key -> (priority_index, block_id), plus grid params.
    The grid origin includes door bounds so doors align to the same lattice.
    """
    mins = [m.bounds[0] for m in meshes.values()] + [d["verts"].min(axis=0) for d in door_verts]
    maxs = [m.bounds[1] for m in meshes.values()] + [d["verts"].max(axis=0) for d in door_verts]
    all_min = np.array(mins).min(axis=0)
    all_max = np.array(maxs).max(axis=0)

    dims = np.ceil((all_max - all_min) / pitch).astype(np.int64) + 3
    X, Y, Z = int(dims[0]), int(dims[1]), int(dims[2])
    plane = X * Y

    prio_index = {c: i for i, c in enumerate(CLASS_PRIORITY)}
    winner: dict[int, tuple] = {}
    per_class_voxels: dict[str, int] = {}

    for cls, mesh in meshes.items():
        vg = mesh.voxelized(pitch=pitch)
        if fill:
            try:
                vg = vg.fill()
            except Exception:
                pass
        centers = np.asarray(vg.points, dtype=np.float64)
        if centers.size == 0:
            per_class_voxels[cls] = 0
            continue
        idx = np.clip(np.round((centers - all_min) / pitch).astype(np.int64), 0, dims - 1)
        keys = idx[:, 0] + X * idx[:, 1] + plane * idx[:, 2]
        per_class_voxels[cls] = int(len(np.unique(keys)))
        pi = prio_index.get(cls, prio_index["other"])
        block = CLASS_BLOCKS.get(cls, CLASS_BLOCKS["other"])
        for k in keys.tolist():
            cur = winner.get(k)
            if cur is None or pi > cur[0]:
                winner[k] = (pi, block)

    grid = {"all_min": all_min, "dims": dims, "X": X, "plane": plane, "pitch": pitch}
    return winner, grid, per_class_voxels


def place_doors(winner, grid, door_verts, mode):
    """Carve each door opening to air and place a functional two-half door.

    mode: 'functional' (real openable door), 'air' (just a passable gap), or
    'solid' (leave a wood block plugging the opening).
    Returns the number of door instances placed.
    """
    all_min, dims = grid["all_min"], grid["dims"]
    X, plane, pitch = grid["X"], grid["plane"], grid["pitch"]
    placed = 0

    for d in door_verts:
        v = d["verts"]
        idx = np.clip(np.round((v - all_min) / pitch).astype(np.int64), 0, dims - 1)
        # mesh index space: axis0=x, axis1=y(plan), axis2=z(up)
        mnx, mny, mnz = idx.min(axis=0)
        mxx, mxy, mxz = idx.max(axis=0)
        wx, wy = (mxx - mnx + 1), (mxy - mny + 1)

        def key(x, y, z):
            return int(x) + X * int(y) + plane * int(z)

        if mode == "solid":
            for x in range(mnx, mxx + 1):
                for y in range(mny, mxy + 1):
                    for z in range(mnz, mxz + 1):
                        winner[key(x, y, z)] = (99, "minecraft:oak_planks")
            placed += 1
            continue

        # Clear the whole opening footprint (all heights) to air so it is walkable.
        for x in range(mnx, mxx + 1):
            for y in range(mny, mxy + 1):
                for z in range(mnz, mxz + 1):
                    winner.pop(key(x, y, z), None)

        if mode == "air":
            placed += 1
            continue

        # Functional door(s): face along the thin horizontal axis (= wall normal).
        # Orientation is decided from the RAW METRE extents (thin ~0.15 m vs wide
        # ~0.9 m), not the voxel-cell footprint, which is ambiguous at 1 m (both
        # round to 1 cell). Leaf COUNT comes from the IFC OverallWidth.
        # MC mapping at unpack: mc_x = mesh_x, mc_z = mesh_y.
        ex_m = float(v[:, 0].max() - v[:, 0].min())
        ey_m = float(v[:, 1].max() - v[:, 1].min())
        thin_x = ex_m <= ey_m
        facing = "east" if thin_x else "south"  # east -> faces +/-x, south -> faces +/-z
        lower = f"{DOOR_BLOCK}[facing={facing},half=lower,hinge=left,open=false,powered=false]"
        upper = f"{DOOR_BLOCK}[facing={facing},half=upper,hinge=left,open=false,powered=false]"
        wide_lo, wide_hi = (mny, mxy) if thin_x else (mnx, mxx)
        wide_cells = wide_hi - wide_lo + 1
        # Fill the opening width with door cells (OverallWidth in cells, pitch-aware:
        # at 1 m a 0.9 m door is 1 cell; at 0.5 m it is ~2 contiguous cells).
        n_leaves = max(1, round(d["width_m"] / pitch)) if d.get("width_m") else 1
        n_leaves = min(n_leaves, wide_cells)
        mid = (wide_lo + wide_hi) // 2
        start = max(wide_lo, mid - (n_leaves - 1) // 2)
        coords = [min(start + i, wide_hi) for i in range(n_leaves)]
        # A real door: ~2 m tall, standing ON the floor (not filling the whole
        # opening). Anchor the bottom to the actual floor by probing the room
        # cells beside the doorway (along the walk/normal direction), then build
        # up door_h cells (2 at 1 m, ~4 at 0.5 m), capped to the opening top.
        door_h = max(2, round(2.0 / pitch))

        def floor_under(cx, cy):
            probes = [(cx - 1, cy), (cx + 1, cy)] if thin_x else [(cx, cy - 1), (cx, cy + 1)]
            found = []
            for px, py in probes:
                for cz in range(mnz + 2, mnz - 4, -1):
                    if key(px, py, cz) in winner:
                        found.append(cz)
                        break
            return min(found) if found else None  # min = the floor, not a wall-top

        def place_leaf(cx, cy):
            fz = floor_under(cx, cy)
            bottom = (fz + 1) if fz is not None else mnz
            top = min(bottom + door_h - 1, mxz)
            for i, cz in enumerate(range(bottom, top + 1)):
                winner[key(cx, cy, cz)] = (100, lower if i == 0 else upper)

        if thin_x:
            cx = (mnx + mxx) // 2
            for cy in coords:
                place_leaf(cx, cy)
        else:
            cy = (mny + mxy) // 2
            for cx in coords:
                place_leaf(cx, cy)
        placed += 1

    return placed


def unpack_and_write(winner, grid, out_dir):
    X, plane, pitch = grid["X"], grid["plane"], grid["pitch"]
    keys = np.fromiter(winner.keys(), dtype=np.int64, count=len(winner))
    blocks = [winner[int(k)][1] for k in keys]
    zs = keys // plane
    rem = keys - zs * plane
    ys = rem // X
    xs = rem - ys * X
    # mesh (x, y, z-up) -> Minecraft (x, y-up=z, z=-y)
    # Negate the swapped horizontal axis: a bare y<->z swap is orientation-
    # reversing (det -1) and would mirror the model N<->S. IFC +Y is North,
    # which is Minecraft -Z, so z = -mesh_y keeps handedness (det +1).
    mc = np.stack([xs, zs, -ys], axis=1)
    cmin = mc.min(axis=0)
    mc -= cmin

    out_dir.mkdir(parents=True, exist_ok=True)
    counts: Counter = Counter()
    with (out_dir / "blocks.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["x", "y", "z", "block"])
        for (x, y, z), b in zip(mc.tolist(), blocks):
            counts[b.split("[")[0]] += 1
            w.writerow([x, y, z, b])
    return {
        "origin_shift_xyz": cmin.tolist(),
        "minecraft_grid_xyz": (mc.max(axis=0) + 1).tolist(),
        "total_blocks": int(len(blocks)),
        "blocks_by_id": dict(counts.most_common()),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ifc", type=Path)
    ap.add_argument("--pitch", type=float, default=1.0, help="Voxel size in METRES")
    ap.add_argument("--doors", choices=["functional", "air", "solid"], default="functional",
                    help="how to represent IfcDoor (default: functional openable door)")
    ap.add_argument("--fill", action="store_true",
                    help="Solid-fill each class (rarely wanted; meaningless for non-watertight IFC)")
    ap.add_argument("--out-dir", type=Path, default=Path("out/unbc"))
    ap.add_argument("--threads", type=int, default=max(1, multiprocessing.cpu_count() - 1))
    args = ap.parse_args()

    ifc_path = args.ifc.expanduser().resolve()
    if not ifc_path.exists():
        raise SystemExit(f"Input IFC does not exist: {ifc_path}")
    out_dir = args.out_dir.expanduser().resolve()

    print(f"Opening {ifc_path.name} ...", flush=True)
    model = ifcopenshell.open(str(ifc_path))
    print(f"Extracting geometry with {args.threads} threads ...", flush=True)
    meshes, door_verts, ex_stats = extract(model, args.threads)
    print("Solid faces by class:", ex_stats["solid_faces_by_class"], flush=True)
    print(f"Door elements: {ex_stats['door_elements']}", flush=True)

    print(f"Voxelizing at pitch={args.pitch} m ...", flush=True)
    winner, grid, per_class = voxelize_solids(meshes, door_verts, args.pitch, args.fill)
    placed = place_doors(winner, grid, door_verts, args.doors)
    print(f"Placed {placed} {args.doors} doors", flush=True)
    write_stats = unpack_and_write(winner, grid, out_dir)

    summary = {
        "input_ifc": str(ifc_path),
        "schema": model.schema,
        "pitch_m": args.pitch,
        "door_mode": args.doors,
        "doors_placed": placed,
        "world_bounds_min_m": grid["all_min"].tolist(),
        "model_size_m_xyz": (grid["dims"] * args.pitch).tolist(),
        **ex_stats,
        "per_class_voxels": per_class,
        **write_stats,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps({k: summary[k] for k in (
        "total_blocks", "doors_placed", "door_mode", "minecraft_grid_xyz", "blocks_by_id",
    )}, indent=2))
    print(f"\nWrote {out_dir/'blocks.csv'} and {out_dir/'summary.json'}")


if __name__ == "__main__":
    main()
