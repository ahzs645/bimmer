#!/usr/bin/env python3
"""Walkability audit for a converted world (blocks.csv).

Answers, with vanilla-Minecraft movement rules, the two questions that
caught every stair/connectivity bug in this project (see LESSONS.md S8-S10):

  1. Per stairwell: does it CLIMB (bottom landing -> top landing) and is it
     CONNECTED to the entrance component at all?
  2. Per storey: what share of interior floor cells is reachable on foot
     from the ground-level entrance doors?

Movement model (the VASA/Gorte walkability invariant, adapted to Minecraft):
8-direction steps; +1 up is a WALK onto an oriented stair block (2-air
suffices) but a JUMP onto a full cube (needs clearance above the head at the
source); drops to -3 are allowed; every stand cell needs support below and
2 passable cells above. Doors count as passable (they open).

Usage:  audit_walkability.py [path/to/blocks.csv]
"""
import csv
import sys
from collections import defaultdict, deque

PATH = sys.argv[1] if len(sys.argv) > 1 else "out/unbc_1m/blocks.csv"
STAIR_BASES = ("minecraft:stone_bricks", "minecraft:stone_brick_stairs")

solid = {}
stair_cells = set()
with open(PATH) as f:
    r = csv.reader(f)
    next(r)
    for x, y, z, b in r:
        p = (int(x), int(y), int(z))
        solid[p] = b
        if b.split("[")[0] in STAIR_BASES:
            stair_cells.add(p)


def passable(p):
    b = solid.get(p)
    return b is None or "door" in b


def standable(p):
    x, y, z = p
    below = solid.get((x, y - 1, z))
    return (below is not None and "door" not in below
            and passable(p) and passable((x, y + 1, z)))


DIRS = [(dx, dz) for dx in (-1, 0, 1) for dz in (-1, 0, 1) if (dx, dz) != (0, 0)]


def neighbors(p):
    x, y, z = p
    for dx, dz in DIRS:
        for dy in (0, 1, -1, -2, -3):
            q = (x + dx, y + dy, z + dz)
            if standable(q):
                if dy == 1:
                    below = solid.get((q[0], q[1] - 1, q[2]))
                    walk = below is not None and "stair" in below.split("[")[0]
                    if not walk and not passable((x, y + 2, z)):
                        continue
                yield q
                break


seeds = []
for p, b in solid.items():
    if "oak_door" in b and "half=lower" in b and p[1] <= 3:
        x, y, z = p
        for dx, dz in DIRS:
            q = (x + dx, y, z + dz)
            if standable(q):
                seeds.append(q)
seen = set(seeds)
dq = deque(seeds)
while dq:
    p = dq.popleft()
    for q in neighbors(p):
        if q not in seen:
            seen.add(q)
            dq.append(q)

# ---- stairwell clusters ----
unvisited = set(stair_cells)
wells = []
while unvisited:
    s = unvisited.pop()
    comp = [s]
    q = deque([s])
    while q:
        x, y, z = q.popleft()
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    n = (x + dx, y + dy, z + dz)
                    if n in unvisited:
                        unvisited.remove(n)
                        comp.append(n)
                        q.append(n)
    if len(comp) >= 6:
        wells.append(comp)

bad = 0
total = 0
for w in sorted(wells, key=len, reverse=True):
    xs = [p[0] for p in w]
    ys = [p[1] for p in w]
    zs = [p[2] for p in w]
    y0, y1 = min(ys), max(ys)
    if y1 - y0 < 2:
        continue
    total += 1
    stand = {(x, y + 1, z) for (x, y, z) in w if standable((x, y + 1, z))}
    reach = sum(1 for c in stand if c in seen)
    lo = [c for c in stand if c[1] <= y0 + 2]
    hi = [c for c in stand if c[1] >= y1]
    climb = False
    if lo and hi:
        s2 = set(lo)
        q = deque(lo)
        while q:
            p = q.popleft()
            for n in neighbors(p):
                if not (min(xs) - 2 <= n[0] <= max(xs) + 2
                        and min(zs) - 2 <= n[2] <= max(zs) + 2):
                    continue
                if n not in s2:
                    s2.add(n)
                    q.append(n)
        climb = any(c in s2 for c in hi)
    tags = []
    if not climb:
        tags.append("NOT-CLIMBABLE")
    if stand and reach == 0:
        tags.append("ISOLATED")
    if tags:
        bad += 1
        print(f"y{y0}-{y1} x({min(xs)},{max(xs)}) z({min(zs)},{max(zs)}) {tags}")

# ---- per-storey interior reachability ----
tot = defaultdict(int)
rch = defaultdict(int)
for p in list(solid.keys()):
    x, y, z = p
    s = (x, y + 1, z)
    if not standable(s):
        continue
    if not any((x, y + 1 + j, z) in solid for j in range(1, 5)):
        continue  # open-air deck, not interior
    tot[s[1]] += 1
    if s in seen:
        rch[s[1]] += 1
T = sum(tot.values())
R = sum(rch.values())
print(f"bad wells: {bad}/{total}   interior reachable: {R}/{T} = {R / T:.0%}")
for y in sorted(tot):
    if tot[y] > 50:
        print(f"  y={y:2d}: {rch[y] / tot[y]:>4.0%} ({rch[y]}/{tot[y]})")
