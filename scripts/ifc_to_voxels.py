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
* REAL STAIRS: stepped stair-class cubes are refined into oriented
  `minecraft:*_stairs` blocks (facing from the ascent direction); thin floor
  plates can become `*_slab`s with --floor-slabs. Both render as real block
  models in the minecraft-web-client renderer (see RENDERERS.md).
* FUNCTIONAL DOORS: every IfcDoor becomes a real, openable `minecraft:*_door`
  (two halves, oriented to the wall) sitting in a walk-through opening, instead
  of a solid block plugging the doorway. IfcRailing -> oak_fence (renders as a
  real post-and-rail fence; swap CLASS_BLOCKS["railing"] for another *_fence).

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
    "railing": "minecraft:oak_fence",
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

# Refinement targets: the cube block a class voxelizes to -> its real shaped
# block-state. Stairs replace the stepped `stair`-class cubes; slabs replace
# thin single-voxel `floor`-class plates. Both render as real models in the
# minecraft-web-client renderer (see RENDERERS.md).
STAIR_CUBE = CLASS_BLOCKS["stair"]           # minecraft:stone_bricks
STAIR_SHAPED = "minecraft:stone_brick_stairs"
FLOOR_CUBE = CLASS_BLOCKS["floor"]           # minecraft:smooth_stone
SLAB_SHAPED = "minecraft:smooth_stone_slab"

