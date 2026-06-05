#!/usr/bin/env python3
"""Build cell_catalog.xml from a PDK's Liberty (.lib) files.

Run this ONCE when adding a new PDK. The output XML is committed to
platforms/<pdk>/cell_catalog.xml and consumed by the Reporter at runtime
to present the Planner with available cell sizes and sizeup targets.

Usage:
    python3 scripts/python/utils/build_cell_catalog.py \
        --lib-dir OpenROAD-flow-scripts/flow/platforms/<pdk>/lib/ \
        --pdk <pdk>                  # nangate45 | asap7
        --output platforms/<pdk>/cell_catalog.xml

How it works:
    1. Scan all .lib files in --lib-dir for cell definitions.
    2. For each cell name, apply the PDK's naming regex to extract
       (family, size, vt_tier).
    3. Group cells by family, collect unique sizes in ascending order.
    4. Write platforms/<pdk>/cell_catalog.xml.

Cell name conventions:
    nangate45:  FAMILY_Xn          e.g. AND2_X1, AND2_X4
    asap7:      FAMILYxS_ASAP7_75t_VT  e.g. AND2x1_ASAP7_75t_R, BUFx16f_ASAP7_75t_SL
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys
import xml.dom.minidom as minidom
import xml.etree.ElementTree as ET
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

# PDK cell-name regex patterns — match run.py pdk_configs exactly
PDK_PATTERNS = {
    "nangate45": re.compile(r"^([A-Z][A-Z0-9]*?)_(X\d+)$"),
    "asap7":     re.compile(r"^([A-Z][A-Za-z0-9]*?)(x[p\d]+[a-z]?)_ASAP7_75t_([A-Z]+)$"),
}

CELL_DEF_RE = re.compile(r'^\s*cell\s*\(\s*["\']?([A-Za-z0-9_]+)["\']?\s*\)')


def is_dont_use(cell_name: str, pdk: str) -> bool:
    """Exclude DONT_USE_CELLS so the catalog never lists forbidden cells as
    available sizing/VT targets. Mirrors the ORFS platform config.mk
    DONT_USE_CELLS globs (which the standalone OpenROAD process never sees)."""
    if pdk == "asap7":
        # config.mk: *x1p*_ASAP7*  *xp*_ASAP7*  SDF*  ICG*
        return ("x1p" in cell_name or "xp" in cell_name
                or cell_name.startswith("SDF") or cell_name.startswith("ICG"))
    return False


def parse_cells_from_lib(lib_path: pathlib.Path) -> List[str]:
    """Return every cell name found in a Liberty file (.lib or .lib.gz)."""
    names: List[str] = []
    try:
        if lib_path.suffix == ".gz":
            import gzip
            with gzip.open(lib_path, "rt", errors="replace") as fh:
                text = fh.read()
        else:
            text = lib_path.read_text(errors="replace")
        for line in text.splitlines():
            m = CELL_DEF_RE.match(line)
            if m:
                names.append(m.group(1))
    except OSError as e:
        print(f"[WARN] Could not read {lib_path}: {e}", file=sys.stderr)
    return names


def extract_family_size(cell_name: str, pdk: str) -> Optional[Tuple[str, str]]:
    """Return (family, size_key) or None if the cell name doesn't match."""
    pat = PDK_PATTERNS.get(pdk)
    if pat is None:
        print(f"[ERROR] Unknown PDK '{pdk}'. Supported: {list(PDK_PATTERNS)}", file=sys.stderr)
        sys.exit(1)
    m = pat.match(cell_name)
    if not m:
        return None
    family = m.group(1)
    size   = m.group(2)
    return family, size


def size_sort_key(size: str) -> float:
    """Sort sizes numerically. 'X1' < 'X2' < 'X4'. 'x1' < 'x1p5' < 'x2'."""
    s = size.lstrip("Xx")
    s = s.rstrip("abcdefghijklmnopqrstuvwxyz")
    s = s.replace("p", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def build_catalog(lib_dir: pathlib.Path, pdk: str) -> Dict[str, List[str]]:
    """Scan lib_dir, extract (family, size) pairs, return sorted catalog."""
    catalog: Dict[str, set] = defaultdict(set)
    lib_files = sorted(lib_dir.rglob("*.lib")) + sorted(lib_dir.rglob("*.lib.gz"))
    if not lib_files:
        print(f"[ERROR] No .lib/.lib.gz files found under {lib_dir}", file=sys.stderr)
        sys.exit(1)
    print(f"Scanning {len(lib_files)} liberty file(s) in {lib_dir}")
    n_skipped = 0
    for f in lib_files:
        for cell in parse_cells_from_lib(f):
            if is_dont_use(cell, pdk):
                n_skipped += 1
                continue
            result = extract_family_size(cell, pdk)
            if result:
                family, size = result
                catalog[family].add(size)
    print(f"Skipped {n_skipped} don't-use cell(s) (DONT_USE_CELLS filter)")

    return {
        family: sorted(sizes, key=size_sort_key)
        for family, sizes in sorted(catalog.items())
    }


def write_xml(catalog: Dict[str, List[str]], output: pathlib.Path, pdk: str) -> None:
    lib_name = {"nangate45": "Nangate45", "asap7": "ASAP7"}.get(pdk, pdk)
    root = ET.Element("library", name=lib_name)
    for family, sizes in catalog.items():
        cell_el = ET.SubElement(root, "cell", name=family)
        for size in sizes:
            ET.SubElement(cell_el, "size", name=size)

    raw = ET.tostring(root, encoding="unicode")
    pretty = minidom.parseString(raw).toprettyxml(indent="  ")
    lines = [l for l in pretty.splitlines() if l.strip()]
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines) + "\n")
    print(f"Written: {output}  ({len(catalog)} cell families)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lib-dir", required=True, type=pathlib.Path,
                    help="Directory containing PDK .lib files (searched recursively)")
    ap.add_argument("--pdk", required=True, choices=list(PDK_PATTERNS),
                    help="PDK name — determines cell-name regex")
    ap.add_argument("--output", required=True, type=pathlib.Path,
                    help="Output XML path, e.g. platforms/nangate45/cell_catalog.xml")
    args = ap.parse_args()

    catalog = build_catalog(args.lib_dir, args.pdk)
    write_xml(catalog, args.output, args.pdk)


if __name__ == "__main__":
    main()
