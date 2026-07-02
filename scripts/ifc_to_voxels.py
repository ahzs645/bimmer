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
  `minecraft:*_stairs` blocks (facing from the ascent direction, corner shapes
  via the vanilla algorithm); thin floor plates can become `*_slab`s with
  --floor-slabs. Both render as real block models in the minecraft-web-client
  renderer (see RENDERERS.md).
* SPIRAL STAIRS: SPIRAL_STAIR assemblies are rebuilt as clean walkable spirals
  (newel + winding oriented treads, ends anchored to the measured start/end
  angles) instead of voxelized into an unclimbable blob (--spiral).
* OVERRIDES: --overrides JSON can pin individual doors by IfcDoor GlobalId
  (skip / raise / facing / leaves); out/<name>/doors.csv maps every door's
  GlobalId to where it landed.
* FUNCTIONAL DOORS: every IfcDoor becomes a real, openable `minecraft:*_door`
  (two halves, oriented to the wall) sitting in a walk-through opening and
  anchored on top of the adjacent walking floor, instead of a solid block
  plugging the doorway. IfcRailing -> oak_fence with explicit connection
  states so railings render as connected post-and-rail runs (swap
  CLASS_BLOCKS["railing"] for another *_fence).

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
# Solid/structural beats transparent; stairs beat EVERYTHING solid (walls
# included): stair flights in stairwells run flush against the shaft walls, so
# at coarse pitches the outer ring of treads lands in the same cells as the
# wall ring — if walls won, spiral/half-turn staircases lost their walking
# path and became unclimbable (verified on the UNBC model's spiral stair).
CLASS_PRIORITY = [
    "glass", "railing", "frame", "roof", "floor", "structure", "wall", "other", "stair",
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


def extract(model, threads: int, spiral_mode: str = "synth"):
    """Iterate geometry once.

    Returns (solid meshes by class, door meshes, spiral assemblies, stats).
    With spiral_mode='synth', the flights/stringers of every SPIRAL_STAIR
    assembly are routed to `spirals` (per assembly) instead of the merged
    'stair' class, so synth_spiral_stairs() can replace them with a clean,
    walkable Minecraft spiral (their voxelization at coarse pitch is a
    jumpy, wall-pinched blob).
    """
    settings = ifcopenshell.geom.settings()
    settings.set("use-world-coords", True)

    # product id -> spiral assembly id, for SPIRAL_STAIR flights and stringers
    spiral_part_of: dict[int, int] = {}
    if spiral_mode == "synth":
        for st in model.by_type("IfcStair"):
            if getattr(st, "ShapeType", None) != "SPIRAL_STAIR":
                continue
            for rel in st.IsDecomposedBy or []:
                for obj in rel.RelatedObjects:
                    if obj.is_a() in ("IfcStairFlight", "IfcMember"):
                        spiral_part_of[obj.id()] = st.id()

    iterator = ifcopenshell.geom.iterator(settings, model, threads)
    if not iterator.initialize():
        raise RuntimeError("Geometry iterator failed to initialize (no geometry?)")

    verts_by_class: dict[str, list[np.ndarray]] = defaultdict(list)
    faces_by_class: dict[str, list[np.ndarray]] = defaultdict(list)
    offset_by_class: dict[str, int] = defaultdict(int)
    door_meshes: list[dict] = []  # [{verts, width_m, gid}] in world metres
    spirals: dict[int, list[np.ndarray]] = defaultdict(list)  # assembly id -> vert arrays
    unit_scale = ifcopenshell.util.unit.calculate_unit_scale(model)  # file unit -> metres
    type_counts: Counter = Counter()
    excluded_counts: Counter = Counter()
    processed = 0

    while True:
        shape = iterator.get()
        element = model.by_id(shape.id)
        ifc_type = element.is_a()
        geom = shape.geometry
        v = np.asarray(geom.verts, dtype=np.float64).reshape((-1, 3))
        f = np.asarray(geom.faces, dtype=np.int64).reshape((-1, 3))

        if ifc_type in EXCLUDE_TYPES:
            excluded_counts[ifc_type] += 1
        elif ifc_type in DOOR_TYPES:
            if len(v):
                w = getattr(element, "OverallWidth", None)
                door_meshes.append({"verts": v, "width_m": (w * unit_scale) if w else None,
                                    "gid": element.GlobalId})
                type_counts[ifc_type] += 1
                processed += 1
        elif shape.id in spiral_part_of and len(v):
            spirals[spiral_part_of[shape.id]].append(v)
            type_counts[ifc_type] += 1
            processed += 1
        elif len(v) and len(f):
            cls = class_for(ifc_type)
            # IfcMember covers both curtain-wall mullions AND stair stringers;
            # reclassify members that decompose a stair/ramp assembly so
            # stringers voxelize with the staircase, not as "frame" concrete.
            if ifc_type == "IfcMember":
                dec = element.Decomposes
                if dec and dec[0].RelatingObject.is_a() in ("IfcStair", "IfcRamp"):
                    cls = "stair"
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
        "spiral_assemblies": len(spirals),
    }
    return meshes, door_meshes, spirals, stats


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


