#!/usr/bin/env python3
"""See what `--rectify` will do to a model, before converting it.

Rectification is the one stage that visibly *moves the building* — whole wings
swing onto the voxel grid — and until now the only way to look at it was to run
the full conversion and compare two worlds. That is roughly forty minutes to
answer "which wings did it find, and where do they end up".

It does not need to be. `compute_wing_transforms` reads IFC wall **placements**
and nothing else: no geometry, no meshing. This runs the identical function the
engine runs, and draws the answer.

    python3 scripts/preview_rectify.py model.ifc
    python3 scripts/preview_rectify.py model.ifc --svg out/rectify.svg --json out/wings.json

The SVG is a before/after plan: every wall as a dot, axis-aligned walls in
grey, each wing in its own colour, and the same walls again under the rigid
transform the engine would apply. It is written by hand rather than through a
plotting library so this stays runnable wherever ifcopenshell is.

What it shows is Phase 1 (see RECTIFY.md) — per-wing rotation plus the
push-apart shove. It does not show seam stitching, which happens later against
the voxel grid and cannot be known from placements alone.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import ifcopenshell
import ifcopenshell.util.unit
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rectify import (  # noqa: E402 -- after the sys.path line, deliberately
    WING_HULL_MARGIN_M,
    compute_wing_transforms,
    wing_for_point,
    wing_records,
)

# Colour-blind-safe qualitative hues; grey is reserved for the on-grid spine.
WING_COLOURS = ["#e66100", "#5d3a9b", "#1a85ff", "#d41159", "#008080", "#994f00",
                "#40b0a6", "#e1be6a"]
SPINE = "#9a9a9a"


def wall_plan(model):
    """Every wall's plan position and angle, exactly as the engine reads them."""
    from ifcopenshell.util import placement as _placement

    scale = ifcopenshell.util.unit.calculate_unit_scale(model)
    points, angles = [], []
    for wall in model.by_type("IfcWall") + model.by_type("IfcWallStandardCase"):
        try:
            matrix = _placement.get_local_placement(wall.ObjectPlacement)
        except Exception:
            continue
        points.append((float(matrix[0][3]) * scale, float(matrix[1][3]) * scale))
        angles.append(math.degrees(math.atan2(matrix[1][0], matrix[0][0])) % 90.0)
    return np.asarray(points, dtype=float), np.asarray(angles, dtype=float)


def move(wing, points):
    """The wing's rigid motion, in plan. Mirrors `apply_wing` for 2-D points."""
    px, py = wing["pivot"]
    c, s = wing["cos"], wing["sin"]
    tx, ty = wing.get("shift", (0.0, 0.0))
    dx, dy = points[:, 0] - px, points[:, 1] - py
    return np.stack([px + c * dx - s * dy + tx, py + s * dx + c * dy + ty], axis=1)


def clipping(moved, spine, radius=2.0):
    """Wing walls sitting within `radius` of an on-grid wall.

    The same score `compute_wing_transforms` minimises when it chooses between
    the two grid-aligning rotations and then shoves the wing outward. Reported
    before and after so the shove is visible as a number, not just a claim.
    """
    if not len(moved) or not len(spine):
        return 0
    squared = ((moved[:, None, :] - spine[None, :, :]) ** 2).sum(2).min(axis=1)
    return int((squared < radius * radius).sum())


def assign(wings, points):
    """Wing index per wall, or -1 for the on-grid spine.

    Membership is the engine's own test: inside the wing's convex hull, plus a
    margin. That is a hull, not a classification -- an axis-aligned wall that
    happens to sit inside a wing's hull belongs to the wing and moves with it.
    Worth knowing when reading the picture: grey dots inside a coloured cloud
    are not mistakes, and they move too.
    """
    owner = np.full(len(points), -1, dtype=int)
    for index, wing in enumerate(wings):
        for row, (x, y) in enumerate(points):
            if owner[row] == -1 and wing_for_point([wing], x, y, WING_HULL_MARGIN_M):
                owner[row] = index
    return owner


