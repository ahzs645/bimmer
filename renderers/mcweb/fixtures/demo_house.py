#!/usr/bin/env python3
"""Generate a tiny demo blocks.csv that exercises every block *shape* the
minecraft-web-client renderer handles natively but BlockCraft could not:
real functional doors, stairs, slabs, glass, and connected iron-bar railings.

This is a hand-authored stand-in for a pipeline `blocks.csv` (same
`x,y,z,block` schema, same block-state strings as scripts/ifc_to_voxels.py),
so the Anvil exporter and the renderer can be verified without an IFC.

Usage:  python3 renderers/mcweb/fixtures/demo_house.py > demo_house.csv
"""
from __future__ import annotations

import sys

W = 7  # footprint is W x W
H = 4  # wall height


def rows():
    # floor slab layer (real slabs, not cubes)
    for x in range(W):
        for z in range(W):
            yield x, 0, z, "minecraft:smooth_stone_slab[type=top]"

    # four walls of white concrete, with a glass window band at y=2
    for y in range(1, H):
        for x in range(W):
            for z in range(W):
                edge = x in (0, W - 1) or z in (0, W - 1)
                if not edge:
                    continue
                # leave the doorway (south wall, z=0, x=3) open for the door
                if z == 0 and x == 3 and y in (1, 2):
                    continue
                if y == 2 and not (x in (0, W - 1) and z in (0, W - 1)):
                    yield x, y, z, "minecraft:light_blue_stained_glass"
                else:
                    yield x, y, z, "minecraft:white_concrete"

    # functional two-half door in the south doorway, facing north (into the room)
    yield 3, 1, 0, "minecraft:oak_door[facing=north,half=lower,hinge=left,open=false,powered=false]"
    yield 3, 2, 0, "minecraft:oak_door[facing=north,half=upper,hinge=left,open=false,powered=false]"

    # a short external stair run up to the doorway (real stair block-states)
    yield 3, 0, -1, "minecraft:stone_brick_stairs[facing=north,half=bottom,shape=straight]"
    yield 3, 0, -2, "minecraft:stone_brick_stairs[facing=north,half=bottom,shape=straight]"

    # flat roof of concrete
    for x in range(W):
        for z in range(W):
            yield x, H, z, "minecraft:white_concrete"

    # fence railing parapet around the roof edge (real post-and-rail models)
    for x in range(W):
        for z in range(W):
            if x in (0, W - 1) or z in (0, W - 1):
                yield x, H + 1, z, "minecraft:oak_fence"


def main():
    out = sys.stdout
    out.write("x,y,z,block\n")
    for x, y, z, b in rows():
        out.write(f"{x},{y},{z},{b}\n")


if __name__ == "__main__":
    main()
