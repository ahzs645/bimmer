#!/usr/bin/env python3
"""Does this IFC satisfy what `ifc_to_voxels.py` actually reads?

The engine is semantic, not geometric: walkability comes from IFC *facts*
(product class, `OverallWidth`, stair aggregation, host relationships), with
geometry only as a tie-breaker (see ASSUMPTIONS.md). So an IFC can open fine,
voxelize without an error, and still produce a building whose stairwells are
solid and whose stringers are curtain-wall grey -- because a relationship the
engine reads was never written.

That failure is silent today. This script makes it visible *before* a 40-minute
conversion, and it is the gate for accepting an IFC from a new producer -- the
Revit desktop exporter, Autodesk Platform Services, or Reviter's native RVT
recovery (see REVITER.md).

It reads attributes and relationships only -- no `create_shape`, no meshing --
so it runs on an 80 MB model in seconds.

    python3 scripts/check_ifc_contract.py model.ifc
    python3 scripts/check_ifc_contract.py model.ifc --json contract.json
    python3 scripts/check_ifc_contract.py --self-test

Exit code is 1 if any check FAILs, 0 otherwise (WARNs do not fail the gate).

The engine's own tables are read out of `ifc_to_voxels.py` with `ast` rather
than copied here, so this file cannot drift from the converter it checks. It
deliberately does not import that module: it needs no trimesh.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

import ifcopenshell
import ifcopenshell.util.unit

ENGINE = Path(__file__).with_name("ifc_to_voxels.py")

# Statuses, worst last -- the run's verdict is the worst status any check took.
OK, WARN, FAIL = "OK", "WARN", "FAIL"
_RANK = {OK: 0, WARN: 1, FAIL: 2}


def load_engine_constants(path: Path = ENGINE) -> dict:
    """Read the converter's literal tables without importing it.

    Importing would pull in trimesh and ifcopenshell.geom for a script that
    only reads attributes. Parsing keeps the single source of truth in the
    engine while letting this run anywhere ifcopenshell does.
    """
    wanted = {"SEMANTIC_CLASSES", "EXCLUDE_TYPES", "DOOR_TYPES", "CLASS_PRIORITY",
              "CLASS_BLOCKS"}
    found: dict = {}
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name) and target.id in wanted:
            found[target.id] = ast.literal_eval(node.value)
    missing = wanted - found.keys()
    if missing:
        raise SystemExit(
            f"{path.name} no longer defines {sorted(missing)} as module-level "
            "literals; this checker reads them from there and must be updated."
        )
    return found


def load_engine_function(name: str, path: Path = ENGINE):
    """Lift one dependency-free helper out of the engine so it can be tested.

    Same reason as `load_engine_constants`: the engine imports trimesh, which
    this script has no use for. Only functions that close over nothing may be
    lifted this way, and `exec` on an empty namespace enforces that -- a helper
    that grew a dependency raises here instead of being tested against a stale
    copy.
    """
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            namespace: dict = {}
            exec(compile(ast.Module([node], []), str(path), "exec"), namespace)
            return namespace[name]
    raise SystemExit(f"{path.name} no longer defines {name}()")


class Report:
    """Ordered check results plus the machine-readable payload behind them."""

    def __init__(self) -> None:
        self.checks: list[dict] = []

    def add(self, name: str, status: str, headline: str,
            consequence: str = "", detail: dict | None = None) -> None:
        self.checks.append({
            "check": name,
            "status": status,
            "headline": headline,
            # What breaks in the *voxel world* if this is not satisfied. A
            # checker that only says "missing" teaches nobody why to care.
            "consequence": consequence,
            "detail": detail or {},
        })

    @property
    def verdict(self) -> str:
        return max((c["status"] for c in self.checks), key=lambda s: _RANK[s], default=OK)

    def render(self) -> str:
        mark = {OK: "ok", WARN: "warn", FAIL: "FAIL"}
        lines = []
        for c in self.checks:
            lines.append(f"[{mark.get(c['status'], c['status']):^6}] {c['check']}: {c['headline']}")
            if c["status"] != OK and c["consequence"]:
                lines.append(f"           -> {c['consequence']}")
        lines.append("")
        lines.append(f"verdict: {self.verdict}")
        return "\n".join(lines)


def _plural(count: int, noun: str, suffix: str = "s") -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}{suffix}"


def _by_type(model, name: str):
    """`by_type` that tolerates an entity the file's schema does not declare.

    The two schemas this pipeline accepts do not agree on their vocabulary --
    `IfcCartesianPointList3D` arrived with IFC4, for one -- and ifcopenshell
    raises rather than returning nothing for a name its schema has never heard
    of. A checker that has to run on both cannot let that be an exception.
    """
    try:
        return model.by_type(name)
    except RuntimeError:
        return []


def _products(model):
    """Physical products only: no spatial containers, no subtractive features."""
    out = []
    for p in _by_type(model, "IfcProduct"):
        if p.is_a("IfcSpatialStructureElement") or p.is_a("IfcFeatureElement"):
            continue
        out.append(p)
    return out


def check_units(model, report: Report) -> float:
    scale = ifcopenshell.util.unit.calculate_unit_scale(model)
    detail = {"unit_scale_to_metres": scale}
    if abs(scale - 1.0) < 1e-9:
        report.add("units", OK, "length unit is metres (scale 1.0)", detail=detail)
    else:
        # Not an error: IfcOpenShell normalises geometry to metres regardless,
        # and the engine scales OverallWidth by exactly this factor. It is
        # worth stating because --pitch is in metres and a mm file that
        # reported the wrong unit would be 1000x off with no other symptom.
        report.add("units", WARN, f"length unit is {scale} m per file unit",
                   "--pitch is in metres; confirm the model spans ~1e2 m, not 1e5.",
                   detail)
    return scale


def _sampled_extent(model, sample: int):
    """Building extent from whichever carrier this file uses for coordinates.

    Two export styles need covering. The Autodesk exporter distributes elements
    with per-element `IfcLocalPlacement`s; Reviter's exporter bakes world
    coordinates into `IfcCartesianPointList3D` and gives every element the same
    placement. Reading only one of the two reports a building 0 m across on
    files written the other way.
    """
    lo = [float("inf")] * 3
    hi = [float("-inf")] * 3
    seen = 0

    for lst in _by_type(model, "IfcCartesianPointList3D")[:sample]:
        for point in lst.CoordList:
            for axis in range(3):
                value = point[axis]
                lo[axis] = min(lo[axis], value)
                hi[axis] = max(hi[axis], value)
            seen += 1

    for placement in _by_type(model, "IfcAxis2Placement3D")[:sample]:
        location = placement.Location
        if location is None or location.Coordinates is None:
            continue
        coords = location.Coordinates
        if len(coords) != 3:
            continue
        for axis in range(3):
            lo[axis] = min(lo[axis], coords[axis])
            hi[axis] = max(hi[axis], coords[axis])
        seen += 1

    if seen == 0 or any(v == float("inf") for v in lo):
        return None, 0
    return [hi[a] - lo[a] for a in range(3)], seen


def check_up_axis(model, scale: float, sample: int, report: Report) -> None:
    spans, seen = _sampled_extent(model, sample)
    if spans is None:
        report.add("up_axis", WARN, "no coordinates sampled; cannot infer the vertical axis",
                   "Run with a larger --sample, or the file carries no explicit coordinates.")
        return

    metres = [s * scale for s in spans]
    vertical = min(range(3), key=lambda a: metres[a])
    detail = {"spans_metres": [round(v, 3) for v in metres],
              "shortest_axis": "xyz"[vertical], "points_sampled": seen}
    text = " x ".join(f"{v:.1f}" for v in metres)

    if vertical == 2:
        report.add("up_axis", OK, f"Z-up: model spans {text} m", detail=detail)
    else:
        # IFC *defines* +Z as up, so this is a broken producer rather than a
        # dialect. The engine builds its grid as (x, y, z_up) and unpacks to
        # Minecraft as mc = [x, z_up, -y]: a Y-up file voxelizes the building
        # lying on its side, and every storey becomes a vertical wall.
        report.add("up_axis", FAIL, f"shortest axis is {'xyz'[vertical]}, not Z (spans {text} m)",
                   "The engine assumes +Z up. A Y-up file voxelizes the building on its side. "
                   "Note web-ifc reports Y-up axes by convention -- if a producer measured its "
                   "output through web-ifc, it may have written what it measured.",
                   detail)


def check_class_coverage(model, consts: dict, report: Report) -> None:
    semantic = consts["SEMANTIC_CLASSES"]
    excluded = consts["EXCLUDE_TYPES"]
    doors = consts["DOOR_TYPES"]

    per_class: Counter = Counter()
    fell_through: Counter = Counter()
    total = 0
    for product in _products(model):
        ifc_type = product.is_a()
        total += 1
        if ifc_type in excluded:
            per_class["(excluded)"] += 1
        elif ifc_type in doors:
            per_class["(door)"] += 1
        elif ifc_type in semantic:
            per_class[semantic[ifc_type]] += 1
        else:
            per_class["other"] += 1
            fell_through[ifc_type] += 1

    other = per_class["other"]
    share = other / total if total else 0.0
    detail = {"products": total, "per_voxel_class": dict(per_class.most_common()),
              "unmapped_ifc_types": dict(fell_through.most_common(12)),
              "other_share": round(share, 4)}

    headline = (f"{_plural(total, 'product')}; {other} ({share:.1%}) land in the "
                "catch-all 'other' class")
    consequence = ("'other' voxelizes as solid light-grey concrete with no walkability "
                   "semantics -- a stair or railing that arrives unclassified becomes a "
                   "solid block that seals the space it occupies.")
    if share == 0:
        report.add("class_coverage", OK, headline, detail=detail)
    elif share < 0.02:
        report.add("class_coverage", WARN, headline, consequence, detail)
    else:
        report.add("class_coverage", FAIL, headline, consequence, detail)


def check_doors(model, consts: dict, scale: float, pitch: float, report: Report) -> None:
    doors = [d for t in consts["DOOR_TYPES"] for d in _by_type(model, t)]
    if not doors:
        report.add("doors", FAIL, "no IfcDoor products",
                   "Functional openable doors are the pipeline's design point; without "
                   "IfcDoor every doorway voxelizes shut as wall mass.")
        return

    widths = []
    missing = 0
    for door in doors:
        width = getattr(door, "OverallWidth", None)
        if width is None or width <= 0:
            missing += 1
        else:
            widths.append(width * scale)

    leaves = Counter(max(1, round(w / pitch)) for w in widths)
    # `n = round(width/pitch)` flips leaf count across each half-cell boundary,
    # so a width within a few cm of one is a coin toss between a single and a
    # double door. Measuring the fragile population is more useful than
    # asserting a threshold the engine does not actually use.
    fragile = sum(1 for w in widths if abs((w / pitch) % 1.0 - 0.5) < 0.1)

    detail = {"doors": len(doors), "missing_overall_width": missing,
              "leaf_histogram": dict(sorted(leaves.items())),
              "fragile_near_half_cell": fragile, "pitch_m": pitch}
    headline = (f"{_plural(len(doors), 'door')}; {missing} without OverallWidth; "
                f"{fragile} within 0.1 cell of a leaf-count boundary")
    consequence = ("Doors with no OverallWidth fall back to a single leaf, so every double "
                   "door in an entrance bank narrows to 1 m. A producer that derives "
                   "OverallWidth from a bounding box rather than the host wall's direction "
                   "reports a diagonal, not a width -- see REVITER.md.")

    if missing == 0 and fragile <= len(doors) * 0.05:
        report.add("doors", OK, headline, detail=detail)
    elif missing > len(doors) * 0.5:
        report.add("doors", FAIL, headline, consequence, detail)
    else:
        report.add("doors", WARN, headline, consequence, detail)


def check_stair_aggregation(model, report: Report) -> None:
    """The engine's three stair passes all key on `IfcRelAggregates`.

    `element.Decomposes` decides (1) whether an IfcMember is a stringer or a
    curtain-wall mullion, (2) which flights share a stairwell for the climb
    audit and switchback rebuild, and (3) which flights belong to a
    SPIRAL_STAIR and should be replaced by a synthesised walkable spiral.
    A file with flights but no aggregation passes every geometric check and
    still produces unclimbable stairwells.
    """
    stairs = _by_type(model, "IfcStair") + _by_type(model, "IfcRamp")
    flights = _by_type(model, "IfcStairFlight") + _by_type(model, "IfcRampFlight")
    aggregated = [f for f in flights if f.Decomposes]
    # IFC2X3 spells the shape enum `ShapeType`, IFC4 `PredefinedType`. The
    # engine reads both (`stair_shape`); reading one would silently disable
    # spiral synthesis on half the files this pipeline now accepts.
    spiral = [s for s in _by_type(model, "IfcStair")
              if (getattr(s, "ShapeType", None)
                  or getattr(s, "PredefinedType", None)) == "SPIRAL_STAIR"]
    stringers = [m for m in _by_type(model, "IfcMember")
                 if m.Decomposes and m.Decomposes[0].RelatingObject.is_a() in ("IfcStair", "IfcRamp")]

    detail = {"stair_and_ramp_containers": len(stairs), "flights": len(flights),
              "flights_with_aggregate_parent": len(aggregated),
              "spiral_stairs": len(spiral), "members_inside_a_stair": len(stringers)}
    headline = (f"{_plural(len(stairs), 'stair/ramp container')}, "
                f"{_plural(len(flights), 'flight')}, {len(aggregated)} aggregated; "
                f"{len(spiral)} spiral")

    if not flights and not stairs:
        report.add("stair_aggregation", FAIL, "no stairs or flights at all",
                   "Every storey above the ground floor becomes unreachable.", detail)
    elif not stairs or not aggregated:
        report.add("stair_aggregation", FAIL, headline,
                   "Flights exist but no IfcRelAggregates links them to a container. "
                   "Stringers voxelize as curtain-wall frame, each flight becomes its own "
                   "'well' so the climb audit and switchback rebuild cannot see a "
                   "stairwell, and spiral synthesis never triggers.", detail)
    elif len(aggregated) < len(flights):
        report.add("stair_aggregation", WARN, headline,
                   f"{len(flights) - len(aggregated)} flights have no container; those "
                   "stairwells are rebuilt per flight rather than per well.", detail)
    else:
        report.add("stair_aggregation", OK, headline, detail=detail)


def check_openings(model, report: Report) -> None:
    """Host relationships: needed for parametric opening replay (RECTIFY Phase 3).

    Phase 1 rectification moves whole wings rigidly, so doors ride their walls
    and nothing has to be replayed. Phase 3 rewrites the wall graph, and then a
    door has to be re-instantiated at the same fraction along its *moved* host
    wall. That needs `FillsVoids -> IfcOpeningElement -> VoidsElements -> wall`
    to resolve; a file without it can be voxelized but cannot be rectified
    beyond Phase 1.
    """
    doors = _by_type(model, "IfcDoor") + _by_type(model, "IfcWindow")
    hosted = 0
    for element in doors:
        for fills in getattr(element, "FillsVoids", None) or []:
            opening = fills.RelatingOpeningElement
            if opening is not None and (getattr(opening, "VoidsElements", None) or []):
                hosted += 1
                break

    voids = len(_by_type(model, "IfcRelVoidsElement"))
    fills_rels = len(_by_type(model, "IfcRelFillsElement"))
    share = hosted / len(doors) if doors else 0.0
    detail = {"doors_and_windows": len(doors), "with_resolvable_host": hosted,
              "host_share": round(share, 4), "rel_voids": voids, "rel_fills": fills_rels}
    headline = f"{hosted}/{len(doors)} doors+windows resolve to a host element ({share:.1%})"
    consequence = ("Voxelizes fine, but RECTIFY Phase 3 (per-storey wall-graph "
                   "schematization) cannot replay openings onto rewritten walls without "
                   "the host chain. Phase 1's rigid wing rotation is unaffected.")

    if share >= 0.9:
        report.add("openings", OK, headline, detail=detail)
    elif share > 0:
        report.add("openings", WARN, headline, consequence, detail)
    else:
        report.add("openings", WARN, headline, consequence, detail)


# Everything a player can stand on, whatever class the producer chose for it.
FLOOR_CLASS_TYPES = ("IfcSlab", "IfcCovering", "IfcRoof", "IfcRamp")


def check_floor_plates(model, report: Report) -> None:
    """Floors are what the walkability audit walks on.

    Counted by distinct `Tag` across every floor class, NOT by IfcSlab
    entities, and that is not a detail. Producers disagree about both halves
    of an entity count:

    - **Class.** A Revit Roof is one `IfcRoof` to one producer and an
      `IfcRoof` container plus N `IfcSlab(.ROOF.)` parts to another. A Revit
      Ramp is one `IfcRamp` to one and a container plus a separate
      `IfcSlab(.LANDING.)` to the other.
    - **Cardinality.** The decomposing producer writes 161 `IfcSlab` entities
      for 107 Revit elements, all sharing the parent's Tag.

    Counting `IfcSlab` entities alone therefore reported this building's
    recovery as 94 against 107 -- "floor/landing recovery remains incomplete",
    carried as a HIGH severity gap in REVITER.md for weeks. It was an artifact
    of this function. Joined on Tag across the four classes, all 94 match, the
    13 "missing" are 12 Revit Roofs and one ramp landing that the recovery
    writes under their own class with plan footprints agreeing to 0.7%, and
    the recovery has 182 floor-class elements against 172. Measured
    independently of Tag, 99.92% of the other producer's standable surface is
    reproduced within half a metre; 87 sq m of 103,935 is genuinely absent.

    A count that cannot survive a change of producer is not a contract check.
    """
    counts = {name: len(_by_type(model, name)) for name in FLOOR_CLASS_TYPES}
    tagged = set()
    untagged = 0
    for name in FLOOR_CLASS_TYPES:
        for product in _by_type(model, name):
            tag = getattr(product, "Tag", None)
            if tag:
                tagged.add(str(tag).strip())
            else:
                untagged += 1
    storeys = len(_by_type(model, "IfcBuildingStorey"))
    total = len(tagged) + untagged

    detail = {**counts, "storeys": storeys,
              "floor_class_elements": total, "distinct_tags": len(tagged),
              "untagged_products": untagged}
    headline = (f"{total} floor-class elements by Tag ("
                + ", ".join(f"{n} {name[3:].lower()}" for name, n in counts.items())
                + f" as products) across {storeys} storeys")
    consequence = ("Floor plates are the walkable surface and the door sill anchor. "
                   "A producer whose slab recovery is incomplete yields storeys the "
                   "reachability audit reports as unreachable because there is nothing "
                   "to stand on -- not because a door failed.")

    if total == 0:
        report.add("floor_plates", FAIL, headline, consequence, detail)
    elif storeys and total < storeys:
        report.add("floor_plates", WARN, headline, consequence, detail)
    else:
        report.add("floor_plates", OK, headline, detail=detail)


def check_join_key(model, report: Report) -> None:
    """`Tag` is the only identifier that survives a change of IFC producer.

    Two IFCs of the same building written by different exporters share no
    GlobalIds -- each producer derives them its own way. They do share the
    Revit element id, which both write into `Tag`. Anything keyed on GlobalId
    (a curated --overrides file, a doors.csv diff between two builds) silently
    matches nothing across producers; keyed on Tag it transfers.
    """
    products = _products(model)
    tagged = sum(1 for p in products if getattr(p, "Tag", None))
    numeric = sum(1 for p in products
                  if getattr(p, "Tag", None) and str(p.Tag).strip().lstrip("-").isdigit())
    share = tagged / len(products) if products else 0.0

    detail = {"products": len(products), "with_tag": tagged,
              "with_numeric_tag": numeric, "tag_share": round(share, 4)}
    headline = f"{tagged}/{len(products)} products carry a Tag ({numeric} numeric)"
    consequence = ("Without Tag the only cross-producer join is geometric registration. "
                   "Overrides and per-door diffs then cannot be reused between an "
                   "Autodesk export and a Reviter recovery of the same building.")

    if share >= 0.95:
        report.add("join_key", OK, headline, detail=detail)
    elif share > 0:
        report.add("join_key", WARN, headline, consequence, detail)
    else:
        report.add("join_key", WARN, headline, consequence, detail)


def check_provenance(model, consts: dict, report: Report) -> None:
    """If the producer declares geometry provenance, grade it by voxel class.

    Reviter writes a `Reviter_Recovery` property set stating whether each body
    is native, reconstructed or an axis-aligned bounds fallback. A bounds box
    is nearly harmless for a wall (a wall *is* a box) and fatal for a stair or
    a railing: a stair's AABB is a solid cube that seals its own stairwell.
    Grading the fallbacks by class turns one global percentage into the list
    of elements that will actually break the world.
    """
    semantic = consts["SEMANTIC_CLASSES"]
    by_class: dict[str, Counter] = defaultdict(Counter)
    seen = 0

    for rel in _by_type(model, "IfcRelDefinesByProperties"):
        pset = rel.RelatingPropertyDefinition
        if not pset or not pset.is_a("IfcPropertySet") or pset.Name != "Reviter_Recovery":
            continue
        provenance = None
        for prop in pset.HasProperties or []:
            if prop.is_a("IfcPropertySingleValue") and prop.Name in (
                    "GeometryProvenance", "FinalProvenance", "Provenance"):
                value = prop.NominalValue
                provenance = getattr(value, "wrappedValue", value)
                break
        if provenance is None:
            continue
        for obj in rel.RelatedObjects or []:
            cls = "(door)" if obj.is_a() == "IfcDoor" else semantic.get(obj.is_a(), "other")
            by_class[str(provenance)][cls] += 1
            seen += 1

    if seen == 0:
        report.add("provenance", OK, "no Reviter_Recovery property set (producer declares none)",
                   detail={"products_with_provenance": 0})
        return

    fallback = by_class.get("bounds-fallback", Counter())
    # Classes whose walkability depends on their real shape. A box here is not
    # an approximation, it is a wall where a passage should be.
    critical = sum(fallback[c] for c in ("stair", "railing", "floor", "roof"))
    detail = {"products_with_provenance": seen,
              "by_provenance": {k: dict(v.most_common()) for k, v in by_class.items()},
              "critical_bounds_fallbacks": critical}
    headline = (f"{_plural(seen, 'product')} declare provenance; "
                f"{sum(fallback.values())} bounds fallbacks, {critical} of them "
                "in walkability-critical classes")
    consequence = ("A bounds fallback on a stair, railing, floor or roof is a solid box "
                   "where the world needs a shape: stairwells seal, guardrails become "
                   "walls, and the reachability audit reports the loss as unreachable floor.")

    if critical == 0:
        report.add("provenance", OK, headline, detail=detail)
    elif critical < 50:
        report.add("provenance", WARN, headline, consequence, detail)
    else:
        report.add("provenance", FAIL, headline, consequence, detail)


def audit(path: Path, pitch: float, sample: int) -> Report:
    consts = load_engine_constants()
    model = ifcopenshell.open(str(path))
    report = Report()
    report.add("schema", OK, f"{model.schema}, {len(_by_type(model, 'IfcProduct'))} products",
               detail={"schema": model.schema})
    scale = check_units(model, report)
    check_up_axis(model, scale, sample, report)
    check_class_coverage(model, consts, report)
    check_doors(model, consts, scale, pitch, report)
    check_stair_aggregation(model, report)
    check_floor_plates(model, report)
    check_openings(model, report)
    check_join_key(model, report)
    check_provenance(model, consts, report)
    return report


# --------------------------------------------------------------------------
# Self-test: two synthetic IFCs standing in for the two producers, so the
# checker's verdicts are exercised without the 80 MB model (which is not in
# this repository and never will be).
# --------------------------------------------------------------------------

def _fixture(path: Path, *, aggregate_stairs: bool, door_width, tag: bool,
             provenance: str | None = None, schema: str = "IFC4",
             spiral: bool = False) -> None:
    import uuid

    model = ifcopenshell.file(schema=schema)
    # The attribute that carries a stair's shape was renamed between the two
    # schemas this pipeline accepts. The fixture writes whichever one its
    # schema declares, so the self-test exercises both spellings.
    shape_attr = "ShapeType" if schema == "IFC2X3" else "PredefinedType"

    def guid():
        return ifcopenshell.guid.compress(uuid.uuid4().hex)

    person = model.create_entity("IfcPerson", FamilyName="fixture")
    org = model.create_entity("IfcOrganization", Name="fixture")
    person_org = model.create_entity("IfcPersonAndOrganization", person, org)
    application = model.create_entity(
        "IfcApplication", org, "1", "check_ifc_contract fixture", "fixture")
    history = model.create_entity(
        "IfcOwnerHistory", person_org, application, ChangeAction="NOCHANGE",
        CreationDate=0)

    metre = model.create_entity("IfcSIUnit", UnitType="LENGTHUNIT", Name="METRE")
    units = model.create_entity("IfcUnitAssignment", [metre])
    origin = model.create_entity("IfcCartesianPoint", (0.0, 0.0, 0.0))
    axis = model.create_entity("IfcAxis2Placement3D", origin)
    context = model.create_entity(
        "IfcGeometricRepresentationContext", ContextType="Model",
        CoordinateSpaceDimension=3, Precision=1e-5, WorldCoordinateSystem=axis)
    project = model.create_entity("IfcProject", guid(), history, "fixture",
                                  RepresentationContexts=[context], UnitsInContext=units)
    placement = model.create_entity("IfcLocalPlacement", RelativePlacement=axis)
    site = model.create_entity("IfcSite", guid(), history, "site",
                               ObjectPlacement=placement, CompositionType="ELEMENT")
    building = model.create_entity("IfcBuilding", guid(), history, "building",
                                   ObjectPlacement=placement, CompositionType="ELEMENT")
    storey = model.create_entity("IfcBuildingStorey", guid(), history, "L1",
                                 ObjectPlacement=placement, CompositionType="ELEMENT",
                                 Elevation=0.0)
    model.create_entity("IfcRelAggregates", guid(), history, RelatingObject=project,
                        RelatedObjects=[site])
    model.create_entity("IfcRelAggregates", guid(), history, RelatingObject=site,
                        RelatedObjects=[building])
    model.create_entity("IfcRelAggregates", guid(), history, RelatingObject=building,
                        RelatedObjects=[storey])

    made = []

    def product(entity, name, **kwargs):
        index = len(made) + 1
        item = model.create_entity(
            entity, guid(), history, name, ObjectPlacement=placement,
            Tag=str(1000 + index) if tag else None, **kwargs)
        made.append(item)
        return item

    # A Z-up box roughly the shape of a small building, so the extent sample
    # has something to read. Placements work in both schemas; the point list
    # is IFC4-only (tessellation arrived with IFC4), and it is the carrier a
    # world-coordinate producer such as Reviter actually uses -- so writing
    # both is what makes the extent sample cover both producer styles.
    for corner in ((0.0, 0.0, 0.0), (40.0, 60.0, 8.0)):
        model.create_entity("IfcAxis2Placement3D",
                            model.create_entity("IfcCartesianPoint", corner))
    if schema != "IFC2X3":
        model.create_entity("IfcCartesianPointList3D",
                            [(0.0, 0.0, 0.0), (40.0, 60.0, 8.0)])

    for i in range(4):
        product("IfcWall", f"wall-{i}")
    product("IfcSlab", "floor")
    product("IfcCovering", "ceiling")
    product("IfcRoof", "roof")
    product("IfcColumn", "column")
    product("IfcPlate", "curtain-panel")
    mullion = product("IfcMember", "mullion")

    door = product("IfcDoor", "door", OverallWidth=door_width, OverallHeight=2.1)
    opening = model.create_entity("IfcOpeningElement", guid(), history, "opening",
                                  ObjectPlacement=placement)
    model.create_entity("IfcRelVoidsElement", guid(), history,
                        RelatingBuildingElement=made[0], RelatedOpeningElement=opening)
    model.create_entity("IfcRelFillsElement", guid(), history,
                        RelatingOpeningElement=opening, RelatedBuildingElement=door)

    flight = product("IfcStairFlight", "flight")
    stringer = product("IfcMember", "stringer")
    if aggregate_stairs:
        shape = "SPIRAL_STAIR" if spiral else "STRAIGHT_RUN_STAIR"
        stair = product("IfcStair", "stair", **{shape_attr: shape})
        model.create_entity("IfcRelAggregates", guid(), history, RelatingObject=stair,
                            RelatedObjects=[flight, stringer])

    model.create_entity(
        "IfcRelContainedInSpatialStructure", guid(), history,
        RelatedElements=made, RelatingStructure=storey)

    if provenance:
        # Grade by class: the stair fallback is the one that matters.
        for target, value in ((made[0], "native"), (flight, provenance)):
            prop = model.create_entity(
                "IfcPropertySingleValue", Name="GeometryProvenance",
                NominalValue=model.create_entity("IfcLabel", value))
            pset = model.create_entity("IfcPropertySet", guid(), history,
                                       "Reviter_Recovery", HasProperties=[prop])
            model.create_entity("IfcRelDefinesByProperties", guid(), history,
                                RelatedObjects=[target], RelatingPropertyDefinition=pset)

    # Unused but written so the file is self-consistent for a reader.
    _ = (mullion, storey)
    model.write(str(path))


def self_test() -> int:
    import tempfile

    failures: list[str] = []

    def expect(name: str, got: str, want: str, context: str) -> None:
        if got != want:
            failures.append(f"{context}: {name} was {got}, expected {want}")

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        good = tmp / "producer-a.ifc"
        _fixture(good, aggregate_stairs=True, door_width=0.9, tag=True)
        report = audit(good, pitch=1.0, sample=500)
        by_name = {c["check"]: c["status"] for c in report.checks}
        for check in ("units", "up_axis", "class_coverage", "doors",
                      "stair_aggregation", "floor_plates", "openings", "join_key"):
            expect(check, by_name[check], OK, "producer-a (complete)")
        expect("verdict", report.verdict, OK, "producer-a (complete)")

        thin = tmp / "producer-b.ifc"
        _fixture(thin, aggregate_stairs=False, door_width=None, tag=False,
                 provenance="bounds-fallback")
        report = audit(thin, pitch=1.0, sample=500)
        by_name = {c["check"]: c["status"] for c in report.checks}
        expect("stair_aggregation", by_name["stair_aggregation"], FAIL,
               "producer-b (no aggregation)")
        expect("doors", by_name["doors"], FAIL, "producer-b (no OverallWidth)")
        expect("join_key", by_name["join_key"], WARN, "producer-b (no Tag)")
        expect("provenance", by_name["provenance"], WARN, "producer-b (stair is a box)")
        expect("up_axis", by_name["up_axis"], OK, "producer-b (no Tag)")
        expect("verdict", report.verdict, FAIL, "producer-b (no aggregation)")

        fragile = tmp / "producer-c.ifc"
        _fixture(fragile, aggregate_stairs=True, door_width=1.5, tag=True)
        report = audit(fragile, pitch=1.0, sample=500)
        by_name = {c["check"]: c["status"] for c in report.checks}
        expect("doors", by_name["doors"], WARN, "producer-c (width on a leaf boundary)")

        # A spiral stair must be found in either schema. This is the branch
        # that was dead on IFC4 until `stair_shape` read both spellings.
        for schema in ("IFC2X3", "IFC4"):
            spiral_file = tmp / f"spiral-{schema}.ifc"
            _fixture(spiral_file, aggregate_stairs=True, door_width=0.9, tag=True,
                     schema=schema, spiral=True)
            report = audit(spiral_file, pitch=1.0, sample=500)
            found = next(c for c in report.checks if c["check"] == "stair_aggregation")
            expect("spiral_stairs", str(found["detail"]["spiral_stairs"]), "1",
                   f"{schema} spiral stair")

    # The engine's own reader, tested directly rather than by proxy.
    stair_shape = load_engine_function("stair_shape")

    class _Stub:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    expect("engine stair_shape(IFC2X3)",
           str(stair_shape(_Stub(ShapeType="SPIRAL_STAIR"))), "SPIRAL_STAIR", "engine")
    expect("engine stair_shape(IFC4)",
           str(stair_shape(_Stub(PredefinedType="SPIRAL_STAIR"))), "SPIRAL_STAIR", "engine")
    expect("engine stair_shape(neither)",
           str(stair_shape(_Stub())), "None", "engine")

    if failures:
        print("self-test FAILED")
        for line in failures:
            print(f"  {line}")
        return 1
    print("self-test passed: 5 synthetic producers across both schemas, "
          "plus the engine's stair_shape reader")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ifc", nargs="?", type=Path, help="IFC file to check")
    ap.add_argument("--pitch", type=float, default=1.0,
                    help="Voxel size in metres, matching the intended conversion")
    ap.add_argument("--sample", type=int, default=5000,
                    help="Coordinate carriers to sample when inferring the vertical axis")
    ap.add_argument("--json", type=Path, help="Write the full report as JSON")
    ap.add_argument("--self-test", action="store_true",
                    help="Check this script against synthetic IFCs and exit")
    args = ap.parse_args()

    if args.self_test:
        return self_test()
    if not args.ifc:
        ap.error("give an IFC file, or --self-test")

    report = audit(args.ifc, args.pitch, args.sample)
    print(report.render())
    if args.json:
        args.json.write_text(json.dumps(
            {"file": str(args.ifc), "pitch_m": args.pitch,
             "verdict": report.verdict, "checks": report.checks}, indent=2))
        print(f"wrote {args.json}")
    return 1 if report.verdict == FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
