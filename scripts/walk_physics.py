#!/usr/bin/env python3
"""Vanilla-Minecraft movement over a converted world, in one place.

`audit_walkability.py` has always owned these rules, and owning them as
module-level code meant nothing else could use them: a tool that wanted to walk
the world -- to render what a player sees, to trace a route through a rectified
wing -- had to reimplement the movement model, and a reimplementation that
drifts is worse than no check at all. It would agree with the audit right up
until it mattered.

The model (the VASA / Gorte walkability invariant, adapted to Minecraft):
8-direction steps; +1 up is a WALK onto an oriented stair block (2 air above
suffices) but a JUMP onto a full cube (which also needs clearance above the
player's head at the source); drops to -3 are allowed; every stand cell needs
support below it and 2 passable cells above. Doors count as passable, because
they open.

Coordinates are the Minecraft frame written into blocks.csv: (x, y, z) with y
up. See REVITER.md for how a world metre becomes one of these.
"""

from __future__ import annotations

import csv
from collections import deque
from pathlib import Path

STAIR_BASES = ("minecraft:stone_bricks", "minecraft:stone_brick_stairs")
DIRS = [(dx, dz) for dx in (-1, 0, 1) for dz in (-1, 0, 1) if (dx, dz) != (0, 0)]


class World:
    """A blocks.csv, indexed for the movement queries above."""

    def __init__(self, solid: dict[tuple[int, int, int], str]):
        self.solid = solid
        self.stair_cells = {p for p, b in solid.items()
                            if b.split("[")[0] in STAIR_BASES}

    @classmethod
    def load(cls, path: str | Path) -> "World":
        solid: dict[tuple[int, int, int], str] = {}
        with open(path) as handle:
            reader = csv.reader(handle)
            next(reader)
            for x, y, z, block in reader:
                solid[(int(x), int(y), int(z))] = block
        return cls(solid)

    def passable(self, p) -> bool:
        block = self.solid.get(p)
        return block is None or "door" in block

    def standable(self, p) -> bool:
        x, y, z = p
        below = self.solid.get((x, y - 1, z))
        return (below is not None and "door" not in below
                and self.passable(p) and self.passable((x, y + 1, z)))

    def neighbors(self, p):
        x, y, z = p
        for dx, dz in DIRS:
            for dy in (0, 1, -1, -2, -3):
                q = (x + dx, y + dy, z + dz)
                if self.standable(q):
                    if dy == 1:
                        below = self.solid.get((q[0], q[1] - 1, q[2]))
                        walk = below is not None and "stair" in below.split("[")[0]
                        # Stepping onto a full cube is a jump, so the player
                        # needs room above their head where they start.
                        if not walk and not self.passable((x, y + 2, z)):
                            continue
                    yield q
                    break

    def entrances(self, max_y: int = 3) -> list[tuple[int, int, int]]:
        """Stand cells beside a ground-level door -- where a player comes in."""
        seeds = []
        for p, block in self.solid.items():
            if "door" not in block or "half=lower" not in block or p[1] > max_y:
                continue
            x, y, z = p
            for dx, dz in DIRS:
                q = (x + dx, y, z + dz)
                if self.standable(q):
                    seeds.append(q)
        return seeds

    def reachable(self, seeds) -> set:
        seen = set(seeds)
        queue = deque(seeds)
        while queue:
            for q in self.neighbors(queue.popleft()):
                if q not in seen:
                    seen.add(q)
                    queue.append(q)
        return seen

    def route(self, seeds, goal=None):
        """A shortest walk from any seed, to `goal` or to the farthest cell.

        Returned as the list of stand cells a player actually occupies, which
        is what a first-person walk needs -- not a set, and not a distance.
        """
        previous = {p: None for p in seeds}
        queue = deque(seeds)
        last = seeds[0] if seeds else None
        while queue:
            p = queue.popleft()
            last = p
            if goal is not None and p == goal:
                break
            for q in self.neighbors(p):
                if q not in previous:
                    previous[q] = p
                    queue.append(q)
        target = goal if goal is not None and goal in previous else last
        path = []
        while target is not None:
            path.append(target)
            target = previous[target]
        return path[::-1]