def place_doors(winner, grid, door_verts, mode, overrides=None):
    """Carve each door opening to air and place a functional two-half door.

    mode: 'functional' (real openable door), 'air' (just a passable gap), or
    'solid' (leave a wood block plugging the opening).
    overrides: optional per-door dict keyed by IfcDoor GlobalId (see
    --overrides): {"skip": bool, "raise": int, "facing": str, "leaves": int}.
    Returns (number of door instances placed, per-door placement records).
    """
    overrides = overrides or {}
    all_min, dims = grid["all_min"], grid["dims"]
    X, plane, pitch = grid["X"], grid["plane"], grid["pitch"]

    def key(x, y, z):
        return int(x) + X * int(y) + plane * int(z)

    # A real door is exactly door_h cells tall (2 at 1 m, ~4 at 0.5 m).
    door_h = max(2, round(2.0 / pitch))

    def door_state(facing, half, hinge):
        return (f"{DOOR_BLOCK}[facing={facing},half={half},"
                f"hinge={hinge},open=false,powered=false]")

    def passable(k):
        w = winner.get(k)
        return w is None or "_door" in w[1]

    def sill_bottom(thin_x, cx, cy, mnz):
        # Probe the room cells to either side ALONG THE WALL NORMAL (never along
        # the wall itself, solid at every height) for walkable surfaces near the
        # sill: a solid cell with door-height headroom above it (walls, mullions
        # and glazing columns fail the headroom test; fences aren't floors).
        # Among all candidates pick the one CLOSEST TO THE IFC SILL (the door
        # mesh bottom) — the sill is authoritative. Taking the highest surface
        # instead hoisted 1000+ facade doors onto adjacent roof decks/terraces
        # a couple of blocks above their true floor. Returns None when no
        # walkable surface is nearby (e.g. a fully glazed curtain wall).
        probes = [(cx - 1, cy), (cx + 1, cy)] if thin_x else [(cx, cy - 1), (cx, cy + 1)]
        cands = []
        for px, py in probes:
            for cz in range(mnz + 2, mnz - 4, -1):
                w = winner.get(key(px, py, cz))
                if w is None or "_door" in w[1] or "_fence" in w[1]:
                    continue
                if all(passable(key(px, py, cz + j)) for j in range(1, door_h + 1)):
                    cands.append(cz + 1)   # bottom = surface + 1
        # Trust the IFC sill: a "surface" more than 2 cells from it is not this
        # door's floor (it's a roof/deck on top of the wall enclosing a plugged
        # doorway) — better to sit exactly at the sill and carve there.
        cands = [b for b in cands if abs(b - mnz) <= 2]
        if not cands:
            return None
        return min(cands, key=lambda b: (abs(b - mnz), b))

    # PASS 1: plan every door on the PRISTINE grid (probing before any carve so
    # no door's carve skews a neighbour's floor probe), then carve, then place.
    plans = []          # functional-door placements
    records = []        # per-door placement info (for doors.csv / overrides)
    placed = 0
    for d in door_verts:
        ov = overrides.get(d.get("gid"), {})
        if ov.get("skip"):
            records.append({"gid": d.get("gid"), "skipped": True})
            continue
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

        if mode == "air":
            # Clear the whole opening footprint (all heights) to a passable gap.
            for x in range(mnx, mxx + 1):
                for y in range(mny, mxy + 1):
                    for z in range(mnz, mxz + 1):
                        winner.pop(key(x, y, z), None)
            placed += 1
            continue

        # Functional door(s): face along the wall normal. The mesh extents give
        # a first guess (thin ~0.15 m axis = normal), but door families with
        # deep frames/reveals are thinner along the WRONG axis — probing along
        # the wall then anchors the door to the top of the wall (one whole
        # family here landed on the roof). So both axes are scored by how OPEN
        # their probe columns are at sill level (a doorway has room/air on both
        # sides of its normal; along the wall it's solid), and the open axis
        # wins; mesh extents only break ties.
        # MC mapping at unpack: mc_x = mesh_x, mc_z = mesh_y.
        def config(thin_x):
            wide_lo, wide_hi = (mny, mxy) if thin_x else (mnx, mxx)
            wide_cells = wide_hi - wide_lo + 1
            # Fill the opening width with door cells (OverallWidth in cells,
            # pitch-aware: at 1 m a 0.9 m door is 1 cell; at 0.5 m ~2 cells).
            n = max(1, round(d["width_m"] / pitch)) if d.get("width_m") else 1
            if ov.get("leaves"):
                n = int(ov["leaves"])
            n = min(n, wide_cells)
            mid = (wide_lo + wide_hi) // 2
            start = max(wide_lo, mid - (n - 1) // 2)
            coords = [min(start + i, wide_hi) for i in range(n)]
            # Put the leaf in the WALL PLANE, not the bbox middle: a door with
            # a deep frame/threshold spans 2+ cells along its normal, and the
            # middle cell can be the one proud of the wall — the door then
            # stands in the corridor beside a hole. Score each depth cell by
            # the solid wall cells flanking the opening at door height and
            # take the best (ties -> middle, the old behaviour).
            d_lo, d_hi = (mnx, mxx) if thin_x else (mny, mxy)
            mid_d = (d_lo + d_hi) // 2
            fixed = mid_d
            if d_hi > d_lo:
                flanks = (wide_lo - 1, wide_hi + 1)
                best = -1
                for dv in range(d_lo, d_hi + 1):
                    solid = 0
                    for wv in flanks:
                        for cz in range(mnz, mnz + 3):
                            kk = key(dv, wv, cz) if thin_x else key(wv, dv, cz)
                            w = winner.get(kk)
                            if w is not None and "_door" not in w[1]:
                                solid += 1
                    score = solid * 10 - abs(dv - mid_d)   # prefer centre on ties
                    if score > best:
                        best = score
                        fixed = dv
            return coords, fixed, n

        def openness(thin_x, coords, fixed):
            total = 0
            for wv in coords:
                cx, cy = (fixed, wv) if thin_x else (wv, fixed)
                probes = [(cx - 1, cy), (cx + 1, cy)] if thin_x else [(cx, cy - 1), (cx, cy + 1)]
                for px, py in probes:
                    for cz in range(mnz, mnz + 3):
                        if key(px, py, cz) not in winner:
                            total += 1
            return total

        cfg_x, cfg_y = config(True), config(False)
        open_x, open_y = openness(True, *cfg_x[:2]), openness(False, *cfg_y[:2])
        if open_x != open_y:
            thin_x = open_x > open_y
        else:
            ex_m = float(v[:, 0].max() - v[:, 0].min())
            ey_m = float(v[:, 1].max() - v[:, 1].min())
            thin_x = ex_m <= ey_m
        if ov.get("facing"):            # override wins outright
            thin_x = ov["facing"] in ("east", "west")
        facing = ov.get("facing") or ("east" if thin_x else "south")
        coords, fixed, n_leaves = cfg_x if thin_x else cfg_y
        # One floor level per door element: leaves of a double door must not
        # end up a block apart (each probing its own neighbourhood), and
        # overlapping IfcDoors at the same opening must resolve to the same
        # bottom so one door's lower half never half-overwrites another.
        bottoms = []
        for wv in coords:
            cx, cy = (fixed, wv) if thin_x else (wv, fixed)
            b = sill_bottom(thin_x, cx, cy, mnz)
            if b is not None:
                bottoms.append(b)
        bottom = min(bottoms, key=lambda b: (abs(b - mnz), b)) if bottoms else mnz
        bottom += int(ov.get("raise", 0))
        depth = (mnx, mxx) if thin_x else (mny, mxy)
        plans.append((thin_x, facing, bottom, fixed, coords, depth))
        records.append({"gid": d.get("gid"), "facing": facing, "leaves": n_leaves,
                        "bottom": bottom, "fixed": fixed, "coords": coords,
                        "thin_x": thin_x, "sill": mnz})
        placed += 1

    if mode in ("solid", "air"):
        return placed, records

    # PASS 2a: carve every passage — ONLY the passage. The old full-bbox carve
    # also wiped the glazing/framing around wide-framed doors (curtain-wall and
    # shop-front doors carry metres of side panels in their bbox), leaving
    # free-standing doors in blown-out holes. The passage is: each leaf column,
    # through the whole wall depth, door-height tall from the resolved bottom.
    # All carves happen before any placement so adjacent doorways sharing cells
    # can't wipe a freshly placed neighbour leaf.
    for thin_x, facing, bottom, fixed, coords, depth in plans:
        for wv in coords:
            for dv in range(depth[0], depth[1] + 1):
                cx, cy = (dv, wv) if thin_x else (wv, dv)
                for j in range(door_h):
                    winner.pop(key(cx, cy, bottom + j), None)

    # PASS 2b: place the leaves and their thresholds.
    for thin_x, facing, bottom, fixed, coords, depth in plans:
        for wv in coords:
            cx, cy = (fixed, wv) if thin_x else (wv, fixed)
            for j in range(door_h):
                winner[key(cx, cy, bottom + j)] = (
                    100, door_state(facing, "lower" if j == 0 else "upper", "left"))
            # Threshold: never leave a door hanging over a hole. Drop a floor
            # block if the cell directly below the leaf is empty.
            below = key(cx, cy, bottom - 1)
            if below not in winner:
                winner[below] = (50, FLOOR_CUBE)

    # PASS 2.5: drop unpaired door halves. Two overlapping IfcDoors at one
    # opening can resolve to bottoms one cell apart (their meshes differ), so
    # the later door's lower half overwrites the earlier door's upper, leaving
    # a headless lower half beneath a complete door. Keep every upper+lower
    # pair (scanning top-down, so stacked storeys survive) and carve the rest.
    door_cols: dict[tuple, dict] = defaultdict(dict)
    for k, (_, b) in list(winner.items()):
        if "_door" in b:
            z = k // plane
            rem = k - z * plane
            door_cols[(rem - (rem // X) * X, rem // X)][z] = "upper" in b
    for (cx, cy), col in door_cols.items():
        paired = set()
        for cz in sorted(col, reverse=True):
            if cz in paired:
                continue
            if col[cz] and (cz - 1) in col and not col[cz - 1]:
                paired.add(cz)
                paired.add(cz - 1)
        for cz in col:
            if cz not in paired:
                winner.pop(key(cx, cy, cz), None)

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
        fc = b.split("facing=")[1].split(",")[0]
        if fc in ("east", "west"):     # wall runs along grid-y
            runs_axis[(fc, int(x), int(z))].append(int(y))
        else:                          # north/south: wall runs along grid-x
            runs_axis[(fc, int(y), int(z))].append(int(x))

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
                    cx, cy = (perp, w) if fc in ("east", "west") else (w, perp)
                    for j in range(door_h):
                        kk = key(cx, cy, z + j)
                        cur = winner.get(kk)
                        if cur and "_door" in cur[1]:
                            winner[kk] = (cur[0], set_hinge(cur[1], hinge))
            run = [] if c is None else [c]

    return placed, records


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
    stair_cells = {k for k, (_, b) in winner.items() if b == STAIR_CUBE}
    converted = 0
    facing_at: dict[int, str] = {}   # refined stair key -> facing (for corner shapes)
    for k in stair_cells:
        z = k // plane
        rem = k - z * plane
        y = rem // X
        x = rem - y * X
        if key(x, y, z + 1) in occupied:           # top not exposed -> underside cube
            continue
        # A rise counts only when the raised neighbour is itself part of the
        # staircase: a wall or a railing fence one level up beside a tread must
        # not steer the facing.
        rises = [(dx, dy) for (dx, dy) in GRID_TO_FACING
                 if key(x + dx, y + dy, z + 1) in stair_cells]
        if not rises or len(rises) >= 3:            # flat landing / ridge -> leave cube
            continue
        # prefer the rise whose opposite (downhill) side is open tread
        facing_dir = next((d for d in rises if key(x - d[0], y - d[1], z) not in occupied), rises[0])
        facing_at[k] = GRID_TO_FACING[facing_dir]
        converted += 1

    # Corner shapes (vanilla algorithm): a stair whose uphill neighbour turns
    # becomes an outer corner, one whose downhill neighbour turns becomes an
    # inner corner. Without this, winding stairs (spiral / curved / half-turn
    # flights) paste with the stored shape=straight and show notched corners --
    # saved worlds and schematic pastes do NOT recompute the shape.
    F2G = {v: k for k, v in GRID_TO_FACING.items()}
    CCW = {"north": "west", "west": "south", "south": "east", "east": "north"}

    def shape_for(k, x, y, z, facing):
        fdx, fdy = F2G[facing]
        uphill = facing_at.get(key(x + fdx, y + fdy, z))
        if uphill is not None and abs(F2G[uphill][0]) != abs(fdx):  # perpendicular axis
            udx, udy = F2G[uphill]
            side = facing_at.get(key(x - udx, y - udy, z))
            if side != facing:
                return "outer_left" if uphill == CCW[facing] else "outer_right"
        downhill = facing_at.get(key(x - fdx, y - fdy, z))
        if downhill is not None and abs(F2G[downhill][0]) != abs(fdx):
            ddx, ddy = F2G[downhill]
            side = facing_at.get(key(x + ddx, y + ddy, z))
            if side != facing:
                return "inner_left" if downhill == CCW[facing] else "inner_right"
        return "straight"

    for k, facing in facing_at.items():
        z = k // plane
        rem = k - z * plane
        y = rem // X
        x = rem - y * X
        shape = shape_for(k, x, y, z, facing)
        winner[k] = (winner[k][0], f"{STAIR_SHAPED}[facing={facing},half=bottom,shape={shape}]")
    return converted


def synth_spiral_stairs(winner, grid, spirals):
    """Replace each SPIRAL_STAIR assembly with a synthesized, walkable spiral.

    A voxelized spiral flight at coarse pitch is a jumpy blob pinched between
    the shaft walls: treads stack in tight columns, so refine_stairs() can't
    orient them and the climb needs jumping. Instead we rebuild the staircase
    from its parameters: a centre newel column and one tread per step winding
    around it on a Chebyshev ring, with the start/end angles and the winding
    direction measured from the real flight mesh so both ends land where the
    IFC stair starts and ends. Rises get oriented stair blocks (facing the
    travel direction), flats get cubes, and headroom above each tread is
    carved. Returns the number of spiral assemblies synthesized.
    """
    all_min, dims = grid["all_min"], grid["dims"]
    X, plane, pitch = grid["X"], grid["plane"], grid["pitch"]
    prio = CLASS_PRIORITY.index("stair")

    def key(x, y, z):
        return int(x) + X * int(y) + plane * int(z)

    def ring_offsets(r):
        # Chebyshev ring of radius r, as (dx, dy, angle) sorted by angle
        offs = [(dx, dy) for dx in range(-r, r + 1) for dy in range(-r, r + 1)
                if max(abs(dx), abs(dy)) == r]
        return sorted(((dx, dy, np.arctan2(dy, dx)) for dx, dy in offs), key=lambda t: t[2])

    built = 0
    for verts in spirals.values():
        V = np.concatenate(verts, axis=0)
        cx_m, cy_m = (V[:, 0].min() + V[:, 0].max()) / 2, (V[:, 1].min() + V[:, 1].max()) / 2
        zmin_m, zmax_m = float(V[:, 2].min()), float(V[:, 2].max())
        h_cells = max(2, round((zmax_m - zmin_m) / pitch))
        # walk line ~ mid-tread: half the outer radius, and never at the shaft
        # wall (the bbox edge), so treads don't displace the enclosing walls
        outer_m = max(V[:, 0].max() - V[:, 0].min(), V[:, 1].max() - V[:, 1].min()) / 2
        r = max(1, min(3, round(0.55 * outer_m / pitch)))

        # winding + start/end angles: circular mean of vertex angles per z-band,
        # unwrapped so multi-revolution spirals keep their full sweep
        nb = max(4, h_cells * 2)
        bands = np.clip(((V[:, 2] - zmin_m) / max(zmax_m - zmin_m, 1e-9) * nb).astype(int), 0, nb - 1)
        ang = np.arctan2(V[:, 1] - cy_m, V[:, 0] - cx_m)
        means = []
        for b in range(nb):
            sel = bands == b
            if sel.sum():
                means.append(np.arctan2(np.sin(ang[sel]).mean(), np.cos(ang[sel]).mean()))
        if len(means) < 2:
            continue
        means = np.unwrap(np.array(means))
        theta0, sweep = float(means[0]), float(means[-1] - means[0])
        if abs(sweep) < 0.5:  # degenerate — not actually winding
            continue

        ccx = int(round((cx_m - all_min[0]) / pitch))
        ccy = int(round((cy_m - all_min[1]) / pitch))
        z0 = int(round((zmin_m - all_min[2]) / pitch))

        ring = ring_offsets(r)
        # guarantee at least one ring cell per rise: a noisy (under-measured)
        # sweep would otherwise spread the rises over too few cells and force
        # 2-block jumps mid-flight. Extends past the measured end angle if the
        # geometry was too short — walkability beats exact end alignment.
        min_sweep = (h_cells + 1) * (2 * np.pi / len(ring))
        if abs(sweep) < min_sweep:
            sweep = np.sign(sweep) * min_sweep
        # trace the ring cells the sweep crosses, THEN spread the rises over
        # them — assigning heights per angular step instead would stack a rise
        # on an unchanged cell (a vertical jump mid-flight)
        n = max(h_cells * 2, int(round(abs(sweep) / (2 * np.pi / len(ring)))) * 2)
        cells_seq = []
        for i in range(n + 1):
            th = theta0 + sweep * i / n
            dx, dy, _ = min(ring, key=lambda t: abs(np.angle(np.exp(1j * (t[2] - th)))))
            cell = (ccx + dx, ccy + dy)
            if not cells_seq or cells_seq[-1] != cell:
                cells_seq.append(cell)
        m = len(cells_seq)
        path = [(px, py, z0 + round(j * h_cells / max(m - 1, 1)))
                for j, (px, py) in enumerate(cells_seq)]

        occupied_path = {(px, py, pz) for px, py, pz in path}
        for i, (px, py, pz) in enumerate(path):
            if i and pz > path[i - 1][2]:
                tx, ty = px - path[i - 1][0], py - path[i - 1][1]
                if (tx, ty) not in GRID_TO_FACING:   # diagonal move: pick dominant axis
                    tx, ty = (tx, 0) if abs(tx) >= abs(ty) else (0, ty)
                facing = GRID_TO_FACING.get((tx, ty), "north")
                winner[key(px, py, pz)] = (
                    prio, f"{STAIR_SHAPED}[facing={facing},half=bottom,shape=straight]")
            else:
                winner[key(px, py, pz)] = (prio, STAIR_CUBE)
            # headroom: clear 2 cells above the tread unless another tread is there
            for j in (1, 2):
                if (px, py, pz + j) not in occupied_path:
                    winner.pop(key(px, py, pz + j), None)
        # newel column
        for zi in range(z0, z0 + h_cells + 1):
            if (ccx, ccy, zi) not in occupied_path:
                winner[key(ccx, ccy, zi)] = (prio, STAIR_CUBE)
        built += 1
    return built


def refine_fences(winner, grid):
    """Write connection states onto railing fence blocks.

    Fence arms (north/east/south/west) are stored block-state properties: a
    bare `minecraft:oak_fence` renders as an isolated post in saved worlds,
    schematic pastes and prismarine-based renderers, because nothing triggers
    the in-game neighbour update that would compute the connections. Connect
    each fence to adjacent fences and to full-cube solids (not doors, stairs,
    or slabs, which vanilla fences don't visually join on those faces).
    Returns the number of fences given at least one connection.
    """
    X, plane = grid["X"], grid["plane"]

    def key(x, y, z):
        return int(x) + X * int(y) + plane * int(z)

    fence_block = CLASS_BLOCKS["railing"]
    fence_set = {k for k, (_, b) in winner.items() if b.split("[")[0] == fence_block}

    # Collapse vertical stacks to a SINGLE fence, like a vanilla Minecraft
    # railing: a ~1.1 m guardrail voxelizes into 2 stacked cells at 1 m pitch,
    # but one fence block already reads (and collides) as a railing — the
    # stacked look is wrong. Adjacent-above fence cells are always the same
    # railing (storeys are several cells apart), so keep only the bottom cell.
    stacked = set(fence_set)   # membership snapshot: "had a fence below" must
    for k in stacked:          # use the ORIGINAL stack, not the shrinking set
        z = k // plane
        rem = k - z * plane
        if key(rem - (rem // X) * X, rem // X, z - 1) in stacked:
            winner.pop(k, None)
            fence_set.discard(k)

    # Stair-flight railings round onto the treads themselves at coarse pitch:
    # a fence standing ON a stair block plugs the flight (fences collide 1.5
    # blocks tall), so the staircase becomes unwalkable. Drop those — the
    # treads matter more than the guardrail. Fences on floors/decks stay.
    for k in list(fence_set):
        z = k // plane
        rem = k - z * plane
        below = winner.get(key(rem - (rem // X) * X, rem // X, z - 1))
        if below is not None and below[1].split("[")[0] in (STAIR_CUBE, STAIR_SHAPED):
            winner.pop(k, None)
            fence_set.discard(k)
    fence_keys = list(fence_set)

    def connects(k):
        if k in fence_set:
            return True
        w = winner.get(k)
        if w is None:
            return False
        b = w[1]
        return not any(s in b for s in ("_door", "_stairs[", "_slab", "_fence"))

    # grid (dx, dy) -> fence arm property, same axis mapping as GRID_TO_FACING
    arms = {"north": (0, 1), "east": (1, 0), "south": (0, -1), "west": (-1, 0)}
    connected = 0
    for k in fence_keys:
        z = k // plane
        rem = k - z * plane
        y = rem // X
        x = rem - y * X
        props = {a: connects(key(x + dx, y + dy, z)) for a, (dx, dy) in arms.items()}
        if any(props.values()):
            connected += 1
        state = ",".join(f"{a}={'true' if v else 'false'}" for a, v in sorted(props.items()))
        winner[k] = (winner[k][0], f"{fence_block}[{state}]")
    return connected


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
    ap.add_argument("--spiral", choices=["synth", "voxel"], default="synth",
                    help="SPIRAL_STAIR handling: 'synth' (default) rebuilds a clean walkable "
                         "spiral from the stair's parameters; 'voxel' keeps raw voxelization")
    ap.add_argument("--overrides", type=Path, default=None,
                    help="JSON overrides, e.g. {\"doors\": {\"<GlobalId>\": "
                         "{\"skip\": true, \"raise\": 1, \"facing\": \"north\", \"leaves\": 2}}} "
                         "(GlobalIds are listed in out/<name>/doors.csv)")
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

    overrides = {}
    if args.overrides:
        overrides = json.loads(args.overrides.read_text(encoding="utf-8"))

    print(f"Opening {ifc_path.name} ...", flush=True)
    model = ifcopenshell.open(str(ifc_path))
    print(f"Extracting geometry with {args.threads} threads ...", flush=True)
    meshes, door_verts, spirals, ex_stats = extract(model, args.threads, args.spiral)
    print("Solid faces by class:", ex_stats["solid_faces_by_class"], flush=True)
    print(f"Door elements: {ex_stats['door_elements']}", flush=True)

    print(f"Voxelizing at pitch={args.pitch} m ...", flush=True)
    winner, grid, per_class = voxelize_solids(meshes, door_verts, args.pitch, args.fill)
    placed, door_records = place_doors(winner, grid, door_verts, args.doors,
                                       overrides.get("doors"))
    print(f"Placed {placed} {args.doors} doors", flush=True)

    stairs_converted = slabs_converted = 0
    if args.stairs == "real":
        stairs_converted = refine_stairs(winner, grid)
        print(f"Refined {stairs_converted} stair cubes -> oriented stairs", flush=True)
    spirals_built = synth_spiral_stairs(winner, grid, spirals) if spirals else 0
    if spirals_built:
        print(f"Synthesized {spirals_built} walkable spiral staircase(s)", flush=True)
    if args.floor_slabs:
        slabs_converted = refine_floor_slabs(winner, grid)
        print(f"Refined {slabs_converted} thin floor cubes -> slabs", flush=True)
    fences_connected = refine_fences(winner, grid)
    print(f"Connected {fences_connected} railing fences", flush=True)

    write_stats = unpack_and_write(winner, grid, out_dir)

    # doors.csv: every door's GlobalId + where it landed in schematic coords,
    # so stubborn doors can be hand-tuned via --overrides.
    shift = write_stats["origin_shift_xyz"]
    with (out_dir / "doors.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["global_id", "x", "y", "z", "facing", "leaves", "sill_offset", "skipped"])
        for r in door_records:
            if r.get("skipped"):
                w.writerow([r["gid"], "", "", "", "", "", "", "yes"])
                continue
            wv = r["coords"][0]
            gx, gy = (r["fixed"], wv) if r["thin_x"] else (wv, r["fixed"])
            w.writerow([r["gid"], gx - shift[0], r["bottom"] - shift[1], -gy - shift[2],
                        r["facing"], r["leaves"], r["bottom"] - r["sill"], ""])

    summary = {
        "input_ifc": str(ifc_path),
        "schema": model.schema,
        "pitch_m": args.pitch,
        "door_mode": args.doors,
        "doors_placed": placed,
        "stairs_mode": args.stairs,
        "stairs_converted": stairs_converted,
        "spirals_synthesized": spirals_built,
        "slabs_converted": slabs_converted,
        "fences_connected": fences_connected,
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
