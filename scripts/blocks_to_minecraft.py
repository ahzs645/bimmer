#!/usr/bin/env python3
"""Convert voxel block CSV files to Minecraft schematic formats."""

from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class BlockRow:
    x: int
    y: int
    z: int
    block: str


def read_blocks(path: Path, default_block: str) -> list[BlockRow]:
    rows: list[BlockRow] = []
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        required = {"x", "y", "z"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError(f"{path} must contain CSV columns: x,y,z")
        for row in reader:
            rows.append(
                BlockRow(
                    x=int(row["x"]),
                    y=int(row["y"]),
                    z=int(row["z"]),
                    block=(row.get("block") or default_block).strip() or default_block,
                )
            )
    if not rows:
        raise ValueError(f"No block rows found in {path}")
    return rows


def normalize_blocks(rows: list[BlockRow]) -> tuple[list[BlockRow], tuple[int, int, int]]:
    min_x = min(row.x for row in rows)
    min_y = min(row.y for row in rows)
    min_z = min(row.z for row in rows)
    offset = (min_x, min_y, min_z)
    normalized = [
        BlockRow(row.x - min_x, row.y - min_y, row.z - min_z, row.block)
        for row in rows
    ]
    return normalized, offset


def bounds(rows: list[BlockRow]) -> dict[str, list[int]]:
    return {
        "min_xyz": [
            min(row.x for row in rows),
            min(row.y for row in rows),
            min(row.z for row in rows),
        ],
        "max_xyz": [
            max(row.x for row in rows),
            max(row.y for row in rows),
            max(row.z for row in rows),
        ],
    }


def write_schem(rows: list[BlockRow], output: Path, version_name: str) -> Path:
    import mcschematic

    version = getattr(mcschematic.Version, version_name, None)
    if version is None:
        raise ValueError(f"Unknown mcschematic version: {version_name}")

    output = output.with_suffix(".schem")
    output.parent.mkdir(parents=True, exist_ok=True)
    schematic = mcschematic.MCSchematic()
    for row in rows:
        schematic.setBlock((row.x, row.y, row.z), row.block)

    schematic.save(str(output.parent), output.stem, version)
    return output


def parse_block_state(block: str) -> tuple[str, dict[str, str]]:
    """Split 'minecraft:oak_door[facing=east,half=lower]' -> (id, {props})."""
    if "[" not in block:
        return block, {}
    base, rest = block.split("[", 1)
    rest = rest.rstrip("]")
    props: dict[str, str] = {}
    for pair in rest.split(","):
        if "=" in pair:
            k, v = pair.split("=", 1)
            props[k.strip()] = v.strip()
    return base.strip(), props


def write_litematic(rows: list[BlockRow], output: Path, name: str) -> Path:
    from litemapy import BlockState, Region

    output = output.with_suffix(".litematic")
    output.parent.mkdir(parents=True, exist_ok=True)
    max_x = max(row.x for row in rows)
    max_y = max(row.y for row in rows)
    max_z = max(row.z for row in rows)
    region = Region(0, 0, 0, max_x + 1, max_y + 1, max_z + 1)
    for row in rows:
        base, props = parse_block_state(row.block)
        region[row.x, row.y, row.z] = BlockState(base, **props) if props else BlockState(base)

    schematic = region.as_schematic(name=name)
    schematic.save(str(output))
    return output


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("blocks_csv", type=Path, help="CSV with x,y,z,block columns")
    parser.add_argument("output", type=Path, help="Output path; suffix is normalized to .schem or .litematic")
    parser.add_argument("--format", choices=["schem", "litematic"], default="schem")
    parser.add_argument("--default-block", default="minecraft:stone")
    parser.add_argument("--minecraft-version", default="JE_1_20_4", help="mcschematic Version enum name")
    parser.add_argument("--preserve-origin", action="store_true", help="Do not shift the minimum coordinate to 0,0,0")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    input_path = args.blocks_csv.expanduser().resolve()
    output_path = args.output.expanduser().resolve()
    rows = read_blocks(input_path, args.default_block)
    original_bounds = bounds(rows)

    offset = (0, 0, 0)
    if not args.preserve_origin:
        rows, offset = normalize_blocks(rows)

    if args.format == "schem":
        written = write_schem(rows, output_path, args.minecraft_version)
    else:
        written = write_litematic(rows, output_path, output_path.stem)

    summary = {
        "input_csv": str(input_path),
        "output": str(written),
        "format": args.format,
        "block_count": len(rows),
        "original_bounds": original_bounds,
        "normalization_offset_xyz": list(offset),
        "output_bounds": bounds(rows),
        "output_size_bytes": written.stat().st_size,
    }
    summary_path = written.with_suffix(written.suffix + ".summary.json")
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
