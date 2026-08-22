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

# Rectification lives in its own module because it reads IFC wall placements
# and nothing else -- no geometry, no meshing. Keeping it here meant a tool
# that only wants to LOOK at the rectification had to import trimesh and the
# whole voxel engine to reach it. See RECTIFY.md and preview_rectify.py.
from rectify import apply_wing, compute_wing_transforms, wing_for_point, wing_records

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


def stair_shape(stair) -> str | None:
    """The stair's shape enum, whichever schema the file speaks.

    IFC2X3 calls it `ShapeType`; IFC4 renamed it `PredefinedType` with the same
    enumerators. Reading only `ShapeType` looks harmless -- getattr returns
    None and the loop skips -- but on an IFC4 file it silently disables spiral
    synthesis for EVERY spiral stair in the model, and the artifact (a jumpy
    wall-pinched blob in the stairwell) reads as a voxelization limit rather
    than a missed branch. The UNBC Autodesk export is IFC2X3; Reviter writes
    IFC4, so both spellings reach this engine.
    """
    return getattr(stair, "ShapeType", None) or getattr(stair, "PredefinedType", None)


def extract(model, threads: int, spiral_mode: str = "synth", wings=None):
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
            if stair_shape(st) != "SPIRAL_STAIR":
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
    stair_groups: dict[int, list[np.ndarray]] = defaultdict(list)  # stair assembly id -> vert arrays
    unit_scale = ifcopenshell.util.unit.calculate_unit_scale(model)  # file unit -> metres
    wing_cache: dict[int, dict | None] = {}
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

        if wings and len(v):
            # rectification: rotate every element of an off-grid wing into
            # the wing's own (orthogonal) frame. Assignment is cached per
            # AGGREGATE root so a stair and all its flights/stringers/
            # railings rotate together even if a member's own centroid
            # falls just outside the wing hull.
            dec = getattr(element, "Decomposes", None)
            root = dec[0].RelatingObject.id() if dec else shape.id
            if root not in wing_cache:
                cx, cy = v[:, 0].mean(), v[:, 1].mean()
                wing_cache[root] = wing_for_point(wings, cx, cy)
            if wing_cache[root] is not None:
                v = apply_wing(wing_cache[root], v)

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
            # Stair geometry is ALSO grouped per assembly so stairwells whose
            # voxelization is impassable (scissor flights capping each other
            # at coarse pitch) can be rebuilt as clean walkable runs later.
            if cls == "stair":
                dec = getattr(element, "Decomposes", None)
                aid = dec[0].RelatingObject.id() if dec else shape.id
                stair_groups[aid].append(v)
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
        "stair_assemblies": len(stair_groups),
    }
    return meshes, door_meshes, spirals, stair_groups, stats


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
    plan_recs = []      # record of plans[i] (records also holds skipped doors)
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
        plan_recs.append(records[-1])
        placed += 1

    if mode in ("solid", "air"):
        return placed, records

    # Harmonize levels of nearby doors on the same wall that share the same
    # IFC sill: their floor probes can diverge by one cell (a slab edge that
    # rounds a cell higher beside one of them), which reads as doors randomly
    # jumping half a step along a corridor. When sills agree the doors belong
    # at ONE level — snap the cluster to the bottom closest to the sill.
    def plan_span(pl):
        return min(pl[4]), max(pl[4])
    for i in range(len(plans)):
        ti, fi, bi, xi, ci, di = plans[i]
        si = plan_recs[i]["sill"]
        for j in range(i + 1, len(plans)):
            tj, fj, bj, xj, cj, dj = plans[j]
            if ti != tj or abs(xi - xj) > 1 or bi == bj:
                continue
            if plan_recs[j]["sill"] != si:
                continue
            lo_i, hi_i = plan_span(plans[i])
            lo_j, hi_j = plan_span(plans[j])
            if max(lo_i, lo_j) - min(hi_i, hi_j) > 3:   # wide-axis gap
                continue
            best = min(bi, bj, key=lambda b: (abs(b - si), b))
            plans[i] = (ti, fi, best, xi, ci, di)
            plans[j] = (tj, fj, best, xj, cj, dj)
            plan_recs[i]["bottom"] = plan_recs[j]["bottom"] = best
            bi = best

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

    # PASS 2c: anchor storefront doors into their glazing. A curtain-wall
    # entrance door is flanked by glass too thin to voxelize at door level
    # (mullion/frame geometry crowds it out), leaving the leaf standing alone
    # in the plaza while the glazing starts a cell above. Where a leaf's side
    # cell is empty at door height but the SAME column holds glass or frame
    # just above, pull that glazing down to the floor beside the leaf.
    glass_prio = CLASS_PRIORITY.index("glass")
    glass_block = CLASS_BLOCKS["glass"]
    anchor_blocks = (CLASS_BLOCKS["glass"], CLASS_BLOCKS["frame"])

    def side_cell(fixed, thin_x, sv):
        return (fixed, sv) if thin_x else (sv, fixed)

    for thin_x, facing, bottom, fixed, coords, depth in plans:
        lo, hi = min(coords), max(coords)

        def occupied_at(sv):
            cx, cy = side_cell(fixed, thin_x, sv)
            return any(key(cx, cy, bottom + j) in winner for j in range(door_h))

        for wv, sgn in ((lo, -1), (hi, 1)):
            if occupied_at(wv + sgn):
                continue
            cx, cy = side_cell(fixed, thin_x, wv + sgn)
            # (a) glazing directly above the flanking cell -> pull it down
            above = [winner.get(key(cx, cy, bottom + door_h + j)) for j in range(0, 2)]
            if any(w is not None and w[1].split("[")[0] in anchor_blocks for w in above):
                for j in range(door_h):
                    winner[key(cx, cy, bottom + j)] = (glass_prio, glass_block)
                continue
            # (b) whole glazing bay missing: if the door is FREE-standing (open
            # on both sides), bridge to the nearest solid within 3 cells with
            # glass so the entrance reads as a storefront, not a lone leaf.
            # One-sided doors are left alone — their open side is usually a
            # real passage.
            if occupied_at(lo - 1) or occupied_at(hi + 1):
                continue
            for dist in (2, 3):
                if occupied_at(wv + sgn * dist):
                    for g in range(1, dist):
                        gx, gy = side_cell(fixed, thin_x, wv + sgn * g)
                        for j in range(door_h):
                            winner[key(gx, gy, bottom + j)] = (glass_prio, glass_block)
                    break

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


