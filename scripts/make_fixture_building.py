#!/usr/bin/env python3
"""A small two-wing building, in real IFC solids, for testing the whole pipeline.

The UNBC model is 67 MB of someone's project and is in neither repository, so
every claim about this engine has had to be either measured on that one file or
demonstrated on a fixture too thin to voxelize. This is the missing middle: a
building small enough to convert in seconds and complete enough to *walk* —
floor slabs, real walls with real openings, doors in those openings, a ceiling,
and an off-grid wing that gives `--rectify` something to do.

    python3 scripts/make_fixture_building.py --out out/fixture/building.ifc
    python3 scripts/make_fixture_building.py --out b.ifc --wing-degrees 0

The `--wing-degrees 0` variant is the control: the same building with nothing
off-grid, so any difference between the two is the wing and not the fixture.

Geometry is `IfcExtrudedAreaSolid` over rectangle profiles with per-element
placements — the shape Revit's own exporter produces, and the one the
rectification pass reads placements from.
"""

from __future__ import annotations

import argparse
import math
import uuid
from pathlib import Path

import ifcopenshell

# Everything is in metres. The interior is 3 m so a 2-block player fits with
# headroom at 1 m/voxel; rooms are >= 4 m wide so they survive F2/F3 rounding.
WALL_T = 0.3
WALL_H = 3.0
SLAB_T = 0.3
DOOR_W = 1.0
DOOR_H = 2.1


