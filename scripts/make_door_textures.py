#!/usr/bin/env python3
"""Generate 16x16 door textures for BlockCraft (closed + open).

The door is rendered as a thin flat panel (see voxel-worker.js), so these
textures only ever show on the panel's two faces. Closed = a vertical paneled
wooden door; open = a thin door swung against the jamb with the rest clear.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

WOOD = (150, 100, 55, 255)
WOOD_DARK = (96, 60, 30, 255)
WOOD_LIGHT = (176, 124, 74, 255)
HANDLE = (225, 200, 90, 255)
CLEAR = (0, 0, 0, 0)


def closed() -> Image.Image:
    # Seamless VERTICALLY: only left/right borders + continuous vertical panels,
    # so stacking N cells reads as ONE tall door (not N stacked doors).
    img = Image.new("RGBA", (16, 16), WOOD)
    px = img.load()
    # vertical frame on the left/right edges (no top/bottom border)
    for y in range(16):
        px[0, y] = px[1, y] = WOOD_DARK
        px[14, y] = px[15, y] = WOOD_DARK
    # central stile
    for y in range(16):
        px[7, y] = WOOD_DARK
        px[8, y] = WOOD_LIGHT
    # two recessed vertical panels, full height (tile seamlessly)
    for (x0, x1) in ((3, 6), (9, 12)):
        for y in range(16):
            px[x0, y] = px[x1, y] = WOOD_DARK
            for x in range(x0 + 1, x1):
                px[x, y] = WOOD_LIGHT
    # continuous vertical handle bar near the latch side
    for y in range(16):
        px[12, y] = HANDLE
    return img


def open_leaf() -> Image.Image:
    img = Image.new("RGBA", (16, 16), CLEAR)
    px = img.load()
    # thin door swung flat against the left jamb
    for x in range(0, 3):
        for y in range(0, 16):
            px[x, y] = WOOD if x == 1 else WOOD_DARK
    px[2, 8] = HANDLE
    return img


def main() -> None:
    blocks_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(
        "blockcraft/client/assets/textures/blocks")
    blocks_dir = blocks_dir.expanduser().resolve()
    blocks_dir.mkdir(parents=True, exist_ok=True)
    closed().save(blocks_dir / "door.png")
    open_leaf().save(blocks_dir / "door_open.png")
    print(f"wrote {blocks_dir/'door.png'} and {blocks_dir/'door_open.png'}")


if __name__ == "__main__":
    main()
