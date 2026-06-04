#!/usr/bin/env python3
"""Phase-1B artifact: stuck-path follow-through table.

For every iteration N where the Selector named a top stuck_path, look at
iteration N+1's Planner decision and check whether the endpoint name (or
a distinctive cell-instance token from the Selector's guidance) appears
in the next iteration's plans/reasoning. When it does, record the pairing
so we can show the dialogue actually drove targeting.

Output: a markdown table sorted by actual ΔWNS achieved in iter N+1
(largest gains first). Filtering and tie-breaking are conservative so
the table contains strong cases worth quoting.
"""
import argparse
import csv
import json
import pathlib
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

ROOT = pathlib.Path(__file__).resolve().parents[3]
UTIL_SWEEPS  = ROOT / "util_sweeps" / "asap7"
CLOCK_SWEEP  = ROOT / "clock_sweep" / "orfs_fix" / "asap7"


def _find_variants() -> List[Tuple[str, str, pathlib.Path]]:
    out: List[Tuple[str, str, pathlib.Path]] = []
    for root in (UTIL_SWEEPS, CLOCK_SWEEP):
        if not root.exists():
            continue
        for design_dir in sorted(root.iterdir()):
            if not design_dir.is_dir():
                continue
            for vdir in sorted(design_dir.iterdir()):
                if not vdir.is_dir():
                    continue
                if not re.match(r"\d+_?\d*$", vdir.name):
                    # accept both '<util>_<cp>' and '<cp>'
                    continue
                tag = f"{design_dir.name}/{vdir.name}"
                # nested-design layout fallback
                if (vdir / "LLM_iterations").exists():
                    llm = vdir / "LLM_iterations"
                else:
                    sub = next((s for s in vdir.iterdir()
                                if s.is_dir() and (s / "LLM_iterations").exists()),
                               None)
                    if sub is None:
                        continue
                    llm = sub / "LLM_iterations"
                out.append((design_dir.name, tag, llm))
    return out


def _iter_dirs(llm_root: pathlib.Path):
    items = []
    for d in llm_root.iterdir():
        m = re.match(r"Iteration(\d+)$", d.name)
        if m and d.is_dir():
            items.append((int(m.group(1)), d))
    return sorted(items, key=lambda p: p[0])


def _planner_text(planner: Dict[str, Any]) -> str:
    """Concat all free-text fields the Planner emits for substring search."""
    chunks: List[str] = []
    chunks.extend(planner.get("rationale") or [])
    for p in planner.get("plans") or []:
        chunks.extend(p.get("reasoning") or [])
        chunks.append(p.get("expected_outcome") or "")
        if p.get("plan_type") == "eco":
            for ch in p.get("changes") or []:
                chunks.append(ch.get("reason") or "")
                chunks.append(ch.get("inst") or "")
                chunks.append(ch.get("net") or "")
    return " || ".join(str(c) for c in chunks if c)


def _tokens_from_guidance(strategy_note: Dict[str, Any]) -> List[str]:
    """Pick distinctive tokens to search for in next-iter Planner text.

    We try, in priority order:
    1. stuck_paths entries verbatim
    2. Cell instance IDs from guidance (patterns: _\d{4,}_ or HA[xa-z]+, etc.)
    3. Net names matching net\d{2,}
    """
    tokens: List[str] = []
    for sp in strategy_note.get("stuck_paths") or []:
        if isinstance(sp, str) and sp.strip():
            tokens.append(sp.strip())
    guidance = " ".join(strategy_note.get("guidance") or [])
    for pat in (r"_\d{4,}_", r"HA[A-Za-z]+\w*", r"net\d{3,}",
                r"place\d{3,}", r"BUFx\d+\w*", r"FA\w*",
                r"instr_addr_o\[\d+\]", r"if_stage_i\.\w+"):
        for m in re.findall(pat, guidance):
            if m not in tokens:
                tokens.append(m)
    return tokens


def _wns_delta_chosen(selector_next: Dict[str, Any]) -> Optional[float]:
    """Return the chosen plan's actual ΔWNS for iter N+1."""
    chosen = selector_next.get("chosen_plan")
    for pr in selector_next.get("plan_results") or []:
        if pr.get("plan") == chosen:
            return pr.get("delta_wns")
    return None


