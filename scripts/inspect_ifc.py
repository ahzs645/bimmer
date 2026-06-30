#!/usr/bin/env python3
"""Fast structural probe of an IFC file: schema, units, storeys, element types,
and an empirical check of the geometry scale IfcOpenShell returns.

This is intentionally cheap: it does NOT convert all geometry. It creates shapes
for only a small sample of products to measure the coordinate scale so we can
choose a correct voxel pitch before the expensive full conversion.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

import ifcopenshell
import ifcopenshell.geom
import numpy as np


def project_length_unit(model) -> dict:
    info = {"raw": None, "to_metre_factor": None}
    for ua in model.by_type("IfcUnitAssignment"):
        for u in ua.Units:
            if u.is_a("IfcSIUnit") and u.UnitType == "LENGTHUNIT":
                prefix = u.Prefix
                name = u.Name
                factor = 1.0
                prefixes = {
                    "MILLI": 1e-3, "CENTI": 1e-2, "DECI": 1e-1,
                    "KILO": 1e3, "MICRO": 1e-6,
                }
                if prefix in prefixes:
                    factor = prefixes[prefix]
                info["raw"] = f"{prefix or ''}{name}"
                info["to_metre_factor"] = factor
    return info


def sample_geometry_scale(model, settings, max_samples: int = 40) -> dict:
    """Create shapes for a handful of products and report coordinate magnitudes.

    If IfcOpenShell returns metres, a building spans tens of units.
    If it returns the file's millimetres, it spans tens of thousands.
    """
    mags = []
    sampled = 0
    products = model.by_type("IfcProduct")
    # Spread the sample across the file rather than the first N (often setup geometry)
    step = max(1, len(products) // (max_samples * 4))
    for product in products[::step]:
        if sampled >= max_samples:
            break
        if not getattr(product, "Representation", None):
            continue
        try:
            shape = ifcopenshell.geom.create_shape(settings, product)
            verts = np.asarray(shape.geometry.verts, dtype=np.float64).reshape((-1, 3))
            if len(verts):
                mags.append(np.abs(verts).max())
                sampled += 1
        except Exception:
            continue
    if not mags:
        return {"sampled": 0}
    return {
        "sampled": sampled,
        "max_abs_coord_seen": float(np.max(mags)),
        "median_abs_coord_seen": float(np.median(mags)),
        "interpretation": (
            "looks like METRES" if np.max(mags) < 5000 else "looks like MILLIMETRES"
        ),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("ifc", type=Path)
    ap.add_argument("--out", type=Path, default=Path("out/analysis/ifc_inspect.json"))
    args = ap.parse_args()

    model = ifcopenshell.open(str(args.ifc.expanduser().resolve()))

    # Storeys with elevation
    storeys = []
    for s in model.by_type("IfcBuildingStorey"):
        storeys.append({"name": s.Name, "elevation_file_units": s.Elevation})
    storeys.sort(key=lambda d: (d["elevation_file_units"] is None, d["elevation_file_units"]))

    # Element type counts among physical products
    type_counts = Counter(p.is_a() for p in model.by_type("IfcProduct"))
    with_geom = sum(1 for p in model.by_type("IfcProduct") if getattr(p, "Representation", None))

    settings = ifcopenshell.geom.settings()
    settings.set("use-world-coords", True)
    scale = sample_geometry_scale(model, settings)

    summary = {
        "file": str(args.ifc),
        "schema": model.schema,
        "length_unit": project_length_unit(model),
        "counts": {
            "total_products": len(model.by_type("IfcProduct")),
            "products_with_representation": with_geom,
            "storeys": len(storeys),
        },
        "geometry_scale_probe": scale,
        "storeys": storeys,
        "top_product_types": type_counts.most_common(40),
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
