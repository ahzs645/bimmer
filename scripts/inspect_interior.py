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
from scipy import ndimage

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


def outdoors_by_escape(world: World, exposed) -> set:
    """Of the cells with open sky above them, the ones that are simply OUTSIDE.

    A cell is outdoors when you could leave the model from it without ever
    passing under a roof: flood horizontally at that level through open,
    unroofed columns and see whether the flood reaches the edge of the world.
    What does not escape is enclosed by taller building on every side -- a
    light well, a courtyard that closed up, a missing patch of roof.

    This replaces counting how many neighbouring columns are taller, which
    cannot tell a light well from the foot of a tall wall: stand on a low roof
    one block from a tower and three of your eight neighbours are the tower, so
    the rule called it a hole. On the real building that mislabelled the strip
    along the base of every taller mass, and the first-person views of those
    "holes" showed open sky and the campus in the distance -- which is what
    outdoors looks like.
    """
    if not world.solid:
        return set()

    cells = np.array(list(world.solid.keys()), dtype=np.int64)
    lo = cells.min(axis=0) - 1
    # Two clear layers above the topmost block: the highest stand cell sits one
    # above it, and the headroom test reads one above that.
    hi = cells.max(axis=0) + np.array([2, 4, 2])
    shape = tuple(hi - lo)
    dense = np.zeros(shape, dtype=bool)
    dense[cells[:, 0] - lo[0], cells[:, 1] - lo[1], cells[:, 2] - lo[2]] = True

    # The highest solid block in each column, as -1 where the column is empty.
    any_y = dense.any(axis=1)
    top = np.where(any_y, shape[1] - 1 - np.argmax(dense[:, ::-1, :], axis=1), -(10 ** 9))

    by_level = {}
    for cell in exposed:
        by_level.setdefault(cell[1], []).append(cell)

    outside = set()
    for level, here in by_level.items():
        y = level - lo[1]
        if not 0 <= y < shape[1] - 1:
            continue
        # open: a player-height gap here; roofed: something at least two blocks
        # up. The flood may cross the first and never the second.
        free = (top < level + 2) & ~dense[:, y, :] & ~dense[:, y + 1, :]
        labels, _ = ndimage.label(free)
        border = set(labels[0, :]) | set(labels[-1, :]) | set(labels[:, 0]) | set(labels[:, -1])
        border.discard(0)
        for cell in here:
            if labels[cell[0] - lo[0], cell[2] - lo[2]] in border:
                outside.add(cell)
    return outside