def svg(points, owner, wings, path: Path, size: int = 900) -> None:
    """A before/after plan, drawn as two panels sharing one scale."""
    after = points.copy()
    for index, wing in enumerate(wings):
        rows = owner == index
        if rows.any():
            after[rows] = move(wing, points[rows])

    both = np.vstack([points, after])
    lo, hi = both.min(axis=0), both.max(axis=0)
    span = np.maximum(hi - lo, 1e-6)
    pad = 24
    panel = size - 2 * pad
    # One scale for both panels: a rectification that moves a wing 40 m must
    # not be flattered by each panel being fitted to its own extent.
    scale = panel / max(span[0], span[1])
    height = int(span[1] * scale) + 2 * pad

    def place(xy, offset):
        # SVG y grows downward; plan y grows north, so flip it.
        return (offset + pad + (xy[:, 0] - lo[0]) * scale,
                pad + (hi[1] - xy[:, 1]) * scale)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{2 * size + 40}" height="{height + 40}" '
        f'viewBox="0 0 {2 * size + 40} {height + 40}">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        '<style>text{font:14px system-ui,sans-serif;fill:#333}'
        '.t{font-weight:600}</style>',
    ]
    # A rule between the panels: without it two point clouds on one white field
    # read as one drawing, and the whole point is comparing them.
    parts.append(f'<line x1="{size + 20}" y1="6" x2="{size + 20}" y2="{height + 12}" '
                 'stroke="#dcdcdc" stroke-width="1"/>')
    for offset, data, title in ((0, points, "before"), (size + 40, after, "after --rectify")):
        xs, ys = place(data, offset)
        parts.append(f'<text class="t" x="{offset + pad}" y="18">{title}</text>')
        for row in range(len(data)):
            colour = SPINE if owner[row] < 0 else WING_COLOURS[owner[row] % len(WING_COLOURS)]
            parts.append(f'<circle cx="{xs[row]:.1f}" cy="{ys[row] + 20:.1f}" r="1.6" '
                         f'fill="{colour}" fill-opacity="0.75"/>')
    legend_y = height + 32
    parts.append(f'<text x="{pad}" y="{legend_y}">grey: on-grid spine</text>')
    for index, wing in enumerate(wings):
        colour = WING_COLOURS[index % len(WING_COLOURS)]
        tx, ty = wing.get("shift", (0.0, 0.0))
        shove = f", shove ({tx:+.0f},{ty:+.0f}) m" if (tx or ty) else ""
        parts.append(
            f'<text x="{pad + 190 + index * 230}" y="{legend_y}" fill="{colour}">'
            f'wing {index + 1}: {wing["n"]} walls, {wing["deg"]:+.0f}&#176;{shove}</text>')
    parts.append("</svg>")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(parts), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ifc", nargs="?", type=Path, help="IFC to preview")
    ap.add_argument("--svg", type=Path, default=None, help="before/after plan (default: beside the IFC)")
    ap.add_argument("--json", type=Path, default=None, help="the wing transforms, replayable")
    ap.add_argument("--no-svg", action="store_true", help="report only")
    ap.add_argument("--self-test", action="store_true",
                    help="check the preview against a synthetic two-grid building")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    if not args.ifc:
        ap.error("give an IFC file, or --self-test")

    model = ifcopenshell.open(str(args.ifc))
    points, angles = wall_plan(model)
    if not len(points):
        raise SystemExit(f"{args.ifc.name} has no readable IfcWall placements; "
                         "nothing to rectify and nothing to preview.")

    on_axis = (angles < 3) | (angles > 87)
    print(f"{len(points):,} walls; {on_axis.sum():,} axis-aligned "
          f"({on_axis.mean():.0%}), {(~on_axis).sum():,} off-grid")

    wings = compute_wing_transforms(model)
    if not wings:
        print("\nNo rectifiable wing found: no off-axis angle family is large and "
              "contiguous enough. --rectify would be a no-op on this model.")
        return 0

    owner = assign(wings, points)
    spine = points[on_axis]
    print(f"\n{len(wings)} wing(s):")
    for index, wing in enumerate(wings):
        rows = owner == index
        before = clipping(points[rows], spine)
        after = clipping(move(wing, points[rows]), spine)
        tx, ty = wing.get("shift", (0.0, 0.0))
        shove = f", shove ({tx:+.0f}, {ty:+.0f}) m" if (tx or ty) else ""
        print(f"  wing {index + 1}: {wing['n']:>5} walls  rotate {wing['deg']:+6.1f}deg  "
              f"about ({wing['pivot'][0]:7.1f}, {wing['pivot'][1]:7.1f}) m{shove}")
        print(f"           walls within 2 m of the spine: {before} -> {after}")

    claimed = int((owner >= 0).sum())
    print(f"\n{claimed:,} of {len(points):,} walls ({claimed / len(points):.0%}) "
          "are inside a wing and would move.")

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps({
            "input_ifc": str(args.ifc),
            "walls": int(len(points)),
            "axis_aligned": int(on_axis.sum()),
            "hull_margin_m": WING_HULL_MARGIN_M,
            # The same records --rectify writes into summary.json, so a preview
            # and a real build can be compared field by field.
            "wings": wing_records(wings),
        }, indent=2), encoding="utf-8")
        print(f"wrote {args.json}")

    if not args.no_svg:
        out = args.svg or args.ifc.with_suffix(".rectify.svg")
        svg(points, owner, wings, out)
        print(f"wrote {out}")
    return 0


