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


# The curtain wall's parts, which are not walls. `IfcCurtainWall` itself is NOT
# in this list: measured on the UNBC export, all 1,835 of them carry their own
# `IfcLocalPlacement` entity that resolves to the IDENTITY matrix and no
# `Representation` at all -- they are aggregate containers. Reading them would
# add 1,835 phantom walls stacked on the world origin, every one of them
# reading as perfectly axis-aligned, and neither route in this function can
# recover a position the file does not carry.
FACADE_TYPES = ("IfcPlate", "IfcMember")


def wall_plan(model, min_axis_ratio=1.2, include_facade=False):
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

    `include_facade` widens the population to the curtain wall's own elements
    (`FACADE_TYPES`), which are not walls and are therefore invisible to the
    element list every wing hull is built from. It is OFF by default because
    it was measured and did not pay: on UNBC it adds 16,396 usable parts to
    the 14,902 walls and moves four of the six wings, and the hull it widens
    was not the thing that was missing the glazing. The numbers are in
    RECTIFY.md, "Building the hull from the facade too".

    A facade part is only usable when its placement states a plan DIRECTION.
    Measured on the same export, 9,546 of 19,707 `IfcMember` mullions carry a
    placement whose local X axis is vertical -- they run up the facade, not
    along it -- so `atan2(m[1][0], m[0][0])` is `atan2(0, 0)` and every one of
    them reads as EXACTLY 0 degrees. Kept, they are 9,546 elements asserting
    that the building is axis-aligned where it is not, and they land in the
    collision target every wing's pivot and rotation are scored against. They
    are dropped, and counted out loud.
    """
    from ifcopenshell.util import placement as _placement

    scale = ifcopenshell.util.unit.calculate_unit_scale(model)
    # Deduplicated by id, and that is not defensive coding. `IfcWallStandardCase`
    # is a SUBTYPE of `IfcWall`, and `by_type` returns subtypes, so the obvious
    # concatenation counts every standard-case wall twice -- on this building
    # 7,381 of 7,521, reported for weeks as "14,902 walls". It does not move a
    # hull, because a duplicated point is already in the hull of itself. It does
    # double `n`, hence the share-derived family and wing thresholds, hence
    # `overlap()` against a FIXED shove penalty -- which is how a double count
    # in a census reaches the geometry.
    walls = list({wall.id(): wall for wall in
                  model.by_type("IfcWall") + model.by_type("IfcWallStandardCase")}.values())
    facade = []
    if include_facade:
        for kind in FACADE_TYPES:
            facade.extend(model.by_type(kind))

    matrices, kept, undirected = [], [], 0
    for element, is_facade in ([(w, False) for w in walls] + [(f, True) for f in facade]):
        try:
            matrix = np.asarray(_placement.get_local_placement(element.ObjectPlacement),
                                dtype=float)
        except Exception:
            continue
        # A part whose local X axis is vertical has no plan direction to read.
        # atan2(0, 0) is 0.0, which is indistinguishable from "on the grid".
        if is_facade and math.hypot(matrix[0][0], matrix[1][0]) < 1e-6:
            undirected += 1
            continue
        matrices.append(matrix)
        kept.append(element)
    if undirected:
        print(f"  (wall plan: {undirected} facade parts carry no plan direction "
              "-- placement local X is vertical -- and were left out)", flush=True)
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


# Fitted on UNBC's 14,902 walls: an angle family needed 250 walls (1.7%) and a
# wing 60 (0.4%). As absolute numbers they are a statement that no building
# smaller than that one has wings -- a 200-wall building gets no rectification
# at all, silently, with the run reporting success. Kept as SHARES, with floors
# low enough for a small building and small enough that on UNBC the share still
# governs (0.017 x 14,902 = 253, 0.004 x 14,902 = 60: unchanged).
FAMILY_SHARE, FAMILY_FLOOR = 0.017, 6
WING_SHARE, WING_FLOOR = 0.004, 4


def compute_wing_transforms(model, min_family=None, eps=9.0, min_wing=None,
                            include_facade=False):
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

    P, true_angles, source = wall_plan(model, include_facade=include_facade)
    if not len(P):
        return []
    if min_family is None:
        min_family = max(FAMILY_FLOOR, round(FAMILY_SHARE * len(P)))
    if min_wing is None:
        min_wing = max(WING_FLOOR, round(WING_SHARE * len(P)))
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
    skipped_flat = 0
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
        # A wing is a REGION. A cluster whose walls all lie on one line is a
        # wall run -- a facade, a long corridor side -- and it has no 2-D hull
        # to test membership against. Qhull raises on it, so this used to be
        # unreachable only because the old wall floor of 60 made such a cluster
        # too small to qualify.
        spread = np.linalg.eigvalsh(np.cov((W - W.mean(axis=0)).T))
        if spread[0] < 0.25:  # minor axis under half a metre: a line
            skipped_flat += 1
            continue
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


def wing_index_for_verts(wings, v, margin=WING_HULL_MARGIN_M):
    """Which wing each vertex falls in, as an int array (-1 = none).

    Called once per element on the whole vertex array, so it rejects a wing on
    the element's bounding box first: a half-plane whose nearest bbox corner is
    already outside cannot admit any vertex, and most elements are nowhere near
    most wings.
    """
    out = np.full(len(v), -1, dtype=np.int64)
    if not len(v):
        return out
    lo = v[:, :2].min(axis=0)
    hi = v[:, :2].max(axis=0)
    for i, w in enumerate(wings):
        for e in w["eqs"]:
            nearest = (e[0] * (lo[0] if e[0] > 0 else hi[0])
                       + e[1] * (lo[1] if e[1] > 0 else hi[1]) + e[2])
            if nearest > margin:
                break                      # whole bbox outside this half-plane
        else:
            inside = out < 0
            for e in w["eqs"]:
                inside &= (e[0] * v[:, 0] + e[1] * v[:, 1] + e[2] <= margin)
            out[inside] = i
    return out


def apply_wings_piecewise(wings, v, f, max_edge=0.5, margin=WING_HULL_MARGIN_M):
    """Rotate the parts of one mesh that lie in a wing, and only those.

    `wing_for_point` decides an element's wing from its CENTROID. That is right
    for a wall -- small, and wholly on one side of the seam -- and wrong for a
    floor slab, which spans the wing and the spine, so its centroid sits
    outside the hull. Measured on the real building: 90-99% of the walls
    touching each hull rotate, against 25-78% of the plates. The wing's walls
    swing 32 or 58 degrees away and the floor they stood on stays exactly where
    it was, which is why a rectified build has storeys of bare plate with no
    envelope in the column at all.

    A rigid motion applied to a REGION has to cut whatever crosses the region's
    boundary. This does that on the triangles: a mesh whose vertices disagree
    is subdivided until its triangles are smaller than `max_edge`, and each
    triangle then goes wholly with the wing its own centroid falls in. The
    plate tears at the hull, its wing half travels with the wing, and the seam
    that opens is the seam the stitcher already exists to bridge.

    Returns (verts, faces); triangles are emitted independently, because two
    neighbouring triangles that go to different wings cannot share a vertex.
    """
    where = wing_index_for_verts(wings, v, margin)
    tri = v[f]                                     # (n, 3, 3)
    same = (where[f] == where[f][:, :1]).all(axis=1)
    if same.all():
        # Fast path: one assignment for the whole mesh, no subdivision, no
        # vertex duplication. This is every wall and almost every element.
        index = int(where[f[0, 0]]) if len(f) else -1
        return (apply_wing(wings[index], v) if index >= 0 else v), f

    # Only the straddling triangles are subdivided; the rest pass through.
    keep = tri[same]
    work = tri[~same]
    for _ in range(6):
        edge = np.linalg.norm(work - work[:, [1, 2, 0]], axis=2).max(axis=1)
        big = edge > max_edge
        if not big.any():
            break
        a, b, c = work[big, 0], work[big, 1], work[big, 2]
        ab, bc, ca = (a + b) / 2, (b + c) / 2, (c + a) / 2
        work = np.concatenate([
            work[~big],
            np.stack([a, ab, ca], axis=1), np.stack([ab, b, bc], axis=1),
            np.stack([ca, bc, c], axis=1), np.stack([ab, bc, ca], axis=1),
        ])
    tri = np.concatenate([keep, work]) if len(keep) else work

    centre = tri.mean(axis=1)
    index = wing_index_for_verts(wings, centre, margin)
    moved = tri.reshape(-1, 3).copy()
    flat = np.repeat(index, 3)
    for i in np.unique(flat):
        if i < 0:
            continue
        moved[flat == i] = apply_wing(wings[i], moved[flat == i])
    return moved, np.arange(len(moved), dtype=np.int64).reshape(-1, 3)


def adjacency_claims(elements, wings, touch_m=0.6, reach_m=6.0, rounds=3,
                     margin=WING_HULL_MARGIN_M):
    """Elements the hull missed that are JOINED to elements it claimed.

    A wing's hull is the convex hull of its WALL placements. A curtain wall is
    not a wall: `IfcPlate` panels and `IfcMember` mullions are their own
    elements hanging on the facade, so the hull never encloses them. The wall
    behind the glazing rotates 32 degrees and the glazing stays where it was,
    driven through the rooms that moved. Audited against the recovered model's
    own architectural plan, that is 409 of 605 findings -- mullions and panels,
    at the same plan position on storey after storey -- and it is also why
    glass voxels fall 5% under `--rectify` while the per-triangle assignment
    recovers none of them: there was nothing in the hull to cut.

    Widening the margin would sweep them in and sweep in the spine's walls with
    them, which is the cost the report already calls "knocked OFF the grid".
    This claims by CONTACT instead: an element the hull missed travels with the
    wing when it touches something the wing claimed. 81% of the findings sit
    within 2 m of a hull edge and 96% within 5, so `reach_m` bounds the claim to
    the hull's own neighbourhood and the cascade cannot walk off across the
    building.

    `elements` is (id, verts) with verts in world metres. Returns {id: wing}.
    """
    if not wings:
        return {}

    boxes: dict[int, tuple] = {}
    seeds: dict[int, dict] = {}
    for element_id, verts in elements:
        if not len(verts):
            continue
        lo = verts.min(axis=0)
        hi = verts.max(axis=0)
        boxes[element_id] = (lo[0], lo[1], lo[2], hi[0], hi[1], hi[2])
        # Claimed already if ANY vertex is inside a hull -- the same test the
        # per-triangle assignment makes, asked once for the whole element.
        for wing in wings:
            inside = np.ones(len(verts), dtype=bool)
            for e in wing["eqs"]:
                inside &= (e[0] * verts[:, 0] + e[1] * verts[:, 1] + e[2] <= margin)
                if not inside.any():
                    break
            if inside.any():
                seeds[element_id] = wing
                break

    def whole_box_slack(box):
        """How far the element's FARTHEST corner sits outside the nearest hull.

        The farthest corner, not the nearest, and that is the difference
        between claiming a mullion and dragging the spine. Contact is tested on
        bounding boxes, so a forty-metre corridor wall that happens to touch a
        wing at one end touches it by its box too -- and being claimed, the
        whole corridor swings 32 degrees away. Measured on the fixture: with a
        nearest-corner test the pass claimed 34 elements and opened 138
        see-through cells in a building that had none. Requiring ALL of an
        element to sit in the hull's neighbourhood keeps the small things that
        hang on a facade and leaves the long things that merely reach it.
        """
        best = None
        for wing in wings:
            worst = max(
                max(e[0] * x + e[1] * y for x in (box[0], box[3]) for y in (box[1], box[4])) + e[2]
                for e in wing["eqs"])
            slack = worst - margin
            best = slack if best is None else min(best, slack)
        return best

    candidates = {i: b for i, b in boxes.items()
                  if i not in seeds and whole_box_slack(b) <= reach_m}
    if not candidates:
        return {}

    cell = max(2.0, touch_m * 8)

    def cells_of(box):
        for x in range(int((box[0] - touch_m) // cell), int((box[3] + touch_m) // cell) + 1):
            for y in range(int((box[1] - touch_m) // cell), int((box[4] + touch_m) // cell) + 1):
                yield (x, y)

    def touches(a, b):
        return (a[0] - touch_m <= b[3] and b[0] - touch_m <= a[3]
                and a[1] - touch_m <= b[4] and b[1] - touch_m <= a[4]
                and a[2] - touch_m <= b[5] and b[2] - touch_m <= a[5])

    claims: dict[int, dict] = {}
    frontier = dict(seeds)
    for _ in range(rounds):
        if not frontier or not candidates:
            break
        index: dict[tuple, list] = {}
        for element_id, wing in frontier.items():
            for key in cells_of(boxes[element_id]):
                index.setdefault(key, []).append((element_id, wing))
        won: dict[int, dict] = {}
        for element_id, box in candidates.items():
            for key in cells_of(box):
                hit = next((wing for other, wing in index.get(key, ())
                            if touches(box, boxes[other])), None)
                if hit is not None:
                    won[element_id] = hit
                    break
        if not won:
            break
        claims.update(won)
        for element_id in won:
            candidates.pop(element_id, None)
        frontier = won
    return claims


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