def see_through(world: World, interior) -> list[dict]:
    """Interior stand cells with a clear horizontal line out of the model.

    Standing indoors and seeing the sky sideways is the one defect a
    reachability percentage can never report: the cell is reachable, it is
    under a roof, it counts as interior, and the wall in front of it is simply
    not there. It is also the first thing a person notices, which is how this
    check came to exist -- the frames looked wrong before any number did.

    Axis rays rather than a full sweep: a voxel wall is axis-aligned, so a run
    that clears one of the four cardinal directions is a hole in a wall, and
    the whole level is answered by four cumulative sums instead of a ray per
    cell. Returns clusters, largest first, so the answer is a place to stand.
    """
    interior = list(interior)
    if not interior or not world.solid:
        return []
    cells = np.array(list(world.solid.keys()), dtype=np.int64)
    lo = cells.min(axis=0)
    hi = cells.max(axis=0)
    dense = np.zeros(tuple(hi - lo + 1), dtype=bool)
    dense[cells[:, 0] - lo[0], cells[:, 1] - lo[1], cells[:, 2] - lo[2]] = True

    by_level = {}
    for cell in interior:
        by_level.setdefault(cell[1], []).append(cell)

    open_cells = []
    for level, here in by_level.items():
        y = level - lo[1]
        if not 0 <= y < dense.shape[1]:
            continue
        plane = dense[:, y, :]
        clear = ((np.cumsum(plane[::-1, :], axis=0)[::-1, :] == 0)
                 | (np.cumsum(plane, axis=0) == 0)
                 | (np.cumsum(plane[:, ::-1], axis=1)[:, ::-1] == 0)
                 | (np.cumsum(plane, axis=1) == 0))
        open_cells += [c for c in here if clear[c[0] - lo[0], c[2] - lo[2]]]

    seen, groups = set(), []
    pool = set(open_cells)
    for cell in sorted(pool):
        if cell in seen:
            continue
        group, queue = [], deque([cell])
        seen.add(cell)
        while queue:
            x, y, z = queue.popleft()
            group.append((x, y, z))
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for dz in (-1, 0, 1):
                        n = (x + dx, y + dy, z + dz)
                        if n in pool and n not in seen:
                            seen.add(n)
                            queue.append(n)
        groups.append({"cells": len(group), "at": list(sorted(group)[len(group) // 2])})
    return sorted(groups, key=lambda g: g["cells"], reverse=True)


def tread_facings(world: World) -> dict:
    """Every shaped stair block, mapped to the direction it climbs."""
    out = {}
    for p, block in world.solid.items():
        if "stairs" in block and "facing=" in block:
            out[p] = block.split("facing=")[1].split(",")[0].split("]")[0]
    return out


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

    def facing(p):
        block = stair_cells[p]
        return block.split("facing=")[1].split(",")[0].split("]")[0] if "facing=" in block else None

    out = []
    for well in sorted(wells, key=len, reverse=True):
        ys = [p[1] for p in well]
        facings = Counter(f for p in well if (f := facing(p)))
        above = [p for p in reached if p[1] > min(ys) + 1
                 and abs(p[0] - int(np.mean([q[0] for q in well]))) < 40]
        served = sum(1 for p in above if p not in without)

        # WHERE the turn happens, not just that the well contains two facings.
        # A well can hold "north" and "east" because two unrelated flights got
        # clustered together; a well that faces north up to y=8 and east above
        # it is one flight that turns, and only the second is a bend you could
        # walk. The per-level sequence separates them, and it is also what a
        # reader needs to go and look at the thing.
        by_level = {}
        for level in sorted(set(ys)):
            here = Counter(f for p in well if p[1] == level and (f := facing(p)))
            if here:
                by_level[level] = here.most_common(1)[0][0]
        turns = sum(1 for a, b in zip(list(by_level.values()), list(by_level.values())[1:]) if a != b)

        xs = [p[0] for p in well]
        zs = [p[2] for p in well]
        out.append({
            "cells": len(well),
            "rise": max(ys) - min(ys),
            "connected": any(p in reached or (p[0], p[1] + 1, p[2]) in reached for p in well),
            "facings": dict(facings),
            "bends": len(facings) > 1,
            "facing_by_level": by_level,
            "turns": turns,
            "at": [int(np.median(xs)), min(ys), int(np.median(zs))],
            "bbox": [[min(xs), min(ys), min(zs)], [max(xs), max(ys), max(zs)]],
            "cells_only_it_serves": served,
        })
    return out


def leaks(world: World, inside: set, outside: set) -> list[dict]:
    """Where the player crosses from indoors to outdoors, clustered.

    "28,426 outdoor cells are reachable" is a symptom, not a finding: the fix
    depends entirely on WHERE the boundary is crossed. One stair landing that
    opens onto a roof is one missing door; two hundred scattered crossings are
    a wall one block too short everywhere. This returns the crossings, so the
    difference is visible.
    """
    out_edges = {}
    for cell in inside:
        for q in world.neighbors(cell):
            if q in outside:
                out_edges.setdefault(cell, []).append(q)
    if not out_edges:
        return []

    # Cluster the indoor side: crossings within a couple of blocks of each
    # other are one opening seen from several stand cells, not several openings.
    seen, groups = set(), []
    for cell in sorted(out_edges):
        if cell in seen:
            continue
        group, queue = [], deque([cell])
        seen.add(cell)
        while queue:
            x, y, z = queue.popleft()
            group.append((x, y, z))
            for dx in (-2, -1, 0, 1, 2):
                for dy in (-1, 0, 1):
                    for dz in (-2, -1, 0, 1, 2):
                        n = (x + dx, y + dy, z + dz)
                        if n in out_edges and n not in seen:
                            seen.add(n)
                            queue.append(n)
        # A representative EDGE, not a representative cell from each side: the
        # first version paired the group's first indoor cell with the first
        # outdoor cell of any edge in it, and printed crossings 90 blocks long.
        here = min(group)
        groups.append({"cells": len(group),
                       "crossings": sum(len(out_edges[c]) for c in group),
                       "inside": list(here), "outside": list(out_edges[here][0])})
    return sorted(groups, key=lambda g: g["crossings"], reverse=True)


def climb(world: World, well: dict, treads: dict, back: int = 4) -> list[tuple]:
    """Camera stations up one stairwell: one per flight, looking the way it
    climbs.

    "Does it bend" is a question about what the player sees while climbing, so
    the camera stands where the player would and faces where they would face.
    Two details make the difference between a frame that shows a staircase and
    a frame that shows a grey wall:

    * the eye goes on the cell ABOVE the tread (feet on the step), not in it;
    * it backs off a few cells downhill first. Voxelized stairs rise a full
      block per step, so from the step itself the next tread is a chest-high
      slab filling the frame -- true, and useless. From a few steps back the
      flight reads as a flight.
    """
    # `facing` is written by the voxelizer as the ASCENT direction, and the
    # renderer measures yaw from +z toward +x (see `best_view`), so the camera
    # looks the way the flight goes up.
    YAW = {"south": 0.0, "east": np.pi / 2, "north": np.pi, "west": -np.pi / 2}
    STEP = {"south": (0, 1), "east": (1, 0), "north": (0, -1), "west": (-1, 0)}

    # One station per FLIGHT, not per level: a flight is a run of consecutive
    # levels sharing a facing, and three frames of the same flight from three
    # steps apart are three pictures of one thing. The turn is between flights.
    flights = []
    for level, face in sorted(well["facing_by_level"].items()):
        if flights and flights[-1][0] == face and level == flights[-1][2] + 1:
            flights[-1][2] = level
        else:
            flights.append([face, level, level])

    (x0, y0, z0), (x1, y1, z1) = well["bbox"]
    stations = []
    for face, low, high in flights:
        here = [p for p in treads
                if low <= p[1] <= high and x0 <= p[0] <= x1 and z0 <= p[2] <= z1
                and treads[p] == face]
        if not here:
            continue
        level = low
        dx, dz = STEP[face]
        foot = min(here, key=lambda p: (p[1], p[0] * dx + p[2] * dz))   # bottom of the flight

        # Back off DOWN THE SLOPE, one block down per block back: that is the
        # line a player descending the flight actually occupies, and it clears
        # the treads instead of burrowing into them. Settle onto whatever is
        # underfoot at each stop -- a landing, the floor below, the ramp
        # itself -- so the camera ends up where a player stands, not floating.
        eye_cell = (foot[0], foot[1] + 1, foot[2])
        for n in range(1, back + 1):
            raw = (foot[0] - dx * n, foot[1] + 1 - n, foot[2] - dz * n)
            if not (world.passable(raw) and world.passable((raw[0], raw[1] + 1, raw[2]))):
                break                       # backed into a wall; stop here
            settled = next(((raw[0], raw[1] + dy, raw[2])
                            for dy in (0, 1, 2, -1, -2, -3, -4, 3)
                            if world.standable((raw[0], raw[1] + dy, raw[2]))), None)
            # Keep walking back through open air, but only ever STAND the camera
            # somewhere a player could stand. Without this the slope runs out
            # past the end of the building and the frame is shot from inside
            # the ground: the two worst frames of the first real run were at
            # y=-1 and y=-3.
            if settled is None:
                continue
            # A doorway is standable and reads as a doorway, not as the bottom
            # of a staircase; stop one short of one.
            if any("door" in (world.solid.get((settled[0], settled[1] + h, settled[2])) or "")
                   for h in (0, 1)):
                break
            eye_cell = settled
        stations.append((eye_cell, YAW[face], level, face))
    return stations


def inspect(blocks: Path, out_dir: Path, views: int = 8,
            width: int = 448, height: int = 252, stair_views: int = 0,
            outside_views: int = 0) -> dict:
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

    # Roof or hole? Both are "standable with open sky above".
    #
    # Decided twice before and wrong both times. By LEVEL (a level is outdoors
    # when most of it is exposed) broke on stepped roofs and called 53% of the
    # interior holes. By NEIGHBOUR COUNT (exposed, but three neighbouring
    # columns are taller) broke along the foot of every tall mass, and the
    # first-person views of those "holes" showed the campus and the horizon.
    #
    # Decided by ESCAPE now: outdoors means you could leave without going under
    # a roof. See `outdoors_by_escape`.
    roof_cells = outdoors_by_escape(world, sorted(exposed))
    envelope_holes = sorted(exposed - roof_cells)
    outdoor_levels = sorted({p[1] for p in roof_cells})

    outdoors = roof_cells
    interior = standable_cells - outdoors
    # And the reverse question: can the player get out where they should not?
    outside_reachable = sorted(outdoors & reached)

    per_storey = {}
    levels = storeys(list(interior))
    for level in levels:
        on_level = {p for p in interior if p[1] == level}
        got = on_level & reached
        per_storey[level] = {"standable": len(on_level), "reachable": len(got),
                             "share": len(got) / max(1, len(on_level))}

    cut_off = pockets(world, interior, reached)
    stairs = stair_report(world, reached)
    crossings = leaks(world, interior & reached, outdoors & reached)
    through = see_through(world, sorted(interior & reached))
    through_cells = sum(g["cells"] for g in through)

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

    # A stairwell that "bends" and a player who "gets onto the roof" are both
    # claims about what you would see standing there, and both were wrong once
    # in ways the numbers could not show: a well whose two facings came from two
    # unrelated flights reads as a bend, and a roof cell one block outside a
    # parapet reads as the player being outdoors. Render them.
    climbed = []
    if stair_views:
        treads = tread_facings(world)
        turning = [w for w in stairs if w["turns"] >= 1 and w["connected"]]
        for index, well in enumerate(turning[:stair_views]):
            stations = climb(world, well, treads)
            for step, (cell, yaw, level, face) in enumerate(stations):
                eye = (cell[0] + 0.5, cell[1] + 1.62, cell[2] + 0.5)
                render(grid, colours, glow, lo, eye, yaw, -0.3, width, height).save(
                    out_dir / f"stair_{index:02d}_{step:02d}_y{level}_{face}.png")
            if stations:
                climbed.append({"at": well["at"], "rise": well["rise"],
                                "turns": well["turns"],
                                "stations": [{"cell": list(c), "level": lv, "faces": f}
                                             for c, _, lv, f in stations]})

    looked_out = []
    if outside_views and (outside_reachable or envelope_holes):
        for index, cell in enumerate(spread(outside_reachable, outside_views)):
            eye = (cell[0] + 0.5, cell[1] + 1.62, cell[2] + 0.5)
            # Down the slope of the roof rather than at the horizon: what makes
            # a roof-poke visible is the building falling away below the feet.
            for tag, pitch in (("out", 0.0), ("down", 0.55)):
                render(grid, colours, glow, lo, eye, best_view(world, cell), pitch,
                       width, height).save(
                    out_dir / f"outside_{index:02d}_{tag}_{cell[0]}_{cell[1]}_{cell[2]}.png")
            looked_out.append(list(cell))

        # Looking UP from inside a hole: sky overhead with the building's own
        # walls around it. This is the frame that separates a hole from a spot
        # that is simply outdoors -- both see sky, only one is boxed in.
        for index, cell in enumerate(spread(envelope_holes, outside_views)):
            eye = (cell[0] + 0.5, cell[1] + 1.62, cell[2] + 0.5)
            render(grid, colours, glow, lo, eye, best_view(world, cell), -1.2,
                   width, height).save(
                out_dir / f"hole_{index:02d}_{cell[0]}_{cell[1]}_{cell[2]}.png")

        # And the wall that is not there: standing on an interior cell that has
        # a clear line out, looking along it. This is the one a person spots
        # before any metric does.
        for index, group in enumerate(through[:outside_views]):
            cell = tuple(group["at"])
            eye = (cell[0] + 0.5, cell[1] + 1.62, cell[2] + 0.5)
            render(grid, colours, glow, lo, eye, best_view(world, cell), 0.0,
                   width, height).save(
                out_dir / f"seethrough_{index:02d}_{cell[0]}_{cell[1]}_{cell[2]}.png")

        # And the doorway itself: standing indoors, looking at the gap. A view
        # from the roof shows that the player got out; only this shows how.
        for index, leak in enumerate(crossings[:outside_views]):
            a, b = leak["inside"], leak["outside"]
            yaw = float(np.arctan2(b[0] - a[0], b[2] - a[2]))
            eye = (a[0] + 0.5, a[1] + 1.62, a[2] + 0.5)
            render(grid, colours, glow, lo, eye, yaw, 0.0, width, height).save(
                out_dir / f"leak_{index:02d}_{a[0]}_{a[1]}_{a[2]}.png")

    report = {
        "blocks": str(blocks),
        "entrances": len(seeds),
        "reachable_interior": len(interior & reached),
        "interior": len(interior),
        "roof_cells": len(roof_cells),
        "per_storey": per_storey,
        "cut_off_pockets": [{"cells": len(g), "at": list(g[0])} for g in cut_off[:10]],
        "cut_off_total": sum(len(g) for g in cut_off),
        "envelope_holes": len(envelope_holes),
        "envelope_holes_sample": [list(p) for p in envelope_holes[:10]],
        "outside_reachable": len(outside_reachable),
        "outside_reachable_sample": [list(p) for p in outside_reachable[:10]],
        "crossings": len(crossings),
        "crossings_top": crossings[:10],
        "see_through": through_cells,
        "see_through_share": through_cells / max(1, len(interior & reached)),
        "see_through_top": through[:10],
        "stairwells": stairs,
        "climbed": climbed,
        "looked_out": looked_out,
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
        if not report["roof_cells"]:
            failures.append("the roof should be recognised as roof, not as holes")
        if report["outside_reachable"] > 4:
            failures.append(f"a sealed box should not let the player outside; "
                            f"{report['outside_reachable']} outdoor cells reachable")
        if len(list((tmp / "out").glob("view_*.png"))) != 3:
            failures.append("a view per region should be written")
        # The fixture's walls are complete, so nothing indoors may see out
        # sideways. This is the baseline the next fixture breaks on purpose.
        if report["see_through"]:
            failures.append(f"a walled fixture should see out of nothing, "
                            f"got {report['see_through']} cells: {report['see_through_top'][:2]}")

        # The same box with one wall block knocked out at eye level: exactly
        # the cells whose row or column runs through the gap should see out.
        holed = tmp / "holed.csv"
        with holed.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["x", "y", "z", "block"])
            writer.writerows(r for r in rows if (r[0], r[1], r[2]) != (5, 1, 0))
        report = inspect(holed, tmp / "holed", views=1, width=64, height=36,
                         outside_views=1)
        if not report["see_through"]:
            failures.append("a wall with a block missing should be seen through")
        elif report["see_through_top"][0]["at"][0] != 5:
            failures.append(f"the see-through should be the column through the gap, "
                            f"got {report['see_through_top'][0]}")
        if not list((tmp / "holed").glob("seethrough_*.png")):
            failures.append("the missing wall should be shown from indoors")

        # A dog-leg: three treads east, then three north. Two facings in one
        # well is not enough to call that a bend -- the earlier version said
        # "bends" for any well holding two facings, including two separate
        # straight flights that happened to touch. What makes this one a bend
        # is that the facing CHANGES AS YOU RISE, and that is what is asserted.
        blocks = tmp / "stairs.csv"
        rows = []
        for x in range(16):
            for z in range(16):
                rows.append((x, 0, z, "minecraft:smooth_stone"))
                rows.append((x, 9, z, "minecraft:deepslate_tiles"))
        for y in range(1, 9):
            for i in range(16):
                for p2 in ((i, y, 0), (i, y, 15), (0, y, i), (15, y, i)):
                    rows.append((*p2, "minecraft:white_concrete"))
        for y, half in ((1, "lower"), (2, "upper")):
            rows.append((0, y, 8, f"minecraft:oak_door[facing=east,half={half}]"))
        for cell, face in (((3, 1, 8), "east"), ((4, 2, 8), "east"), ((5, 3, 8), "east"),
                           ((6, 4, 8), "north"), ((6, 5, 7), "north"), ((6, 6, 6), "north"),
                           ((6, 7, 5), "north"), ((6, 8, 4), "north")):
            rows.append((*cell, f"minecraft:stone_brick_stairs[facing={face}]"))
        # ... and out through an open hatch onto the roof, which is the whole
        # "poking outside" failure in miniature: nothing is broken, the stair
        # simply arrives above the roof line and the player keeps walking.
        rows = [r for r in rows if (r[0], r[1], r[2]) not in {(6, 9, 4), (6, 9, 5)}]
        with blocks.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["x", "y", "z", "block"])
            writer.writerows(rows)

        report = inspect(blocks, tmp / "stairs", views=1, width=64, height=36,
                         stair_views=1, outside_views=1)
        wells = report["stairwells"]
        if not wells:
            failures.append("the dog-leg stair should be found as a well")
        else:
            well = wells[0]
            if well["turns"] != 1:
                failures.append(f"the dog-leg should turn exactly once, got {well['turns']} "
                                f"from {well['facing_by_level']}")
            if well["rise"] != 7:
                failures.append(f"the flight rises 7, got {well['rise']}")
            if not well["connected"]:
                failures.append("a stair reachable from the door should not read ISOLATED")
        if not report["climbed"]:
            failures.append("a turning stairwell should be climbed in first person")
        else:
            faces = [st["faces"] for st in report["climbed"][0]["stations"]]
            if faces != ["east", "north"]:
                failures.append(f"one station per flight, following the turn; got {faces}")
            floor = min(y for _, y, _ in World.load(blocks).solid)
            for st in report["climbed"][0]["stations"]:
                if st["cell"][1] <= floor:
                    failures.append(f"a camera station fell through the floor: {st['cell']}")
        if len(list((tmp / "stairs").glob("stair_*.png"))) != 2:
            failures.append("a frame per flight should be written")
        if not report["outside_reachable"]:
            failures.append("the stair through the roof hatch should reach outdoors")
        if report["crossings"] != 1:
            failures.append(f"the one hatch should be ONE crossing, "
                            f"got {report['crossings']}: {report['crossings_top'][:3]}")
        if not list((tmp / "stairs").glob("leak_*.png")):
            failures.append("the crossing should be shown from indoors")
        # A crossing is ONE STEP: the pair has to be an edge of the walk graph,
        # not one cell from each side of the cluster. The first version paired
        # the group's first indoor cell with the first outdoor cell of any edge
        # in it and reported crossings 90 blocks long.
        for leak in report["crossings_top"]:
            (ax, ay, az), (bx, by, bz) = leak["inside"], leak["outside"]
            if abs(ax - bx) > 1 or abs(az - bz) > 1 or not -3 <= by - ay <= 1:
                failures.append(f"a crossing should be one player step, got "
                                f"{leak['inside']} -> {leak['outside']}")

    if failures:
        print("self-test FAILED")
        for line in failures:
            print(f"  {line}")
        return 1
    print("self-test passed: sealed room located, roof hole found, dog-leg climbed")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("blocks", nargs="?", type=Path)
    ap.add_argument("--out", type=Path, default=Path("out/inspect"))
    ap.add_argument("--views", type=int, default=8)
    ap.add_argument("--stair-views", type=int, default=0,
                    help="climb this many turning stairwells in first person")
    ap.add_argument("--outside-views", type=int, default=0,
                    help="stand on this many reachable outdoor cells")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return self_test()
    if not args.blocks:
        ap.error("give a blocks.csv, or --self-test")

    r = inspect(args.blocks, args.out, args.views,
                stair_views=args.stair_views, outside_views=args.outside_views)
    print(f"{r['reachable_interior']:,} of {r['interior']:,} INTERIOR cells reachable "
          f"from {r['entrances']} entrance cells")
    print(f"  ({r['roof_cells']:,} standable cells are roof -- open sky, nothing covering them)")
    for level, s in sorted(r["per_storey"].items()):
        print(f"  y={level:<3} {s['reachable']:>6,}/{s['standable']:<6,} = {s['share']:.0%}")

    print(f"\ncut off: {r['cut_off_total']:,} cells in {len(r['cut_off_pockets'])} "
          "pocket(s) shown")
    for pocket in r["cut_off_pockets"][:5]:
        print(f"  {pocket['cells']:>5} cells, e.g. at {tuple(pocket['at'])}")

    print(f"\nholes in the envelope: {r['envelope_holes']:,} interior cells with open sky above")
    for cell in r["envelope_holes_sample"][:5]:
        print(f"  {tuple(cell)}")

    print(f"\npoking outside: {r['outside_reachable']:,} outdoor cells the player can reach, "
          f"through {r['crossings']:,} opening(s)")
    for leak in r["crossings_top"][:5]:
        print(f"  {tuple(leak['inside'])} -> {tuple(leak['outside'])} "
              f"({leak['crossings']} crossings over {leak['cells']} stand cells)")

    print(f"\nseeing straight out: {r['see_through']:,} interior cells "
          f"({r['see_through_share']:.1%}) have a clear horizontal line out of the model, "
          f"in {len(r['see_through_top'])}+ clusters")
    for group in r["see_through_top"][:5]:
        print(f"  {group['cells']:>5} cells, e.g. at {tuple(group['at'])}")

    print(f"\nstairwells: {len(r['stairwells'])}")
    for well in r["stairwells"]:
        if well["turns"]:
            turn = (f"turns {well['turns']}x: "
                    + " -> ".join(f"y{lv}:{f}" for lv, f in sorted(well["facing_by_level"].items())))
        elif well["bends"]:
            turn = "two facings, one level (" + "/".join(well["facings"]) + ")"
        else:
            turn = "straight"
        print(f"  {well['cells']:>3} cells at {tuple(well['at'])}, rise {well['rise']}, {turn}, "
              f"{'connected' if well['connected'] else 'ISOLATED'}, "
              f"serves {well['cells_only_it_serves']:,} cells nothing else reaches")

    if r["climbed"]:
        print(f"\nclimbed {len(r['climbed'])} turning stairwell(s) in first person "
              f"-> {args.out}/stair_*.png")
    if r["looked_out"]:
        print(f"stood on {len(r['looked_out'])} reachable outdoor cell(s) "
              f"-> {args.out}/outside_*.png")

    print(f"\n{len(r['views'])} views -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