class Builder:
    def __init__(self) -> None:
        self.file = ifcopenshell.file(schema="IFC4")
        f = self.file
        org = f.create_entity("IfcOrganization", Name="bimmer")
        self.history = f.create_entity(
            "IfcOwnerHistory",
            f.create_entity("IfcPersonAndOrganization",
                            f.create_entity("IfcPerson", FamilyName="fixture"), org),
            f.create_entity("IfcApplication", org, "1", "make_fixture_building", "fixture"),
            ChangeAction="NOCHANGE", CreationDate=0)
        metre = f.create_entity("IfcSIUnit", UnitType="LENGTHUNIT", Name="METRE")
        area = f.create_entity("IfcSIUnit", UnitType="AREAUNIT", Name="SQUARE_METRE")
        volume = f.create_entity("IfcSIUnit", UnitType="VOLUMEUNIT", Name="CUBIC_METRE")
        radian = f.create_entity("IfcSIUnit", UnitType="PLANEANGLEUNIT", Name="RADIAN")
        units = f.create_entity("IfcUnitAssignment", [metre, area, volume, radian])

        self.origin = f.create_entity("IfcCartesianPoint", (0.0, 0.0, 0.0))
        world = f.create_entity("IfcAxis2Placement3D", self.origin)
        self.context = f.create_entity(
            "IfcGeometricRepresentationContext", ContextType="Model",
            CoordinateSpaceDimension=3, Precision=1e-5, WorldCoordinateSystem=world)
        self.body = f.create_entity(
            "IfcGeometricRepresentationSubContext", ContextIdentifier="Body",
            ContextType="Model", ParentContext=self.context, TargetView="MODEL_VIEW")
        self.up = f.create_entity("IfcDirection", (0.0, 0.0, 1.0))

        project = f.create_entity("IfcProject", self.guid(), self.history, "Fixture",
                                  RepresentationContexts=[self.context], UnitsInContext=units)
        zero = f.create_entity("IfcLocalPlacement", RelativePlacement=world)
        site = f.create_entity("IfcSite", self.guid(), self.history, "Site",
                               ObjectPlacement=zero, CompositionType="ELEMENT")
        building = f.create_entity("IfcBuilding", self.guid(), self.history, "Building",
                                   ObjectPlacement=zero, CompositionType="ELEMENT")
        self.storey = f.create_entity("IfcBuildingStorey", self.guid(), self.history, "L0",
                                      ObjectPlacement=zero, CompositionType="ELEMENT",
                                      Elevation=0.0)
        for parent, child in ((project, site), (site, building), (building, self.storey)):
            f.create_entity("IfcRelAggregates", self.guid(), self.history,
                            RelatingObject=parent, RelatedObjects=[child])
        self.products: list = []

    def guid(self) -> str:
        return ifcopenshell.guid.compress(uuid.uuid4().hex)

    def _box(self, cx, cy, z, length, width, height, degrees):
        """One extruded rectangle, placed and rotated in plan."""
        f = self.file
        profile = f.create_entity("IfcRectangleProfileDef", "AREA", None,
                                  f.create_entity("IfcAxis2Placement2D",
                                                  f.create_entity("IfcCartesianPoint", (0.0, 0.0))),
                                  length, width)
        solid = f.create_entity(
            "IfcExtrudedAreaSolid", profile,
            f.create_entity("IfcAxis2Placement3D", self.origin), self.up, height)
        shape = f.create_entity("IfcShapeRepresentation", self.body, "Body", "SweptSolid", [solid])
        radians = math.radians(degrees)
        placement = f.create_entity("IfcLocalPlacement", RelativePlacement=f.create_entity(
            "IfcAxis2Placement3D",
            f.create_entity("IfcCartesianPoint", (float(cx), float(cy), float(z))), None,
            f.create_entity("IfcDirection", (math.cos(radians), math.sin(radians), 0.0))))
        return placement, f.create_entity("IfcProductDefinitionShape", None, None, [shape])

    def add(self, entity, name, cx, cy, z, length, width, height, degrees, **kwargs):
        placement, shape = self._box(cx, cy, z, length, width, height, degrees)
        product = self.file.create_entity(
            entity, self.guid(), self.history, name, ObjectPlacement=placement,
            Representation=shape, Tag=str(len(self.products) + 1), **kwargs)
        self.products.append(product)
        return product

    def wall(self, name, a, b, gaps=(), base_z=0.0):
        """A wall from a to b, split around each (centre_t, width) opening.

        Splitting rather than subtracting keeps the fixture readable and gives
        the voxelizer a real hole to find, which is what the door placer needs:
        an opening it cannot walk through is the F3/D10 failure, not a door bug.
        """
        (x1, y1), (x2, y2) = a, b
        length = math.hypot(x2 - x1, y2 - y1)
        degrees = math.degrees(math.atan2(y2 - y1, x2 - x1))

        spans = [(0.0, length)]
        for centre, width in gaps:
            out = []
            for lo, hi in spans:
                if centre - width / 2 > lo:
                    out.append((lo, min(hi, centre - width / 2)))
                if centre + width / 2 < hi:
                    out.append((max(lo, centre + width / 2), hi))
            spans = out

        radians = math.radians(degrees)
        made = []
        for index, (lo, hi) in enumerate(spans):
            if hi - lo < 0.05:
                continue
            mid = (lo + hi) / 2
            made.append(self.add(
                "IfcWall", f"{name}-{index}",
                x1 + mid * math.cos(radians), y1 + mid * math.sin(radians),
                base_z + SLAB_T, hi - lo, WALL_T, WALL_H, degrees))
        return made

    def door(self, name, a, b, t, base_z=0.0):
        """A door leaf sitting in a wall's gap, `t` metres along a->b."""
        (x1, y1), (x2, y2) = a, b
        length = math.hypot(x2 - x1, y2 - y1)
        degrees = math.degrees(math.atan2(y2 - y1, x2 - x1))
        radians = math.radians(degrees)
        return self.add(
            "IfcDoor", name,
            x1 + t * math.cos(radians), y1 + t * math.sin(radians), base_z + SLAB_T,
            DOOR_W * 0.9, WALL_T * 0.5, DOOR_H, degrees,
            OverallHeight=DOOR_H, OverallWidth=DOOR_W * 0.9,
            PredefinedType="DOOR", OperationType="SINGLE_SWING_LEFT")

    def multi_box(self, entity, name, boxes, **kwargs):
        """One product whose body is several extruded boxes.

        A stair flight is a stepped solid, and `IfcShapeRepresentation.Items`
        takes a list -- so a flight is one product with one tread per item,
        rather than a product per tread. That matters downstream: the engine
        groups stair geometry per assembly, and a flight split into eleven
        products is eleven things to group.
        """
        f = self.file
        items = []
        for (cx, cy, z, length, width, height, degrees) in boxes:
            profile = f.create_entity("IfcRectangleProfileDef", "AREA", None,
                                      f.create_entity("IfcAxis2Placement2D",
                                                      f.create_entity("IfcCartesianPoint", (0.0, 0.0))),
                                      length, width)
            radians = math.radians(degrees)
            spot = f.create_entity("IfcAxis2Placement3D",
                                   f.create_entity("IfcCartesianPoint",
                                                   (float(cx), float(cy), float(z))), None,
                                   f.create_entity("IfcDirection",
                                                   (math.cos(radians), math.sin(radians), 0.0)))
            items.append(f.create_entity("IfcExtrudedAreaSolid", profile, spot, self.up, height))
        shape = f.create_entity("IfcShapeRepresentation", self.body, "Body", "SweptSolid", items)
        zero = f.create_entity("IfcLocalPlacement", RelativePlacement=f.create_entity(
            "IfcAxis2Placement3D", self.origin))
        product = f.create_entity(
            entity, self.guid(), self.history, name, ObjectPlacement=zero,
            Representation=f.create_entity("IfcProductDefinitionShape", None, None, [shape]),
            Tag=str(len(self.products) + 1), **kwargs)
        self.products.append(product)
        return product

    def stair(self, name, base_z, top_z, start, run_dir, turn_dir, width=1.2,
              treads_per_flight=3):
        """An L-shaped stair: a flight, a landing, then a flight at 90 degrees.

        Straight runs never exercise the corner-shape logic, and a stair that
        does not bend cannot show whether a bend survives voxelization. The
        landing between the flights is what makes the turn a turn rather than a
        diagonal.

        Emitted as an IfcStair aggregating its flights, because three passes in
        the engine key on that relationship and a bare flight is invisible to
        all of them.
        """
        rise = (top_z - base_z) / (treads_per_flight * 2)
        going = rise                      # 45 degrees: what a 1 m voxel can hold
        flights = []
        x, y = start
        rdx, rdy = run_dir
        z = base_z

        boxes = []
        for _ in range(treads_per_flight):
            boxes.append((x + rdx * going / 2, y + rdy * going / 2, z,
                          going, width, rise, math.degrees(math.atan2(rdy, rdx))))
            x += rdx * going
            y += rdy * going
            z += rise
        flights.append(self.multi_box("IfcStairFlight", f"{name}-flight-1", boxes))

        landing_deg = math.degrees(math.atan2(rdy, rdx))
        self.multi_box("IfcSlab", f"{name}-landing",
                       [(x + rdx * width / 2, y + rdy * width / 2, z - rise,
                         width, width, rise, landing_deg)],
                       PredefinedType="LANDING")
        x += rdx * width
        y += rdy * width

        tdx, tdy = turn_dir
        boxes = []
        for _ in range(treads_per_flight):
            boxes.append((x + tdx * going / 2, y + tdy * going / 2, z,
                          going, width, rise, math.degrees(math.atan2(tdy, tdx))))
            x += tdx * going
            y += tdy * going
            z += rise
        flights.append(self.multi_box("IfcStairFlight", f"{name}-flight-2", boxes))

        container = self.file.create_entity(
            "IfcStair", self.guid(), self.history, name,
            ObjectPlacement=self.file.create_entity(
                "IfcLocalPlacement",
                RelativePlacement=self.file.create_entity("IfcAxis2Placement3D", self.origin)),
            Tag=str(len(self.products) + 1), PredefinedType="QUARTER_TURN_STAIR")
        self.products.append(container)
        self.file.create_entity("IfcRelAggregates", self.guid(), self.history,
                                RelatingObject=container, RelatedObjects=flights)
        return container, (x, y)

    def slab(self, name, cx, cy, z, length, width, degrees, predefined="FLOOR"):
        return self.add("IfcSlab", name, cx, cy, z, length, width, SLAB_T, degrees,
                        PredefinedType=predefined)

    def write(self, path: Path) -> None:
        self.file.create_entity(
            "IfcRelContainedInSpatialStructure", self.guid(), self.history,
            RelatedElements=self.products, RelatingStructure=self.storey)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file.write(str(path))