def carve_stair_headroom(winner, grid):
    """Re-open stairwell floor openings above stair flights.

    IfcOpenShell subtracts the stairwell hole from each slab, but at coarse
    pitch the slab's surface voxels round INTO the opening and cap the flight
    below: a player (2 blocks tall) can't pass, so every storey above the
    first becomes unreachable on foot. For every stair block, clear the two
    cells above its walking surface of FLOOR/ROOF-class blocks (the closed
    hole). Walls and other stair flights are never touched — a wall beside a
    flight is real, and scissor flights legitimately cross. Returns the
    number of cells cleared.
    """
    X, plane = grid["X"], grid["plane"]

    def key(x, y, z):
        return int(x) + X * int(y) + plane * int(z)

    clearable = {CLASS_BLOCKS["floor"], CLASS_BLOCKS["roof"], SLAB_SHAPED.split("[")[0]}
    stair_keys = [k for k, (_, b) in winner.items()
                  if b.split("[")[0] in (STAIR_CUBE, STAIR_SHAPED)]
    cleared = 0
    for k in stair_keys:
        z = k // plane
        rem = k - z * plane
        y = rem // X
        x = rem - y * X
        for j in (1, 2, 3):   # feet, head, and one spare over the tread top
            kk = key(x, y, z + j)
            w = winner.get(kk)
            if w is not None and w[1].split("[")[0] in clearable:
                winner.pop(kk)
                cleared += 1
    return cleared


