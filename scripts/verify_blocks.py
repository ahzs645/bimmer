#!/usr/bin/env python3
"""QA checks for a generated blocks.csv: door pairing/anchoring, double-door
hinge mirroring, fence connection states, stair shapes.

Usage: python scripts/verify_blocks.py [out/unbc_1m]
"""
import csv
import sys
from collections import defaultdict, Counter

out = sys.argv[1] if len(sys.argv) > 1 else "out/unbc_1m"
cells = {}
with open(f"{out}/blocks.csv") as fh:
    for row in csv.DictReader(fh):
        cells[(int(row["x"]), int(row["y"]), int(row["z"]))] = row["block"]

doors = {p: b for p, b in cells.items() if b.startswith("minecraft:oak_door")}
print("door blocks:", len(doors), "(even)" if len(doors) % 2 == 0 else "(ODD — corrupt!)")

# pair matching within columns
cols = defaultdict(dict)
for (x, y, z), b in doors.items():
    cols[(x, z)][y] = "L" if "half=lower" in b else "U"
orphans = 0
for (x, z), ys in cols.items():
    used = set()
    for y in sorted(ys):
        if y in used: continue
        if ys[y] == "L" and ys.get(y + 1) == "U" and (y + 1) not in used:
            used.add(y); used.add(y + 1)
    leftover = [(y, ys[y]) for y in sorted(ys) if y not in used]
    orphans += len(leftover)
print("orphan door halves (not part of a lower+upper pair):", orphans)

# walkthrough + anchoring test per lower half
lowers = {p: b for p, b in doors.items() if "half=lower" in b}
def passable(p):
    b = cells.get(p)
    return b is None or "oak_door" in b
sunk = floating = blocked = ok = no_thresh = 0
bad_examples = []
for (x, y, z), b in sorted(lowers.items()):
    facing = b.split("facing=")[1].split(",")[0]
    n = [(x - 1, z), (x + 1, z)] if facing in ("east", "west") else [(x, z - 1), (x, z + 1)]
    # threshold below the leaf
    if cells.get((x, y - 1, z)) is None:
        no_thresh += 1
    # each side: find standing surface = highest solid with 2 passable above, scan y+1..y-2
    side_levels = []
    for px, pz in n:
        lvl = None
        for py in range(y + 1, y - 3, -1):
            bb = cells.get((px, py, pz))
            if bb is not None and "oak_door" not in bb:
                if passable((px, py + 1, pz)) and passable((px, py + 2, pz)):
                    lvl = py
                break
        side_levels.append(lvl)
    lv = [l for l in side_levels if l is not None]
    if not lv:
        continue  # glazed both sides etc.
    top = max(lv)
    if top >= y + 1:
        sunk += 1; bad_examples.append(("sunk", (x, y, z), top))
    elif top < y - 1:
        floating += 1; bad_examples.append(("float", (x, y, z), top))
    else:
        # passage: both door cells passable (they're doors) and the cells beside at walking level
        ok += 1
print(f"lower halves: {len(lowers)}  ok-level: {ok}  sunk: {sunk}  floating: {floating}  no-threshold-below: {no_thresh}")
for e in bad_examples[:10]: print("   ", e)

# free-standing doors: a door should have wall material beside it along the
# wall run axis (a lone door on a slab means it was hoisted off its wall or
# its surroundings were carved away)
walled2 = walled1 = free = 0
free_ex = []
for (x, y, z), b in sorted(lowers.items()):
    facing = b.split("facing=")[1].split(",")[0]
    rd = (0, 1) if facing in ("east", "west") else (1, 0)
    sides = 0
    for s in (-1, 1):
        for dy in (0, 1):
            bb = cells.get((x + rd[0] * s, y + dy, z + rd[1] * s))
            if bb is not None and "oak_door" not in bb:
                sides += 1
                break
    if sides == 2: walled2 += 1
    elif sides == 1: walled1 += 1
    else:
        free += 1
        if len(free_ex) < 8: free_ex.append((x, y, z))
print(f"walled both sides: {walled2}  one side: {walled1}  FREE-STANDING: {free}")
for e in free_ex: print("   free:", e)

# double door runs
run_stats = Counter(); unmirrored = 0; stepped = 0
visited = set()
for (x, y, z), b in sorted(lowers.items()):
    if (x, y, z) in visited: continue
    facing = b.split("facing=")[1].split(",")[0]
    d = (0, 1) if facing in ("east", "west") else (1, 0)
    if (x - d[0], y, z - d[1]) in lowers: continue
    run = [(x, y, z)]
    while True:
        nx = (run[-1][0] + d[0], y, run[-1][2] + d[1])
        if nx in lowers and lowers[nx].split("facing=")[1].split(",")[0] == facing:
            run.append(nx); visited.add(nx)
        else:
            break
    run_stats[len(run)] += 1
    if len(run) >= 2:
        hinges = [lowers[p].split("hinge=")[1].split(",")[0] for p in run]
        if len(set(hinges)) == 1:
            unmirrored += 1
print("door runs by width:", dict(sorted(run_stats.items())))
print("multi-leaf runs with un-mirrored hinges:", unmirrored)

# adjacent same-facing lowers at DIFFERENT y (stepped double door leaves)
for (x, y, z), b in lowers.items():
    facing = b.split("facing=")[1].split(",")[0]
    d = (0, 1) if facing in ("east", "west") else (1, 0)
    for dy in (-1, 1):
        q = (x + d[0], y + dy, z + d[1])
        if q in lowers and lowers[q].split("facing=")[1].split(",")[0] == facing:
            stepped += 1
print("adjacent same-facing leaves at offset heights (stepped pairs):", stepped // 1)

# fences
fences = [(p, b) for p, b in cells.items() if "oak_fence" in b]
with_states = sum(1 for _, b in fences if "[" in b)
connected = sum(1 for _, b in fences if "=true" in b)
print(f"\nfences: {len(fences)}  with states: {with_states}  with >=1 connection: {connected}")

# stairs
st = Counter(b for _, b in cells.items() if "stone_brick_stairs" in b)
shapes = Counter(b.split("shape=")[1].rstrip("]") for b in st.elements())
print("\nstair blocks:", sum(st.values()), " shapes:", dict(shapes))
fac = Counter(b.split("facing=")[1].split(",")[0] for b in st.elements())
print("stair facings:", dict(fac))