def local(ox, oy, degrees):
    """A frame: local (x, y) -> world (x, y)."""
    radians = math.radians(degrees)
    c, s = math.cos(radians), math.sin(radians)
    return lambda x, y: (ox + x * c - y * s, oy + x * s + y * c)


def slab_with_hole(b, name, frame, width, height, z, hole, predefined="FLOOR"):
    """A floor slab as four rectangles around an opening.

    A stairwell needs a hole in the slab above it or the stair climbs into a
    ceiling -- lesson S8, which disconnected every upper floor of the real
    model. Four rectangles is the cheap way to say that in IFC without a
    boolean.
    """
    x0, y0, x1, y1 = hole
    pieces = [(0, 0, width, y0), (0, y1, width, height - y1),
              (0, y0, x0, y1 - y0), (x1, y0, width - x1, y1 - y0)]
    for index, (px, py, pw, ph) in enumerate(pieces):
        if pw <= 0.01 or ph <= 0.01:
            continue
        cx, cy = frame.at(px + pw / 2, py + ph / 2)
        b.slab(f"{name}-{index}", cx, cy, z, pw, ph, frame.degrees, predefined)


def block(b, name, frame, cols, rows, cell_x, cell_y, west_gap=True,
          base_z=0.0, hole=None):
    """A rectangular block of `cols` x `rows` rooms, with a door in every wall.

    A block, not a room: wing detection clusters wall POSITIONS and then takes
    their convex hull, so a wing whose walls all sit on its perimeter gives a
    sparse, near-collinear scatter and no hull. A real building's partitions
    fill the interior, and so must a fixture that stands in for one.
    """
    width, height = cols * cell_x, rows * cell_y
    degrees = frame.degrees
    corner = frame.at

    if hole:
        slab_with_hole(b, f"{name}-floor", frame, width, height, base_z, hole)
    else:
        b.slab(f"{name}-floor", *frame.centre(width / 2, height / 2),
               base_z, width, height, degrees)

    # Perimeter. The west side is where this block joins whatever precedes it.
    b.wall(f"{name}-w", corner(0, 0), corner(0, height),
           gaps=[(height / 2, DOOR_W)] if west_gap else (), base_z=base_z)
    if name != "spine":
        b.wall(f"{name}-e", corner(width, 0), corner(width, height), base_z=base_z)
    b.wall(f"{name}-s", corner(0, 0), corner(width, 0), base_z=base_z)
    b.wall(f"{name}-n", corner(0, height), corner(width, height), base_z=base_z)
    if west_gap:
        b.door(f"{name}-door-w", corner(0, 0), corner(0, height), height / 2, base_z)

    for column in range(1, cols):
        x = column * cell_x
        a, z = corner(x, 0), corner(x, height)
        # One doorway per room the partition separates, so every room has a way
        # out and reachability is a question about the conversion, not the plan.
        gaps = [((row + 0.5) * cell_y, DOOR_W) for row in range(rows)]
        b.wall(f"{name}-v{column}", a, z, gaps=gaps, base_z=base_z)
        for centre, _ in gaps:
            b.door(f"{name}-door-v{column}-{centre:.0f}", a, z, centre, base_z)

    for row in range(1, rows):
        y = row * cell_y
        a, z = corner(0, y), corner(width, y)
        gaps = [((column + 0.5) * cell_x, DOOR_W) for column in range(cols)]
        b.wall(f"{name}-h{row}", a, z, gaps=gaps, base_z=base_z)
        for centre, _ in gaps:
            b.door(f"{name}-door-h{row}-{centre:.0f}", a, z, centre, base_z)


