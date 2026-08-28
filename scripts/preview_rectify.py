#!/usr/bin/env python3
"""See what `--rectify` will do to a model, before converting it.

Rectification is the one stage that visibly *moves the building* — whole wings
swing onto the voxel grid.

`docs/rectify_walls_before_after.png` and `docs/rectify_voxel_plan_before_after.png`
already show that happening on the UNBC model, and they are better evidence than
anything here: they are the real campus, from a real run. What they are not is
**reproducible**. They were committed with Phase 1 in July 2026 and no generator
went with them, so they cannot be re-made after an engine change, re-run at a
different pitch, or pointed at another model — including an IFC recovered from
the RVT rather than exported by Revit.

This makes that view a tool. `compute_wing_transforms` reads IFC wall
**placements** and nothing else: no geometry, no meshing, so it answers in
seconds rather than the ~40 minutes two full conversions take. It runs the
identical function the engine runs, and draws the answer.

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
import subprocess
import sys
from pathlib import Path

import ifcopenshell
import ifcopenshell.util.unit
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rectify import (  # noqa: E402 -- after the sys.path line, deliberately
    WING_HULL_MARGIN_M,
    compute_wing_transforms,
    on_grid,
    wall_plan,
    wing_for_point,
    wing_records,
)

# Colour-blind-safe qualitative hues; the two greys are the walls that do not
# belong to any wing.
WING_COLOURS = ["#e66100", "#5d3a9b", "#1a85ff", "#d41159", "#008080", "#994f00",
                "#40b0a6", "#e1be6a"]
SPINE = "#9a9a9a"
# Off-grid, but in no wing: too few of its angle family, or too far from the
# cluster. It stays exactly where it is and voxelizes as a jagged line, which
# is a different fact from "already on the grid" and needs its own colour.
# Measured on UNBC, this is about 7% of walls -- the committed
# docs/rectify_walls_before_after.png shows the same population.
UNCLAIMED = "#c8b8d8"


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


DAMAGE = "#c1121f"
SEAM = "#e09f3e"
# Walls that were ON the grid and are off it afterwards. Membership is a hull
# test, so a wing sweeps up any axis-aligned wall standing inside its hull and
# rotates it by the wing's angle -- which takes a wall that voxelized cleanly
# and makes it jagged. That is the pass doing the exact opposite of its job to
# a population it never counted.
KNOCKED = "#7b2cbf"


def damage(points, angles, after_angles, after_points, owner, on_axis, wings,
           clash_m=2.0, joined_m=3.0, parted_m=6.0):
    """What the move costs, per wall, not per wing.

    The report so far has been the case FOR rectification: wings squared,
    clipping down. Both of RECTIFY.md's measured costs are per-wall and were
    invisible here -- walls that end up inside something (27 of them on UNBC,
    which no rigid motion can separate because the source model interlocks
    there), and joints pulled apart at the seams, which is what the stitcher
    then has to bridge and where its corridors come from.

    Returns three boolean masks over the walls: `clashing` after the move,
    `parted` (was touching the spine, now is not), and `knocked` -- walls that
    were already on the grid and are off it afterwards, because a wing's hull
    swept them up and rotated them.
    """
    from scipy.spatial import cKDTree

    moved = owner >= 0
    clashing = np.zeros(len(points), dtype=bool)
    parted = np.zeros(len(points), dtype=bool)
    knocked = moved & on_axis & ~on_grid(after_angles)
    if not moved.any():
        return clashing, parted, knocked

    spine = points[on_axis & ~moved]
    spine_tree = cKDTree(spine) if len(spine) else None

    for index in range(len(wings)):
        rows = owner == index
        if not rows.any():
            continue
        # Everything this wing must not be inside: the spine, plus every other
        # wing where IT ends up.
        blocks = [spine] if len(spine) else []
        for other in range(len(wings)):
            if other != index and (owner == other).any():
                blocks.append(after_points[owner == other])
        if blocks:
            tree = cKDTree(np.vstack(blocks))
            clashing[rows] = tree.query(after_points[rows], k=1)[0] < clash_m

        if spine_tree is not None:
            was = spine_tree.query(points[rows], k=1)[0]
            now = spine_tree.query(after_points[rows], k=1)[0]
            # A joint that was in contact and is now well clear. That gap is
            # not damage in itself -- the stitcher exists for it -- but it is
            # where the synthesised corridors will have to go.
            parted[rows] = (was <= joined_m) & (now > parted_m)

    return clashing, parted, knocked


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


def svg(points, angles, owner, on_axis, wings, path: Path, long_edge: int = 1400) -> None:
    """A before/after plan, drawn as oriented wall segments.

    Two things the first version of this got wrong, both of which made it
    useless at the size anyone actually views it:

    - **A wall is a stick, not a dot.** Rectification is entirely about angle,
      and a dot has none. Drawn as a short segment at its true plan angle, a
      jagged 58-degree wing and an orthogonal one are told apart at a glance;
      drawn as dots they are two identical grey clouds.
    - **The panels are laid out along the model's short axis.** Side by side
      is right for a tall building and halves the scale of a wide one. The
      aspect ratio decides, so the drawing fills the canvas either way.

    A third fault came from reading the figure this repository already
    committed: walls with no wing were all drawn as "on-grid spine", when some
    of them are off-grid walls whose angle family was too small or too scattered
    to cluster. Those do not move and do voxelize as jagged lines, which is the
    opposite of being on the grid, so they get their own colour.
    """
    after_points = points.copy()
    after_angles = angles.copy()
    for index, wing in enumerate(wings):
        rows = owner == index
        if rows.any():
            after_points[rows] = move(wing, points[rows])
            after_angles[rows] = angles[rows] + wing["deg"]

    both = np.vstack([points, after_points])
    lo, hi = both.min(axis=0), both.max(axis=0)
    span = np.maximum(hi - lo, 1e-6)

    # `gap` has to clear the second panel's title, which is drawn above its
    # own top edge; at 28 it sat on the first panel's bottom border.
    pad, gap, header, footer = 20, 48, 34, 48
    # Stack a wide model, sit a tall one side by side: either way the panels
    # run along the model's LONG axis and the scale stays as large as it can.
    stacked = span[0] >= span[1]
    if stacked:
        panel_w = long_edge - 2 * pad
        scale = panel_w / span[0]
        panel_h = span[1] * scale
        width = long_edge
        height = header + panel_h * 2 + gap + footer
        offsets = [(pad, header), (pad, header + panel_h + gap)]
    else:
        panel_h = long_edge - header - footer
        scale = panel_h / span[1]
        panel_w = span[0] * scale
        width = pad * 2 + panel_w * 2 + gap
        height = long_edge
        offsets = [(pad, header), (pad + panel_w + gap, header)]

    # A wall drawn shorter than a few pixels is a dot again. Length is in world
    # metres so it stays honest, floored so it stays visible.
    stick_m = max(2.0, float(span.max()) / 90.0)
    stroke = max(1.1, scale * 0.9)

    def segments(data, data_angles, ox, oy):
        radians = np.radians(data_angles)
        half = stick_m / 2.0
        dx, dy = np.cos(radians) * half, np.sin(radians) * half
        x1 = ox + (data[:, 0] - dx - lo[0]) * scale
        x2 = ox + (data[:, 0] + dx - lo[0]) * scale
        # SVG y grows downward; plan y grows north, so flip it.
        y1 = oy + (hi[1] - (data[:, 1] - dy)) * scale
        y2 = oy + (hi[1] - (data[:, 1] + dy)) * scale
        return x1, y1, x2, y2

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.0f}" height="{height:.0f}" '
        f'viewBox="0 0 {width:.0f} {height:.0f}">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        '<style>text{font:15px system-ui,-apple-system,sans-serif;fill:#444}'
        '.t{font-size:17px;font-weight:650;fill:#111}</style>',
    ]
    clashing, parted, knocked = damage(points, angles, after_angles, after_points,
                                       owner, on_axis, wings)
    panels = ((offsets[0], points, angles, "before", False),
              (offsets[1], after_points, after_angles, "after --rectify", True))
    for (ox, oy), data, data_angles, title, is_after in panels:
        parts.append(f'<rect x="{ox:.0f}" y="{oy:.0f}" width="{panel_w:.0f}" '
                     f'height="{panel_h:.0f}" fill="#fbfbfc" stroke="#e4e4e8"/>')
        parts.append(f'<text class="t" x="{ox:.0f}" y="{oy - 9:.0f}">{title}</text>')
        x1, y1, x2, y2 = segments(data, data_angles, ox, oy)
        for row in range(len(data)):
            if owner[row] >= 0:
                colour = WING_COLOURS[owner[row] % len(WING_COLOURS)]
            else:
                colour = SPINE if on_axis[row] else UNCLAIMED
            width, opacity = stroke, 0.85
            # Only the after panel carries damage: a clash is a fact about
            # where the wall ENDS UP, and drawing it on the before panel would
            # say the move caused something it did not.
            if is_after and clashing[row]:
                colour, width, opacity = DAMAGE, stroke * 2.2, 1.0
            elif is_after and knocked[row]:
                colour, width, opacity = KNOCKED, stroke * 1.8, 1.0
            elif is_after and parted[row]:
                colour, width, opacity = SEAM, stroke * 1.8, 1.0
            parts.append(
                f'<line x1="{x1[row]:.1f}" y1="{y1[row]:.1f}" x2="{x2[row]:.1f}" '
                f'y2="{y2[row]:.1f}" stroke="{colour}" stroke-width="{width:.1f}" '
                f'stroke-linecap="round" stroke-opacity="{opacity}"/>')

    # Advance by the label's *rendered* length: `&#176;` is six characters of
    # markup and one degree sign on screen, so counting the source overshoots
    # and the entries drift apart until the last one leaves the canvas.
    def advance(label: str) -> float:
        rendered = label.replace("&#176;", "\u00b0")
        return 46 + 8.0 * len(rendered)

    legend_y = height - 16
    spine_n = int((on_axis & (owner < 0)).sum())
    loose_n = int((~on_axis & (owner < 0)).sum())
    entries = [(SPINE, f"on-grid spine: {spine_n} walls")]
    if clashing.any():
        entries.append((DAMAGE, f"clashing after the move: {int(clashing.sum())} walls"))
    if knocked.any():
        entries.append((KNOCKED, f"knocked OFF the grid: {int(knocked.sum())} walls"))
    if parted.any():
        entries.append((SEAM, f"seam pulled open: {int(parted.sum())} walls (stitcher's work)"))
    if loose_n:
        entries.append((UNCLAIMED, f"off-grid, no wing: {loose_n} walls (stay jagged)"))
    for index, wing in enumerate(wings):
        tx, ty = wing.get("shift", (0.0, 0.0))
        shove = f", shove ({tx:+.0f}, {ty:+.0f}) m" if (tx or ty) else ""
        entries.append((WING_COLOURS[index % len(WING_COLOURS)],
                        f'wing {index + 1}: {wing["n"]} walls, {wing["deg"]:+.0f}&#176;{shove}'))

    cursor = float(pad)
    for colour, label in entries:
        parts.append(f'<text x="{cursor:.0f}" y="{legend_y:.0f}">'
                     f'<tspan fill="{colour}" font-size="19">&#9644;</tspan> {label}</text>')
        cursor += advance(label)
    parts.append("</svg>")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(parts), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("model", nargs="?", type=Path, help=".ifc, or .rvt to parse first")
    ap.add_argument("--svg", type=Path, default=None, help="before/after plan (default: beside the IFC)")
    ap.add_argument("--json", type=Path, default=None, help="the wing transforms, replayable")
    ap.add_argument("--no-svg", action="store_true", help="report only")
    ap.add_argument("--self-test", action="store_true",
                    help="check the preview against a synthetic two-grid building")
    ap.add_argument("--include-facade", action="store_true",
                    help="build the wing hulls from the curtain wall's IfcPlate/IfcMember "
                         "parts as well as from walls (measured in RECTIFY.md; off by "
                         "default, and the report says why)")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    if not args.model:
        ap.error("give an .ifc or .rvt file, or --self-test")

    source_file = args.model.expanduser().resolve()
    if source_file.suffix.lower() == ".rvt":
        # Rectification reads walls and nothing else, so this is the one stage
        # that works off an RVT recovery whether or not the parser's IFC would
        # clear the voxel engine's contract. --skip-contract says so rather
        # than failing a preview on a gate about something else entirely.
        ifc = source_file.with_suffix(".preview.ifc")
        print(f"parsing {source_file.name} -> {ifc.name}", flush=True)
        result = subprocess.run([sys.executable, str(Path(__file__).with_name("rvt_to_ifc.py")),
                                 str(source_file), "--out", str(ifc), "--skip-contract"])
        if result.returncode != 0:
            raise SystemExit("\nrectify preview: the parser failed; its error is above.")
        args.model = ifc

    model = ifcopenshell.open(str(args.model))
    points, angles, source = wall_plan(model, include_facade=args.include_facade)
    if not len(points):
        raise SystemExit(f"{args.model.name} has no readable walls; "
                         "nothing to rectify and nothing to preview.")
    if source == "degenerate-placements-no-geometry":
        raise SystemExit(
            f"{args.model.name} puts every wall on one shared placement and carries no "
            "tessellated wall bodies, so where its walls are cannot be read.\n"
            "Rectification would report 100% axis-aligned and silently do nothing.")

    where = {"placements": "per-element placements",
             "tessellation": "wall footprints (the file shares one placement)"}[source]
    on_axis = on_grid(angles)
    population = "walls + curtain wall parts" if args.include_facade else "walls"
    print(f"{len(points):,} {population}, read from {where}; {on_axis.sum():,} axis-aligned "
          f"({on_axis.mean():.0%}), {(~on_axis).sum():,} off-grid")

    wings = compute_wing_transforms(model, include_facade=args.include_facade)
    if not wings:
        print("\nNo rectifiable wing found: no off-axis angle family is large and "
              "contiguous enough. --rectify would be a no-op on this model.")
        return 0

    owner = assign(wings, points)
    # The spine is what does NOT move. An axis-aligned wall inside a wing's
    # hull travels with the wing, so scoring the wing against it counts the
    # wing against itself -- which is why this line and the damage line below
    # used to disagree about the same building.
    spine = points[on_axis & (owner < 0)]
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

    after_points = points.copy()
    for index, wing in enumerate(wings):
        rows = owner == index
        if rows.any():
            after_points[rows] = move(wing, points[rows])
    after_angles = angles.copy()
    for index, wing in enumerate(wings):
        rows = owner == index
        after_angles[rows] = angles[rows] + wing["deg"]
    clashing, parted, knocked = damage(points, angles, after_angles, after_points,
                                       owner, on_axis, wings)
    print("\nwhat it costs:")
    print(f"  {int(knocked.sum())} wall(s) were ALREADY on the grid and are rotated off it "
          "-- swept up by a wing's hull and squared to the wrong frame")
    print(f"  {int(clashing.sum())} wall(s) end up within 2 m of the spine or another "
          "wing (no rigid motion separates a model that interlocks there)")
    print(f"  {int(parted.sum())} wall(s) were touching the spine and are now clear of it "
          "-- the seams the stitcher has to bridge")

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps({
            "input": str(args.model),
            "walls": int(len(points)),
            "axis_aligned": int(on_axis.sum()),
            "include_facade": bool(args.include_facade),
            "hull_margin_m": WING_HULL_MARGIN_M,
            # The same records --rectify writes into summary.json, so a preview
            # and a real build can be compared field by field.
            "wings": wing_records(wings),
        }, indent=2), encoding="utf-8")
        print(f"wrote {args.json}")

    if not args.no_svg:
        out = args.svg or args.model.with_suffix(".rectify.svg")
        svg(points, angles, owner, on_axis, wings, out)
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

    # A scatter of odd angles: too few of any one family to be a wing, so they
    # stay put and voxelize as jagged lines. Without them the fixture has only
    # two populations and cannot tell "on the grid" apart from "off the grid and
    # not moving" -- which is exactly the confusion this fixture missed once.
    for index in range(24):
        wall(-40.0 + index * 1.5, -30.0 - index * 0.7, 17.0 + index * 2.0)

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


def _two_wing_fixture(path: Path) -> None:
    """A spine and two wings at DIFFERENT angles, placed close together.

    One wing cannot catch either of the faults this exercises. A single family
    hides a wing being rotated by another family's angle, because there is only
    one angle; and a single wing hides every wing being squared while blind to
    its neighbours, because there is no neighbour.
    """
    import uuid

    model = ifcopenshell.file(schema="IFC4")
    guid = lambda: ifcopenshell.guid.compress(uuid.uuid4().hex)  # noqa: E731

    org = model.create_entity("IfcOrganization", Name="fixture")
    history = model.create_entity(
        "IfcOwnerHistory",
        model.create_entity("IfcPersonAndOrganization",
                            model.create_entity("IfcPerson", FamilyName="fixture"), org),
        model.create_entity("IfcApplication", org, "1", "two-wing fixture", "fixture"),
        ChangeAction="NOCHANGE", CreationDate=0)
    metre = model.create_entity("IfcSIUnit", UnitType="LENGTHUNIT", Name="METRE")
    origin = model.create_entity("IfcCartesianPoint", (0.0, 0.0, 0.0))
    axis = model.create_entity("IfcAxis2Placement3D", origin)
    context = model.create_entity(
        "IfcGeometricRepresentationContext", ContextType="Model",
        CoordinateSpaceDimension=3, Precision=1e-5, WorldCoordinateSystem=axis)
    model.create_entity("IfcProject", guid(), history, "fixture",
                        RepresentationContexts=[context],
                        UnitsInContext=model.create_entity("IfcUnitAssignment", [metre]))

    def wall(x: float, y: float, degrees: float) -> None:
        radians = math.radians(degrees)
        placement = model.create_entity("IfcLocalPlacement", RelativePlacement=(
            model.create_entity(
                "IfcAxis2Placement3D",
                model.create_entity("IfcCartesianPoint", (x, y, 0.0)), None,
                model.create_entity("IfcDirection",
                                    (math.cos(radians), math.sin(radians), 0.0)))))
        model.create_entity("IfcWall", guid(), history, f"wall {x:.0f},{y:.0f}",
                            ObjectPlacement=placement)

    for column in range(20):
        for row in range(15):
            wall(column * 4.0, row * 4.0, 0.0 if row % 2 else 90.0)

    def block(ox: float, oy: float, degrees: float) -> None:
        radians = math.radians(degrees)
        for column in range(20):
            for row in range(15):
                lx, ly = column * 4.0, row * 4.0
                wall(ox + lx * math.cos(radians) - ly * math.sin(radians),
                     oy + lx * math.sin(radians) + ly * math.cos(radians),
                     degrees + (0.0 if row % 2 else 90.0))

    block(175.0, 10.0, 58.0)     # one family
    block(150.0, 150.0, 35.0)    # a different one, near enough to interfere
    model.write(str(path))


def _shared_placement_fixture(path: Path, wing_degrees: float = 58.0) -> None:
    """The same building written the way an RVT recovery writes it.

    One `IfcLocalPlacement` for every product, and the wall's real position and
    rotation baked into an `IfcTriangulatedFaceSet` in world coordinates. Read
    through placements this file is 600 walls at one point at zero degrees --
    a perfectly grid-aligned building with nothing to rectify.
    """
    import uuid

    model = ifcopenshell.file(schema="IFC4")
    guid = lambda: ifcopenshell.guid.compress(uuid.uuid4().hex)  # noqa: E731

    org = model.create_entity("IfcOrganization", Name="fixture")
    history = model.create_entity(
        "IfcOwnerHistory",
        model.create_entity("IfcPersonAndOrganization",
                            model.create_entity("IfcPerson", FamilyName="fixture"), org),
        model.create_entity("IfcApplication", org, "1", "shared-placement fixture", "fixture"),
        ChangeAction="NOCHANGE", CreationDate=0)
    metre = model.create_entity("IfcSIUnit", UnitType="LENGTHUNIT", Name="METRE")
    origin = model.create_entity("IfcCartesianPoint", (0.0, 0.0, 0.0))
    axis = model.create_entity("IfcAxis2Placement3D", origin)
    context = model.create_entity(
        "IfcGeometricRepresentationContext", ContextType="Model",
        CoordinateSpaceDimension=3, Precision=1e-5, WorldCoordinateSystem=axis)
    body = model.create_entity(
        "IfcGeometricRepresentationSubContext", ContextIdentifier="Body",
        ContextType="Model", ParentContext=context, TargetView="MODEL_VIEW")
    model.create_entity("IfcProject", guid(), history, "fixture",
                        RepresentationContexts=[context],
                        UnitsInContext=model.create_entity("IfcUnitAssignment", [metre]))
    # THE point of this fixture: one placement, shared by every wall.
    shared = model.create_entity("IfcLocalPlacement", RelativePlacement=axis)

    def wall(x: float, y: float, degrees: float) -> None:
        radians = math.radians(degrees)
        c, s_ = math.cos(radians), math.sin(radians)
        corners = [(-3.6, -0.15), (3.6, -0.15), (3.6, 0.15), (-3.6, 0.15)]
        base = [(x + lx * c - ly * s_, y + lx * s_ + ly * c) for lx, ly in corners]
        coords = [(px, py, 0.0) for px, py in base] + [(px, py, 3.0) for px, py in base]
        face_set = model.create_entity(
            "IfcTriangulatedFaceSet",
            model.create_entity("IfcCartesianPointList3D", coords),
            Closed=False,
            CoordIndex=[(1, 2, 3), (1, 3, 4), (5, 6, 7), (5, 7, 8)])
        shape = model.create_entity(
            "IfcShapeRepresentation", body, "Body", "Tessellation", [face_set])
        model.create_entity(
            "IfcWall", guid(), history, f"wall {x:.0f},{y:.0f}",
            ObjectPlacement=shared,
            Representation=model.create_entity("IfcProductDefinitionShape", None, None, [shape]))

    for column in range(20):
        for row in range(15):
            wall(column * 4.0, row * 4.0, 0.0 if row % 2 else 90.0)
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
        points, angles, source = wall_plan(model)
        expect(source == "placements", f"the fixture uses per-element placements, got {source}")
        expect(len(points) == 624, f"fixture should hold 624 walls, read {len(points)}")

        on_axis = on_grid(angles)
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

            loose = int((~on_axis & (owner < 0)).sum())
            expect(loose > 0, "the fixture must carry off-grid walls that join no wing, "
                              "or the three-population colouring is never exercised")

            out = tmp / "preview.svg"
            svg(points, angles, owner, on_axis, wings, out)
            text = out.read_text()
            expect(text.startswith("<svg") and text.rstrip().endswith("</svg>"),
                   "the SVG should be well formed")
            expect(text.count("<line") == 1248,
                   f"both panels should draw all 624 walls, found {text.count('<line')}")
            expect("before" in text and "after --rectify" in text,
                   "both panels should be labelled")
            # The bug the committed UNBC figure exposed: these were drawn and
            # labelled as on-grid spine, which is the opposite of what they are.
            expect(UNCLAIMED in text,
                   "off-grid walls in no wing must not be coloured as on-grid spine")
            expect("stay jagged" in text, "and the legend must say what they are")

        # The same building written the way an RVT recovery writes it. Read
        # through placements it is 600 walls at one point at zero degrees, and
        # rectification is a no-op that reports success -- which is exactly how
        # this went unnoticed until the two producers were compared.
        shared = tmp / "shared-placement.ifc"
        _shared_placement_fixture(shared)
        shared_model = ifcopenshell.open(str(shared))

        from ifcopenshell.util import placement as _placement  # noqa: PLC0415
        matrices = {tuple(np.asarray(_placement.get_local_placement(w.ObjectPlacement))
                          .round(6).ravel())
                    for w in shared_model.by_type("IfcWall")}
        expect(len(matrices) == 1,
               "the fixture must share ONE placement, or it is not testing the fallback")

        shared_points, shared_angles, shared_source = wall_plan(shared_model)
        expect(shared_source == "tessellation",
               f"a shared-placement file must fall back to footprints, got {shared_source}")
        expect(len(shared_points) == 600,
               f"all 600 walls should be recovered, got {len(shared_points)}")
        shared_on_axis = on_grid(shared_angles)
        expect(int(shared_on_axis.sum()) == 300,
               f"300 on-grid expected, got {int(shared_on_axis.sum())} -- "
               "all 600 means the placements were trusted")

        shared_wings = compute_wing_transforms(shared_model)
        expect(len(shared_wings) == 1,
               f"the wing must be found from footprints too, found {len(shared_wings)}")

        # Two wings at different angles, close enough to interfere.
        pair = tmp / "two-wing.ifc"
        _two_wing_fixture(pair)
        pair_model = ifcopenshell.open(str(pair))
        pair_points, pair_angles, _ = wall_plan(pair_model)
        pair_wings = compute_wing_transforms(pair_model)
        expect(len(pair_wings) == 2, f"two wings expected, found {len(pair_wings)}")

        if len(pair_wings) == 2:
            # Each wing must be squared by ITS OWN family's angle. When the
            # family did not travel with its cluster, both got the last one.
            squared = [abs((wing["deg"] + angle) % 90.0) < 2.0
                       or abs((wing["deg"] + angle) % 90.0 - 90.0) < 2.0
                       for wing, angle in zip(pair_wings, (58.0, 35.0))]
            rotations = [round(w["deg"], 1) for w in pair_wings]
            expect(len({round(w["deg"], 1) for w in pair_wings}) == 2,
                   f"two families must give two rotations, got {rotations}")
            expect(all(squared), f"each wing must land on the grid, rotations {rotations}")

            # And having moved, the wings must not be inside each other.
            pair_owner = assign(pair_wings, pair_points)
            moved = [move(w, pair_points[pair_owner == i]) for i, w in enumerate(pair_wings)]
            expect(clipping(moved[0], moved[1]) == 0,
                   f"the two wings overlap after placement: "
                   f"{clipping(moved[0], moved[1])} walls within 2 m of the other wing")

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