def _match_count(tokens: List[str], text: str) -> Tuple[int, List[str]]:
    matched = []
    for t in tokens:
        if t and t in text:
            matched.append(t)
    return len(matched), matched


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out",
                    default=str(ROOT / "stuck_path_followthrough.md"))
    ap.add_argument("--min-tokens", type=int, default=1,
                    help="Minimum number of guidance tokens that must appear "
                         "in iter N+1's text to count as a follow-through")
    ap.add_argument("--top", type=int, default=20,
                    help="Print the top N cases by actual ΔWNS gain")
    args = ap.parse_args()

    variants = _find_variants()
    print(f"Scanning {len(variants)} variants")

    candidates = []  # one entry per N→N+1 follow-through

    for design, tag, llm_root in variants:
        iters = _iter_dirs(llm_root)
        for idx, (n, idir) in enumerate(iters[:-1]):
            sj = idir / "selector_decision.json"
            next_n, next_idir = iters[idx + 1]
            if next_n != n + 1:
                continue
            pj_next = next_idir / "planner_decision.json"
            sj_next = next_idir / "selector_decision.json"
            if not (sj.exists() and pj_next.exists() and sj_next.exists()):
                continue
            try:
                sel = json.loads(sj.read_text())
                pln_next = json.loads(pj_next.read_text())
                sel_next = json.loads(sj_next.read_text())
            except (OSError, json.JSONDecodeError):
                continue

            strat = sel.get("strategy_note") or {}
            tokens = _tokens_from_guidance(strat)
            if not tokens:
                continue

            text = _planner_text(pln_next)
            n_matches, matched = _match_count(tokens, text)
            if n_matches < args.min_tokens:
                continue

            dwns = _wns_delta_chosen(sel_next)
            top_stuck = (strat.get("stuck_paths") or [""])[0]
            top_guid = (strat.get("guidance") or [""])[0]

            candidates.append({
                "tag": tag,
                "iter_from": n,
                "iter_to": n + 1,
                "stuck_path": top_stuck,
                "guidance_excerpt": top_guid[:280],
                "tokens_matched": matched[:4],
                "n_matches": n_matches,
                "next_chosen_seq": ",".join(
                    sel_next.get("chosen_sequence") or []),
                "actual_dwns": dwns,
                "plateau_diagnosis": strat.get("plateau_diagnosis", ""),
            })

    # Sort by gain, descending; ties broken by larger n_matches
    candidates.sort(key=lambda r: (
        r["actual_dwns"] if r["actual_dwns"] is not None else -1e9,
        r["n_matches"]
    ), reverse=True)

    out = pathlib.Path(args.out)
    with out.open("w") as fh:
        fh.write("# Stuck-path follow-through: Selector hint → next Planner action\n\n")
        fh.write(
            "For each row below, the Selector at iteration **N** named the "
            "named endpoint or cell as the dominant bottleneck. The next "
            "iteration's Planner text contains the same identifier(s); the "
            "rightmost column shows whether the iter N+1 chosen plan actually "
            "improved WNS, and by how much.\n\n"
        )
        fh.write(f"_Filter: ≥{args.min_tokens} guidance token(s) must appear "
                 f"verbatim in iter N+1 Planner text. Top {args.top} by ΔWNS gain._\n\n")
        fh.write("| # | Variant | N→N+1 | Plateau | Stuck path / cell | "
                 "Selector guidance (excerpt) | Tokens matched | "
                 "Iter N+1 sequence | Actual ΔWNS (ps) |\n")
        fh.write("|---|---------|-------|---------|-------------------|"
                 "------------------------------|----------------|"
                 "-------------------|-----------------|\n")
        for i, c in enumerate(candidates[: args.top], start=1):
            dwns_str = (f"{c['actual_dwns']:+.2f}"
                        if c['actual_dwns'] is not None else "—")
            stuck = (c['stuck_path'] or "—")[:50]
            guid = c['guidance_excerpt'].replace("|", "\\|").replace("\n", " ")
            toks = ", ".join(f"`{t}`" for t in c['tokens_matched'])
            seq = c['next_chosen_seq'] or "—"
            fh.write(f"| {i} | `{c['tag']}` | "
                     f"{c['iter_from']}→{c['iter_to']} | "
                     f"{c['plateau_diagnosis'] or '—'} | "
                     f"`{stuck}` | {guid}… | {toks} | "
                     f"`{seq}` | **{dwns_str}** |\n")

        fh.write("\n---\n\n")
        fh.write(f"Total follow-through pairs found: **{len(candidates)}**.\n")

    print(f"written: {out}")
    print(f"  total follow-through pairs: {len(candidates)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
