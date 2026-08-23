#!/usr/bin/env python3
"""Phase-1 plan rectification: find off-grid wings and how to square them.

Lifted out of `ifc_to_voxels.py` unchanged. It was always independent of the
voxel engine -- it reads IFC wall *placements* and nothing else, no geometry,
no meshing -- but living inside a module that imports trimesh meant nothing
could use it without the whole conversion stack. `preview_rectify.py` needs
exactly this and nothing else, and so does any future audit of what
rectification does to a model.

The proposal, the measured results, and the phases that are and are not
implemented are in RECTIFY.md.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict

import ifcopenshell
import ifcopenshell.util.unit
import numpy as np

def _tessellated_points(product):
    """A product's tessellated coordinates, read without meshing.

    `IfcTriangulatedFaceSet` and `IfcPolygonalFaceSet` both hang their vertices
    off a shared `IfcCartesianPointList3D`, so a wall's footprint is plain
    attribute access. Meshing the model to learn where its walls are would cost
    the whole conversion, which is the thing this pass exists to run ahead of.
    """
    representation = getattr(product, "Representation", None)
    if representation is None:
        return []
    out = []
    for shape in representation.Representations or []:
        for item in shape.Items or []:
            coordinates = getattr(item, "Coordinates", None)
            coord_list = getattr(coordinates, "CoordList", None) if coordinates else None
            if coord_list:
                out.extend(coord_list)
    return out


def _principal_angle(plan):
    """The dominant direction of a plan footprint, and how dominant it is.

    A wall is long and thin, so the first principal axis of its footprint is
    the wall's direction. The eigenvalue ratio comes back with it because a
    square footprint -- a stub, a column-like segment -- has no direction, and
    a number invented for it would land in the angle histogram as noise.
    """
    centred = plan - plan.mean(axis=0)
    eigenvalues, eigenvectors = np.linalg.eigh(centred.T @ centred)
    if eigenvalues[1] <= 1e-12:
        return None, 0.0
    axis = eigenvectors[:, 1]
    ratio = float(eigenvalues[1] / max(eigenvalues[0], 1e-12))
    return math.degrees(math.atan2(axis[1], axis[0])), ratio


def wall_plan(model, min_axis_ratio=1.2):
    """Every wall's plan position and true angle, however the file carries them.

    Two producers write the same building in incompatible ways, and reading
    only the first silently returns a building with no wings at all:

    - **Per-element placements** (Revit's own exporter, most others). Each wall
      carries its own `IfcLocalPlacement`, so position and rotation are one
      cheap matrix read.
    - **One shared placement, geometry in world coordinates** (Reviter's RVT
      recovery). Every product resolves to the SAME matrix, so every wall reads
      as sitting at one point at zero degrees. `compute_wing_transforms` then
      finds 100% axis-aligned walls, no off-axis family, and no wings -- and
      `--rectify` becomes a no-op that reports success.

    So the placements are used only when they actually distinguish the walls,
    and the footprint is read otherwise. Returns `(points_m, degrees, source)`.
    """
    from ifcopenshell.util import placement as _placement

    scale = ifcopenshell.util.unit.calculate_unit_scale(model)
    walls = model.by_type("IfcWall") + model.by_type("IfcWallStandardCase")

    matrices, kept = [], []
    for wall in walls:
        try:
            matrices.append(np.asarray(_placement.get_local_placement(wall.ObjectPlacement),
                                       dtype=float))
            kept.append(wall)
        except Exception:
            continue
    if not kept:
        return np.empty((0, 2)), np.empty(0), "none"

    points = np.array([[m[0][3] * scale, m[1][3] * scale] for m in matrices])
    angles = np.array([math.degrees(math.atan2(m[1][0], m[0][0])) for m in matrices])

    # Do the placements distinguish the walls at all? One shared placement is
    # not "every wall at the origin", it is "this file does not say".
    distinct = len({(round(x, 4), round(y, 4)) for x, y in points})
    if distinct > max(2, len(points) // 100):
        return points, angles, "placements"

    geo_points, geo_angles, flat = [], [], 0
    for wall, matrix in zip(kept, matrices):
        raw = _tessellated_points(wall)
        if len(raw) < 3:
            continue
        local = np.asarray(raw, dtype=float)
        # Compose the placement anyway: a producer may use both, and a shared
        # placement that carries the model origin still has to be applied.
        world = local @ matrix[:3, :3].T + matrix[:3, 3]
        plan = world[:, :2] * scale
        angle, ratio = _principal_angle(plan)
        if angle is None or ratio < min_axis_ratio:
            flat += 1
            continue
        geo_points.append(plan.mean(axis=0))
        geo_angles.append(angle)

    if not geo_points:
        # Neither route works. Say so rather than returning a building that
        # happens to look perfectly grid-aligned.
        return points, angles, "degenerate-placements-no-geometry"
    if flat:
        print(f"  (wall plan: {flat} wall footprints had no dominant direction "
              "and were left out of the angle histogram)", flush=True)
    return np.array(geo_points), np.array(geo_angles), "tessellation"


def on_grid(angles):
    """Which walls already sit on the voxel grid. The engine's own test.

    The fold to mod 90 is for finding grid *families* -- walls at 10 and 100
    degrees are one family. They are not one wall, so nothing that draws or
    measures an individual wall may use the folded value.
    """
    family = np.asarray(angles) % 90.0
    return (family < 3) | (family > 87)


def compute_wing_transforms(model, min_family=250, eps=9.0, min_wing=60):
    """Phase-1 plan rectification: find off-grid WINGS and how to square them.

    Buildings like UNBC are several orthogonal grids in one model: 65 % of
    walls are axis-aligned but whole wings sit at e.g. 58° and voxelize as
    jagged staircase lines. Each such wing is orthogonal *in its own frame*,
    so the fix is a rigid rotation per wing, not per wall (see RECTIFY.md).

    From the IFC wall placements (cheap - no geometry):
      1. histogram wall plan-angles mod 90°; every off-axis angle family
         with >= min_family walls is a rectification candidate;
      2. cluster that family's walls spatially (union-find, eps metres) -
         each cluster is one WING;
      3. pivot = the wing wall nearest any axis-aligned wall (the seam with
         the campus spine stays put while the far end swings);
      4. of the two grid-aligning rotations (-a and 90-a) keep the one that
         lands the fewest wing walls within 2 m of existing axis-aligned
         walls (least overlap), ties to the smaller swing.

    Returns wings as [{"eqs": hull half-planes, "pivot", "cos", "sin"}] in
    world metres; None-safe consumer is extract().
    """
    from scipy.spatial import ConvexHull, cKDTree

    P, true_angles, source = wall_plan(model)
    if not len(P):
        return []
    A = np.asarray(true_angles) % 90.0
    on_axis = on_grid(true_angles)
    # The collision target every candidate rotation and shove is scored
    # against. It used to be `P[on_axis][::3]` -- every THIRD on-grid wall, and
    # only on-grid walls. Both were wrong in the same direction:
    #
    #   * the 3x subsample made a wing look about three times cleaner than it
    #     was, and it existed only because the score was an O(len(Q) x len(main))
    #     distance matrix. A KD-tree makes the full set cheaper than the
    #     subsample was, so there is nothing left to trade;
    #   * scoring against on-grid walls alone means six wings were each squared
    #     against the spine while blind to one another. A wing shoved five
    #     metres to clear the spine could be shoved straight into its neighbour
    #     and score zero for it.
    #
    # Wings are decided in order, so a wing is scored against the spine, the
    # wings already placed (at their final positions) and the wings not yet
    # placed (where they still are).
    main = P[on_axis]

    families = []
    off = ~on_axis
    binned = np.round(A[off]).astype(int) % 90
    for b, n in Counter(binned.tolist()).most_common():
        if n < min_family or any(abs(b - fb) <= 4 for fb in families):
            continue
        families.append(b)

    def cluster(Q):
        n = len(Q)
        parent = list(range(n))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        cells = defaultdict(list)
        for i, (x, y) in enumerate(Q):
            cells[(int(x // eps), int(y // eps))].append(i)
        for (cx, cy), idx in cells.items():
            neigh = [j for dx in (-1, 0, 1) for dy in (-1, 0, 1)
                     for j in cells.get((cx + dx, cy + dy), [])]
            for i in idx:
                for j in neigh:
                    if j > i and np.hypot(*(Q[i] - Q[j])) <= eps:
                        ri, rj = find(i), find(j)
                        if ri != rj:
                            parent[ri] = rj
        roots = defaultdict(list)
        for i in range(n):
            roots[find(i)].append(i)
        return [Q[idx] for idx in roots.values() if len(idx) >= min_wing]

    def rotated(Q, pivot, deg):
        t = math.radians(deg)
        R = np.array([[math.cos(t), -math.sin(t)], [math.sin(t), math.cos(t)]])
        return (Q - pivot) @ R.T + pivot

    def clash_tree(extra):
        """A KD-tree over everything a wing must not land on."""
        stack = [main] + [block for block in extra if len(block)]
        return cKDTree(np.vstack(stack)) if stack else None

    def overlap(Q, tree):
        if tree is None or not len(Q):
            return 0
        return int((tree.query(Q, k=1)[0] < 2.0).sum())

    # Every wing's walls, gathered before any is placed, so each one can be
    # scored against the others rather than against the spine alone.
    # (family angle, wall positions) -- the angle has to travel WITH its
    # cluster. Flattening the per-family loop without it leaves every wing
    # rotated by whichever family happened to be scanned last.
    candidates = []
    for fam in families:
        fam_pts = P[off][np.abs(((binned - fam) + 45) % 90 - 45) <= 4]
        candidates.extend((fam, W) for W in cluster(fam_pts))

    placed: list[np.ndarray] = []          # wings already squared, where they end up
    wings = []
    for index, (fam, W) in enumerate(candidates):
        # Not-yet-placed neighbours count where they currently stand: a wing
        # cannot be scored against a position that has not been chosen yet, and
        # ignoring them entirely is what let two wings converge on one spot.
        others = placed + [candidates[j][1] for j in range(index + 1, len(candidates))]
        tree = clash_tree(others)
        # Pivot at the seam: the wing wall nearest the spine, so the joint
        # stays put and the far end swings. Through the same tree rather than a
        # full distance matrix -- on this building that was 34 million pairs.
        main_tree = cKDTree(main)
        seam_distance, seam_main = main_tree.query(W, k=1)
        i = int(np.argmin(seam_distance))
        pivot = (W[i] + main[seam_main[i]]) / 2.0
        cands = sorted((-float(fam), 90.0 - float(fam)), key=abs)
        deg = min(cands, key=lambda d: (overlap(rotated(W, pivot, d), tree), abs(d)))
        # push-apart: rotation alone cannot guarantee the wing does not
        # swing INTO the main building (measured on UNBC: the big east
        # wing's +32 put ~2% of its walls inside the spine). If the
        # rotated wing still clips, translate it outward in whole-metre
        # steps until interpenetration is (near) minimal - the seam gap
        # this opens is exactly what stitch_seams bridges afterwards.
        R = rotated(W, pivot, deg)
        best_t = (0.0, 0.0)
        best_c = overlap(R, tree)
        if best_c > max(3, int(0.005 * len(W))):
            for ang in range(0, 360, 45):
                ux, uy = math.cos(math.radians(ang)), math.sin(math.radians(ang))
                for dist in (1, 2, 3, 4, 5):
                    c = overlap(R + np.array([ux * dist, uy * dist]), tree)
                    # prefer less clipping, then the smaller shove
                    if c + 0.4 * dist < best_c + 0.4 * math.hypot(*best_t):
                        best_c = c
                        best_t = (round(ux * dist, 3), round(uy * dist, 3))
        hull = ConvexHull(W)
        wings.append({"eqs": hull.equations.copy(), "pivot": pivot,
                      "deg": deg, "n": len(W), "shift": best_t,
                      "clash": best_c,
                      "cos": math.cos(math.radians(deg)),
                      "sin": math.sin(math.radians(deg))})
        placed.append(R + np.array(best_t))
    return wings


# How far outside a wing's convex hull an element still counts as that wing's.
# Elements straddle the hull edge (a wall's mesh reaches past the placement
# points the hull was built from), so a bare hull under-claims the boundary.
WING_HULL_MARGIN_M = 2.5


def wing_for_point(wings, x, y, margin=WING_HULL_MARGIN_M):
    for w in wings:
        if all(e[0] * x + e[1] * y + e[2] <= margin for e in w["eqs"]):
            return w
    return None


def apply_wing(w, v):
    """Rigidly rotate (and push-apart shift) verts about the wing pivot."""
    px, py = w["pivot"]
    c, s = w["cos"], w["sin"]
    tx, ty = w.get("shift", (0.0, 0.0))
    out = v.copy()
    dx, dy = v[:, 0] - px, v[:, 1] - py
    out[:, 0] = px + c * dx - s * dy + tx
    out[:, 1] = py + s * dx + c * dy + ty
    return out


def wing_records(wings):
    """The rectification transforms, in a form a consumer can replay.

    Without this the rectified world is a one-way trip. Every other stage of
    the conversion is recorded -- `world_bounds_min_m` and `origin_shift_xyz`
    let anyone map an IFC coordinate to a blocks.csv cell -- but `--rectify`
    inserts a per-wing rigid motion that only ever reached stdout, so a cell in
    a rectified build could not be traced back to the element it came from.
    That blocks the two things rectification is for: comparing a rectified
    build against the faithful one element by element, and RECTIFY Phase 3's
    parametric opening replay, which has to know where a wall moved.

    `eqs` are the wing hull's half-planes in source metres (a point is in the
    wing when every `e[0]*x + e[1]*y + e[2] <= margin`), so the assignment
    itself replays too, not just the motion.
    """
    return [{
        "walls": int(w["n"]),
        "rotation_deg": float(w["deg"]),
        "pivot_xy_m": [float(w["pivot"][0]), float(w["pivot"][1])],
        "shift_xy_m": [float(w.get("shift", (0.0, 0.0))[0]),
                       float(w.get("shift", (0.0, 0.0))[1])],
        "hull_half_planes": [[float(c) for c in row] for row in w["eqs"]],
        "hull_margin_m": WING_HULL_MARGIN_M,
    } for w in wings]


