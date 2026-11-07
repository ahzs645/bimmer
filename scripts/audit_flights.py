#!/usr/bin/env python3
"""Two walk checks the stairwell audit does not make.

audit_walkability.py asks whether each stairwell climbs. These ask the
questions that walking the world first-person kept answering instead:

  flights  every connected run of tread stand cells that climbs >= 2 must
           touch walkable floor at its bottom AND its top. A flight that
           meets nothing at the top is a dead end a step too high (or a wall
           too far) to leave -- the most visible stair defect in game.
  drops    an indoor floor edge (a ceiling overhead) should not drop the
           player straight onto bare world ground: that is a floor plate that
           stops short of its facade, with nothing under the building.

Same movement model as everything else (walk_physics). Prints counts and
where each dead-ended flight is, largest first.

    python3 scripts/audit_flights.py out/unbc_1m/blocks.csv
"""
from __future__ import annotations

import sys
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from walk_physics import World  # noqa: E402


def flights(world: World):
    on_treads = {(x, y + 1, z) for (x, y, z) in world.stair_cells
                 if world.standable((x, y + 1, z))}
    seen, good, bad = set(), 0, []
    for s in on_treads:
        if s in seen:
            continue
        comp, exits, queue = [s], set(), deque([s])
        seen.add(s)
        while queue:
            p = queue.popleft()
            for n in world.neighbors(p):
                if n in on_treads:
                    if n not in seen:
                        seen.add(n)
                        comp.append(n)
                        queue.append(n)
                else:
                    exits.add(n)
        ys = [c[1] for c in comp]
        lo, hi = min(ys), max(ys)
        if hi - lo < 2:
            continue
        bottom = any(e[1] <= lo + 1 for e in exits)
        top = any(e[1] >= hi - 1 for e in exits)
        if bottom and top:
            good += 1
            continue
        xs = [c[0] for c in comp]
        zs = [c[2] for c in comp]
        bad.append((hi - lo, (min(xs), max(xs)), (lo, hi), (min(zs), max(zs)),
                    "no bottom" if not bottom else "no top"))
    return good, sorted(bad, reverse=True)


def drops(world: World):
    """Indoor drop cells: empty cells you can walk into off a floor (two air
    above, air below), with a ceiling 2-6 above, beside a stand cell at the
    same height. Returns (cells, of which fall >= 3, of which have nothing at
    all under them -- bare world ground)."""
    solid = world.solid
    low = {}
    for (x, y, z) in solid:
        if y < low.get((x, z), 1 << 30):
            low[(x, z)] = y
    cells = set()
    for (x, y, z) in list(solid):
        s = (x, y + 1, z)
        if not world.standable(s):
            continue
        for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            c = (x + dx, y + 1, z + dz)
            if c in cells:
                continue
            if any((c[0], c[1] + j, c[2]) in solid for j in (-1, 0, 1)):
                continue
            if not any((c[0], c[1] + k, c[2]) in solid for k in range(2, 7)):
                continue                       # open sky: a terrace edge, not indoors
            cells.add(c)
    far = bare = 0
    for (x, y, z) in cells:
        if low.get((x, z), 1 << 30) >= y:
            bare += 1
            far += 1
            continue
        d = 1
        while (x, y - 1 - d, z) not in solid:
            d += 1
        far += d >= 3
    return len(cells), far, bare


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else "out/unbc_1m/blocks.csv"
    world = World.load(path)
    good, bad = flights(world)
    print(f"flights climbing >= 2: {good + len(bad)}   dead-ended: {len(bad)}")
    for rise, xr, yr, zr, why in bad:
        print(f"  rise {rise:2d}  x{xr} y{yr} z{zr}  {why}")
    cells, far, bare = drops(world)
    print(f"indoor drop cells: {cells}   falling >= 3: {far}   onto bare world ground: {bare}")


if __name__ == "__main__":
    main()
