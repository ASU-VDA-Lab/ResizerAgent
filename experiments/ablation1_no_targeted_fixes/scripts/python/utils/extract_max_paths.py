#!/usr/bin/env python3
"""Parse a timing report and emit an enumerated list of violating paths.

Each path chain entry contains 7 fields:
  inst/pin  master  cell_dly_ps  net_dly_ps  slew_ps  cap_fF  net

  cell_dly = intrinsic gate delay (ps)
  net_dly  = wire RC delay from this output to the next cell's input (ps)
  slew     = output transition time (ps)
  cap      = output load capacitance (fF)
  net      = name of the net connecting this output to the next stage
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys
from typing import List, Optional

START_RE   = re.compile(r"^Startpoint:\s*(.+)")
END_RE     = re.compile(r"^Endpoint:\s*(.+)")
# OpenROAD format: "  -62.6577   slack (VIOLATED)" — number precedes the word "slack"
SLACK_RE   = re.compile(r"([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s+slack\b", re.IGNORECASE)
NUM_RE     = re.compile(r"([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)")
ARRIVAL_RE = re.compile(r"([+-]?\d+(?:\.\d+)?)\s+data arrival time", re.IGNORECASE)
REQUIRED_RE= re.compile(r"([+-]?\d+(?:\.\d+)?)\s+data required time", re.IGNORECASE)

# Output line: cap  slew  cell_delay  cumulative  edge  inst/pin  (master)
# Cap is in the leftmost numeric column (cols 1-8), so leading whitespace is short (1-8 chars).
OUTPUT_RE = re.compile(
    r"^\s{1,8}([\d.]+)\s+([\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([v^])\s+(\S+)\s+\(([^)]+)\)"
)

# Input line: slew  net_delay  cumulative  edge  inst/pin  (master)
# Cap column is blank, so leading whitespace is long (9+ chars).
INPUT_RE = re.compile(
    r"^\s{9,}([\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([v^])\s+(\S+)\s+\(([^)]+)\)"
)

# Net line: deeply indented net_name (net)
NET_RE = re.compile(r"^\s{20,}(\S+)\s+\(net\)\s*$")


def read_text(path: pathlib.Path) -> str:
    try:
        return path.read_text()
    except OSError as exc:
        raise SystemExit(f"Failed to read '{path}': {exc}") from exc


def split_sections(text: str) -> List[str]:
    sections: List[str] = []
    current: List[str] = []
    for line in text.splitlines():
        if START_RE.match(line) and current:
            sections.append("\n".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        sections.append("\n".join(current))
    return sections


def _is_clock_cell(name: str, master: str) -> bool:
    """Return True for clock-tree cells and ports that should be skipped."""
    # Plain clock port or input
    bare = name.lower()
    if bare in ("clk", "clock") or (bare.startswith("clk") and "/" not in bare):
        return True
    if "/" in name:
        inst = name.rsplit("/", 1)[0].lower()
        pin  = name.rsplit("/", 1)[1].lower()
        # Clock-tree buffers
        if inst.startswith("clkbuf_") or inst.startswith("clknet_"):
            return True
        # Clock input pins of any cell
        if pin in ("clk", "ck", "clock", "clkn"):
            return True
    # Anything the report flags as "(net)" or "(in)" type — handled upstream,
    # but guard against stray matches
    m_lower = master.lower()
    if m_lower in ("net", "in", "out"):
        return True
    return False


def _is_data_port(name: str, master: str) -> bool:
    """Return True for primary output ports (type 'out') — these are endpoints, not cells."""
    return master.lower() == "out"


def summarize(section: str) -> dict:
    """Extract per-stage timing info using a state machine over the report lines.

    State machine:
      OUTPUT line  → save as pending stage
      NET line     → save net name for pending stage
      INPUT line   → take net_delay, emit pending stage, clear pending
    """
    info = {
        "start":    "Unknown start",
        "end":      "Unknown end",
        "slack":    "?",
        "arrival":  None,
        "required": None,
        "block":    section,
        "chain":    [],
    }

    pending_output: Optional[dict] = None  # output line data waiting to be emitted
    pending_net:    Optional[str]  = None  # net name seen after the output line
    in_data_path = False                   # True once past the launch-FF Q output

    for line in section.splitlines():

        # ── header fields ────────────────────────────────────────────────────
        m = START_RE.match(line)
        if m:
            info["start"] = m.group(1).strip()
            continue

        m = END_RE.match(line)
        if m:
            info["end"] = m.group(1).strip()
            continue

        m = ARRIVAL_RE.search(line)
        if m:
            info["arrival"] = m.group(1)
            continue

        m = REQUIRED_RE.search(line)
        if m:
            info["required"] = m.group(1)
            continue

        m = SLACK_RE.search(line)
        if m and "data" not in line.lower():
            info["slack"] = m.group(1)
            continue

        # ── net line ─────────────────────────────────────────────────────────
        m = NET_RE.match(line)
        if m:
            pending_net = m.group(1)
            continue

        # ── output line (4 numeric columns) ──────────────────────────────────
        m = OUTPUT_RE.match(line)
        if m:
            cap, slew, cell_delay, _time, _edge, name, master = m.groups()

            if _is_clock_cell(name, master) or _is_data_port(name, master):
                continue

            # If there's a pending stage without a following input line yet,
            # emit it now with net_delay=0 (shouldn't normally happen mid-path).
            if pending_output is not None:
                _emit_stage(info["chain"], pending_output, pending_net)
            pending_output = {
                "name": name, "master": master,
                "cell_delay": cell_delay, "slew": slew, "cap": cap,
            }
            pending_net  = None
            in_data_path = True
            continue

        # ── input line (3 numeric columns) ───────────────────────────────────
        m = INPUT_RE.match(line)
        if m and in_data_path:
            _slew_in, net_delay, _time, _edge, name, master = m.groups()

            if pending_output is not None:
                _emit_stage(info["chain"], pending_output, pending_net, net_delay)
                pending_output = None
                pending_net    = None
            continue

    # Any remaining pending output (e.g. endpoint that had no following input)
    # is the capture pin itself — do not emit.

    return info


def _emit_stage(
    chain: list,
    output_data: dict,
    net_name: Optional[str],
    net_delay: str = "0.00",
) -> None:
    name   = output_data["name"]
    master = output_data["master"]

    # Split inst/pin
    if "/" in name:
        inst = name.rsplit("/", 1)[0]
        pin  = name.rsplit("/", 1)[1]
    else:
        inst = name
        pin  = ""

    try:
        cd = float(output_data["cell_delay"])
        nd = float(net_delay)
        sl = float(output_data["slew"])
        cp = float(output_data["cap"])
    except (ValueError, TypeError):
        return

    net = net_name if net_name else "?"
    entry = f"{inst}/{pin}  {master}  {cd:.2f}  {nd:.2f}  {sl:.2f}  {cp:.4f}  {net}"

    if not chain or chain[-1] != entry:
        chain.append(entry)


def format_paths(paths: List[dict], limit: Optional[int]) -> str:
    if limit is not None:
        paths = paths[:limit]
    if not paths:
        return "No violating paths found."
    blocks = []
    for idx, path in enumerate(paths, start=1):
        header = f"{idx}) {path['start']} -> {path['end']} (slack {path['slack']} ns)"
        chain_elems = path.get("chain") or []
        if chain_elems:
            chain_block = (
                "Path chain:\n"
                "  # inst/pin  master  cell_dly_ps  net_dly_ps  slew_ps  cap_fF  net\n  "
                + "\n  ".join(chain_elems)
            )
        else:
            chain_block = "Path chain: (unparsed)"
        footer = ""
        if path.get("arrival") or path.get("required"):
            footer = "\nData arrival time: {arr}\nData required time: {req}\nSlack: {slk}".format(
                arr=path.get("arrival", "?"),
                req=path.get("required", "?"),
                slk=path.get("slack", "?"),
            )
        blocks.append(f"{header}\n{chain_block}{footer}")
    return "\n\n".join(blocks)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract max violating paths from a timing report."
    )
    parser.add_argument(
        "report_path",
        type=pathlib.Path,
        help="Path to the timing report text file.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional number of paths to include.",
    )
    parser.add_argument(
        "--write-selected",
        type=pathlib.Path,
        help="If set, write only the selected (limited) path sections to this file.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.report_path.is_file():
        raise SystemExit(f"Report file '{args.report_path}' does not exist.")
    text = read_text(args.report_path)
    sections = split_sections(text)
    dedup = []
    seen = set()
    for section in sections:
        if not section.strip():
            continue
        info = summarize(section)
        key = (info["start"], info["end"], " ".join(info["chain"]))
        if key in seen:
            continue
        seen.add(key)
        dedup.append(info)
    paths = dedup

    if args.limit is not None:
        selected = paths[: args.limit]
    else:
        selected = paths

    if args.write_selected:
        args.write_selected.parent.mkdir(parents=True, exist_ok=True)
        selected_text = "\n\n".join(p["block"].strip() for p in selected if p.get("block"))
        args.write_selected.write_text(selected_text + "\n")

    print(format_paths(paths, args.limit))


if __name__ == "__main__":
    main()