def rebuild_blocked_stairs(winner, grid, stair_groups):
    """Rebuild stair assemblies whose voxelized flights a player cannot climb.

    At 1 m/block a storey is ~3 cells tall. A scissor / half-turn stair's
    return flight then crosses directly over the lower flight, so no matter
    how the treads voxelize there is no 2-block player headroom — the upper
    storeys become unreachable on foot (observed: everything above storey 4
    was 0% reachable). Voxelizing harder cannot fix a discretization
    impossibility, so, like the spiral synthesis, we REBUILD: for each
    assembly whose treads are substantially head-blocked, clear its voxelized
    stair cells and lay ONE clean switchback run through the well — one rise
    per cell, oriented stair blocks, cubes at turning points, guaranteed
    headroom carved above every tread. Open-air stairs (unblocked treads)
    are left exactly as voxelized. Returns the number of assemblies rebuilt.
    """
    all_min, dims = grid["all_min"], grid["dims"]
    X, plane, pitch = grid["X"], grid["plane"], grid["pitch"]
    prio = CLASS_PRIORITY.index("stair")

    def key(x, y, z):
        return int(x) + X * int(y) + plane * int(z)

    def block_at(x, y, z):
        w = winner.get(key(x, y, z))
        return None if w is None else w[1].split("[")[0]

    stair_family = (STAIR_CUBE, STAIR_SHAPED)
    clearable = {CLASS_BLOCKS["floor"], CLASS_BLOCKS["roof"],
                 SLAB_SHAPED.split("[")[0], *stair_family}
    fence_block = CLASS_BLOCKS["railing"]
    rebuilt = 0
    protected: set = set()   # cells laid by earlier rebuilds — never clear

    # Several IFC assemblies can share one stairwell (scissor pairs are
    # modelled as "Stair:NNN" and "Stair:NNN:2"; fire-escape towers stack one
    # assembly per storey on one footprint) — rebuilding them separately makes
    # each clear the other's fresh run. Merge groups into WELLS — but only
    # when their horizontal FOOTPRINTS genuinely overlap. Mere bbox contact is
    # not enough: entrance steps and ramps that touch a stair tower's box
    # edge-on would chain into one long "well", and the synthesized run would
    # march across the whole facade instead of switchbacking inside the tower.
    def same_well(lo_a, hi_a, lo_b, hi_b):
        for ax in (0, 1):   # horizontal axes: need real overlap, not a touch
            ov = min(hi_a[ax], hi_b[ax]) - max(lo_a[ax], lo_b[ax])
            need = min(1.5, 0.4 * min(hi_a[ax] - lo_a[ax], hi_b[ax] - lo_b[ax]))
            if ov < need:
                return False
        return min(hi_a[2], hi_b[2]) >= max(lo_a[2], lo_b[2])  # z: contact ok
    merged = []
    for verts in stair_groups.values():
        V = np.concatenate(verts, axis=0)
        merged.append([V.min(axis=0) - 0.5, V.max(axis=0) + 0.5, [V]])
    changed = True
    while changed:
        changed = False
        out = []
        for lo, hi, vs in merged:
            for m in out:
                if same_well(lo, hi, m[0], m[1]):
                    m[0] = np.minimum(m[0], lo)
                    m[1] = np.maximum(m[1], hi)
                    m[2].extend(vs)
                    changed = True
                    break
            else:
                out.append([lo, hi, vs])
        merged = out

    for lo_m, hi_m, verts_list in merged:
        V = np.concatenate(verts_list, axis=0)
        idx0 = np.floor((V.min(axis=0) - all_min) / pitch).astype(int)
        idx1 = np.ceil((V.max(axis=0) - all_min) / pitch).astype(int)
        x0, y0, z0 = (max(0, int(a)) for a in idx0)
        x1, y1, z1 = (int(a) for a in np.minimum(idx1, dims - 1))
        h = z1 - z0
        if h < 2:
            continue

        # collect this well's stair cells and CLIMB-TEST it: can a 2-block
        # player actually walk (8-dir, 1-block steps) from the bottom storey
        # to the top storey through the current voxels? "Treads look open" is
        # not enough — chunky scissor blobs often dead-end mid-well.
        cells = [(x, y, z) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)
                 for z in range(z0, z1 + 1) if block_at(x, y, z) in stair_family]
        if not cells:
            continue

        px0, px1 = max(0, x0 - 1), min(int(dims[0]) - 1, x1 + 1)
        py0, py1 = max(0, y0 - 1), min(int(dims[1]) - 1, y1 + 1)

        def stand_cells():
            out = set()
            for x in range(px0, px1 + 1):
                for y in range(py0, py1 + 1):
                    for z in range(max(0, z0 - 1), z1 + 2):
                        w = winner.get(key(x, y, z))
                        if w is None or "_door" in w[1] or "_fence" in w[1]:
                            continue
                        if (winner.get(key(x, y, z + 1)) is None
                                and winner.get(key(x, y, z + 2)) is None):
                            out.add((x, y, z + 1))
            return out

        stair_shaped_base = STAIR_SHAPED.split("[")[0]

        def climbs(stand):
            starts = {p for p in stand if p[2] <= z0 + 1}
            goal_z = z1
            frontier = list(starts)
            seenl = set(starts)
            while frontier:
                x, y, z = frontier.pop()
                if z >= goal_z:
                    return True
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        if dx == dy == 0:
                            continue
                        for dz in (0, 1, -1, -2):
                            q = (x + dx, y + dy, z + dz)
                            if q not in stand or q in seenl:
                                continue
                            if dz == 1:
                                # stepping onto an oriented stair block is a
                                # walk (2-air suffices); stepping onto a full
                                # cube is a JUMP and needs clearance above the
                                # head at the source, or vanilla physics blocks
                                # it (this let the terraced lobby stair pass
                                # while being unclimbable in game)
                                if (block_at(q[0], q[1], q[2] - 1) != stair_shaped_base
                                        and winner.get(key(x, y, z + 2)) is not None):
                                    continue
                            seenl.add(q)
                            frontier.append(q)
            return False

        if climbs(stand_cells()):
            continue

        # -- rebuild: clear the voxelized flights and stale railing fences
        for (x, y, z) in cells:
            if (x, y, z) not in protected:
                winner.pop(key(x, y, z), None)
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                for z in range(z0, z1 + 2):
                    if (x, y, z) in protected:
                        continue
                    w = winner.get(key(x, y, z))
                    if w is not None and w[1].split("[")[0] == fence_block:
                        winner.pop(key(x, y, z))

        # long axis = run direction; start at the end nearest the lowest treads
        along_x = (x1 - x0) >= (y1 - y0)
        lo, hi = (x0, x1) if along_x else (y0, y1)
        s_lo, s_hi = (y0, y1) if along_x else (x0, x1)
        low_band = V[V[:, 2] <= V[:, 2].min() + 0.6]
        start_m = low_band[:, 0 if along_x else 1].mean()
        start_c = int(round((start_m - all_min[0 if along_x else 1]) / pitch))
        forward = 1 if abs(start_c - lo) <= abs(start_c - hi) else -1
        pos = lo if forward == 1 else hi

        # run column: NOT blindly the middle — a stair tower's middle line can
        # be its curtain-wall/door plane, which threads the run through every
        # storey exit door (door cells can't take treads, and door thresholds
        # veto the headroom pops -> the run gets unclimbable gaps). Pick the
        # lane with the least doors/protected mass along the whole climb.
        def lane_cost(c):
            cost = 0
            for p in range(lo, hi + 1):
                cx, cy = (p, c) if along_x else (c, p)
                for z in range(z0, z1 + 2):
                    w = winner.get(key(cx, cy, z))
                    if w is None:
                        continue
                    base = w[1].split("[")[0]
                    if "_door" in w[1]:
                        cost += 25
                    elif base not in clearable and base != fence_block:
                        cost += 1
            return cost
        col = min(range(s_lo, s_hi + 1), key=lane_cost)

        placed_cells = []
        z = z0
        while z <= z1:
            cx, cy = (pos, col) if along_x else (col, pos)
            nxt = pos + forward
            turning = nxt < lo or nxt > hi
            if turning:
                b = STAIR_CUBE                      # landing corner
                forward = -forward
                if col + 1 <= s_hi:
                    col += 1
                elif col - 1 >= s_lo:
                    col -= 1
            else:
                d = (forward, 0) if along_x else (0, forward)
                facing = GRID_TO_FACING[d]
                b = f"{STAIR_SHAPED}[facing={facing},half=bottom,shape=straight]"
            cur = winner.get(key(cx, cy, z))
            if cur is not None and "_door" in cur[1]:
                # never overwrite a door — cross its threshold FLAT (don't
                # rise this step): a skipped tread plus a rise would leave a
                # 2-cell jump no player can climb.
                if not turning:
                    pos += forward
                continue
            winner[key(cx, cy, z)] = (prio, b)
            placed_cells.append((cx, cy, z))
            if not turning:
                pos += forward
            z += 1

        # headroom above every tread; support cube under floating treads.
        # Walls/structure/mullions clear too: the synthesized run often
        # pierces a rounded wall line inside the shaft, and a concrete cell
        # hovering 1-2 above a tread makes the whole flight unclimbable
        # (same "the walking path is the point" rule as CLASS_PRIORITY —
        # this was exactly why rebuilt fire-escape wells still failed).
        # Glass clears too: in the glazed fire-escape towers the shaft
        # glazing rounds inward directly over the run, and a pane 1-2 cells
        # above a tread blocks the climb exactly like concrete. Only cells
        # straight above placed treads are affected, so the puncture is a
        # couple of interior panes, not the facade.
        placed_set = set(placed_cells)
        head_clearable = clearable | {fence_block, CLASS_BLOCKS["wall"],
                                      CLASS_BLOCKS["structure"],
                                      CLASS_BLOCKS["frame"],
                                      CLASS_BLOCKS["glass"],
                                      CLASS_BLOCKS["other"]}
        for (cx, cy, z) in placed_cells:
            for j in (1, 2, 3):
                if (cx, cy, z + j) in placed_set or (cx, cy, z + j) in protected:
                    continue
                w = winner.get(key(cx, cy, z + j))
                if w is None or w[1].split("[")[0] not in head_clearable:
                    continue
                over = winner.get(key(cx, cy, z + j + 1))
                if over is not None and "_door" in over[1]:
                    continue   # that's a door threshold — leave it standing
                winner.pop(key(cx, cy, z + j))
            if z >= 1 and winner.get(key(cx, cy, z - 1)) is None:
                winner[key(cx, cy, z - 1)] = (prio, STAIR_CUBE)
                protected.add((cx, cy, z - 1))
        protected.update(placed_set)
        rebuilt += 1
        # verify the rebuilt well actually climbs now — a silent failure
        # here is how broken fire escapes shipped the first time
        if not climbs(stand_cells()):
            print(f"  WARNING: rebuilt stairwell x[{x0},{x1}] y[{y0},{y1}] "
                  f"z[{z0},{z1}] still fails its climb test", flush=True)
    return rebuilt


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


