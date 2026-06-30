#!/usr/bin/env python3
"""Probe whether the IFC carries usable per-face material colors via IfcOpenShell.

If shape.geometry.materials / material_ids give real RGB per element, we can do
true color->Minecraft-block matching instead of only semantic-class defaults.
"""
from __future__ import annotations

import sys
from collections import Counter

import ifcopenshell
import ifcopenshell.geom
import numpy as np

ifc = sys.argv[1]
model = ifcopenshell.open(ifc)
settings = ifcopenshell.geom.settings()
settings.set("use-world-coords", True)

it = ifcopenshell.geom.iterator(settings, model, 4)
assert it.initialize()

color_by_type: dict[str, Counter] = {}
n_with_mat = 0
n_total = 0
sample_named = Counter()
checked = 0
while True:
    shape = it.get()
    g = shape.geometry
    ifc_type = model.by_id(shape.id).is_a()
    mats = list(getattr(g, "materials", []) or [])
    mids = np.asarray(getattr(g, "material_ids", []) or [], dtype=np.int64)
    n_total += 1
    if mats:
        n_with_mat += 1
        # representative color = material covering the most faces
        if len(mids):
            counts = Counter(mids[mids >= 0].tolist())
            if counts:
                top = counts.most_common(1)[0][0]
                m = mats[top]
                col = None
                if getattr(m, "has_diffuse", True):
                    d = m.diffuse
                    def comp(attr, idx):
                        val = getattr(d, attr, None)
                        if callable(val):
                            val = val()
                        if val is None:
                            val = d[idx]
                        return round(float(val), 2)
                    col = (comp("r", 0), comp("g", 1), comp("b", 2))
                name = getattr(m, "name", "")
                color_by_type.setdefault(ifc_type, Counter())[(name, col)] += 1
                sample_named[name] += 1
    checked += 1
    if checked >= 4000 or not it.next():
        break

print(f"sampled {n_total} elements; {n_with_mat} had materials")
print("\nTop material names overall:")
for name, c in sample_named.most_common(25):
    print(f"  {c:5d}  {name!r}")
print("\nDominant (name,color) per element type:")
for t, c in sorted(color_by_type.items()):
    top = c.most_common(3)
    print(f"  {t}:")
    for (name, col), n in top:
        print(f"      x{n:<5d} name={name!r} rgb={col}")
