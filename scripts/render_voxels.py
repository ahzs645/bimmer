#!/usr/bin/env python3
"""Render a voxel blocks.csv to preview PNGs (isometric + plan + elevations).

Pure numpy + Pillow, no Minecraft needed. Colors each voxel by its block id so
the semantic classes (glass, concrete walls, stone floors, ...) are visible.
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

import numpy as np
from PIL import Image

# Approximate Minecraft block colors (RGB)
BLOCK_RGB = {
    "minecraft:white_concrete": (207, 213, 214),
    "minecraft:smooth_stone": (159, 159, 159),
    "minecraft:stone": (127, 127, 127),
    "minecraft:gray_concrete": (54, 57, 61),
    "minecraft:light_blue_stained_glass": (74, 180, 214),
    "minecraft:oak_planks": (162, 130, 78),
    "minecraft:oak_door": (168, 120, 60),
    "minecraft:oak_fence": (154, 123, 79),
    "minecraft:stone_bricks": (122, 121, 122),
    "minecraft:stone_brick_stairs": (122, 121, 122),
    "minecraft:smooth_stone_slab": (159, 159, 159),
    "minecraft:deepslate_tiles": (60, 60, 66),
    "minecraft:light_gray_concrete": (125, 125, 115),
}
DEFAULT_RGB = (190, 120, 120)


def load_blocks(path: Path):
    xs, ys, zs, cols = [], [], [], []
    with path.open(newline="", encoding="utf-8") as fh:
        r = csv.DictReader(fh)
        for row in r:
            xs.append(int(row["x"]))
            ys.append(int(row["y"]))
            zs.append(int(row["z"]))
            cols.append(row.get("block", ""))
    pts = np.array([xs, ys, zs], dtype=np.int64).T
    # strip block-state suffix: colors key off the base id
    rgb = np.array([BLOCK_RGB.get(c.split("[", 1)[0], DEFAULT_RGB) for c in cols], dtype=np.uint8)
    return pts, rgb


def render_ortho(pts, rgb, axis_h, axis_v, axis_depth, flip_v, flip_depth, pad, bg):
    """Paint points to an image; nearer depth wins (painter's z-buffer)."""
    h = pts[:, axis_h]
    v = pts[:, axis_v]
    d = pts[:, axis_depth]
    if flip_v:
        v = v.max() - v
    if flip_depth:
        d = d.max() - d
    h = h - h.min()
    v = v - v.min()
    W = int(h.max()) + 1 + 2 * pad
    H = int(v.max()) + 1 + 2 * pad
    img = np.full((H, W, 3), bg, dtype=np.uint8)
    zbuf = np.full((H, W), -1, dtype=np.int64)
    # depth shade: nearer = brighter
    drange = max(1, int(d.max() - d.min()))
    shade = 0.55 + 0.45 * (d - d.min()) / drange
    order = np.argsort(d)  # far first so near overwrites
    for i in order:
        yy = H - 1 - (int(v[i]) + pad)
        xx = int(h[i]) + pad
        if d[i] >= zbuf[yy, xx]:
            zbuf[yy, xx] = d[i]
            img[yy, xx] = (rgb[i].astype(np.float32) * shade[i]).clip(0, 255).astype(np.uint8)
    return Image.fromarray(img)


def render_iso(pts, rgb, scale, pad, bg):
    """Simple axonometric (2:1 iso) projection with painter's algorithm."""
    x = pts[:, 0].astype(np.float64)
    y = pts[:, 1].astype(np.float64)  # up
    z = pts[:, 2].astype(np.float64)
    sx = (x - z)
    sy = (x + z) * 0.5 - y
    sx = (sx - sx.min())
    sy = (sy - sy.min())
    W = int(sx.max()) + 1 + 2 * pad
    H = int(sy.max()) + 1 + 2 * pad
    img = np.full((H, W, 3), bg, dtype=np.uint8)
    depth = (x + z + y)  # larger = nearer to viewer (top-front-right)
    drange = max(1.0, depth.max() - depth.min())
    shade = 0.5 + 0.5 * (depth - depth.min()) / drange
    zbuf = np.full((H, W), -1e18)
    order = np.argsort(depth)
    for i in order:
        yy = H - 1 - (int(round(sy[i])) + pad)
        xx = int(round(sx[i])) + pad
        if depth[i] >= zbuf[yy, xx]:
            zbuf[yy, xx] = depth[i]
            img[yy, xx] = (rgb[i] * shade[i]).clip(0, 255).astype(np.uint8)
    out = Image.fromarray(img)
    if scale != 1:
        out = out.resize((W * scale, H * scale), Image.NEAREST)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("blocks_csv", type=Path)
    ap.add_argument("--out-dir", type=Path, default=None)
    ap.add_argument("--iso-scale", type=int, default=2)
    args = ap.parse_args()

    csv_path = args.blocks_csv.expanduser().resolve()
    out_dir = (args.out_dir or csv_path.parent / "preview").expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    pts, rgb = load_blocks(csv_path)
    print(f"{len(pts)} voxels; bounds {pts.min(axis=0).tolist()}..{pts.max(axis=0).tolist()}")
    bg = (18, 20, 24)

    # axes: 0=x (width), 1=y (up/height), 2=z (length)
    iso = render_iso(pts, rgb, args.iso_scale, pad=4, bg=bg)
    iso.save(out_dir / "iso.png")

    plan = render_ortho(pts, rgb, axis_h=0, axis_v=2, axis_depth=1,
                        flip_v=False, flip_depth=False, pad=2, bg=bg)
    plan.save(out_dir / "plan.png")

    front = render_ortho(pts, rgb, axis_h=0, axis_v=1, axis_depth=2,
                         flip_v=False, flip_depth=True, pad=2, bg=bg)
    front.save(out_dir / "elev_front.png")

    side = render_ortho(pts, rgb, axis_h=2, axis_v=1, axis_depth=0,
                        flip_v=False, flip_depth=True, pad=2, bg=bg)
    side.save(out_dir / "elev_side.png")

    print(f"Wrote: {', '.join(p.name for p in sorted(out_dir.glob('*.png')))} -> {out_dir}")


if __name__ == "__main__":
    main()
