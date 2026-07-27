#!/usr/bin/env python3
"""
design_setup.py — Stage a dataset design/config into the ORFS flow tree,
following ORFS's designs/ layout. Copies files only; runs no flow.

Selection:
    --pdk     asap7 | nangate45
    --design  aes | ibex | jpeg
    --config  <util>_<cp>      e.g. 70_430

Staging (ORFS rules):
    dataset/<pdk>/<design>/<design>_<config>/design/  ->  openroad-flow-scripts/flow/designs/src/<srcdir>/
    dataset/<pdk>/<design>/<design>_<config>/setup/   ->  openroad-flow-scripts/flow/designs/<pdk>/<design>/
"""
from __future__ import annotations
import argparse
import pathlib
import shutil
import sys

ROOT    = pathlib.Path(__file__).resolve().parent.parent.parent      # ResizerAgent/
DATASET = ROOT / "AE" / "dataset"
TREE    = ROOT / "openroad-flow-scripts" / "flow"


def die(msg: str) -> "None":
    print(f"[design_setup][ERROR] {msg}", file=sys.stderr)
    sys.exit(1)


def _list(paths) -> str:
    return ", ".join(sorted(p.name for p in paths if p.is_dir())) or "(none)"


def main() -> int:
    ap = argparse.ArgumentParser(description="Stage a dataset design/config into the ORFS flow tree.")
    ap.add_argument("--pdk", default="asap7", choices=["asap7", "nangate45"], help="Target PDK")
    ap.add_argument("--design", required=True, help="Design nickname, e.g. aes/ibex/jpeg")
    ap.add_argument("--config", required=True, help="<util>_<cp>, e.g. 70_430")
    ap.add_argument("--dry-run", action="store_true", help="Show actions without copying")
    args = ap.parse_args()

    cfgname = f"{args.design}_{args.config}"
    src = DATASET / args.pdk / args.design / cfgname

    if not (DATASET / args.pdk / args.design).is_dir():
        die(f"no such design '{args.design}' under dataset/{args.pdk}/ "
            f"(have: {_list((DATASET / args.pdk).iterdir()) if (DATASET/args.pdk).is_dir() else 'none'})")
    if not src.is_dir():
        die(f"config '{args.config}' not found for {args.pdk}/{args.design} "
            f"(have: {_list((DATASET/args.pdk/args.design).iterdir())})")

    setup_dir, design_dir = src / "setup", src / "design"
    if not setup_dir.is_dir() or not design_dir.is_dir():
        die(f"malformed dataset entry (missing design/ or setup/) at {src}")
    if not TREE.is_dir():
        die(f"ORFS tree not found: {TREE}\n"
            f"        (clone the submodule: git submodule update --init openroad-flow-scripts)")

    dest_setup = TREE / "designs" / args.pdk / args.design   # config.mk + SDC live here
    dest_rtl   = TREE / "designs" / "src"                     # design/ mirrors src/<srcdir>/

    print(f"[design_setup] pdk={args.pdk} design={args.design} config={args.config}")
    print(f"  source : {src.relative_to(ROOT)}")
    print(f"  -> {dest_setup.relative_to(ROOT)}   (config.mk + SDC)")
    print(f"  -> {dest_rtl.relative_to(ROOT)}/*   (RTL: {', '.join(p.name for p in design_dir.iterdir() if p.is_dir())})")

    if args.dry_run:
        print("  [dry-run] no files copied.")
        return 0

    dest_setup.mkdir(parents=True, exist_ok=True)
    dest_rtl.mkdir(parents=True, exist_ok=True)
    shutil.copytree(setup_dir,  dest_setup, dirs_exist_ok=True)
    shutil.copytree(design_dir, dest_rtl,   dirs_exist_ok=True)

    util, _, cp = args.config.partition("_")
    print(f"[design_setup] staged OK — {args.design} at util={util}%, CP={cp}ps "
          f"is ready in the ORFS tree.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
