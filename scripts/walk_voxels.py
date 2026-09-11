#!/usr/bin/env python3
"""Walk the converted world and render what a player sees.

Every check in this repository so far reports a *number* about the world: cells
reachable, wells that climb, walls that clash. HANDOFF is blunt about why that
is not enough — several bugs passed the metric that shared their own wrong
assumption and only fell to walking the world in a client. But walking it in a
client needs a client, a world export, and a network the sandbox does not have,
so in practice nobody walks anything.

This walks it here. It takes the route the walkability audit would take —
literally the same movement model, from `walk_physics` — and renders the
player's view along it with a voxel raycaster. If the rectified wing is a
corridor you can get down, that is visible; if it is a jagged pinch or a wall
in your face, that is visible too, and neither shows up in a percentage.

    python3 scripts/walk_voxels.py out/unbc_1m/blocks.csv --out out/walk
    python3 scripts/walk_voxels.py blocks.csv --out walk --to 40,1,12
    python3 scripts/walk_voxels.py --self-test

Writes numbered PNG frames, an animated GIF, and a plan showing the route.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mc_palette import EMISSIVE, rgb_for_block  # noqa: E402
from walk_physics import World  # noqa: E402

EYE_HEIGHT = 1.62          # Minecraft's player eye height, in blocks
FOV_DEGREES = 70.0
MAX_STEPS = 96             # how far a ray marches before it is called sky
# Distance shading, not atmospheric fog. Minecraft's interiors get darker with
# distance because they get darker with light level; blending a far corridor
# toward SKY made every interior look like it opened onto the outdoors.
FOG_START, FOG_END = 22.0, 70.0
FOG_COLOUR = np.array([46, 48, 54], dtype=float)
SKY_TOP = np.array([120, 167, 255], dtype=float)
SKY_HORIZON = np.array([196, 218, 255], dtype=float)
# Minecraft shades a block's faces by orientation rather than by any light
# source, which is most of why its worlds read as solid at all.
FACE_SHADE = {0: 0.80, 1: 1.00, 2: 0.62}


def occupancy(world: World):
    """A dense index grid plus the colour table it indexes into."""
    cells = np.array(list(world.solid.keys()), dtype=np.int64)
    lo = cells.min(axis=0) - 1
    hi = cells.max(axis=0) + 2
    shape = tuple(hi - lo)

    names, index_of = [], {}
    grid = np.zeros(shape, dtype=np.uint16)
    for (x, y, z), block in world.solid.items():
        base = block.split("[")[0]
        # A door is passable; drawing it solid would put a wall across every
        # doorway the player is about to walk through.
        if "door" in base:
            continue
        slot = index_of.get(base)
        if slot is None:
            slot = len(names) + 1
            index_of[base] = slot
            names.append(base)
        grid[x - lo[0], y - lo[1], z - lo[2]] = slot

    colours = np.zeros((len(names) + 1, 3), dtype=float)
    glow = np.zeros(len(names) + 1, dtype=bool)
    for slot, base in enumerate(names, start=1):
        colours[slot] = rgb_for_block(base)
        glow[slot] = base in EMISSIVE
    return grid, colours, lo, glow


def render(grid, colours, glow, lo, eye, yaw, pitch, width=384, height=216,
           diagnose=False):
    """One first-person frame, by marching every pixel's ray through the grid.

    A per-pixel DDA in pure Python would be minutes a frame; every ray is
    stepped together as numpy arrays instead, so a frame is one loop of
    MAX_STEPS vector operations rather than width*height of them.
    """
    aspect = width / height
    half = math.tan(math.radians(FOV_DEGREES) / 2)
    px = (np.arange(width) + 0.5) / width * 2 - 1
    py = 1 - (np.arange(height) + 0.5) / height * 2
    gx, gy = np.meshgrid(px * half * aspect, py * half)

    # Camera space (looking down +z), then yaw about the world's vertical.
    dirs = np.stack([gx.ravel(), gy.ravel(), np.ones(gx.size)], axis=1)
    cp, sp = math.cos(pitch), math.sin(pitch)
    dirs = np.stack([dirs[:, 0],
                     dirs[:, 1] * cp - dirs[:, 2] * sp,
                     dirs[:, 1] * sp + dirs[:, 2] * cp], axis=1)
    cy, sy = math.cos(yaw), math.sin(yaw)
    dirs = np.stack([dirs[:, 0] * cy + dirs[:, 2] * sy,
                     dirs[:, 1],
                     -dirs[:, 0] * sy + dirs[:, 2] * cy], axis=1)
    dirs /= np.linalg.norm(dirs, axis=1, keepdims=True)

    origin = np.asarray(eye, dtype=float) - lo
    safe = np.where(np.abs(dirs) < 1e-9, 1e-9, dirs)
    step = np.sign(safe).astype(np.int64)
    delta = np.abs(1.0 / safe)
    voxel = np.floor(origin).astype(np.int64)[None, :].repeat(len(dirs), axis=0)
    side = (np.where(step > 0, voxel + 1 - origin, origin - voxel)) * delta

    left_grid = np.zeros(len(dirs), dtype=bool)
    hit_slot = np.zeros(len(dirs), dtype=np.uint16)
    hit_axis = np.zeros(len(dirs), dtype=np.int64)
    hit_dist = np.full(len(dirs), np.inf)
    live = np.ones(len(dirs), dtype=bool)

    for _ in range(MAX_STEPS):
        if not live.any():
            break
        axis = np.argmin(np.where(live[:, None], side, np.inf), axis=1)
        rows = np.arange(len(dirs))
        travelled = side[rows, axis]
        side[rows, axis] += delta[rows, axis]
        voxel[rows, axis] += step[rows, axis]

        inside = live & np.all((voxel >= 0) & (voxel < grid.shape), axis=1)
        # A ray that leaves the grid is sky and stops; one that stays looks up
        # what it entered.
        left_now = live & ~inside
        left_grid |= left_now
        live &= inside | ~live
        live[~inside] = False
        if inside.any():
            idx = voxel[inside]
            slots = grid[idx[:, 0], idx[:, 1], idx[:, 2]]
            struck = slots > 0
            if struck.any():
                where = np.flatnonzero(inside)[struck]
                hit_slot[where] = slots[struck]
                hit_axis[where] = axis[where]
                hit_dist[where] = travelled[where]
                live[where] = False

    image = np.empty((len(dirs), 3), dtype=float)
    up = np.clip(dirs[:, 1] * 0.5 + 0.5, 0, 1)[:, None]
    image[:] = SKY_HORIZON * (1 - up) + SKY_TOP * up

    struck = hit_slot > 0
    if struck.any():
        shade = np.vectorize(FACE_SHADE.get)(hit_axis[struck])[:, None]
        # A light source is not lit by the room; it lights the room.
        shade = np.where(glow[hit_slot[struck]][:, None], 1.0, shade)
        colour = colours[hit_slot[struck]] * shade
        fog = np.clip((hit_dist[struck] - FOG_START) / (FOG_END - FOG_START), 0, 1)[:, None]
        image[struck] = colour * (1 - fog) + FOG_COLOUR * fog

    frame = Image.fromarray(
        np.clip(image.reshape(height, width, 3), 0, 255).astype(np.uint8), "RGB")
    if not diagnose:
        return frame
    # Why is a pixel not a block? Three different answers, and only one of them
    # is "there is nothing there". A ray that leaves the grid saw out of the
    # model; a ray still alive after MAX_STEPS ran out of march and was painted
    # sky anyway, which puts a false hole at the end of any long sightline.
    return frame, {
        "hit": struck.reshape(height, width),
        "left_grid": (left_grid & ~struck).reshape(height, width),
        "out_of_steps": (live & ~struck).reshape(height, width),
        "distance": np.where(struck, hit_dist, np.nan).reshape(height, width),
    }


def world_to_block(summary: dict, point) -> tuple[int, int, int]:
    """A point in IFC world metres -> the cell it lands in, in this build.

    This is the chain `summary.json` writes down, run forwards. It exists so
    that two builds of one building can be compared at the SAME PLACE: their
    block coordinates differ (the extents differ, so the origin shift differs)
    and under `--rectify` the wing has moved as well, so "cell (34, 2, 1)" is
    not the same room in both. A world metre is.
    """
    x, y, z = (float(v) for v in point)
    # Rectification first: the engine applies the wing motion to the geometry
    # BEFORE voxelizing, so a point inside a wing has to make the same move.
    for wing in summary.get("wings", []) or []:
        margin = wing.get("hull_margin_m", 2.5)
        if all(e[0] * x + e[1] * y + e[2] <= margin for e in wing["hull_half_planes"]):
            px, py = wing["pivot_xy_m"]
            radians = math.radians(wing["rotation_deg"])
            c, sn = math.cos(radians), math.sin(radians)
            dx, dy = x - px, y - py
            tx, ty = wing["shift_xy_m"]
            x, y = px + c * dx - sn * dy + tx, py + sn * dx + c * dy + ty
            break

    transform = summary["voxel_transform"]
    lo = transform["world_bounds_min_m"]
    pitch = transform["pitch_m"]
    shift = summary["origin_shift_xyz"]
    g = [round((v - l) / pitch) for v, l in zip((x, y, z), lo)]
    return (g[0] - shift[0], g[2] - shift[1], -g[1] - shift[2])


def nearest_standable(world: World, cell, radius: int = 6):
    """The closest cell a player can actually stand in. A world point names a
    place, not a foothold -- it may land inside a wall or in the air."""
    x, y, z = cell
    best, best_d = None, None
    for dx in range(-radius, radius + 1):
        for dy in range(-2, 4):
            for dz in range(-radius, radius + 1):
                q = (x + dx, y + dy, z + dz)
                if not world.standable(q):
                    continue
                d = dx * dx + 4 * dy * dy + dz * dz
                if best_d is None or d < best_d:
                    best, best_d = q, d
    return best


def headings(path, ahead=5, smooth=5):
    """A yaw per path cell, smoothed the way a walking player's head moves.

    Two failures this replaces, both of which put the camera into a wall:
    facing the very next cell snaps 45 degrees on every diagonal step, and
    facing a fixed distance ahead swings hard at a corner because the target is
    already round it. Looking ahead and then smoothing the resulting angles
    turns the corner over several steps instead of during one.

    Angles are unwrapped before smoothing: averaging 179 degrees with -179
    gives 0 and points the camera backwards.
    """
    cells = np.array(path, dtype=float)
    targets = cells[np.minimum(np.arange(len(cells)) + ahead, len(cells) - 1)]
    delta = targets - cells
    raw = np.arctan2(delta[:, 0], delta[:, 2])
    # Where the player is at the target already, keep the previous heading
    # rather than snapping to zero.
    still = (np.abs(delta[:, 0]) < 1e-6) & (np.abs(delta[:, 2]) < 1e-6)
    for i in np.flatnonzero(still):
        raw[i] = raw[i - 1] if i else 0.0

    unwrapped = np.unwrap(raw)
    if smooth > 1 and len(unwrapped) > smooth:
        kernel = np.ones(smooth) / smooth
        padded = np.pad(unwrapped, (smooth // 2, smooth // 2), mode="edge")
        unwrapped = np.convolve(padded, kernel, mode="valid")[:len(raw)]
    return unwrapped


def plan_png(world: World, path, out: Path, scale: int = 6) -> None:
    """Where the route went, over the world's floor plan at the walk's level."""
    level = path[0][1]
    cells = [(x, z) for (x, y, z) in world.solid if y in (level - 1, level)]
    if not cells:
        return
    xs, zs = zip(*cells)
    lo_x, lo_z, hi_x, hi_z = min(xs), min(zs), max(xs), max(zs)
    w, h = (hi_x - lo_x + 1) * scale, (hi_z - lo_z + 1) * scale
    canvas = np.full((h, w, 3), 250, dtype=np.uint8)
    for x, z in cells:
        canvas[(z - lo_z) * scale:(z - lo_z + 1) * scale,
               (x - lo_x) * scale:(x - lo_x + 1) * scale] = (185, 185, 190)
    for i, (x, y, z) in enumerate(path):
        tone = (int(230 - 180 * i / max(1, len(path) - 1)), 40, 40)
        canvas[(z - lo_z) * scale:(z - lo_z + 1) * scale,
               (x - lo_x) * scale:(x - lo_x + 1) * scale] = tone
    Image.fromarray(canvas, "RGB").save(out)


def walk(blocks: Path, out_dir: Path, goal=None, stride: int = 2,
         width: int = 384, height: int = 216, max_frames: int = 120,
         start=None) -> dict:
    world = World.load(blocks)
    seeds = world.entrances()
    if not seeds:
        raise SystemExit(f"{blocks}: no ground-level door to start from.")

    # Start from ONE door, in the BIGGEST connected piece of the building.
    #
    # Seeding from every door at once makes "the farthest reachable cell" a few
    # steps away, because a building with a door in every partition has nowhere
    # far from some door. But picking the most peripheral door instead picks an
    # outlier, and on a real campus with 2,133 entrance cells the outlier was a
    # door opening into a sealed two-cell pocket -- a two-frame walk of a
    # 70,000-cell world.
    #
    # So: label the components the doors open into, keep the largest, and take
    # the most peripheral door of THAT. Each cell is visited once across all
    # the labelling passes.
    if start is None:
        component_of, sizes = {}, {}
        for seed in seeds:
            if seed in component_of:
                continue
            piece = world.reachable([seed])
            for cell in piece:
                component_of.setdefault(cell, seed)
            sizes[seed] = len(piece)
        biggest = max(sizes, key=lambda k: sizes[k])
        in_biggest = [s for s in seeds if component_of.get(s) == component_of.get(biggest)]
        centre = np.mean(np.array(in_biggest, dtype=float), axis=0)
        start = max(in_biggest,
                    key=lambda p: (p[0] - centre[0]) ** 2 + (p[2] - centre[2]) ** 2)
    path = world.route([tuple(start)], goal)
    if len(path) < 2:
        raise SystemExit(f"{blocks}: the entrance leads nowhere walkable.")

    grid, colours, lo, glow = occupancy(world)
    yaws = headings(path)
    out_dir.mkdir(parents=True, exist_ok=True)
    frames, indices = [], list(range(0, len(path), stride))[:max_frames]
    for frame_number, index in enumerate(indices):
        x, y, z = path[index]
        eye = (x + 0.5, y + EYE_HEIGHT, z + 0.5)
        image = render(grid, colours, glow, lo, eye, float(yaws[index]),
                       0.0, width, height)
        image.save(out_dir / f"frame_{frame_number:03d}.png")
        frames.append(image)

    if frames:
        frames[0].save(out_dir / "walk.gif", save_all=True, append_images=frames[1:],
                       duration=180, loop=0)
    plan_png(world, path, out_dir / "route.png")
    return {"path_cells": len(path), "frames": len(frames),
            "from": path[0], "to": path[-1],
            "reachable": len(world.reachable(seeds))}


def self_test() -> int:
    """A room with a door and a wall, walked, without needing a conversion."""
    import tempfile

    failures: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        blocks = tmp / "blocks.csv"
        rows = []
        for x in range(12):
            for z in range(8):
                rows.append((x, 0, z, "minecraft:smooth_stone"))
        for x in range(12):
            for z in (0, 7):
                for y in (1, 2):
                    rows.append((x, y, z, "minecraft:white_concrete"))
        for z in range(8):
            for y in (1, 2):
                rows.append((11, y, z, "minecraft:white_concrete"))
        for y, half in ((1, "lower"), (2, "upper")):
            rows.append((0, y, 4, f"minecraft:oak_door[facing=east,half={half}]"))
        # csv.writer, not f-strings: a block state contains commas, and the
        # engine's own writer quotes them. A fixture that does not is testing a
        # file format nothing produces.
        with blocks.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["x", "y", "z", "block"])
            writer.writerows(rows)

        stats = walk(blocks, tmp / "out", stride=1, width=96, height=54)
        if stats["frames"] < 3:
            failures.append(f"expected a multi-frame walk, got {stats['frames']}")
        if not (tmp / "out" / "walk.gif").exists():
            failures.append("no gif written")

        first = np.asarray(Image.open(tmp / "out" / "frame_000.png"), dtype=int)
        if first.std() < 5:
            failures.append("the first frame is a flat field -- nothing was drawn")
        # Standing inside a room, most of the view must be wall and floor
        # rather than sky: a raycaster that misses every block renders a
        # perfectly pleasant blue picture of nothing.
        sky = np.all(np.abs(first - SKY_HORIZON) < 40, axis=2).mean()
        if sky > 0.6:
            failures.append(f"{sky:.0%} of the view is sky from inside a closed room")

    if failures:
        print("self-test FAILED")
        for line in failures:
            print(f"  {line}")
        return 1
    print("self-test passed: a closed room walked, frames drawn, gif written")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("blocks", nargs="?", type=Path, help="a world's blocks.csv")
    ap.add_argument("--out", type=Path, default=Path("out/walk"))
    ap.add_argument("--to", default=None, help="goal cell as x,y,z (default: farthest reachable)")
    ap.add_argument("--from", dest="start", default=None,
                    help="start cell as x,y,z (default: the outermost entrance)")
    ap.add_argument("--from-world", default=None,
                    help="start at IFC world metres x,y,z (mapped via summary.json)")
    ap.add_argument("--to-world", default=None,
                    help="walk to IFC world metres x,y,z (mapped via summary.json)")
    ap.add_argument("--stride", type=int, default=2, help="cells per frame")
    ap.add_argument("--width", type=int, default=384)
    ap.add_argument("--height", type=int, default=216)
    ap.add_argument("--max-frames", type=int, default=120)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return self_test()
    if not args.blocks:
        ap.error("give a blocks.csv, or --self-test")

    goal = tuple(int(v) for v in args.to.split(",")) if args.to else None
    start = tuple(int(v) for v in args.start.split(",")) if args.start else None

    if args.from_world or args.to_world:
        summary_path = args.blocks.with_name("summary.json")
        if not summary_path.exists():
            raise SystemExit(f"{summary_path} is needed to map world metres into this build.")
        summary = json.loads(summary_path.read_text())
        probe = World.load(args.blocks)
        if args.from_world:
            cell = world_to_block(summary, args.from_world.split(","))
            start = nearest_standable(probe, cell)
            if start is None:
                raise SystemExit(f"nothing standable near {cell} (from {args.from_world})")
            print(f"start: world {args.from_world} m -> cell {cell} -> standing at {start}")
        if args.to_world:
            cell = world_to_block(summary, args.to_world.split(","))
            goal = nearest_standable(probe, cell)
            if goal is None:
                raise SystemExit(f"nothing standable near {cell} (from {args.to_world})")
            print(f"goal:  world {args.to_world} m -> cell {cell} -> standing at {goal}")
    stats = walk(args.blocks, args.out, goal, args.stride,
                 args.width, args.height, args.max_frames, start)
    print(f"walked {stats['path_cells']} cells from {stats['from']} to {stats['to']}")
    print(f"  {stats['reachable']:,} cells reachable from the entrances")
    print(f"  {stats['frames']} frames -> {args.out}/walk.gif, route -> {args.out}/route.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
