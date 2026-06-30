#!/usr/bin/env python3
"""Minecraft block palette + colour matching.

Two sub-palettes so transparent IFC materials map to *stained glass* and opaque
materials map to concrete/stone/wood, matched by perceptual distance (CIE-Lab
delta-E). This mirrors the approach in TwentyFiveSoftware/voxelizer (separate
glass palette) and the general sRGB->Lab nearest-block technique.

Colours are approximate average texture RGB (0-255) for Java Edition blocks.
"""

from __future__ import annotations

from functools import lru_cache

# --- opaque structural / architectural palette -----------------------------
OPAQUE_PALETTE: dict[str, tuple[int, int, int]] = {
    "minecraft:white_concrete": (207, 213, 214),
    "minecraft:light_gray_concrete": (125, 125, 115),
    "minecraft:gray_concrete": (54, 57, 61),
    "minecraft:black_concrete": (8, 10, 15),
    "minecraft:cyan_concrete": (21, 119, 136),
    "minecraft:light_blue_concrete": (35, 137, 198),
    "minecraft:blue_concrete": (44, 46, 143),
    "minecraft:brown_concrete": (96, 60, 32),
    "minecraft:red_concrete": (142, 33, 33),
    "minecraft:orange_concrete": (224, 97, 1),
    "minecraft:yellow_concrete": (240, 175, 21),
    "minecraft:lime_concrete": (94, 169, 24),
    "minecraft:green_concrete": (73, 91, 36),
    "minecraft:pink_concrete": (213, 101, 143),
    "minecraft:smooth_stone": (159, 159, 159),
    "minecraft:stone": (127, 127, 127),
    "minecraft:stone_bricks": (122, 122, 122),
    "minecraft:deepslate_tiles": (60, 60, 66),
    "minecraft:polished_andesite": (132, 134, 133),
    "minecraft:polished_diorite": (188, 188, 189),
    "minecraft:smooth_quartz": (235, 229, 222),
    "minecraft:sandstone": (216, 203, 156),
    "minecraft:bricks": (150, 97, 83),
    "minecraft:terracotta": (152, 94, 67),
    "minecraft:white_terracotta": (209, 178, 161),
    "minecraft:light_gray_terracotta": (135, 107, 98),
    "minecraft:oak_planks": (162, 130, 78),
    "minecraft:spruce_planks": (114, 84, 48),
    "minecraft:dark_oak_planks": (66, 43, 20),
    "minecraft:iron_block": (220, 220, 220),
}

# --- transparent / glazing palette -----------------------------------------
GLASS_PALETTE: dict[str, tuple[int, int, int]] = {
    "minecraft:glass": (200, 220, 230),
    "minecraft:white_stained_glass": (236, 240, 240),
    "minecraft:light_gray_stained_glass": (153, 153, 153),
    "minecraft:gray_stained_glass": (76, 76, 76),
    "minecraft:light_blue_stained_glass": (102, 153, 216),
    "minecraft:cyan_stained_glass": (76, 127, 153),
    "minecraft:blue_stained_glass": (51, 51, 178),
    "minecraft:green_stained_glass": (102, 127, 51),
    "minecraft:lime_stained_glass": (127, 204, 25),
    "minecraft:brown_stained_glass": (102, 76, 51),
    "minecraft:black_stained_glass": (25, 25, 25),
}

GLASS_BLOCKS = set(GLASS_PALETTE)


def _srgb_to_lab(rgb: tuple[float, float, float]) -> tuple[float, float, float]:
    """sRGB (0-255) -> CIE L*a*b* (D65)."""
    def lin(c: float) -> float:
        c /= 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = lin(rgb[0]), lin(rgb[1]), lin(rgb[2])
    # linear sRGB -> XYZ (D65)
    x = r * 0.4124 + g * 0.3576 + b * 0.1805
    y = r * 0.2126 + g * 0.7152 + b * 0.0722
    z = r * 0.0193 + g * 0.1192 + b * 0.9505
    # normalise by D65 white
    x, y, z = x / 0.95047, y / 1.0, z / 1.08883

    def f(t: float) -> float:
        return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116
    fx, fy, fz = f(x), f(y), f(z)
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


_LAB_OPAQUE = {b: _srgb_to_lab(c) for b, c in OPAQUE_PALETTE.items()}
_LAB_GLASS = {b: _srgb_to_lab(c) for b, c in GLASS_PALETTE.items()}


@lru_cache(maxsize=4096)
def nearest_block(rgb: tuple[int, int, int], glass: bool = False) -> str:
    """Return the palette block whose colour is perceptually closest to rgb."""
    lab = _srgb_to_lab(rgb)
    table = _LAB_GLASS if glass else _LAB_OPAQUE
    best, best_d = None, 1e18
    for block, blab in table.items():
        d = (lab[0] - blab[0]) ** 2 + (lab[1] - blab[1]) ** 2 + (lab[2] - blab[2]) ** 2
        if d < best_d:
            best, best_d = block, d
    return best


def rgb_for_block(block: str) -> tuple[int, int, int]:
    return OPAQUE_PALETTE.get(block) or GLASS_PALETTE.get(block) or (190, 120, 120)


def is_glass(block: str) -> bool:
    return block in GLASS_BLOCKS


if __name__ == "__main__":  # quick self-test
    tests = [
        ((0, 128, 191), True, "teal glazing"),
        ((247, 247, 247), False, "white wall"),
        ((117, 69, 51), False, "brown door"),
        ((191, 191, 191), False, "gray slab"),
    ]
    for rgb, g, label in tests:
        print(f"{label:16s} {rgb} glass={g} -> {nearest_block(rgb, g)}")
