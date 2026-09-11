#!/usr/bin/env python3
"""End-to-end RVT/IFC -> Minecraft pipeline driver.

Runs every stage in order and writes a manifest:

  RVT --(rvt_to_ifc)--> IFC          (only when the input is a .rvt)
  IFC --(ifc_to_voxels)--> out/<name>/blocks.csv
      --(blocks_to_minecraft)--> out/<name>/<name>.schem [+ .litematic]
      --(export_web)--> web/data/<name>/{voxels.bin,meta.json}
      --(render_voxels)--> out/<name>/preview/*.png

Everything it produces is reproducible from the IFC, so the out/ and web/data/
trees are git-ignored; this script is the source of truth for regenerating them.

Hand it a `.rvt` and stage 0 runs first: the Reviter parser in
`parsers/reviter` recovers an IFC, which is then graded against what the voxel
engine reads before any of it is converted (see REVITER.md). Hand it an `.ifc`
and nothing changes -- that path is untouched.

Example:
  .venv/bin/python scripts/pipeline.py "UNBC Model ... .ifc" --pitch 1.0 --name unbc_1m
  .venv/bin/python scripts/pipeline.py "UNBC Model ... .rvt" --pitch 1.0 --name unbc_1m
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
SCRIPTS = ROOT / "scripts"


def run(label: str, cmd: list[str]) -> None:
    print(f"\n=== {label} ===", flush=True)
    print("  " + " ".join(repr(c) if " " in c else c for c in cmd), flush=True)
    result = subprocess.run(cmd)
    if result.returncode != 0:
        # Each stage diagnoses its own failure on the way out. A traceback from
        # this driver on top of that buries the message that actually explains
        # what went wrong, so fail with the stage name and nothing else.
        raise SystemExit(f"\npipeline: stage '{label}' failed (exit {result.returncode}); "
                         "its error is above.")


def refresh_datasets(web_dir: Path) -> list[dict]:
    """Rebuild web/data/datasets.json from whatever datasets currently exist."""
    data_dir = web_dir / "data"
    entries = []
    for meta_path in sorted(data_dir.glob("*/meta.json")):
        meta = json.loads(meta_path.read_text())
        entries.append({"name": meta["name"], "label": meta.get("label", meta["name"]),
                        "count": meta.get("count", 0), "pitch": meta.get("pitch_m")})
    # coarsest (fewest voxels) first so the viewer defaults to the fast dataset
    entries.sort(key=lambda e: e["count"])
    listing = [{"name": e["name"], "label": e["label"]} for e in entries]
    (data_dir / "datasets.json").write_text(json.dumps(listing, indent=2), encoding="utf-8")
    return entries


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("model", type=Path, help=".ifc, or .rvt to parse first")
    ap.add_argument("--pitch", type=float, default=1.0, help="voxel size in metres")
    ap.add_argument("--name", required=True, help="dataset name, e.g. unbc_1m")
    ap.add_argument("--label", default=None, help="human label for the viewer dropdown")
    ap.add_argument("--doors", choices=["functional", "air", "solid"], default="functional")
    ap.add_argument("--formats", nargs="*", default=["schem", "litematic"],
                    choices=["schem", "litematic"])
    ap.add_argument("--no-web", action="store_true", help="skip web viewer export")
    ap.add_argument("--no-preview", action="store_true", help="skip PNG previews")
    ap.add_argument("--allow-contract-failures", action="store_true",
                    help="convert a parsed IFC that fails the contract gate anyway")
    args = ap.parse_args()

    source = args.model.expanduser().resolve()
    if not source.exists():
        raise SystemExit(f"model not found: {source}")
    out_dir = ROOT / "out" / args.name

    # 0. RVT -> IFC, when we were handed the proprietary format. The parsed IFC
    #    lands beside the voxel output rather than beside the source model, so a
    #    read-only model directory works and each dataset keeps its own IFC and
    #    the provenance sidecar naming the parser commit that produced it.
    if source.suffix.lower() == ".rvt":
        out_dir.mkdir(parents=True, exist_ok=True)
        ifc = out_dir / f"{source.stem}.ifc"
        cmd = [PY, str(SCRIPTS / "rvt_to_ifc.py"), str(source), "--out", str(ifc),
               "--pitch", str(args.pitch)]
        if args.allow_contract_failures:
            cmd.append("--allow-contract-failures")
        run("0/4 RVT -> IFC", cmd)
    else:
        ifc = source
    blocks = out_dir / "blocks.csv"
    label = args.label or f"{args.name} ({args.pitch} m)"

    # 1. voxelize
    run("1/4 IFC -> voxels", [PY, str(SCRIPTS / "ifc_to_voxels.py"), str(ifc),
                              "--pitch", str(args.pitch), "--doors", args.doors,
                              "--out-dir", str(out_dir)])

    # 2. schematics
    for fmt in args.formats:
        run(f"2/4 voxels -> .{fmt}", [PY, str(SCRIPTS / "blocks_to_minecraft.py"), str(blocks),
                                      str(out_dir / args.name), "--format", fmt])

    # 3. web viewer data
    if not args.no_web:
        run("3/4 voxels -> web data", [PY, str(SCRIPTS / "export_web.py"), str(blocks),
                                       "--name", args.name, "--label", label,
                                       "--pitch", str(args.pitch)])
        entries = refresh_datasets(ROOT / "web")
        print("  datasets now available:", [e["name"] for e in entries])

    # 4. previews
    if not args.no_preview:
        run("4/4 voxels -> previews", [PY, str(SCRIPTS / "render_voxels.py"), str(blocks)])

    summary = json.loads((out_dir / "summary.json").read_text())
    print("\n" + "=" * 60)
    print(f"DONE: {args.name}")
    print(f"  voxels:       {summary['total_blocks']:,}")
    print(f"  doors:        {summary['doors_placed']:,} ({summary['door_mode']})")
    print(f"  grid (x,y,z): {summary['minecraft_grid_xyz']}")
    print(f"  schematics:   out/{args.name}/{args.name}.* ")
    if not args.no_web:
        print(f"  viewer:       scripts/serve_viewer.sh  ->  http://127.0.0.1:8765/")
    print("=" * 60)


if __name__ == "__main__":
    main()