class Frame:
    """A rotated local frame: local metres -> world metres."""

    def __init__(self, ox, oy, degrees):
        self.ox, self.oy, self.degrees = ox, oy, degrees
        radians = math.radians(degrees)
        self.c, self.s = math.cos(radians), math.sin(radians)

    def at(self, x, y):
        return (self.ox + x * self.c - y * self.s, self.oy + x * self.s + y * self.c)

    def centre(self, x, y):
        return self.at(x, y)


STOREY = SLAB_T + WALL_H


def build(path: Path, wing_degrees: float, storeys: int = 1) -> None:
    b = Builder()

    spine_w, spine_h = 4 * 10.0, 3 * 5.5
    spine = Frame(0.0, 0.0, 0.0)
    wing = Frame(*spine.at(spine_w, spine_h / 2 - 7.0), wing_degrees)

    # The stairwell: a hole through every slab above the ground floor, in the
    # spine's east end, with the stair standing in it.
    hole = (30.0, 1.0, 37.0, 8.0)

    for level in range(storeys):
        base = level * STOREY
        block(b, f"spine{level}", spine, cols=4, rows=3, cell_x=10.0, cell_y=5.5,
              base_z=base, hole=hole if level else None)
        block(b, f"wing{level}", wing, cols=3, rows=2, cell_x=10.0, cell_y=7.0,
              base_z=base)
        seam_a, seam_b = spine.at(spine_w, 0), spine.at(spine_w, spine_h)
        b.wall(f"spine{level}-seam", seam_a, seam_b,
               gaps=[(spine_h / 2, DOOR_W)], base_z=base)
        b.door(f"door-seam{level}", seam_a, seam_b, spine_h / 2, base)

        if level + 1 < storeys:
            # Rises into the hole in the slab above it, and turns halfway.
            b.stair(f"stair{level}", base + SLAB_T, base + STOREY + SLAB_T,
                    start=spine.at(31.0, 2.5), run_dir=(1.0, 0.0), turn_dir=(0.0, 1.0))

    # Roof over the top storey.
    top = storeys * STOREY
    b.slab("roof-spine", *spine.at(spine_w / 2, spine_h / 2), top,
           spine_w, spine_h, 0.0, "ROOF")
    b.slab("roof-wing", *wing.at(15.0, 7.0), top, 30.0, 14.0, wing_degrees, "ROOF")

    b.write(path)
    print(f"wrote {path}: {len(b.products)} products, {storeys} storey(s), "
          f"wing at {wing_degrees:g} degrees")


