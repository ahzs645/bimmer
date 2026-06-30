#!/usr/bin/env python3
"""Voxelize a mesh file and export Minecraft-oriented block coordinates."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Iterable

import numpy as np
import trimesh


def load_as_mesh(path: Path) -> trimesh.Trimesh:
    loaded = trimesh.load(path, force="scene")

    if isinstance(loaded, trimesh.Trimesh):
        return loaded

    if not isinstance(loaded, trimesh.Scene):
        raise TypeError(f"Unsupported geometry loaded from {path}: {type(loaded).__name__}")

    meshes: list[trimesh.Trimesh] = []
    for node_name in loaded.graph.nodes_geometry:
        transform, geometry_name = loaded.graph[node_name]
        geometry = loaded.geometry.get(geometry_name)
        if not isinstance(geometry, trimesh.Trimesh):
            continue

        mesh = geometry.copy()
        mesh.apply_transform(transform)
        if len(mesh.faces) > 0:
            meshes.append(mesh)

    if not meshes:
        raise ValueError(f"No triangle meshes found in {path}")

    return trimesh.util.concatenate(meshes)


def normalize_indices(indices: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    min_index = indices.min(axis=0)
    return indices - min_index, min_index


def to_minecraft_coords(indices: np.ndarray) -> np.ndarray:
    # Common BIM and mesh exports are Z-up. Minecraft is Y-up.
    return indices[:, [0, 2, 1]]


def iter_rows(indices: np.ndarray, block: str) -> Iterable[tuple[int, int, int, str]]:
    for x, y, z in indices:
        yield int(x), int(y), int(z), block


def write_csv(path: Path, indices: np.ndarray, block: str) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["x", "y", "z", "block"])
        writer.writerows(iter_rows(indices, block))


def write_json(path: Path, metadata: dict, indices: np.ndarray, block: str, max_voxels: int) -> None:
    payload = dict(metadata)
    payload["block"] = block
    payload["voxel_count_in_json"] = int(min(len(indices), max_voxels))
    payload["voxels_truncated"] = bool(len(indices) > max_voxels)
    payload["minecraft_blocks"] = [
        {"x": int(x), "y": int(y), "z": int(z), "block": block}
        for x, y, z in indices[:max_voxels]
    ]
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mesh", type=Path, help="Input mesh path, for example .glb, .gltf, .obj, .stl, .dae")
    parser.add_argument("--pitch", type=float, default=1.0, help="Voxel size in model units")
    parser.add_argument("--out-dir", type=Path, default=Path("out/voxels"), help="Output directory")
    parser.add_argument("--block", default="minecraft:stone", help="Minecraft block id for CSV/JSON exports")
    parser.add_argument("--fill", action="store_true", help="Fill enclosed volumes after surface voxelization")
    parser.add_argument(
        "--max-json-voxels",
        type=int,
        default=100_000,
        help="Limit verbose JSON block records; full coordinates are always written to voxels.npz and blocks.csv",
    )
    parser.add_argument(
        "--no-preview",
        action="store_true",
        help="Skip preview_boxes.obj generation, useful for very large models",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.pitch <= 0:
        raise SystemExit("--pitch must be greater than zero")

    mesh_path = args.mesh.expanduser().resolve()
    if not mesh_path.exists():
        raise SystemExit(f"Input mesh does not exist: {mesh_path}")

    out_dir = args.out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    mesh = load_as_mesh(mesh_path)
    mesh.remove_unreferenced_vertices()

    voxel_grid = mesh.voxelized(pitch=args.pitch)
    if args.fill:
        try:
            voxel_grid = voxel_grid.fill()
        except ModuleNotFoundError as exc:
            if exc.name == "scipy":
                raise SystemExit("--fill requires scipy. Install dependencies with: pip install -r requirements-pipeline.txt") from exc
            raise

    raw_indices = np.asarray(voxel_grid.sparse_indices, dtype=np.int64)
    if raw_indices.size == 0:
        raise SystemExit("Voxelization produced no voxels; try a smaller --pitch or inspect the mesh.")

    normalized_indices, source_min_index = normalize_indices(raw_indices)
    minecraft_indices = to_minecraft_coords(normalized_indices)

    metadata = {
        "input_mesh": str(mesh_path),
        "pitch": args.pitch,
        "source_bounds": mesh.bounds.tolist(),
        "source_min_index": source_min_index.tolist(),
        "voxel_count": int(len(raw_indices)),
        "normalized_grid_shape_xyz": (normalized_indices.max(axis=0) + 1).astype(int).tolist(),
        "minecraft_grid_shape_xyz": (minecraft_indices.max(axis=0) + 1).astype(int).tolist(),
        "axis_mapping": "mesh XYZ to Minecraft XZY, treating mesh Z as vertical Minecraft Y",
        "filled": bool(args.fill),
    }

    np.savez_compressed(
        out_dir / "voxels.npz",
        mesh_indices=normalized_indices,
        minecraft_indices=minecraft_indices,
        pitch=np.asarray([args.pitch], dtype=np.float64),
        source_min_index=source_min_index,
    )
    write_csv(out_dir / "blocks.csv", minecraft_indices, args.block)
    write_json(out_dir / "voxels.json", metadata, minecraft_indices, args.block, args.max_json_voxels)

    if not args.no_preview:
        preview = voxel_grid.as_boxes()
        preview.export(out_dir / "preview_boxes.obj")

    print(json.dumps(metadata, indent=2))
    print(f"Wrote outputs to {out_dir}")


if __name__ == "__main__":
    main()
