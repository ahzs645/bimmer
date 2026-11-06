#!/usr/bin/env python3
"""Room signs: put the campus's room numbers and names into the world.

The IFC has no IfcSpace, so the voxel world has rooms but no names: every
corridor looks like every other one and there is no way to find 07-150
except by already knowing where it is. The room labels exist elsewhere --
openindoormaps publishes the UNBC visitor plans with ~1,400 labelled rooms
("06-270 / Kitchen") as lon/lat points -- but in a different frame.

The join is the doors. Both sides carry the Revit element id of every door:
the plans as `nativeElementId`, the IFC as `IfcDoor.Tag`, and this
pipeline's out/<name>/doors.csv says where each IfcDoor (by GlobalId) landed
in blocks.csv coordinates. So:

  1. pair plan doors with placed doors through Tag            (~1,500 pairs)
  2. fit lon/lat -> blocks.csv (x, z) as a 2-D affine map, trimming the
     pairs that disagree (a door the engine skipped or moved)  and report
     the residual -- a fit that does not land doors within a block or two
     of where they are is not used
  3. take each level's height from the doors on that level
  4. stand a sign on the floor at each label, in the nearest standable
     cell, facing the nearest door, with the number and the name

Signs have no collision box, so a sign never blocks a corridor, and no
cell that is not already empty air over a floor is touched.

Writes a JSON list for renderers/mcweb/export_anvil.js --signs:

    python3 scripts/room_signs.py out/unbc_1m model.ifc \\
        ../openindoormaps/public/data/unbc-plans --out out/unbc_1m/signs.json
    node renderers/mcweb/export_anvil.js out/unbc_1m/blocks.csv \\
        --out world --signs out/unbc_1m/signs.json

    python3 scripts/room_signs.py --self-test
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

SIGN_WIDTH = 15          # characters that fit on one sign line (1.20 font)
SKIP_NAMES = {"corridor", "corr", "hall", "hallway"}


# ---------------------------------------------------------------- inputs
def load_plans(plan_dir: Path):
    """Doors (element id -> lon/lat centroid, level) and room labels."""
    doors, labels = {}, []
    for f in sorted(plan_dir.glob("*.json.gz")):
        with gzip.open(f, "rt", encoding="utf-8") as fh:
            d = json.load(fh)
        layers = d.get("layers")
        if not layers:
            continue
        for ft in layers.get("doors", {}).get("features", []):
            p = ft["properties"]
            ring = ft["geometry"]["coordinates"][0]
            lon = sum(c[0] for c in ring[:-1]) / (len(ring) - 1)
            lat = sum(c[1] for c in ring[:-1]) / (len(ring) - 1)
            doors[str(p["nativeElementId"])] = (lon, lat, p.get("levelId"), p.get("elevationFeet"))
        for ft in layers.get("labels", {}).get("features", []):
            p = ft["properties"]
            if p.get("landmark") or not p.get("name"):
                continue
            lon, lat = ft["geometry"]["coordinates"][:2]
            labels.append({"key": p.get("key"), "name": p["name"], "lon": lon, "lat": lat,
                           "level": p.get("levelId"), "elev_ft": p.get("elevationFeet")})
    # one label per key (a level shared by two plans repeats its labels)
    seen, unique = set(), []
    for lb in labels:
        if lb["key"] in seen:
            continue
        seen.add(lb["key"])
        unique.append(lb)
    return doors, unique


def load_placed_doors(world: Path, ifc_path: Path):
    """Tag -> (x, y, z) of each placed IfcDoor, in blocks.csv coordinates."""
    import ifcopenshell
    tag_of = {d.GlobalId: (d.Tag or "").strip() for d in ifcopenshell.open(str(ifc_path)).by_type("IfcDoor")}
    out = {}
    with (world / "doors.csv").open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if r.get("skipped") == "yes" or not r["x"]:
                continue
            tag = tag_of.get(r["global_id"])
            if tag:
                out[tag] = (int(r["x"]), int(r["y"]), int(r["z"]))
    return out


def load_blocks(world: Path):
    occ = {}
    with (world / "blocks.csv").open(encoding="utf-8") as fh:
        rd = csv.reader(fh)
        next(rd)
        for r in rd:
            occ[(int(r[0]), int(r[1]), int(r[2]))] = r[3]
    return occ


# ---------------------------------------------------------------- the fit
def to_local(lon, lat, lon0, lat0):
    """Equirectangular metres about (lon0, lat0): exact enough over 400 m."""
    r = 6371008.8
    return (np.radians(np.asarray(lon) - lon0) * r * math.cos(math.radians(lat0)),
            np.radians(np.asarray(lat) - lat0) * r)


def fit_affine(src, dst, trim_rounds=4, keep=0.9):
    """Least-squares 2-D affine src->dst, re-fitted on the best `keep` share
    of pairs each round. Returns (A 3x2, residuals of every pair, mask kept)."""
    S = np.hstack([src, np.ones((len(src), 1))])
    mask = np.ones(len(src), bool)
    for _ in range(trim_rounds + 1):
        A, *_ = np.linalg.lstsq(S[mask], dst[mask], rcond=None)
        res = np.linalg.norm(S @ A - dst, axis=1)
        cut = np.quantile(res, keep)
        mask = res <= max(cut, 1.0)
    return A, res, mask


def level_heights(pairs):
    """levelId -> median door y. A door's y is the cell its lower half stands
    in, i.e. the standing height on that level."""
    by = defaultdict(list)
    for level, y in pairs:
        by[level].append(y)
    return {lv: int(np.median(ys)) for lv, ys in by.items()}


# ---------------------------------------------------------------- placement
def wrap(name: str, width=SIGN_WIDTH, lines=3):
    words, out, cur = name.split(), [], ""
    for w in words:
        while len(w) > width:          # a single overlong word is cut
            if cur:
                out.append(cur)
                cur = ""
            out.append(w[:width])
            w = w[width:]
        if not cur:
            cur = w
        elif len(cur) + 1 + len(w) <= width:
            cur += " " + w
        else:
            out.append(cur)
            cur = w
    if cur:
        out.append(cur)
    return out[:lines]


def sign_lines(name: str):
    parts = [p.strip() for p in name.replace("\\n", "\n").split("\n") if p.strip()]
    number = parts[0] if parts else ""
    rest = " ".join(parts[1:])
    return [number[:SIGN_WIDTH]] + wrap(rest)


def standable(occ, x, y, z):
    below = occ.get((x, y - 1, z))
    if below is None or any(t in below for t in ("door", "fence", "sign", "glass")):
        return False
    if (x, y, z) in occ or (x, y + 1, z) in occ:
        return False
    # indoors: a ceiling within 6
    return any((x, y + k, z) in occ for k in range(2, 7))


def find_cell(occ, x, z, y_pred, taken, radius=2, dy=2):
    best = None
    for ddx in range(-radius, radius + 1):
        for ddz in range(-radius, radius + 1):
            for ddy in range(-dy, dy + 1):
                c = (x + ddx, y_pred + ddy, z + ddz)
                if c in taken or not standable(occ, *c):
                    continue
                score = abs(ddx) + abs(ddz) + 1.5 * abs(ddy)
                if best is None or score < best[0]:
                    best = (score, c)
    return best[1] if best else None


def rotation_towards(cell, doors_xyz):
    """Standing-sign rotation (0 = text faces +Z, 4 = -X, 8 = -Z, 12 = +X)
    facing the nearest door on the same storey, so it reads from the door."""
    x, y, z = cell
    near = [(abs(dx - x) + abs(dz - z), dx, dz) for dx, dy, dz in doors_xyz
            if abs(dy - y) <= 1 and abs(dx - x) + abs(dz - z) <= 14]
    if not near:
        return 0
    _, dx, dz = min(near)
    ang = math.atan2(-(dx - x), (dz - z))            # 0 = +Z, pi/2 = -X
    return int(round(ang / (2 * math.pi) * 16)) % 16


def build_signs(occ, plan_doors, placed, labels, max_rms=2.0, log=print):
    pairs = [(plan_doors[t], placed[t]) for t in placed if t in plan_doors]
    if len(pairs) < 20:
        raise SystemExit(f"only {len(pairs)} doors pair up between the plans and the IFC; "
                         "are these the same building?")
    lon0 = float(np.mean([p[0][0] for p in pairs]))
    lat0 = float(np.mean([p[0][1] for p in pairs]))
    ex, ny = to_local([p[0][0] for p in pairs], [p[0][1] for p in pairs], lon0, lat0)
    src = np.stack([ex, ny], axis=1)
    dst = np.array([[p[1][0], p[1][2]] for p in pairs], float)
    A, res, mask = fit_affine(src, dst)
    rms = float(np.sqrt(np.mean(res[mask] ** 2)))
    sx, sz = np.linalg.norm(A[0]), np.linalg.norm(A[1])
    log(f"doors paired: {len(pairs)}; fit on {int(mask.sum())}: rms {rms:.2f} blocks, "
        f"median {np.median(res):.2f}, scale {sx:.3f}/{sz:.3f} blocks per metre")
    if rms > max_rms:
        raise SystemExit(f"fit residual {rms:.2f} blocks exceeds {max_rms}: not placing signs")
    heights = level_heights([(p[0][2], p[1][1]) for p, keep in zip(pairs, mask) if keep])

    doors_xyz = list(placed.values())
    taken, signs, skipped = set(), [], defaultdict(int)
    for lb in labels:
        title = lb["name"].split("\n")
        if len(title) > 1 and title[1].strip().lower().rstrip(".") in SKIP_NAMES:
            skipped["corridor"] += 1
            continue
        if lb["level"] not in heights:
            skipped["level has no placed doors"] += 1
            continue
        lx, lz = to_local([lb["lon"]], [lb["lat"]], lon0, lat0)
        x, z = (np.array([lx[0], lz[0], 1.0]) @ A).round().astype(int)
        cell = find_cell(occ, int(x), int(z), heights[lb["level"]], taken)
        if cell is None:
            skipped["no floor at the label"] += 1
            continue
        taken.add(cell)
        signs.append({"x": cell[0], "y": cell[1], "z": cell[2],
                      "block": f"minecraft:oak_sign[rotation={rotation_towards(cell, doors_xyz)}]",
                      "lines": sign_lines(lb["name"]), "key": lb["key"]})
    log(f"labels: {len(labels)}; signs placed: {len(signs)}; skipped: {dict(skipped)}")
    return signs, {"pairs": len(pairs), "fit_pairs": int(mask.sum()), "rms_blocks": rms,
                   "placed": len(signs), "skipped": dict(skipped)}


# ---------------------------------------------------------------- self-test
def self_test() -> int:
    failures = []
    # a known affine (rotated 30 deg, 1 block/m, offset) is recovered, and a
    # door the engine moved 12 blocks is trimmed instead of bending the fit
    rng = np.random.default_rng(0)
    src = rng.uniform(-150, 150, (200, 2))
    th = math.radians(30)
    R = np.array([[math.cos(th), -math.sin(th)], [math.sin(th), math.cos(th)]])
    dst = src @ R.T + [100, 200]
    dst[:5] += 12
    A, res, mask = fit_affine(src, dst)
    if np.abs(np.hstack([src, np.ones((200, 1))]) @ A - (src @ R.T + [100, 200]))[5:].max() > 0.05:
        failures.append("affine fit did not recover the map")
    if mask[:5].any():
        failures.append("moved doors were not trimmed")
    if sign_lines("06-270\nStaff Kitchen") != ["06-270", "Staff Kitchen"]:
        failures.append(f"lines: {sign_lines('06-270' + chr(10) + 'Staff Kitchen')}")
    if sign_lines("06-256\nManager's Office") != ["06-256", "Manager's", "Office"]:
        failures.append("a 16-character name must wrap onto two lines")
    long = sign_lines("10-B514\nEnvironmental Hydrology Teaching Laboratory")
    if any(len(s) > SIGN_WIDTH for s in long) or len(long) > 4:
        failures.append(f"wrap overflowed: {long}")
    # standable: floor below, two air, ceiling above; a door below is not a floor
    occ = {(0, 0, 0): "minecraft:smooth_stone", (0, 4, 0): "minecraft:smooth_stone",
           (1, 0, 0): "minecraft:oak_door[half=lower]", (1, 4, 0): "minecraft:smooth_stone"}
    if not standable(occ, 0, 1, 0) or standable(occ, 1, 1, 0) or standable(occ, 0, 2, 0):
        failures.append("standable() is wrong")
    if rotation_towards((0, 1, 0), [(0, 1, 5)]) != 0 or rotation_towards((0, 1, 0), [(5, 1, 0)]) != 12:
        failures.append("rotation does not face the door")
    if failures:
        print("self-test FAILED")
        for f in failures:
            print(" ", f)
        return 1
    print("self-test passed: affine recovered with outliers trimmed, lines wrap, "
          "signs stand on floors and face their door")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("world", nargs="?", help="pipeline output dir (blocks.csv, doors.csv)")
    ap.add_argument("ifc", nargs="?", help="the IFC the world was built from (for IfcDoor.Tag)")
    ap.add_argument("plans", nargs="?", help="directory of openindoormaps visitor plans (*.json.gz)")
    ap.add_argument("--out", help="signs JSON (default: <world>/signs.json)")
    ap.add_argument("--max-rms", type=float, default=2.0, help="refuse a fit worse than this many blocks")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        sys.exit(self_test())
    if not (a.world and a.ifc and a.plans):
        ap.error("world, ifc and plans are required")
    world = Path(a.world)
    plan_doors, labels = load_plans(Path(a.plans))
    placed = load_placed_doors(world, Path(a.ifc))
    occ = load_blocks(world)
    signs, stats = build_signs(occ, plan_doors, placed, labels, a.max_rms)
    out = Path(a.out) if a.out else world / "signs.json"
    out.write_text(json.dumps(signs, indent=0), encoding="utf-8")
    (out.with_suffix(".stats.json")).write_text(json.dumps(stats, indent=2), encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