# grid horizontal ascent (dx, dy in plan) -> Minecraft stair `facing`
# (verified against the unpack transform mc = [x, z_up, -y]).
GRID_TO_FACING = {(1, 0): "east", (-1, 0): "west", (0, 1): "north", (0, -1): "south"}


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

    def key(x, y, z):
        return int(x) + X * int(y) + plane * int(z)

    # A real door is exactly door_h cells tall (2 at 1 m, ~4 at 0.5 m).
    door_h = max(2, round(2.0 / pitch))

    def door_state(facing, half, hinge):
        return (f"{DOOR_BLOCK}[facing={facing},half={half},"
                f"hinge={hinge},open=false,powered=false]")

    # PASS 1: carve every opening to air FIRST. Clearing all openings before
    # placing any leaf is essential — adjacent doorways share cells, so a
    # per-door "clear then place" lets one door's carve wipe a neighbour's
    # freshly placed leaf (leaving half-height, upper-less doors).
    plans = []          # functional-door placement, resolved in pass 2
    placed = 0
    for d in door_verts:
        v = d["verts"]
        idx = np.clip(np.round((v - all_min) / pitch).astype(np.int64), 0, dims - 1)
        # mesh index space: axis0=x, axis1=y(plan), axis2=z(up)
        mnx, mny, mnz = (int(a) for a in idx.min(axis=0))
        mxx, mxy, mxz = (int(a) for a in idx.max(axis=0))

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
        wide_lo, wide_hi = (mny, mxy) if thin_x else (mnx, mxx)
        wide_cells = wide_hi - wide_lo + 1
        # Fill the opening width with door cells (OverallWidth in cells, pitch-aware:
        # at 1 m a 0.9 m door is 1 cell; at 0.5 m it is ~2 contiguous cells).
        n_leaves = max(1, round(d["width_m"] / pitch)) if d.get("width_m") else 1
        n_leaves = min(n_leaves, wide_cells)
        mid = (wide_lo + wide_hi) // 2
        start = max(wide_lo, mid - (n_leaves - 1) // 2)
        coords = [min(start + i, wide_hi) for i in range(n_leaves)]
        fixed = (mnx + mxx) // 2 if thin_x else (mny + mxy) // 2
        plans.append((thin_x, facing, mnz, fixed, coords))
        placed += 1

    if mode in ("solid", "air"):
        return placed

    # PASS 2: with every opening carved, anchor each leaf to the room floor and
    # build a door_h-tall, correctly hinged door.
    def floor_top(thin_x, cx, cy, mnz):
        # Probe the room cells to either side ALONG THE WALL NORMAL (never along
        # the wall itself, solid at every height) for the highest floor top near
        # the sill. Scanning up to mnz+1 also lifts doors whose mesh bottom dips a
        # cell into the slab (the "half-sunk into the floor" case). Returns None
        # when no floor voxel is nearby (e.g. a glazed curtain wall).
        probes = [(cx - 1, cy), (cx + 1, cy)] if thin_x else [(cx, cy - 1), (cx, cy + 1)]
        best = None
        for px, py in probes:
            for cz in range(mnz + 1, mnz - 4, -1):
                w = winner.get(key(px, py, cz))
                if w and "_door" not in w[1]:
                    best = cz if best is None else max(best, cz)
                    break
        return best

    for thin_x, facing, mnz, fixed, coords in plans:
        for wv in coords:
            cx, cy = (fixed, wv) if thin_x else (wv, fixed)
            ft = floor_top(thin_x, cx, cy, mnz)
            bottom = (ft + 1) if ft is not None else mnz
            for j in range(door_h):
                winner[key(cx, cy, bottom + j)] = (
                    100, door_state(facing, "lower" if j == 0 else "upper", "left"))
            # Threshold: never leave a door hanging over a hole (the carve above
            # removes the slab under the leaf). Drop a floor block if the cell
            # directly below the leaf is empty.
            below = key(cx, cy, bottom - 1)
            if below not in winner:
                winner[below] = (50, FLOOR_CUBE)

    # PASS 3: mirror double-door hinges. A run of adjacent same-facing lower
    # halves at the same height is one visual double door — even when the leaves
    # come from SEPARATE IfcDoor elements (each a single leaf, which pass 2 left
    # all-hinge=left). Re-hinge each run so the panels meet in the middle. East
    # doors run along grid-y, south doors along grid-x; uppers copy their lower.
    def set_hinge(blockstr, hinge):
        return blockstr.replace("hinge=left", f"hinge={hinge}").replace("hinge=right", f"hinge={hinge}")

    runs_axis = defaultdict(list)   # (facing, perp_fixed, cz) -> [wide coord, ...]
    for k, (_, b) in winner.items():
        if "_door" not in b or "half=lower" not in b:
            continue
        z = k // plane
        rem = k - z * plane
        y = rem // X
        x = rem - y * X
        if "facing=east" in b:
            runs_axis[("east", int(x), int(z))].append(int(y))
        else:
            runs_axis[("south", int(y), int(z))].append(int(x))

    for (fc, perp, z), wides in runs_axis.items():
        wides.sort()
        run = [wides[0]]
        for c in wides[1:] + [None]:
            if c is not None and c == run[-1] + 1:
                run.append(c)
                continue
            L = len(run)
            if L >= 2:  # single leaves keep the default hinge
                for i, w in enumerate(run):
                    hinge = "right" if i < L // 2 else "left"
                    cx, cy = (perp, w) if fc == "east" else (w, perp)
                    for j in range(door_h):
                        kk = key(cx, cy, z + j)
                        cur = winner.get(kk)
                        if cur and "_door" in cur[1]:
                            winner[kk] = (cur[0], set_hinge(cur[1], hinge))
            run = [] if c is None else [c]

    return placed


def refine_stairs(winner, grid):
    """Replace stepped `stair`-class cubes with oriented Minecraft stair blocks.

    A voxelized staircase is a stepped ramp of cubes. For each stair cube whose
    top is exposed (nothing directly above), we look at the four horizontal
    neighbours: the direction whose column rises (a cube one level up) is the
    ascent direction, which is exactly the Minecraft stair `facing`. Cells with
    no rise (flat landings) or rises on 3+/opposite sides (ridges) stay full
    cubes. Underside cubes keep their block (something sits above them).
    Returns the number of cubes converted.
    """
    X, plane = grid["X"], grid["plane"]

    def key(x, y, z):
        return int(x) + X * int(y) + plane * int(z)

    occupied = set(winner.keys())
    stair_keys = [k for k, (_, b) in winner.items() if b == STAIR_CUBE]
    converted = 0
    for k in stair_keys:
        z = k // plane
        rem = k - z * plane
        y = rem // X
        x = rem - y * X
        if key(x, y, z + 1) in occupied:           # top not exposed -> underside cube
            continue
        rises = [(dx, dy) for (dx, dy) in GRID_TO_FACING
                 if key(x + dx, y + dy, z + 1) in occupied]
        if not rises or len(rises) >= 3:            # flat landing / ridge -> leave cube
            continue
        # prefer the rise whose opposite (downhill) side is open tread
        facing_dir = next((d for d in rises if key(x - d[0], y - d[1], z) not in occupied), rises[0])
        facing = GRID_TO_FACING[facing_dir]
        winner[k] = (winner[k][0], f"{STAIR_SHAPED}[facing={facing},half=bottom,shape=straight]")
        converted += 1
    return converted


def refine_floor_slabs(winner, grid):
    """Convert thin, single-voxel `floor`-class plates to bottom slabs.

    A floor plate that is one voxel thick with air directly above and below
    (e.g. a balcony/landing plate, not a thick structural slab resting on
    something) is half-height in reality; a full cube over-thickens it. Floors
    that sit on structure (cube below) or stack (cube above) stay full.
    Returns the number of cubes converted.
    """
    X, plane = grid["X"], grid["plane"]

    def key(x, y, z):
        return int(x) + X * int(y) + plane * int(z)

    occupied = set(winner.keys())
    floor_keys = [k for k, (_, b) in winner.items() if b == FLOOR_CUBE]
    converted = 0
    for k in floor_keys:
        z = k // plane
        rem = k - z * plane
        y = rem // X
        x = rem - y * X
        if key(x, y, z + 1) not in occupied and key(x, y, z - 1) not in occupied:
            winner[k] = (winner[k][0], f"{SLAB_SHAPED}[type=bottom]")
            converted += 1
    return converted


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
    ap.add_argument("--stairs", choices=["real", "cube"], default="real",
                    help="'real' = oriented *_stairs blocks (default); 'cube' = stepped stone-brick cubes")
    ap.add_argument("--floor-slabs", action="store_true",
                    help="convert thin single-voxel floor plates to bottom slabs (default: full cubes)")
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

    stairs_converted = slabs_converted = 0
    if args.stairs == "real":
        stairs_converted = refine_stairs(winner, grid)
        print(f"Refined {stairs_converted} stair cubes -> oriented stairs", flush=True)
    if args.floor_slabs:
        slabs_converted = refine_floor_slabs(winner, grid)
        print(f"Refined {slabs_converted} thin floor cubes -> slabs", flush=True)

    write_stats = unpack_and_write(winner, grid, out_dir)

    summary = {
        "input_ifc": str(ifc_path),
        "schema": model.schema,
        "pitch_m": args.pitch,
        "door_mode": args.doors,
        "doors_placed": placed,
        "stairs_mode": args.stairs,
        "stairs_converted": stairs_converted,
        "slabs_converted": slabs_converted,
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
