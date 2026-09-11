"""The wing transform as a continuous field instead of a step at the hull.

`rectify.py` applies a wing rigidly and decides membership with a hull test:
inside, the full rotation; outside, nothing. The step between those two is
where every torn floor plate and every broken wall join lives, and it is what
`apply_wings_piecewise` cuts cleanly and `close_seam_walls` then walls up with
1,147 cells of invented wall.

This is the same edit with the step replaced by a RAMP. The rotation goes from
full to none across a band straddling the hull boundary, so a plate spanning
the boundary is stretched across it rather than torn at it -- and a stretched
plate is walkable where a torn one is a canyon. That is the only reason this
module exists; it is not a better drawing.

**It is already refuted for drawings.** The same field, measured in the
parser's architectural plan over all 169,116 pairs of elements that touched in
the source model (REVITER.md 2j):

    band        adjacencies broken   wall runs on the grid   p99 strain
    rigid                    2,229                   60.3%            0
     5 m                     5,009                   58.8%         244%
    80 m                    14,158                   22.1%          40%

Strain is the wing's DISPLACEMENT over the band width, and at the far end of a
wing that displacement is tens of metres, so there is no band that is both
gentle and local. Two elements half a foot apart in a field straining 100% end
up a foot apart: continuity is not sufficient for a join to survive, small
strain is, and small strain is not on offer.

Walkability is a different question from either of those, because a voxel grid
does not care that a wall is 6% longer than it was -- it cares whether there is
floor under the player. That question is what this is for, and it is open.
"""
import numpy as np

from rectify import WING_HULL_MARGIN_M

__all__ = ["hull_depth", "wing_weight", "apply_wings_elastic",
           "blend_at_point", "apply_wing_partial", "self_test"]


def hull_depth(wing, x, y, margin=WING_HULL_MARGIN_M):
    """Metres outside `wing`: negative inside the hull, positive beyond it.

    `x` and `y` may be scalars or arrays; the result matches.
    """
    worst = None
    for a, b, c in wing["eqs"]:
        here = a * x + b * y + c
        worst = here if worst is None else np.maximum(worst, here)
    return worst - margin


