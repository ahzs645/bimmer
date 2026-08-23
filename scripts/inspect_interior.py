#!/usr/bin/env python3
"""Sweep the whole interior: what is cut off, what leaks, whether stairs bend.

`audit_walkability.py` answers "what share is reachable" and `walk_voxels.py`
renders one route. Neither says *where* the unreachable part is, whether a room
is open to the sky through a hole nobody meant to leave, or whether a stairwell
turns the way its geometry turns. Those are the questions you ask after a
conversion, and each of them was previously answered by loading the world in a
client and looking.

    python3 scripts/inspect_interior.py out/fixture/blocks.csv --out out/inspect
    python3 scripts/inspect_interior.py blocks.csv --views 12
    python3 scripts/inspect_interior.py --self-test

Reports, and writes a view from each region so the interior can be seen rather
than scored:

  reachability  per storey, with the largest cut-off pockets located
  envelope      interior cells with open sky above them -- holes in the roof,
                and floor cells that are actually outdoors
  stairs        per well: does it climb, is it the only way up, does it bend
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, deque
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from walk_physics import World  # noqa: E402
from walk_voxels import occupancy, render  # noqa: E402


def storeys(cells) -> list[int]:
    """The y levels that carry a real floor population, densest first."""
    counts = Counter(y for _, y, _ in cells)
    floor = max(3, int(0.02 * len(cells)))
    return sorted(y for y, n in counts.items() if n >= floor)


def pockets(world: World, standable_cells: set, reached: set) -> list[list]:
    """Cut-off regions, largest first, each as its own connected component.

    A percentage says 6% is unreachable. It does not say whether that is one
    sealed wing or two hundred rounded-shut cupboards, and those need opposite
    fixes -- the first is a missing connection, the second is F2 wall rounding
    and expected.
    """
    lost = standable_cells - reached
    seen, groups = set(), []
    for cell in lost:
        if cell in seen:
            continue
        group, queue = [], deque([cell])
        seen.add(cell)
        while queue:
            p = queue.popleft()
            group.append(p)
            for q in world.neighbors(p):
                if q in lost and q not in seen:
                    seen.add(q)
                    queue.append(q)
        groups.append(group)
    return sorted(groups, key=len, reverse=True)


def open_to_sky(world: World, cells) -> list:
    """Stand cells with nothing above them all the way out of the model.

    Two different things look like this and both are worth seeing: a hole in
    the roof, and a cell that is simply outdoors (a terrace, or the apron the
    door-grounding pass laid). The caller decides which by where they are; this
    only reports that the column is open.
    """
    top = max(y for _, y, _ in world.solid) if world.solid else 0
    exposed = []
    for x, y, z in cells:
        if all((x, level, z) not in world.solid for level in range(y + 1, top + 2)):
            exposed.append((x, y, z))
    return exposed


def stair_report(world: World, reached: set) -> list[dict]:
    """Per stairwell: does it climb, is it load-bearing, does it turn."""
    stair_cells = {p: b for p, b in world.solid.items()
                   if "stone_brick" in b.split("[")[0] or "stairs" in b}
    if not stair_cells:
        return []

    unvisited = set(stair_cells)
    wells = []
    while unvisited:
        seed = unvisited.pop()
        component, queue = [seed], deque([seed])
        while queue:
            x, y, z = queue.popleft()
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for dz in (-1, 0, 1):
                        n = (x + dx, y + dy, z + dz)
                        if n in unvisited:
                            unvisited.remove(n)
                            component.append(n)
                            queue.append(n)
        if len(component) >= 3:
            wells.append(component)

    # Without the stairs, what can still be reached? Anything above the lowest
    # entrance that survives is being served by something other than the stair.
    treads = set(stair_cells)

    class NoStairs(World):
        def neighbors(self, p):
            for q in super().neighbors(p):
                if (q[0], q[1] - 1, q[2]) not in treads:
                    yield q

    ground = [s for s in world.entrances() if s[1] <= min(y for _, y, _ in world.solid) + 4]
    without = NoStairs(world.solid).reachable(ground) if ground else set()

    out = []
    for well in sorted(wells, key=len, reverse=True):
        ys = [p[1] for p in well]
        facings = Counter(stair_cells[p].split("facing=")[1].split(",")[0].split("]")[0]
                          for p in well if "facing=" in stair_cells[p])
        above = [p for p in reached if p[1] > min(ys) + 1
                 and abs(p[0] - int(np.mean([q[0] for q in well]))) < 40]
        served = sum(1 for p in above if p not in without)
        out.append({
            "cells": len(well),
            "rise": max(ys) - min(ys),
            "connected": any(p in reached or (p[0], p[1] + 1, p[2]) in reached for p in well),
            "facings": dict(facings),
            "bends": len(facings) > 1,
            "cells_only_it_serves": served,
        })
    return out


def inspect(blocks: Path, out_dir: Path, views: int = 8,
            width: int = 448, height: int = 252) -> dict:
    world = World.load(blocks)
    seeds = world.entrances()
    if not seeds:
        raise SystemExit(f"{blocks}: no ground-level door; nothing to enter by.")
    reached = world.reachable(seeds)

    # Every cell a player could stand on, reachable or not -- the denominator.
    standable_cells = set()
    for (x, y, z) in world.solid:
        above = (x, y + 1, z)
        if world.standable(above):
            standable_cells.add(above)

    # Every standable cell, not only the reachable ones: a hole in the roof of
    # a room you cannot get into is still a hole in the roof, and checking only
    # what the player can reach hides exactly the rooms most likely to be wrong.
    exposed = set(open_to_sky(world, sorted(standable_cells)))

    # Standing on the roof is standable and is not interior. Counting it as
    # interior reports the roof as a storey that is 0% reachable, which is both
    # alarming and correct-by-accident: you are not meant to get up there.
    # A level is outdoors when nearly all of it has open sky above.
    levels = storeys(list(standable_cells))
    outdoor_levels = set()
    for level in levels:
        on_level = {p for p in standable_cells if p[1] == level}
        if on_level and len(on_level & exposed) / len(on_level) > 0.8:
            outdoor_levels.add(level)

    outdoors = {p for p in standable_cells if p[1] in outdoor_levels or p in exposed}
    interior = standable_cells - outdoors
    # An open column over an INTERIOR level is a hole in the envelope, not a
    # roof to stand on -- the two look identical until the levels are split.
    envelope_holes = sorted(p for p in exposed if p[1] not in outdoor_levels)
    # And the reverse question: can the player get out where they should not?
    outside_reachable = sorted(outdoors & reached)

    per_storey = {}
    for level in sorted(set(levels) - outdoor_levels):
        on_level = {p for p in interior if p[1] == level}
        got = on_level & reached
        per_storey[level] = {"standable": len(on_level), "reachable": len(got),
                             "share": len(got) / max(1, len(on_level))}

    cut_off = pockets(world, interior, reached)
    stairs = stair_report(world, reached)

    out_dir.mkdir(parents=True, exist_ok=True)
    # Views spread over the interior rather than along one route, so every
    # region gets looked at instead of whichever happened to be on the path.
    picked = spread(sorted(reached), views)
    grid, colours, lo, glow = occupancy(world)
    for index, cell in enumerate(picked):
        yaw = best_view(world, cell)
        eye = (cell[0] + 0.5, cell[1] + 1.62, cell[2] + 0.5)
        render(grid, colours, glow, lo, eye, yaw, 0.0, width, height).save(
            out_dir / f"view_{index:02d}_{cell[0]}_{cell[1]}_{cell[2]}.png")

    report = {
        "blocks": str(blocks),
        "entrances": len(seeds),
        "reachable_interior": len(interior & reached),
        "interior": len(interior),
        "outdoor_levels": sorted(outdoor_levels),
        "per_storey": per_storey,
        "cut_off_pockets": [{"cells": len(g), "at": list(g[0])} for g in cut_off[:10]],
        "cut_off_total": sum(len(g) for g in cut_off),
        "envelope_holes": len(envelope_holes),
        "envelope_holes_sample": [list(p) for p in envelope_holes[:10]],
        "outside_reachable": len(outside_reachable),
        "outside_reachable_sample": [list(p) for p in outside_reachable[:10]],
        "stairwells": stairs,
        "views": [list(p) for p in picked],
    }
    (out_dir / "interior.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def spread(cells, count):
    """`count` cells spaced out over the interior, not clustered in one room."""
    if len(cells) <= count:
        return list(cells)
    points = np.array(cells, dtype=float)
    chosen = [int(np.argmin(points[:, 0] + points[:, 2]))]
    # Farthest-point sampling: each new view is the cell furthest from every
    # view already taken, which is what stops eight views of one corridor.
    best = np.linalg.norm(points - points[chosen[0]], axis=1)
    for _ in range(count - 1):
        nxt = int(np.argmax(best))
        chosen.append(nxt)
        best = np.minimum(best, np.linalg.norm(points - points[nxt], axis=1))
    return [cells[i] for i in chosen]


def best_view(world: World, cell, samples=16):
    """The heading with the most open space -- looking down the room, not into
    a wall a block away."""
    x, y, z = cell
    best, best_open = 0.0, -1
    for i in range(samples):
        yaw = 2 * np.pi * i / samples
        dx, dz = np.sin(yaw), np.cos(yaw)
        openness = 0
        for step in range(1, 12):
            p = (int(round(x + dx * step)), y, int(round(z + dz * step)))
            if p in world.solid and "door" not in world.solid[p]:
                break
            openness += 1
        if openness > best_open:
            best, best_open = yaw, openness
    return best


def self_test() -> int:
    import csv
    import tempfile

    failures: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        blocks = tmp / "blocks.csv"
        rows = []
        # Two rooms; the second is sealed, and part of the roof is missing.
        for x in range(16):
            for z in range(8):
                rows.append((x, 0, z, "minecraft:smooth_stone"))
                if not (10 <= x <= 13 and 2 <= z <= 5):     # a hole in the roof
                    rows.append((x, 3, z, "minecraft:deepslate_tiles"))
        for x in range(16):
            for z in (0, 7):
                for y in (1, 2):
                    rows.append((x, y, z, "minecraft:white_concrete"))
        for z in range(8):
            for y in (1, 2):
                rows.append((0, y, z, "minecraft:white_concrete"))
                rows.append((15, y, z, "minecraft:white_concrete"))
                rows.append((9, y, z, "minecraft:white_concrete"))   # the seal
        for y, half in ((1, "lower"), (2, "upper")):
            rows.append((0, y, 4, f"minecraft:oak_door[facing=east,half={half}]"))
        with blocks.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["x", "y", "z", "block"])
            writer.writerows(rows)

        report = inspect(blocks, tmp / "out", views=3, width=80, height=45)
        if report["cut_off_total"] < 10:
            failures.append(f"the sealed room should be reported cut off, "
                            f"got {report['cut_off_total']} cells")
        if not report["cut_off_pockets"]:
            failures.append("a cut-off pocket should be located, not just counted")
        if report["envelope_holes"] == 0:
            failures.append("the hole in the roof should be found")
        # The fixture's roof is one level and its rooms are another, so the
        # roof must be classified as outdoors rather than counted as a storey
        # that is 0% reachable.
        if not report["outdoor_levels"]:
            failures.append("the roof level should be recognised as outdoors")
        if report["outside_reachable"] > 4:
            failures.append(f"a sealed box should not let the player outside; "
                            f"{report['outside_reachable']} outdoor cells reachable")
        if len(list((tmp / "out").glob("view_*.png"))) != 3:
            failures.append("a view per region should be written")

    if failures:
        print("self-test FAILED")
        for line in failures:
            print(f"  {line}")
        return 1
    print("self-test passed: a sealed room located and a roof hole found")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("blocks", nargs="?", type=Path)
    ap.add_argument("--out", type=Path, default=Path("out/inspect"))
    ap.add_argument("--views", type=int, default=8)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return self_test()
    if not args.blocks:
        ap.error("give a blocks.csv, or --self-test")

    r = inspect(args.blocks, args.out, args.views)
    print(f"{r['reachable_interior']:,} of {r['interior']:,} INTERIOR cells reachable "
          f"from {r['entrances']} entrance cells")
    if r["outdoor_levels"]:
        print(f"  (levels {r['outdoor_levels']} are open sky -- roof, not interior)")
    for level, s in sorted(r["per_storey"].items()):
        print(f"  y={level:<3} {s['reachable']:>6,}/{s['standable']:<6,} = {s['share']:.0%}")

    print(f"\ncut off: {r['cut_off_total']:,} cells in {len(r['cut_off_pockets'])} "
          "pocket(s) shown")
    for pocket in r["cut_off_pockets"][:5]:
        print(f"  {pocket['cells']:>5} cells, e.g. at {tuple(pocket['at'])}")

    print(f"\nholes in the envelope: {r['envelope_holes']:,} interior cells with open sky above")
    for cell in r["envelope_holes_sample"][:5]:
        print(f"  {tuple(cell)}")

    print(f"\npoking outside: {r['outside_reachable']:,} outdoor cells the player can reach")
    for cell in r["outside_reachable_sample"][:5]:
        print(f"  {tuple(cell)}")

    print(f"\nstairwells: {len(r['stairwells'])}")
    for well in r["stairwells"]:
        turn = "bends " + "/".join(well["facings"]) if well["bends"] else "straight"
        print(f"  {well['cells']:>3} cells, rise {well['rise']}, {turn}, "
              f"{'connected' if well['connected'] else 'ISOLATED'}, "
              f"serves {well['cells_only_it_serves']:,} cells nothing else reaches")

    print(f"\n{len(r['views'])} views -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
