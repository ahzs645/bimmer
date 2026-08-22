#!/usr/bin/env python3
"""Stage 0: RVT -> IFC, using the Reviter parser in `parsers/reviter`.

This is the seam between the two halves of the pipeline:

    RVT --[parsers/reviter]--> IFC --[scripts/ifc_to_voxels.py]--> voxels
         the parser                   the interpreter

**The seam is the IFC contract, not code.** Nothing in this repository imports
Reviter's TypeScript; it runs Reviter's CLI and then grades the file it produced
with `check_ifc_contract.py`. That is deliberate. Reviter is a clean-room
decoder for a proprietary format whose internals change often and are fitted to
one building; welding the voxel engine to them would mean every decoder change
could break a world. Holding it to a file-level contract instead means the
parser is free to improve, and the one thing that must stay stable is small and
checkable.

    python3 scripts/rvt_to_ifc.py model.rvt --out out/model.ifc
    python3 scripts/rvt_to_ifc.py --check        # preflight only, no model
    python3 scripts/rvt_to_ifc.py --self-test    # no model, no submodule

A `<out>.provenance.json` sidecar records which parser commit produced the file.
Reviter's own convention is that every measurement names the run it came from;
a voxel world inherits that obligation, because "the stairs are wrong" is a
different bug depending on which parser wrote the IFC.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PARSER = ROOT / "parsers" / "reviter"
ENTRY = Path("scripts") / "extract-geometry.ts"
SCRIPTS = ROOT / "scripts"


def _display(path: Path) -> str:
    """Repo-relative when it is inside the repo, absolute when it is not.

    Preflight runs against a caller-supplied directory (the self-test points it
    at a temp dir), so `relative_to(ROOT)` is not safe to assume.
    """
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


class Preflight(Exception):
    """A precondition failed, with a message that says how to fix it."""


def parse_version(text: str) -> tuple[int, ...]:
    """`v22.22.2` / `>=22.13.0` / `22.13` -> a comparable tuple."""
    match = re.search(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", text)
    if not match:
        raise ValueError(f"no version in {text!r}")
    return tuple(int(part) if part else 0 for part in match.groups())


def required_node(parser_dir: Path = PARSER) -> tuple[int, ...] | None:
    """The parser's own `engines.node` floor, read rather than duplicated.

    Hard-coding it here would mean this file quietly disagrees with the
    submodule the day the parser raises its floor -- and the symptom would be
    a stack trace from inside a decoder rather than a version message.
    """
    manifest = parser_dir / "package.json"
    if not manifest.exists():
        return None
    engines = json.loads(manifest.read_text()).get("engines", {})
    declared = engines.get("node")
    if not declared:
        return None
    try:
        return parse_version(declared)
    except ValueError:
        return None


def preflight(parser_dir: Path = PARSER) -> dict:
    """Everything that must be true before a conversion is worth starting."""
    if not (parser_dir / "package.json").exists():
        raise Preflight(
            f"the Reviter parser is not checked out at {_display(parser_dir)}.\n"
            "  git submodule update --init parsers/reviter")

    if not (parser_dir / ENTRY).exists():
        raise Preflight(
            f"{_display(parser_dir / ENTRY)} is missing. The submodule is "
            "checked out but does not look like Reviter; check the pinned commit.")

    node = shutil.which("node")
    if not node:
        raise Preflight("node is not on PATH. The parser is a Node/TypeScript project.")

    found = parse_version(subprocess.run(
        [node, "--version"], capture_output=True, text=True, check=True).stdout)
    floor = required_node(parser_dir)
    if floor and found < floor:
        raise Preflight(
            f"node {'.'.join(map(str, found))} is older than the parser's declared "
            f"floor of {'.'.join(map(str, floor))}. It also needs "
            "--experimental-strip-types, which arrived in 22.6.")

    if not (parser_dir / "node_modules").exists():
        raise Preflight(
            "the parser's dependencies are not installed.\n"
            "  make parser-setup      (or: npm ci --prefix parsers/reviter)")

    return {"node": node, "node_version": ".".join(map(str, found)),
            "commit": submodule_commit(parser_dir)}


def submodule_commit(parser_dir: Path = PARSER) -> str | None:
    result = subprocess.run(["git", "-C", str(parser_dir), "rev-parse", "HEAD"],
                            capture_output=True, text=True)
    return result.stdout.strip() or None if result.returncode == 0 else None


def sha256(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(chunk):
            digest.update(block)
    return digest.hexdigest()


def extract(rvt: Path, ifc: Path, tools: dict, revit_version: int | None,
            parser_dir: Path = PARSER, quiet: bool = False) -> None:
    cmd = [tools["node"], "--experimental-strip-types", str(ENTRY),
           str(rvt), "--out", str(ifc)]
    if revit_version:
        cmd += ["--revit-version", str(revit_version)]
    if not quiet:
        print(f"=== 0/4 RVT -> IFC (parser {(tools['commit'] or '?')[:8]}) ===", flush=True)
        print("  " + " ".join(cmd), flush=True)
    # cwd is the submodule: the entry point resolves its imports relative to
    # its own tree, and its node_modules lives there rather than in ours.
    subprocess.run(cmd, check=True, cwd=parser_dir,
                   capture_output=quiet)
    if not ifc.exists():
        raise SystemExit(f"the parser exited 0 but wrote no file at {ifc}")


def gate(ifc: Path, pitch: float, quiet: bool = False) -> dict:
    """Grade the produced IFC against what the voxel engine reads."""
    report = ifc.with_suffix(ifc.suffix + ".contract.json")
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "check_ifc_contract.py"), str(ifc),
         "--pitch", str(pitch), "--json", str(report)], capture_output=quiet)
    payload = json.loads(report.read_text()) if report.exists() else {}
    return {"verdict": payload.get("verdict"), "report": str(report),
            "exit_code": result.returncode}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("rvt", nargs="?", type=Path, help=".rvt to convert")
    ap.add_argument("--out", type=Path, help="IFC to write (default: alongside the RVT)")
    ap.add_argument("--pitch", type=float, default=1.0,
                    help="the pitch the IFC will be voxelized at, for the contract gate")
    ap.add_argument("--revit-version", type=int, default=None,
                    help="override the release the parser reads from BasicFileInfo")
    ap.add_argument("--allow-contract-failures", action="store_true",
                    help="convert anyway when the IFC fails the contract gate")
    ap.add_argument("--skip-contract", action="store_true", help="do not run the gate")
    ap.add_argument("--check", action="store_true",
                    help="run the preflight and exit; needs no model")
    ap.add_argument("--self-test", action="store_true",
                    help="check this driver's own logic; needs no model or submodule")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    try:
        tools = preflight()
    except Preflight as exc:
        print(f"rvt_to_ifc: {exc}", file=sys.stderr)
        return 2

    if args.check:
        print(f"parser ready: {_display(PARSER)} @ "
              f"{(tools['commit'] or 'unknown')[:8]}, node {tools['node_version']}")
        return 0
    if not args.rvt:
        ap.error("give a .rvt file, or --check / --self-test")

    rvt = args.rvt.expanduser().resolve()
    if not rvt.exists():
        raise SystemExit(f"RVT not found: {rvt}")
    ifc = (args.out or rvt.with_suffix(".ifc")).expanduser().resolve()
    ifc.parent.mkdir(parents=True, exist_ok=True)

    extract(rvt, ifc, tools, args.revit_version)

    contract = None
    if not args.skip_contract:
        contract = gate(ifc, args.pitch)

    # Pin what produced this file, the way every dated entry in Reviter's docs
    # pins its sources. A world's artifacts are only interpretable against the
    # parser commit that wrote its IFC.
    provenance = {
        "parser": "reviter",
        "parser_commit": tools["commit"],
        "parser_path": _display(PARSER),
        "node_version": tools["node_version"],
        "source_rvt": str(rvt),
        "source_rvt_sha256": sha256(rvt),
        "output_ifc": str(ifc),
        "output_ifc_sha256": sha256(ifc),
        "contract": contract,
    }
    sidecar = ifc.with_suffix(ifc.suffix + ".provenance.json")
    sidecar.write_text(json.dumps(provenance, indent=2), encoding="utf-8")

    print(f"\nwrote {ifc} ({ifc.stat().st_size:,} bytes)")
    print(f"      {sidecar.name}")
    if contract:
        print(f"      contract verdict: {contract['verdict']}")
        if contract["exit_code"] != 0 and not args.allow_contract_failures:
            print("\nThis IFC is missing something the voxel engine reads. Converting it "
                  "would succeed and produce a subtly unwalkable world -- see REVITER.md.\n"
                  "Re-run with --allow-contract-failures to proceed anyway.", file=sys.stderr)
            return 1
    return 0


def self_test() -> int:
    """Exercise the preflight without a parser, a model, or node_modules.

    A conversion needs a 67 MB proprietary file that is not in this repository
    and never will be, so the parts that can be checked here are the version
    comparison and each preflight failure carrying an actionable message.
    """
    import tempfile

    failures: list[str] = []

    def expect(condition: bool, message: str) -> None:
        if not condition:
            failures.append(message)

    expect(parse_version("v22.22.2") == (22, 22, 2), "node --version form")
    expect(parse_version(">=22.13.0") == (22, 13, 0), "engines range form")
    expect(parse_version("22.13") == (22, 13, 0), "two-part form pads with zero")
    expect(parse_version("v22.6.0") < parse_version(">=22.13.0"), "ordering")
    expect(parse_version("v23.0.0") > parse_version(">=22.13.0"), "major ordering")

    with tempfile.TemporaryDirectory() as tmp:
        empty = Path(tmp) / "not-checked-out"
        empty.mkdir()
        try:
            preflight(empty)
            failures.append("an unchecked-out submodule should not pass preflight")
        except Preflight as exc:
            expect("submodule update --init" in str(exc),
                   "the missing-submodule message must say how to fix it")

        expect(required_node(empty) is None, "no manifest -> no declared floor")

        wrong = Path(tmp) / "wrong-repo"
        (wrong / "scripts").mkdir(parents=True)
        (wrong / "package.json").write_text(json.dumps({"engines": {"node": ">=22.13.0"}}))
        expect(required_node(wrong) == (22, 13, 0), "engines.node is read, not hard-coded")
        try:
            preflight(wrong)
            failures.append("a package.json without the entry point should not pass")
        except Preflight as exc:
            expect("does not look like Reviter" in str(exc),
                   "a wrong submodule must be named as such, not reported as missing node")

        no_deps = Path(tmp) / "no-deps"
        (no_deps / ENTRY.parent).mkdir(parents=True)
        (no_deps / ENTRY).write_text("// entry point\n")
        (no_deps / "package.json").write_text(json.dumps({"engines": {"node": ">=22.13.0"}}))
        try:
            preflight(no_deps)
            failures.append("a submodule with no node_modules should not pass")
        except Preflight as exc:
            expect("parser-setup" in str(exc),
                   "the missing-dependencies message must name the make target")

        # A stub parser stands in for Reviter so the plumbing around it -- the
        # argument construction, the submodule cwd, the gate, the sidecar --
        # is exercised without the 67 MB proprietary model it would need.
        if shutil.which("node"):
            failures.extend(_stub_round_trip(Path(tmp)))

    if failures:
        print("self-test FAILED")
        for line in failures:
            print(f"  {line}")
        return 1
    print("self-test passed: version comparison, every preflight failure path, "
          "and a stub-parser round trip through the contract gate")
    return 0


def _stub_round_trip(tmp: Path) -> list[str]:
    """Drive extract() + gate() against a parser that is not Reviter.

    The stub copies a prebuilt IFC to wherever `--out` points, which is exactly
    the contract `extract()` depends on: run the entry point from the parser's
    own directory, hand it an absolute output path, and find a file there
    afterwards.
    """
    sys.path.insert(0, str(SCRIPTS))
    import check_ifc_contract  # noqa: PLC0415 -- optional, only the self-test needs it

    problems: list[str] = []
    stub = tmp / "stub-parser"
    (stub / ENTRY.parent).mkdir(parents=True)
    (stub / "node_modules").mkdir()
    # "type": "module" only silences node's reparse warning; the stub is ESM.
    (stub / "package.json").write_text(
        json.dumps({"type": "module", "engines": {"node": ">=22.13.0"}}))
    (stub / ENTRY).write_text(
        "import { copyFileSync } from 'node:fs';\n"
        "const argv = process.argv.slice(2);\n"
        "const out = argv[argv.indexOf('--out') + 1];\n"
        "copyFileSync(process.env.STUB_IFC, out);\n")

    complete = tmp / "complete.ifc"
    check_ifc_contract._fixture(complete, aggregate_stairs=True, door_width=0.9, tag=True)
    thin = tmp / "thin.ifc"
    check_ifc_contract._fixture(thin, aggregate_stairs=False, door_width=None, tag=False)

    rvt = tmp / "fake.rvt"
    rvt.write_bytes(b"not really an RVT")
    tools = preflight(stub)

    import os  # noqa: PLC0415 -- only needed to hand the stub its payload

    for source, want in ((complete, "OK"), (thin, "FAIL")):
        produced = tmp / f"{source.stem}-out.ifc"
        os.environ["STUB_IFC"] = str(source)
        try:
            extract(rvt, produced, tools, None, parser_dir=stub, quiet=True)
        except Exception as exc:  # noqa: BLE001 -- any failure here is the finding
            problems.append(f"stub extract raised for {source.name}: {exc}")
            continue
        if not produced.exists():
            problems.append(f"stub extract wrote nothing for {source.name}")
            continue
        verdict = gate(produced, 1.0, quiet=True)["verdict"]
        if verdict != want:
            problems.append(f"gate said {verdict} for {source.name}, expected {want}")
    os.environ.pop("STUB_IFC", None)
    return problems


if __name__ == "__main__":
    sys.exit(main())