def wing_weight(wing, x, y, band_m, margin=WING_HULL_MARGIN_M):
    """How much of this wing's motion applies here, 0 to 1.

    Smoothstep rather than a linear ramp: a linear ramp has a corner at each
    end of the band, and a corner in the weight is a kink in every wall that
    crosses it.
    """
    if band_m <= 0:
        return (hull_depth(wing, x, y, margin) <= 0).astype(float)
    half = band_m / 2.0
    depth = hull_depth(wing, x, y, margin)
    t = np.clip((half - depth) / band_m, 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def apply_wings_elastic(wings, v, band_m, margin=WING_HULL_MARGIN_M):
    """Displace vertices by as much of the wings' motion as applies at each.

    Each wing contributes its own DISPLACEMENT, weighted, and they sum. Not
    "pick the nearest wing and apply a fraction of its rotation": that is a
    different map either side of wherever two wings' bands cross over, which
    trades the hull's one discontinuity for several interior ones. The first
    version of this in the parser did exactly that and the sweep test there now
    pins against it.

    No subdivision and no per-triangle assignment: a continuous field has
    nothing to cut at, which is the one place this is simpler than the rigid
    path rather than merely different.
    """
    if not wings or not len(v):
        return v
    out = v.copy()
    dx = np.zeros(len(v))
    dy = np.zeros(len(v))
    total = np.zeros(len(v))
    for wing in wings:
        w = wing_weight(wing, v[:, 0], v[:, 1], band_m, margin)
        hit = w > 0
        if not hit.any():
            continue
        px, py = wing["pivot"]
        c, s = wing["cos"], wing["sin"]
        tx, ty = wing.get("shift", (0.0, 0.0))
        ox, oy = v[hit, 0] - px, v[hit, 1] - py
        mx = px + c * ox - s * oy + tx
        my = py + s * ox + c * oy + ty
        dx[hit] += w[hit] * (mx - v[hit, 0])
        dy[hit] += w[hit] * (my - v[hit, 1])
        total[hit] += w[hit]
    # Wings are disjoint regions, so overlapping bands mean a corner between
    # two of them. Cap the total so a point there cannot be displaced further
    # than either wing alone would have taken it.
    scale = np.where(total > 1.0, 1.0 / np.maximum(total, 1e-12), 1.0)
    out[:, 0] += dx * scale
    out[:, 1] += dy * scale
    return out


def blend_at_point(wings, x, y, band_m, margin=WING_HULL_MARGIN_M):
    """The dominant wing at one point and its weight, or (None, 0.0).

    For things that must not be sheared. A wall 6% longer than it was is a
    wall; a STAIR sheared by 6% is a stair whose treads no longer line up with
    its stringers, and the engine's climb test reads treads. So an assembly
    takes one weight for the whole of itself, at its centroid, and moves
    rigidly by that much -- deformed as a body, never within itself.
    """
    best, best_w = None, 0.0
    for wing in wings:
        w = float(wing_weight(wing, np.array([x]), np.array([y]), band_m, margin)[0])
        if w > best_w:
            best, best_w = wing, w
    return best, best_w


def apply_wing_partial(wing, weight, v):
    """`apply_wing`, scaled: rotate by `weight` of the angle about the pivot.

    NOT a lerp between each vertex and its rigid image -- that would drag
    points across the chord of the arc they should travel, so a half-weight
    element would come out SHORTER than it started and the building would
    pucker toward the pivot. The ANGLE and the shift scale; the rotation stays
    a rotation, so this is rigid at every weight.
    """
    if weight >= 1.0:
        angle = np.arctan2(wing["sin"], wing["cos"])
    else:
        angle = np.arctan2(wing["sin"], wing["cos"]) * weight
    c, s = np.cos(angle), np.sin(angle)
    px, py = wing["pivot"]
    tx, ty = wing.get("shift", (0.0, 0.0))
    out = v.copy()
    dx, dy = v[:, 0] - px, v[:, 1] - py
    out[:, 0] = px + c * dx - s * dy + tx * weight
    out[:, 1] = py + s * dx + c * dy + ty * weight
    return out


def self_test():
    """Every claim in the docstrings above, on geometry small enough to check.

    Run with `python3 scripts/rectify_elastic.py --self-test`.
    """
    import math

    # One wing: the half-plane x >= 0, turning a quarter about (0, -100).
    quarter = {
        "eqs": [(-1.0, 0.0, 0.0)],
        "pivot": (0.0, -100.0),
        "cos": math.cos(math.pi / 2), "sin": math.sin(math.pi / 2),
        "shift": (0.0, 0.0),
    }
    band = 40.0                                  # so the band runs x = -20..20
    xs = np.array([60.0, 20.0, 0.0, -20.0, -60.0])
    ys = np.zeros(5)
    w = wing_weight(quarter, xs, ys, band, margin=0.0)
    assert w[0] == 1.0 and w[1] == 1.0, f"deep inside should be whole, got {w[:2]}"
    assert abs(w[2] - 0.5) < 1e-12, f"the boundary should be half, got {w[2]}"
    assert w[3] == 0.0 and w[4] == 0.0, f"beyond the band should be nothing, got {w[3:]}"

    # Exact at full weight: inside the band the elastic path must agree with
    # the rigid one to the last decimal, or it moves the part that was right.
    from rectify import apply_wing
    inside = np.array([[60.0, 25.0, 3.0], [40.0, -10.0, 3.0]])
    assert np.allclose(apply_wings_elastic([quarter], inside, band, margin=0.0),
                       apply_wing(quarter, inside)), "not exact at weight 1"

    # The join a rigid edge tears: two walls meeting at x = 0, one in the hull
    # and one out. Rigidly, the inside one is carried off and the join is gone;
    # elastically the shared point is one point in one field, so it can only go
    # one place.
    shared = np.array([[0.0, 0.0, 0.0]])
    rigid_gap = np.linalg.norm(apply_wing(quarter, shared)[0, :2] - shared[0, :2])
    assert rigid_gap > 100, f"the fixture should tear rigidly, got {rigid_gap}"
    moved = apply_wings_elastic([quarter], np.vstack([shared, shared]), band, margin=0.0)
    assert np.allclose(moved[0], moved[1]), "one point, two elements, two places"

    # And what it costs: the wall spanning the band comes out longer, because
    # its ends took different amounts of the rotation. Nothing tears; things
    # near the seam distort, and by a lot.
    wall = np.array([[0.0, 0.0, 0.0], [40.0, 0.0, 0.0]])
    out = apply_wings_elastic([quarter], wall, band, margin=0.0)
    length = np.linalg.norm(out[1, :2] - out[0, :2])
    assert 45 < length < 60, f"expected a large but finite strain, got {length} from 40"

    # A partial rotation is a rotation: distance from the pivot is exact at
    # every weight, so an assembly moved this way is not sheared.
    for weight in (0.25, 0.5, 0.75):
        p = np.array([[0.0, 0.0, 0.0]])
        before = math.hypot(0 - quarter["pivot"][0], 0 - quarter["pivot"][1])
        after_pt = apply_wing_partial(quarter, weight, p)[0]
        after = math.hypot(after_pt[0] - quarter["pivot"][0],
                           after_pt[1] - quarter["pivot"][1])
        assert abs(after - before) < 1e-9, f"weight {weight} changed the radius"

    # Two wings whose bands overlap: the field must not JUMP anywhere, because
    # a jump in the field is a tear by another name.
    other = {
        "eqs": [(1.0, 0.0, -30.0)],              # x <= 30
        "pivot": (0.0, 200.0),
        "cos": math.cos(-0.7), "sin": math.sin(-0.7),
        "shift": (0.0, 0.0),
    }
    sweep = np.array([[x, 0.0, 0.0] for x in np.arange(-60.0, 90.0, 0.25)])
    swept = apply_wings_elastic([quarter, other], sweep, band, margin=0.0)
    steps = np.linalg.norm(np.diff(swept[:, :2], axis=0), axis=1)
    assert steps.max() < 4.0, f"the field jumps by {steps.max():.2f} m somewhere"

    # And z is never touched: this is a PLAN transform, and a wing that lifted
    # its own floors would be a different bug entirely.
    assert np.allclose(swept[:, 2], sweep[:, 2]), "z moved"

    print("self-test passed: the wing field is continuous across two wings, "
          "exact inside, keeps one shared point in one place, strains what "
          "spans the band, and moves an assembly rigidly")


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv:
        self_test()
    else:
        print(__doc__)