def _two_grid_fixture(path: Path, wing_degrees: float = 58.0) -> None:
    """A spine on the grid plus a wing rotated off it -- UNBC in miniature.

    The engine's thresholds are absolute (an angle family needs 250 walls, a
    wing 60), so a fixture small enough to be quick still has to be big enough
    to trip them. 600 walls builds in about a second.
    """
    import uuid

    model = ifcopenshell.file(schema="IFC4")
    guid = lambda: ifcopenshell.guid.compress(uuid.uuid4().hex)  # noqa: E731

    person = model.create_entity("IfcPerson", FamilyName="fixture")
    org = model.create_entity("IfcOrganization", Name="fixture")
    history = model.create_entity(
        "IfcOwnerHistory", model.create_entity("IfcPersonAndOrganization", person, org),
        model.create_entity("IfcApplication", org, "1", "preview_rectify fixture", "fixture"),
        ChangeAction="NOCHANGE", CreationDate=0)
    metre = model.create_entity("IfcSIUnit", UnitType="LENGTHUNIT", Name="METRE")
    units = model.create_entity("IfcUnitAssignment", [metre])
    origin = model.create_entity("IfcCartesianPoint", (0.0, 0.0, 0.0))
    context = model.create_entity(
        "IfcGeometricRepresentationContext", ContextType="Model",
        CoordinateSpaceDimension=3, Precision=1e-5,
        WorldCoordinateSystem=model.create_entity("IfcAxis2Placement3D", origin))
    model.create_entity("IfcProject", guid(), history, "fixture",
                        RepresentationContexts=[context], UnitsInContext=units)

    def wall(x: float, y: float, degrees: float) -> None:
        radians = math.radians(degrees)
        placement = model.create_entity("IfcLocalPlacement", RelativePlacement=(
            model.create_entity(
                "IfcAxis2Placement3D",
                model.create_entity("IfcCartesianPoint", (x, y, 0.0)),
                None,
                model.create_entity("IfcDirection",
                                    (math.cos(radians), math.sin(radians), 0.0)))))
        model.create_entity("IfcWall", guid(), history, f"wall {x:.0f},{y:.0f}",
                            ObjectPlacement=placement)

    # Spine: a 20 x 15 grid of on-axis walls, 300 of them.
    for column in range(20):
        for row in range(15):
            wall(column * 4.0, row * 4.0, 0.0 if row % 2 else 90.0)

    # Wing: 300 walls at `wing_degrees`. Its origin is far enough east that the
    # ROTATED footprint clears the spine -- a wing rotated by 58 degrees reaches
    # back about 48 m in -x, so an origin chosen from the unrotated extent puts
    # spine walls inside the wing hull and the fixture stops testing what it
    # meant to.
    radians = math.radians(wing_degrees)
    for column in range(20):
        for row in range(15):
            local_x, local_y = column * 4.0, row * 4.0
            wall(175.0 + local_x * math.cos(radians) - local_y * math.sin(radians),
                 10.0 + local_x * math.sin(radians) + local_y * math.cos(radians),
                 wing_degrees + (0.0 if row % 2 else 90.0))

    model.write(str(path))


def self_test() -> int:
    import tempfile

    failures: list[str] = []

    def expect(condition: bool, message: str) -> None:
        if not condition:
            failures.append(message)

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        fixture = tmp / "two-grid.ifc"
        _two_grid_fixture(fixture)

        model = ifcopenshell.open(str(fixture))
        points, angles = wall_plan(model)
        expect(len(points) == 600, f"fixture should hold 600 walls, read {len(points)}")

        on_axis = (angles < 3) | (angles > 87)
        expect(int(on_axis.sum()) == 300,
               f"300 walls should read as axis-aligned, got {int(on_axis.sum())}")

        wings = compute_wing_transforms(model)
        expect(len(wings) == 1, f"one wing expected, found {len(wings)}")
        if wings:
            wing = wings[0]
            # 58 deg squares onto the grid by -58 or +32; either is correct, and
            # the engine picks whichever leaves less overlap with the spine.
            expect(abs(abs(wing["deg"]) - 58.0) < 2.0 or abs(abs(wing["deg"]) - 32.0) < 2.0,
                   f"rotation should square the wing onto the grid, got {wing['deg']:+.1f}")
            expect(wing["n"] >= 60, f"wing should clear the min-wing floor, got {wing['n']}")

            owner = assign(wings, points)
            claimed = int((owner == 0).sum())
            expect(claimed > 200, f"the wing should claim its own walls, claimed {claimed}")
            expect(int((owner[on_axis] == 0).sum()) == 0,
                   "no axis-aligned spine wall should be claimed by the wing")

            spine = points[on_axis]
            after = clipping(move(wing, points[owner == 0]), spine)
            before_if_naive = clipping(points[owner == 0], spine)
            expect(after <= max(before_if_naive, 3),
                   f"rectification should not drive the wing INTO the spine "
                   f"({before_if_naive} -> {after} walls within 2 m)")

            out = tmp / "preview.svg"
            svg(points, owner, wings, out)
            text = out.read_text()
            expect(text.startswith("<svg") and text.rstrip().endswith("</svg>"),
                   "the SVG should be well formed")
            expect(text.count("<circle") == 1200,
                   f"both panels should draw all 600 walls, found {text.count('<circle')}")
            expect("before" in text and "after --rectify" in text,
                   "both panels should be labelled")

    if failures:
        print("self-test FAILED")
        for line in failures:
            print(f"  {line}")
        return 1
    print("self-test passed: a synthetic two-grid building, one wing found, "
          "squared onto the grid without driving into the spine")
    return 0


if __name__ == "__main__":
    sys.exit(main())