def unblock_door_passages(winner, grid):
    """Break dead-end doors through to the room they serve.

    Pass 2a carves the passage through the door's own bounding-box depth, but
    walls thicker than the door frame — and small rooms whose opposite wall
    rounds into the doorway at coarse pitches — leave 1..3 solid cells
    directly in front of the leaf. The player opens the door onto bare
    concrete. For every lower door half, probe outward along the facing
    normal on both sides: if a short run (<= 3 cells) of carveable solid ends
    at a 2-high open cell with a standable floor, carve that run door-height
    tall. Runs that never reach open space (fully swallowed closets), runs
    through protected classes (stairs, railings, glass, other doors) and
    openings with no floor (exterior drops) are left alone.
    Returns (doors_unblocked, cells_carved).
    """
    X, plane, pitch = grid["X"], grid["plane"], grid["pitch"]
    door_h = max(2, round(2.0 / pitch))
    max_run = max(3, round(3.0 / pitch))  # up to 3 m of wall at any pitch
    # winner stores (priority_index, block_id): decide carveability by block.
    # Glass, railings, stairs and other doors are protected — carving them
    # would puncture facades or destroy walking paths.
    carveable = {CLASS_BLOCKS[c] for c in
                 ("wall", "floor", "structure", "roof", "frame", "other")}

    def key(x, y, z):
        return int(x) + X * int(y) + plane * int(z)

    def passable(k):
        w = winner.get(k)
        return w is None or "_door" in w[1]

    F2G = {v: k for k, v in GRID_TO_FACING.items()}
    lower_doors = []
    for k, (cls, b) in winner.items():
        if "_door" in b and "half=lower" in b:
            z = k // plane
            rem = k - z * plane
            lower_doors.append((rem - (rem // X) * X, rem // X, z,
                                b.split("facing=")[1].split(",")[0]))

    unblocked = carved = 0
    for x, y, z, facing in lower_doors:
        dx, dy = F2G[facing]
        helped = False
        for s in (1, -1):
            run = []          # solid cells to carve if we break through
            for d in range(1, max_run + 2):
                fx, fy = x + dx * d * s, y + dy * d * s
                col = [key(fx, fy, z + h) for h in range(door_h)]
                if all(passable(c) for c in col):
                    if not run:
                        break                      # already open at d=1
                    # standable landing: support within a 3-cell drop
                    landing = None
                    for drop in range(1, 5):
                        bk = winner.get(key(fx, fy, z - drop))
                        if bk is not None and "_door" not in bk[1]:
                            landing = drop
                            break
                        if not passable(key(fx, fy, z - drop)):
                            break
                    if landing is not None and landing <= 4:
                        for c in run:
                            if c in winner:
                                del winner[c]
                                carved += 1
                        helped = True
                    break
                if d > max_run:
                    break                          # wall too thick: leave it
                solid_cols = [c for c in col if not passable(c)]
                if any(winner[c][1].split("[")[0] not in carveable for c in solid_cols):
                    break                          # protected block in the way
                # a plug cell laterally adjacent to ANOTHER door is that
                # door's flanking wall — carving it leaves the neighbour
                # free-standing in the hallway. Leave the plug alone.
                if any("_door" in winner[key(fx - dy * l, fy + dx * l, z + h)][1]
                       for l in (-1, 1) for h in range(door_h)
                       if key(fx - dy * l, fy + dx * l, z + h) in winner):
                    break
                run.extend(solid_cols)
        if helped:
            unblocked += 1
    return unblocked, carved


def connect_hidden_rooms(winner, grid):
    """Carve hallway doors through to enclosed, door-less rooms.

    Voxel rounding can swallow the entire passage between a corridor door and
    the small room it serves: the door sits flush in the hallway wall while
    the room survives as a sealed air pocket that NO door touches. The plain
    unblock pass (<= 3 m reach) won't cut that deep on purpose — tunnelling
    blindly would puncture unrelated rooms. Here the target is verified
    first: label every enclosed air component, keep only components that
    (a) never reach the outside, (b) touch no door, and (c) contain at least
    one standable cell — those are HIDDEN rooms. Then, for each door side
    still plugged, probe up to ~6 m along the facing normal and carve through
    only when the run is carveable wall mass ending inside a hidden room.
    Returns (doors_connected, hidden_rooms_found, hidden_rooms_left, cells).
    """
    try:
        from scipy import ndimage
    except ImportError:
        return 0, 0, 0, 0
    X, plane, pitch = grid["X"], grid["plane"], grid["pitch"]
    dims = grid["dims"]
    nx, ny, nz = int(dims[0]), int(dims[1]), int(dims[2])
    door_h = max(2, round(2.0 / pitch))
    deep_run = max(6, round(6.0 / pitch))
    carveable = {CLASS_BLOCKS[c] for c in
                 ("wall", "floor", "structure", "roof", "frame", "other")}

    def key(x, y, z):
        return int(x) + X * int(y) + plane * int(z)

    occ = np.zeros((nx, ny, nz), dtype=bool)
    doors_mask = np.zeros((nx, ny, nz), dtype=bool)
    keys = np.fromiter(winner.keys(), dtype=np.int64, count=len(winner))
    zs = keys // plane
    rem = keys - zs * plane
    ys = rem // X
    xs = rem - ys * X
    occ[xs, ys, zs] = True
    lower_doors = []
    for k, (cls, b) in winner.items():
        if "_door" not in b:
            continue
        z = k // plane
        r = k - z * plane
        x, y = r - (r // X) * X, r // X
        doors_mask[x, y, z] = True
        if "half=lower" in b:
            lower_doors.append((x, y, z, b.split("facing=")[1].split(",")[0]))

    six = ndimage.generate_binary_structure(3, 1)
    labels, n = ndimage.label(~occ, structure=six)
    # components reaching the array boundary are the outdoors
    open_ids = set(np.unique(np.concatenate([
        labels[0, :, :].ravel(), labels[-1, :, :].ravel(),
        labels[:, 0, :].ravel(), labels[:, -1, :].ravel(),
        labels[:, :, 0].ravel(), labels[:, :, -1].ravel()]))) - {0}
    # components already served by a door (any air cell 6-adjacent to a door)
    door_halo = ndimage.binary_dilation(doors_mask, structure=six)
    served_ids = set(np.unique(labels[door_halo & ~occ])) - {0}
    # standable somewhere: air with solid below and air above
    standable = (~occ[:, :, 1:-1]) & occ[:, :, :-2] & (~occ[:, :, 2:])
    roomy_ids = set(np.unique(labels[:, :, 1:-1][standable])) - {0}
    hidden = (roomy_ids - open_ids - served_ids)
    hidden_found = len(hidden)

    F2G = {v: k for k, v in GRID_TO_FACING.items()}
    connected = carved = 0
    for x, y, z, facing in lower_doors:
        dx, dy = F2G[facing]
        for s in (1, -1):
            run = []
            for d in range(1, deep_run + 1):
                fx, fy = x + dx * d * s, y + dy * d * s
                if not (0 <= fx < nx and 0 <= fy < ny and z + door_h <= nz):
                    break
                col = [(fx, fy, z + h) for h in range(door_h)]
                if not any(occ[c] for c in col):
                    if not run:
                        break                       # already open: not our case
                    comp = labels[fx, fy, z]
                    if comp in hidden:
                        for c in run:
                            winner.pop(key(*c), None)
                            occ[c] = False
                            carved += 1
                        hidden.discard(comp)
                        connected += 1
                    break
                solid = [c for c in col if occ[c]]
                bad = False
                for c in solid:
                    w = winner.get(key(*c))
                    if w is None or w[1].split("[")[0] not in carveable:
                        bad = True
                        break
                # never strip a neighbouring door's flanking wall
                if not bad and any(
                        doors_mask[fx - dy * l, fy + dx * l, z + h]
                        for l in (-1, 1) for h in range(door_h)
                        if 0 <= fx - dy * l < nx and 0 <= fy + dx * l < ny
                        and z + h < nz):
                    bad = True
                if bad:
                    break
                run.extend(solid)
    return connected, hidden_found, len(hidden), carved


def patch_floor_holes(winner, grid, min_ring=6, rounds=3):
    """Make storey plates contiguous: fill pothole gaps in floors/ceilings.

    Surface voxelization of non-watertight slab meshes drops cells - thin
    spots, plate joints and penetrations round out of existence - leaving
    1-2-cell holes in otherwise continuous floor plates. In game they read
    as missing ceiling patches overhead and as pits you can fall through.

    A cell qualifies for patching when the cell BELOW it is empty but at
    least min_ring of its 8 plan-neighbours have solid support at that
    level (so it is a pothole in a plate, not the rim of a real atrium or
    facade edge - big openings never reach 6/8), it is interior (some block
    within 4 above), and no stair block sits within a 3x3 column up to 3
    below (stairwell openings stay open - S8 carved them deliberately).
    Runs a few rounds so 2-cell-wide cracks close from their edges inward.
    Returns cells filled.
    """
    X, plane = grid["X"], grid["plane"]
    floor_prio = CLASS_PRIORITY.index("floor")
    stair_bases = (STAIR_CUBE, STAIR_SHAPED.split("[")[0])

    def key(x, y, z):
        return int(x) + X * int(y) + plane * int(z)

    def unkey(k):
        z = k // plane
        rem = k - z * plane
        return rem - (rem // X) * X, rem // X, z

    DIRS = [(dx, dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1) if (dx, dy) != (0, 0)]
    filled = 0
    for _ in range(rounds):
        # candidate air cells: plan-neighbours of existing blocks
        cands = set()
        for k in winner.keys():
            x, y, z = unkey(k)
            for dx, dy in DIRS:
                if key(x + dx, y + dy, z) not in winner:
                    cands.add((x + dx, y + dy, z))
        add = []
        for (x, y, z) in cands:
            if key(x, y, z - 1) in winner:
                continue
            # never crush the storey below: if a floor lies within 2 cells
            # under the fill, the filled cell would be its walking headroom
            # (this exact bug cost ~3,700 walkable cells on the first try).
            # EXCEPT on sky-open roof decks: there the pocket below is a
            # plenum, not a room, and an uncovered hole drops the player
            # INTO the building - cover those with glass (skylight look);
            # the reachability audit confirms it costs nothing walkable.
            low_floor = key(x, y, z - 2) in winner or key(x, y, z - 3) in winner
            sky_open = not any(key(x, y, z + j) in winner for j in range(0, 10))
            if low_floor and not sky_open:
                continue
            ring = sum(1 for dx, dy in DIRS if key(x + dx, y + dy, z - 1) in winner)
            if ring < min_ring:
                continue
            # no interior-ceiling requirement: roof-deck potholes drop the
            # player INTO the building and patch just as safely (a real
            # skylight voxelizes as glass cells, never as a missing cell,
            # and atrium/light-well openings never reach a 6/8 ring)
            stair_near = False
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for dz in (1, 2, 3):
                        w = winner.get(key(x + dx, y + dy, z - dz))
                        if w is not None and w[1].split("[")[0] in stair_bases:
                            stair_near = True
            if stair_near:
                continue
            add.append((x, y, z - 1, low_floor))
        if not add:
            break
        glass_prio = CLASS_PRIORITY.index("glass")
        for (x, y, z, as_glass) in add:
            if key(x, y, z) not in winner:
                if as_glass:
                    winner[key(x, y, z)] = (glass_prio, CLASS_BLOCKS["glass"])
                else:
                    winner[key(x, y, z)] = (floor_prio, FLOOR_CUBE)
                filled += 1
    return filled


def stamp_terrain_aprons(winner, grid, radius=7, max_door_z=9):
    """Ground exterior doors: grass/dirt aprons where the site should be.

    The IFC has no site terrain, so ground-storey exit doors on the uphill
    side open onto a 1-3 m drop to the flat export ground plane. For every
    ground-storey door with a void side (no support within 4 below the
    stepping-out cell, open sky above), lay a terrain apron: a grass cone
    starting one below the door and sloping down 1 cell per ring until it
    meets existing blocks, dirt-filled 3 deep beneath. Never overwrites
    anything. Also naturally grounds annex wings whose only real-world
    link is outside ground. Returns (doors aproned, cells placed).
    """
    X, plane = grid["X"], grid["plane"]
    Y = plane // X
    floor_prio = CLASS_PRIORITY.index("floor")
    GRASS = "minecraft:grass_block"
    DIRT = "minecraft:dirt"

    def key(x, y, z):
        return int(x) + X * int(y) + plane * int(z)

    def unkey(k):
        z = k // plane
        rem = k - z * plane
        return rem - (rem // X) * X, rem // X, z

    F2G = {v: k for k, v in GRID_TO_FACING.items()}
    doors = []
    for k, (_, b) in list(winner.items()):
        if "_door" in b and "half=lower" in b:
            x, y, z = unkey(k)
            if z <= max_door_z:
                doors.append((x, y, z, b.split("facing=")[1].split(",")[0]))

    aproned = placed = 0
    for (x, y, z, facing) in doors:
        dx, dy = F2G[facing]
        for s in (1, -1):
            ox, oy = x + dx * s, y + dy * s
            # void side: nothing to stand on within 4 below, sky above
            if any(key(ox, oy, z - j) in winner for j in range(1, 5)):
                continue
            if any(key(ox, oy, z + j) in winner for j in range(1, 8)):
                continue
            n_before = placed
            for rx in range(-radius, radius + 1):
                for ry in range(-radius, radius + 1):
                    r = max(abs(rx), abs(ry))
                    top = z - 1 - r          # slope down 1 per ring
                    if top < 0:
                        continue
                    cx, cy = ox + rx, oy + ry
                    if cx < 0 or cy < 0 or cx >= X or cy >= Y:
                        continue             # stay in-grid (key() aliases)
                    if key(cx, cy, top) in winner or key(cx, cy, top + 1) in winner:
                        continue             # never overwrite / never bury
                    winner[key(cx, cy, top)] = (floor_prio, GRASS)
                    placed += 1
                    for d in (1, 2, 3):
                        if top - d >= 0 and key(cx, cy, top - d) not in winner:
                            winner[key(cx, cy, top - d)] = (floor_prio, DIRT)
                            placed += 1
            if placed > n_before:
                aproned += 1
    return aproned, placed


def light_ceilings(winner, grid, spacing=6):
    """Interior lighting: recess sea lanterns into ceilings on a grid.

    The model has no light fixtures we map yet, so interiors are pitch
    black under vanilla lighting. On a spacing x spacing plan grid, every
    interior walking cell (solid support, 2 air, ceiling within 4) gets
    the first solid cell above swapped for a sea lantern - full cube, so
    the storey above keeps its floor; light level 15 pools below.
    Doors, stairs, fences and glass are never replaced.
    Returns lanterns placed.
    """
    X, plane = grid["X"], grid["plane"]
    LANTERN = "minecraft:sea_lantern"
    swappable = {CLASS_BLOCKS[c] for c in ("floor", "roof", "structure", "wall", "other")}

    def key(x, y, z):
        return int(x) + X * int(y) + plane * int(z)

    def unkey(k):
        z = k // plane
        rem = k - z * plane
        return rem - (rem // X) * X, rem // X, z

    lit = 0
    for k in list(winner.keys()):
        x, y, z = unkey(k)
        if x % spacing or y % spacing:
            continue
        # k must be a support with 2-air walking space above
        if key(x, y, z + 1) in winner or key(x, y, z + 2) in winner:
            continue
        for j in (3, 4):
            ck = key(x, y, z + j)
            w = winner.get(ck)
            if w is None:
                continue
            if w[1].split("[")[0] in swappable:
                winner[ck] = (w[0], LANTERN)
                lit += 1
            break
    return lit


def stitch_seams(winner, grid, max_span=14, min_component=30, max_links=60):
    """Phase-2 rectification: reconnect walkable islands with short corridors.

    After per-wing rotation (--rectify) the joints between a rotated wing
    and the main building shear: a corridor stub on the spine side points at
    where the wing corridor USED to be, leaving a wall plug or an air gap of
    a few cells. The same shape of defect exists un-rectified where the
    model has no site terrain (annex wings whose only real-world link is
    outside ground). This pass fixes both the same way the stair rebuild
    fixes stairwells - synthesize the missing piece:

      1. BFS the walkable graph (vanilla physics: 8-dir, +1 up only onto an
         oriented stair or with jump clearance, drops to -3) from the
         ground-level entrance doors.
      2. Component-label the unreachable standable cells; keep components
         of >= min_component cells (real floors, not ledges).
      3. For each island (largest first), find the closest (reachable,
         island) cell pair with |dz| <= 1 within max_span in plan, whose
         straight line crosses only carveable mass (wall/floor/glass/... -
         never doors, stairs or another island's fresh corridor).
      4. Carve the corridor 1 wide x 3 high at the reachable side's level,
         lay FLOOR_CUBE under gaps (an air gap becomes a walkway pad), and
         re-run the BFS so later islands can attach through it.

    Returns (corridors_built, cells_touched).
    """
    X, plane = grid["X"], grid["plane"]
    Y = plane // X

    def key(x, y, z):
        return int(x) + X * int(y) + plane * int(z)

    def unkey(k):
        z = k // plane
        rem = k - z * plane
        return rem - (rem // X) * X, rem // X, z

    stair_base = STAIR_SHAPED.split("[")[0]
    carveable = {CLASS_BLOCKS[c] for c in
                 ("wall", "floor", "structure", "roof", "frame", "glass", "other")}

    def block(x, y, z):
        w = winner.get(key(x, y, z))
        return None if w is None else w[1]

    def passable(x, y, z):
        b = block(x, y, z)
        return b is None or "_door" in b

    def standable(x, y, z):
        # bounds first: key() packs x + X*y + plane*z, so key(-1, y, z)
        # ALIASES the far end of the previous row - once terrain aprons
        # touched the grid edge, the BFS stepped onto phantom cells and
        # marched into negative space until the OOM killer stopped it
        if x < 0 or y < 0 or x >= X or y >= Y or z < 1:
            return False
        b = block(x, y, z - 1)
        return (b is not None and "_door" not in b
                and passable(x, y, z) and passable(x, y, z + 1))

    DIRS = [(dx, dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1) if (dx, dy) != (0, 0)]

    def neighbors(x, y, z):
        for dx, dy in DIRS:
            for dz in (0, 1, -1, -2, -3):
                nx, ny, nz = x + dx, y + dy, z + dz
                if standable(nx, ny, nz):
                    if dz == 1:
                        below = block(nx, ny, nz - 1)
                        walk = below is not None and below.split("[")[0] == stair_base
                        if not walk and not passable(x, y, z + 2):
                            continue
                    yield nx, ny, nz
                    break

    def seeds():
        out = []
        for k, (_, b) in winner.items():
            if "_door" in b and "half=lower" in b:
                x, y, z = unkey(k)
                if z > 4:
                    continue
                for dx, dy in DIRS:
                    if standable(x + dx, y + dy, z):
                        out.append((x + dx, y + dy, z))
        return out

    def bfs(starts):
        seen = set(starts)
        queue = list(starts)
        while queue:
            x, y, z = queue.pop()
            for n in neighbors(x, y, z):
                if n not in seen:
                    seen.add(n)
                    queue.append(n)
        return seen

    built = touched = 0
    log: list[dict] = []
    # islands that fail to link twice stay skipped - re-scanning every
    # far-from-everything island each iteration made the pass quadratic
    # once terrain aprons added many isolated grass islands
    hopeless: dict = {}
    for _round in range(max_links + 8):   # one link per round (re-BFS between)
        reach = bfs(seeds())
        if not reach:
            break
        unreached = set()
        for k in list(winner.keys()):
            x, y, z = unkey(k)
            if (x, y, z + 1) not in reach and standable(x, y, z + 1):
                unreached.add((x, y, z + 1))
        islands = []
        left = set(unreached)
        while left:
            s = left.pop()
            comp = [s]
            queue = [s]
            while queue:
                c = queue.pop()
                for n in neighbors(*c):
                    if n in left:
                        left.discard(n)
                        comp.append(n)
                        queue.append(n)
            if len(comp) >= min_component:
                islands.append(comp)
        if not islands:
            break
        islands.sort(key=len, reverse=True)

        linked = False
        glass_block = CLASS_BLOCKS["glass"]
        frame_block = CLASS_BLOCKS["frame"]
        frame_prio = CLASS_PRIORITY.index("frame")
        rail_block = CLASS_BLOCKS["railing"]
        rail_prio = CLASS_PRIORITY.index("railing")
        stair_family = (STAIR_CUBE, stair_base)

        def line_of(f, t):
            """Feasible corridor from reachable f to island t, or None.

            Returns (cells, cost): cells to carve with per-cell info; cost
            penalizes crossing glazing heavily (an interior wall hole is
            invisible, a curtain-wall hole is not) and open-air pads lightly.
            """
            (rx, ry, rz), (ix, iy, _) = f, t
            n_steps = max(abs(ix - rx), abs(iy - ry))
            cells = []
            cost = 0
            for i in range(1, n_steps):
                px = rx + round((ix - rx) * i / n_steps)
                py = ry + round((iy - ry) * i / n_steps)
                if (px, py, rz) in comp_set or (px, py, rz) in reach:
                    continue
                col_glass = False
                for h in (0, 1, 2):
                    b = block(px, py, rz + h)
                    if b is not None:
                        base = b.split("[")[0]
                        if "_door" in b or base not in carveable:
                            return None
                        if base == glass_block:
                            col_glass = True
                    # never carve a stairwell's headroom or a landing over a
                    # flight: a cell within 3 above a stair block is part of
                    # its climbing envelope (this broke 3 wells before)
                    if any((bb := block(px, py, rz + h - j)) is not None
                           and bb.split("[")[0] in stair_family
                           for j in (1, 2, 3)):
                        return None
                needs_pad = winner.get(key(px, py, rz - 1)) is None
                cells.append((px, py, col_glass, needs_pad))
                cost += 1 + (8 if col_glass else 0) + (1 if needs_pad else 0)
            if not cells:
                # nothing to carve means the gap isn't a solid plug (it's a
                # headroom/level blocker this pass can't fix) - picking such
                # a "free" line would no-op forever
                return None
            return cells, cost

        for comp in islands:
            sig = min(comp)
            if hopeless.get(sig, 0) >= 2:
                continue
            comp_set = set(comp)
            # collect near-minimal candidate pairs, then take the cheapest
            # FEASIBLE line - the shortest is not best when it punches
            # through a curtain wall and a slightly longer one goes through
            # plain interior wall
            best_d = None
            cands = []
            for (x, y, z) in comp:
                for dx in range(-max_span, max_span + 1):
                    for dy in range(-max_span, max_span + 1):
                        d = abs(dx) + abs(dy)
                        if d < 2 or (best_d and d > best_d + 4):
                            continue
                        # flat pairs preferred; +-1-level pairs allowed but
                        # cost extra AND get an oriented stair placed at the
                        # junction so the step is walkable (a bare cube step
                        # may lack jump room -> carved-but-not-connected
                        # links that rebuilt every round until the cap)
                        for dz in (0, 1, -1):
                            cand = (x + dx, y + dy, z + dz)
                            if cand in reach:
                                cands.append((d + (0 if dz == 0 else 3),
                                              (x, y, z), cand))
                                if best_d is None or d < best_d:
                                    best_d = d
            if best_d is None:
                hopeless[sig] = hopeless.get(sig, 0) + 1
                continue
            best_line = None
            for d, isl, rch_ in sorted(cands, key=lambda c: c[0])[:60]:
                if d > best_d + 4:
                    break
                got = line_of(rch_, isl)
                if got is None:
                    continue
                cells, cost = got
                if best_line is None or cost < best_line[0]:
                    best_line = (cost, cells, isl, rch_)
            if best_line is None:
                hopeless[sig] = hopeless.get(sig, 0) + 1
                continue
            _, cells, (ix, iy, iz), (rx, ry, rz) = best_line

            floor_prio = CLASS_PRIORITY.index("floor")
            stair_prio = CLASS_PRIORITY.index("stair")
            carved_kinds: Counter = Counter()
            pads = 0
            # dominant perpendicular, for guardrails and portal frames
            if abs(ix - rx) >= abs(iy - ry):
                perp = ((0, 1), (0, -1))
            else:
                perp = ((1, 0), (-1, 0))
            # longer connectors read as HALLWAYS, not crawl-tunnels: widen
            # carved segments to 2 cells (one parallel lane) when the
            # corridor is >= 3 long. The side lane degrades gracefully -
            # cells that are protected (doors, stair envelopes, glass,
            # existing corridors) just stay, narrowing that spot to 1.
            widen = len(cells) >= 3
            sxw, syw = perp[0]
            for (px, py, col_glass, needs_pad) in cells:
                for h in (0, 1, 2):
                    popped = winner.pop(key(px, py, rz + h), None)
                    if popped is not None:
                        carved_kinds[popped[1].split("[")[0].replace("minecraft:", "")] += 1
                        touched += 1
                if widen and not needs_pad:
                    qx, qy = px + sxw, py + syw
                    side_ok = winner.get(key(qx, qy, rz - 1)) is not None
                    for h in (0, 1, 2):
                        w_ = winner.get(key(qx, qy, rz + h))
                        if w_ is None:
                            continue
                        base = w_[1].split("[")[0]
                        if "_door" in w_[1] or base not in carveable:
                            side_ok = False
                            break
                        if any((bb := block(qx, qy, rz + h - j)) is not None
                               and bb.split("[")[0] in stair_family
                               for j in (1, 2, 3)):
                            side_ok = False
                            break
                    if side_ok:
                        for h in (0, 1, 2):
                            popped = winner.pop(key(qx, qy, rz + h), None)
                            if popped is not None:
                                carved_kinds[popped[1].split("[")[0].replace("minecraft:", "")] += 1
                                touched += 1
                if needs_pad:
                    winner[key(px, py, rz - 1)] = (floor_prio, FLOOR_CUBE)
                    pads += 1
                    touched += 1
                    # open-air catwalk: side decks + guardrails so the bridge
                    # reads as a built walkway, not a floating stone strip
                    # (refine_fences runs later and computes the arm states)
                    for (sx, sy) in perp:
                        qx, qy = px + sx, py + sy
                        if winner.get(key(qx, qy, rz - 1)) is None:
                            winner[key(qx, qy, rz - 1)] = (floor_prio, FLOOR_CUBE)
                            touched += 1
                        if winner.get(key(qx, qy, rz)) is None:
                            winner[key(qx, qy, rz)] = (rail_prio, rail_block)
                            touched += 1
                if col_glass:
                    # frame the glazing opening with mullion-grey concrete so
                    # the hole reads as an intentional portal in the curtain
                    # wall, matching the building's existing mullions
                    for (sx, sy) in perp:
                        qx, qy = px + sx, py + sy
                        for h in (0, 1, 2):
                            w = winner.get(key(qx, qy, rz + h))
                            if w is not None and w[1].split("[")[0] == glass_block:
                                winner[key(qx, qy, rz + h)] = (frame_prio, frame_block)
                    lintel = winner.get(key(px, py, rz + 3))
                    if lintel is not None and lintel[1].split("[")[0] == glass_block:
                        winner[key(px, py, rz + 3)] = (frame_prio, frame_block)
            if iz == rz + 1 and cells:
                # island one level up: a stair at the junction makes the
                # step a WALK (a bare cube edge could need jump room the
                # ceiling does not give)
                jx, jy = cells[-1][0], cells[-1][1]
                tdx, tdy = ix - jx, iy - jy
                if (tdx, tdy) not in GRID_TO_FACING:
                    tdx, tdy = (tdx, 0) if abs(tdx) >= abs(tdy) else (0, tdy)
                facing = GRID_TO_FACING.get((tdx, tdy), "north")
                winner[key(jx, jy, rz)] = (
                    stair_prio,
                    f"{STAIR_SHAPED}[facing={facing},half=bottom,shape=straight]")
            log.append({"from_xyz": (rx, ry, rz), "to_xyz": (ix, iy, iz),
                        "len": len(cells), "island_cells": len(comp),
                        "floor_pads": pads, "carved": dict(carved_kinds)})
            built += 1
            linked = True
            # count the attempt: an island that needs a THIRD corridor is
            # one whose links carve but never connect - stop feeding it
            hopeless[sig] = hopeless.get(sig, 0) + 1
            if built >= max_links:
                return built, touched, log
            break   # re-BFS so the next island can ride this corridor
        if not linked:
            break
    return built, touched, log


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
    ap.add_argument("--rectify", action="store_true",
                    help="Phase-1 plan rectification: rotate off-grid wings "
                         "(whole rigid sections at e.g. 58 deg) onto the world "
                         "grid about their seam with the main building, so "
                         "their walls/corridors voxelize clean instead of "
                         "jagged (see RECTIFY.md)")
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
    wings = None
    if args.rectify:
        wings = compute_wing_transforms(model)
        for w in wings:
            tx, ty = w.get("shift", (0.0, 0.0))
            shove = f" + shove ({tx:+.0f}, {ty:+.0f}) m" if (tx or ty) else ""
            print(f"Rectify: wing of {w['n']} walls rotates {w['deg']:+.0f} deg "
                  f"about seam ({w['pivot'][0]:.0f}, {w['pivot'][1]:.0f}) m{shove}", flush=True)
    meshes, door_verts, spirals, stair_groups, ex_stats = extract(
        model, args.threads, args.spiral, wings=wings)
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
    headroom_cleared = carve_stair_headroom(winner, grid)
    print(f"Cleared {headroom_cleared} closed stairwell-opening cells above flights", flush=True)
    stairs_rebuilt = rebuild_blocked_stairs(winner, grid, stair_groups)
    print(f"Rebuilt {stairs_rebuilt} impassable stair assemblies as clean runs", flush=True)
    doors_unblocked, cells_unplugged = unblock_door_passages(winner, grid)
    print(f"Unblocked {doors_unblocked} dead-end doors ({cells_unplugged} plug cells carved)", flush=True)
    rooms_connected, rooms_hidden, rooms_left, _ = connect_hidden_rooms(winner, grid)
    print(f"Hidden door-less rooms: {rooms_hidden}; connected {rooms_connected} "
          f"through their hallway door, {rooms_left} remain sealed", flush=True)
    holes_filled = patch_floor_holes(winner, grid)
    print(f"Patched {holes_filled} floor-plate pothole cells", flush=True)
    doors_grounded, terrain_cells = stamp_terrain_aprons(winner, grid)
    print(f"Grounded {doors_grounded} exterior door sides with terrain aprons "
          f"({terrain_cells} grass/dirt cells)", flush=True)
    seams_built, seam_cells, seam_log = stitch_seams(winner, grid)
    print(f"Stitched {seams_built} seam corridors ({seam_cells} cells)", flush=True)
    lanterns = light_ceilings(winner, grid)
    print(f"Recessed {lanterns} ceiling sea lanterns for interior light", flush=True)
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

    # seams.csv: every synthesized connector corridor in world (blocks.csv)
    # coordinates, so each one can be audited / visited in the client.
    with (out_dir / "seams.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["from_x", "from_y", "from_z", "to_x", "to_y", "to_z",
                    "length", "island_cells", "floor_pads", "carved"])
        for s in seam_log:
            (fx, fy, fz), (tx, ty, tz) = s["from_xyz"], s["to_xyz"]
            w.writerow([fx - shift[0], fz - shift[1], -fy - shift[2],
                        tx - shift[0], tz - shift[1], -ty - shift[2],
                        s["len"], s["island_cells"], s["floor_pads"],
                        json.dumps(s["carved"])])

    summary = {
        "input_ifc": str(ifc_path),
        "schema": model.schema,
        "pitch_m": args.pitch,
        "door_mode": args.doors,
        "doors_placed": placed,
        "stairs_mode": args.stairs,
        "stairs_converted": stairs_converted,
        "spirals_synthesized": spirals_built,
        "stairs_rebuilt": stairs_rebuilt,
        "doors_unblocked": doors_unblocked,
        "hidden_rooms_found": rooms_hidden,
        "hidden_rooms_connected": rooms_connected,
        "hidden_rooms_left": rooms_left,
        "floor_holes_patched": holes_filled,
        "doors_grounded": doors_grounded,
        "ceiling_lanterns": lanterns,
        "seam_corridors": seams_built,
        "slabs_converted": slabs_converted,
        "fences_connected": fences_connected,
        "world_bounds_min_m": grid["all_min"].tolist(),
        "model_size_m_xyz": (grid["dims"] * args.pitch).tolist(),
        # The whole forward map, written down once so a consumer never has to
        # rediscover it from the source. Everything needed to place an IFC
        # coordinate in this world -- and, with `wings`, to undo a rectified
        # build back to model space -- is in this block.
        "voxel_transform": {
            "world_metres_to_blocks_csv": (
                "g = round((p_metres - world_bounds_min_m) / pitch_m); "
                "blocks.csv row = [g.x, g.z, -g.y] - origin_shift_xyz"
            ),
            "note": (
                "The y<->z swap negates the swapped horizontal axis: a bare "
                "swap is orientation-reversing and would mirror the model "
                "north<->south. IFC +Y is North, which is Minecraft -Z."
            ),
            "pitch_m": args.pitch,
            "world_bounds_min_m": grid["all_min"].tolist(),
            "wings_applied_before_voxelization": bool(wings),
        },
        # Present and empty on a faithful build; populated by --rectify.
        "wings": wing_records(wings) if wings else [],
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