def reshape_as_recovery(source: Path, out: Path) -> None:
    """Rewrite an IFC the way an RVT recovery writes one.

    Reviter gives every product the SAME `IfcLocalPlacement` and bakes world
    coordinates into an `IfcTriangulatedFaceSet`. That is a legal IFC and a
    completely different file to read: anything that asks a wall where it is
    via its placement gets the same answer for every wall in the building.
    Rectification did exactly that and silently found no wings at all.

    Producing that shape here means the RVT path has a regression test that
    needs neither an RVT nor the parser -- only the shape of what the parser
    writes, which is the part this engine has to survive.
    """
    import ifcopenshell.geom

    model = ifcopenshell.open(str(source))
    settings = ifcopenshell.geom.settings()
    settings.set("use-world-coords", True)
    meshes = {}
    iterator = ifcopenshell.geom.iterator(settings, model, 1)
    if iterator.initialize():
        while True:
            shape = iterator.get()
            meshes[shape.id] = (list(shape.geometry.verts), list(shape.geometry.faces))
            if not iterator.next():
                break

    # Built from scratch, not copied. `file.add()` follows an entity's
    # references, so adding the containment relationship drags every original
    # placed product in with it and the output holds each wall twice -- once
    # tessellated on the shared placement and once on its own.
    out_builder = Builder()
    out_file = out_builder.file
    shared = out_file.create_entity(
        "IfcLocalPlacement",
        RelativePlacement=out_file.create_entity("IfcAxis2Placement3D", out_builder.origin))

    for product in model.by_type("IfcProduct"):
        verts_faces = meshes.get(product.id())
        if verts_faces is None:
            continue
        verts, faces = verts_faces
        if len(verts) < 9 or len(faces) < 3:
            continue
        coords = [tuple(verts[i:i + 3]) for i in range(0, len(verts), 3)]
        triangles = [tuple(faces[i + j] + 1 for j in range(3))  # IFC is 1-based
                     for i in range(0, len(faces), 3)]
        face_set = out_file.create_entity(
            "IfcTriangulatedFaceSet",
            out_file.create_entity("IfcCartesianPointList3D", coords),
            Closed=False, CoordIndex=triangles)
        shape = out_file.create_entity(
            "IfcShapeRepresentation", out_builder.body, "Body", "Tessellation", [face_set])
        extra = {}
        if product.is_a("IfcDoor"):
            extra = {"OverallHeight": product.OverallHeight,
                     "OverallWidth": product.OverallWidth,
                     "PredefinedType": "DOOR"}
        copy = out_file.create_entity(
            product.is_a(), out_builder.guid(), out_builder.history, product.Name,
            ObjectPlacement=shared,
            Representation=out_file.create_entity(
                "IfcProductDefinitionShape", None, None, [shape]),
            Tag=product.Tag, **extra)
        out_builder.products.append(copy)

    out_builder.write(out)
    print(f"wrote {out}: {len(out_builder.products)} tessellated products "
          "on one shared placement")
    return


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--wing-degrees", type=float, default=58.0,
                    help="0 for the on-grid control")
    ap.add_argument("--storeys", type=int, default=1,
                    help="2 or more adds a stairwell with an L-shaped stair")
    ap.add_argument("--shared-placement", action="store_true",
                    help="also write a copy shaped the way an RVT recovery writes one")
    args = ap.parse_args()
    build(args.out, args.wing_degrees, args.storeys)
    if args.shared_placement:
        reshape_as_recovery(args.out, args.out.with_name(args.out.stem + "-recovery.ifc"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
