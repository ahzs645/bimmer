#!/usr/bin/env python3
"""Convert IFC product geometry to a mesh file using IfcOpenShell."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import ifcopenshell
import ifcopenshell.geom
import numpy as np
import trimesh


def convert_ifc_to_mesh(ifc_path: Path, output_path: Path) -> dict:
    model = ifcopenshell.open(str(ifc_path))
    settings = ifcopenshell.geom.settings()
    settings.set(settings.USE_WORLD_COORDS, True)

    meshes: list[trimesh.Trimesh] = []
    converted = 0
    skipped = 0
    errors: list[dict] = []

    for product in model.by_type("IfcProduct"):
        if not getattr(product, "Representation", None):
            skipped += 1
            continue

        try:
            shape = ifcopenshell.geom.create_shape(settings, product)
            geometry = shape.geometry
            vertices = np.asarray(geometry.verts, dtype=np.float64).reshape((-1, 3))
            faces = np.asarray(geometry.faces, dtype=np.int64).reshape((-1, 3))
            if len(vertices) and len(faces):
                meshes.append(trimesh.Trimesh(vertices=vertices, faces=faces, process=False))
                converted += 1
            else:
                skipped += 1
        except Exception as exc:
            if len(errors) < 50:
                errors.append(
                    {
                        "id": product.id(),
                        "type": product.is_a(),
                        "name": getattr(product, "Name", None),
                        "error": str(exc)[:240],
                    }
                )
            skipped += 1

    if not meshes:
        raise ValueError(f"No mesh geometry could be created from {ifc_path}")

    mesh = trimesh.util.concatenate(meshes)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    mesh.export(output_path)

    return {
        "input_ifc": str(ifc_path),
        "output_mesh": str(output_path),
        "products_total": len(model.by_type("IfcProduct")),
        "products_converted": converted,
        "products_skipped_or_failed": skipped,
        "mesh_vertices": int(len(mesh.vertices)),
        "mesh_faces": int(len(mesh.faces)),
        "bounds": mesh.bounds.tolist(),
        "sample_errors": errors,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ifc", type=Path, help="Input IFC file")
    parser.add_argument("output", type=Path, help="Output mesh file, for example .obj, .glb, .stl, .dae")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    ifc_path = args.ifc.expanduser().resolve()
    output_path = args.output.expanduser().resolve()
    if not ifc_path.exists():
        raise SystemExit(f"Input IFC does not exist: {ifc_path}")

    summary = convert_ifc_to_mesh(ifc_path, output_path)
    summary_path = output_path.with_suffix(output_path.suffix + ".summary.json")
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
