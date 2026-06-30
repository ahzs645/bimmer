#!/usr/bin/env python3
"""Export a voxel blocks.csv into a BlockCraft building.json.

BlockCraft (the browser voxel game in ./blockcraft) has no concrete/door blocks,
so we remap our Minecraft palette to the closest blocks it ships, and turn
functional doors into walk-through air gaps (BlockCraft has no door block).

Output: blockcraft/server/building.json, consumed by our patched
WorldGeneration.js, which lays a flat world and stamps these blocks in.

Coordinates are pre-offset to BlockCraft world space:
  world_x = csv_x + offset_x   (centred on origin)
  world_y = csv_y + base_y     (building floor sits just above the grass)
  world_z = csv_z + offset_z
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path

# our Minecraft block id  ->  BlockCraft block name (see Server.js blockOrder)
MAP = {
    "minecraft:white_concrete": "hardened_clay_stained_white",
    "minecraft:smooth_stone": "stone",
    "minecraft:stone": "stone",
    "minecraft:gray_concrete": "hardened_clay_stained_gray",
    "minecraft:light_blue_stained_glass": "glass_light_blue",
    "minecraft:iron_bars": "iron_block",                  # railing -> solid metal parapet
    "minecraft:stone_bricks": "stonebrick",
    "minecraft:deepslate_tiles": "stone_andesite",
    "minecraft:light_gray_concrete": "hardened_clay_stained_silver",
    "minecraft:oak_planks": "planks_oak",                 # only if --doors solid was used
}
# blocks dropped entirely
SKIP_PREFIXES = ()


def base_block(block: str) -> str:
    return block.split("[", 1)[0]


def door_block(block: str) -> str:
    """Map a Minecraft door (with facing state) to an axis-specific BlockCraft
    door block so the flat panel is oriented correctly (no neighbour guessing)."""
    facing = "south"
    if "facing=" in block:
        facing = block.split("facing=", 1)[1].split(",", 1)[0].rstrip("]")
    return "door_x" if facing in ("east", "west") else "door_z"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("blocks_csv", type=Path, default=Path("out/unbc_1m/blocks.csv"), nargs="?")
    ap.add_argument("--out", type=Path, default=Path("blockcraft/server/building.json"))
    ap.add_argument("--ground-y", type=int, default=4, help="grass level of the flat world")
    ap.add_argument("--base-y", type=int, default=5, help="world-Y for the building's lowest voxel")
    args = ap.parse_args()

    csv_path = args.blocks_csv.expanduser().resolve()
    rows = []
    maxx = maxz = 0
    with csv_path.open(newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            x, y, z = int(r["x"]), int(r["y"]), int(r["z"])
            rows.append((x, y, z, r.get("block", "minecraft:stone")))  # keep full block (door facing)
            maxx, maxz = max(maxx, x), max(maxz, z)

    offset_x = -(maxx // 2)
    offset_z = -(maxz // 2)

    blocks: dict[str, list] = defaultdict(list)
    skipped = unmapped = 0
    unmapped_names: set[str] = set()
    for x, y, z, block in rows:
        if SKIP_PREFIXES and block.startswith(SKIP_PREFIXES):
            skipped += 1
            continue
        base = base_block(block)
        if base == "minecraft:oak_door":
            name = door_block(block)  # door_x / door_z by facing
        else:
            name = MAP.get(base)
            if name is None:
                unmapped += 1
                unmapped_names.add(base)
                name = "stone"
        blocks[name].append([x + offset_x, y + args.base_y, z + offset_z])

    out = args.out.expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "source": str(csv_path),
        "ground_y": args.ground_y,
        "base_y": args.base_y,
        "offset": [offset_x, args.base_y, offset_z],
        "dims": [maxx + 1, max(y for _, y, _, _ in rows) + 1, maxz + 1],
        "counts": {name: len(v) for name, v in sorted(blocks.items(), key=lambda kv: -len(kv[1]))},
        "doors_as_air": skipped,
        "blocks": blocks,
    }
    out.write_text(json.dumps(payload), encoding="utf-8")
    print(json.dumps({
        "out": str(out),
        "total_blocks": sum(len(v) for v in blocks.values()),
        "doors_as_air": skipped,
        "unmapped_to_stone": unmapped,
        "unmapped_names": sorted(unmapped_names),
        "counts": payload["counts"],
        "world_footprint_x": [offset_x, offset_x + maxx],
        "world_footprint_z": [offset_z, offset_z + maxz],
    }, indent=2))


if __name__ == "__main__":
    main()
