#!/usr/bin/env python3
"""Prepare the in-repo BlockCraft fork to render the UNBC building.

BlockCraft lives in ./blockcraft and is tracked in this repo (our Apache-2.0
fork, already modified: flat world + building stamp, creative+fly spawn,
functional flat doors, stair priority). This script just regenerates the two
things that are NOT committed because they derive from the IFC:

  1. door textures (door.png / door_open.png)
  2. blockcraft/server/building.json  (from a voxel blocks.csv)

Usage:
  .venv/bin/python scripts/setup_blockcraft.py [path/to/blocks.csv]
  (default: out/unbc_0p5m/blocks.csv)
Then: ( cd blockcraft/server && npm install )   # first time
      ( cd blockcraft/client && npm install three && npm install )   # first time
      scripts/run_blockcraft.sh
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BC = ROOT / "blockcraft"


def main() -> None:
    blocks_csv = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "out/unbc_0p5m/blocks.csv"

    if not BC.exists():
        raise SystemExit(f"BlockCraft not found at {BC} (it should be tracked in this repo).")
    if not blocks_csv.exists():
        raise SystemExit(f"blocks.csv not found: {blocks_csv}. Run `make p1` or `make p05` first.")

    subprocess.run([sys.executable, str(ROOT / "scripts/make_door_textures.py"),
                    str(BC / "client/assets/textures/blocks")], check=True)
    subprocess.run([sys.executable, str(ROOT / "scripts/export_blockcraft.py"), str(blocks_csv)], check=True)

    print("\n" + "=" * 60)
    print("BlockCraft ready (door textures + building.json regenerated).")
    print("First time only:")
    print("  ( cd blockcraft/server && npm install )")
    print("  ( cd blockcraft/client && npm install three && npm install )")
    print("Run:   scripts/run_blockcraft.sh")
    print("Open:  http://localhost:3001  -> Direct Connect (localhost:3001)")
    print("=" * 60)


if __name__ == "__main__":
    main()
