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
    from ifcopenshell.util import placement as _placement
    from scipy.spatial import ConvexHull

    scale = ifcopenshell.util.unit.calculate_unit_scale(model)
    pts, angs = [], []
    for w in model.by_type("IfcWall") + model.by_type("IfcWallStandardCase"):
        try:
            M = _placement.get_local_placement(w.ObjectPlacement)
        except Exception:
            continue
        pts.append((float(M[0][3]) * scale, float(M[1][3]) * scale))
        angs.append(math.degrees(math.atan2(M[1][0], M[0][0])) % 90.0)
    P = np.asarray(pts)
    A = np.asarray(angs)
    on_axis = (A < 3) | (A > 87)
    main = P[on_axis][::3]

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

    def overlap(Q):
        d2 = ((Q[:, None, :] - main[None, :, :]) ** 2).sum(2).min(axis=1)
        return int((d2 < 4.0).sum())

    wings = []
    for fam in families:
        fam_pts = P[off][np.abs(((binned - fam) + 45) % 90 - 45) <= 4]
        for W in cluster(fam_pts):
            d2 = ((W[:, None, :] - main[None, :, :]) ** 2).sum(2)
            i, j = np.unravel_index(np.argmin(d2), d2.shape)
            pivot = (W[i] + main[j]) / 2.0
            cands = sorted((-float(fam), 90.0 - float(fam)), key=abs)
            deg = min(cands, key=lambda d: (overlap(rotated(W, pivot, d)), abs(d)))
            # push-apart: rotation alone cannot guarantee the wing does not
            # swing INTO the main building (measured on UNBC: the big east
            # wing's +32 put ~2% of its walls inside the spine). If the
            # rotated wing still clips, translate it outward in whole-metre
            # steps until interpenetration is (near) minimal - the seam gap
            # this opens is exactly what stitch_seams bridges afterwards.
            R = rotated(W, pivot, deg)
            best_t = (0.0, 0.0)
            best_c = overlap(R)
            if best_c > max(3, int(0.005 * len(W))):
                for ang in range(0, 360, 45):
                    ux, uy = math.cos(math.radians(ang)), math.sin(math.radians(ang))
                    for dist in (1, 2, 3, 4, 5):
                        c = overlap(R + np.array([ux * dist, uy * dist]))
                        # prefer less clipping, then the smaller shove
                        if c + 0.4 * dist < best_c + 0.4 * math.hypot(*best_t):
                            best_c = c
                            best_t = (round(ux * dist, 3), round(uy * dist, 3))
            hull = ConvexHull(W)
            wings.append({"eqs": hull.equations.copy(), "pivot": pivot,
                          "deg": deg, "n": len(W), "shift": best_t,
                          "cos": math.cos(math.radians(deg)),
                          "sin": math.sin(math.radians(deg))})
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


