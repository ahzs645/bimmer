#!/usr/bin/env python3
"""Export a voxel blocks.csv to a compact binary + meta.json for the web viewer.

Layout (web/data/<name>/):
  voxels.bin  : records grouped by block id, each record = 3x int16 (x,y,z), LE.
  meta.json   : dims, pitch, and per-group {block,name,rgb,offset,count,opacity}.

Grouping by block id means the viewer can build one InstancedMesh per group
(cheap per-group color + visibility toggles) with no per-instance color buffer.
"""

from __future__ import annotations

import argparse
import csv
import json
import struct
from collections import defaultdict
from pathlib import Path

# block id -> (friendly name, [r,g,b], opacity)
BLOCK_STYLE = {
    "minecraft:white_concrete": ("Walls", [207, 213, 214], 1.0),
    "minecraft:smooth_stone": ("Floors / slabs", [159, 159, 159], 1.0),
    "minecraft:stone": ("Columns / structure", [127, 127, 127], 1.0),
    "minecraft:gray_concrete": ("Curtain-wall framing", [70, 74, 80], 1.0),
    "minecraft:light_blue_stained_glass": ("Glazing", [96, 196, 226], 0.45),
    "minecraft:oak_door": ("Doors (functional)", [168, 120, 60], 1.0),
    "minecraft:oak_planks": ("Doors", [168, 134, 80], 1.0),
    "minecraft:oak_fence": ("Railings", [154, 123, 79], 0.85),
    "minecraft:stone_bricks": ("Stairs / ramps", [128, 127, 128], 1.0),
    "minecraft:stone_brick_stairs": ("Stairs (oriented)", [122, 121, 122], 1.0),
    "minecraft:smooth_stone_slab": ("Floors / slabs", [159, 159, 159], 1.0),
    "minecraft:deepslate_tiles": ("Roof", [64, 64, 72], 1.0),
    "minecraft:light_gray_concrete": ("Other", [150, 150, 140], 1.0),
}
DEFAULT_STYLE = ("Other", [190, 120, 120], 1.0)


def base_block(block: str) -> str:
    """Strip block-state suffix: 'minecraft:oak_door[...]' -> 'minecraft:oak_door'."""
    return block.split("[", 1)[0]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("blocks_csv", type=Path)
    ap.add_argument("--name", required=True, help="dataset folder name, e.g. unbc_1m")
    ap.add_argument("--label", default=None, help="human label for the dropdown")
    ap.add_argument("--pitch", type=float, default=1.0)
    ap.add_argument("--web-dir", type=Path, default=Path("web"))
    args = ap.parse_args()

    csv_path = args.blocks_csv.expanduser().resolve()
    groups: dict[str, list[tuple[int, int, int]]] = defaultdict(list)
    maxx = maxy = maxz = 0
    with csv_path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            x, y, z = int(row["x"]), int(row["y"]), int(row["z"])
            groups[base_block(row.get("block", "minecraft:stone"))].append((x, y, z))
            maxx, maxy, maxz = max(maxx, x), max(maxy, y), max(maxz, z)

    out_dir = (args.web_dir / "data" / args.name).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    # Write grouped binary, build meta
    bin_path = out_dir / "voxels.bin"
    meta_groups = []
    offset = 0
    total = 0
    with bin_path.open("wb") as bf:
        # largest groups first so heavy stuff draws first
        for block, pts in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            name, rgb, opacity = BLOCK_STYLE.get(block, DEFAULT_STYLE)
            buf = bytearray()
            for (x, y, z) in pts:
                buf += struct.pack("<hhh", x, y, z)
            bf.write(buf)
            meta_groups.append({
                "block": block, "name": name, "rgb": rgb, "opacity": opacity,
                "offset": offset, "count": len(pts),
            })
            offset += len(pts)
            total += len(pts)

    meta = {
        "name": args.name,
        "label": args.label or args.name,
        "pitch_m": args.pitch,
        "count": total,
        "dims": [maxx + 1, maxy + 1, maxz + 1],
        "record_bytes": 6,
        "groups": meta_groups,
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(json.dumps({"name": args.name, "count": total, "dims": meta["dims"],
                      "bin_bytes": bin_path.stat().st_size,
                      "groups": [(g["name"], g["count"]) for g in meta_groups]}, indent=2))


if __name__ == "__main__":
    main()
