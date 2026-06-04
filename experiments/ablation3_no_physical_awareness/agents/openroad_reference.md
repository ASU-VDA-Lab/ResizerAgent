# OpenROAD repair_timing Reference

This is a shared reference file. All agents in this framework must read it
before proposing repair sequences, writing TCL scripts, or interpreting
timing repair results.

---

## repair_timing Internal Algorithm

Understanding how `repair_timing` works internally is essential for choosing
the right sequence and knobs.

1. All endpoints with negative setup slack are collected and sorted worst-to-best.
2. For each endpoint, the tool repeatedly finds the current worst path to that
   endpoint and iterates over drivers along the path — sorted by **largest load
   delay first**.
3. For each driver, moves in the **sequence** are tried in order. The **first
   move that improves slack** is committed immediately. Subsequent moves in the
   sequence are never tried on that driver in that pass.
4. After each pass, parasitics and timing are recomputed from scratch. Moves
   that worsen timing or make no progress are rolled back via the ODB journal.
5. The tool exits when: the endpoint meets the slack margin, `max_passes` is
   exhausted, `max_iterations` is exhausted, or area/utilization limits are hit.
6. A **last-gasp** phase runs at the end — stricter, requiring both WNS and TNS
   to improve simultaneously before committing any move.

**Key implication for sequence design:** Sequence order is critical. If `sizeup`
is first and it helps a driver, `buffer` is never tried on that driver — even
if buffer would have been a better long-term move. Put the move most broadly
applicable to this design's bottleneck type first.

---

## repair_timing Knobs

| Flag | Default | Effect |
|------|---------|--------|
| `-setup` | — | Always set — this flow is setup repair only |
| `-sequence {m1 m2 ...}` | all moves | Ordered move list tried per driver |
| `-repair_tns <0–100>` | 100 | % of violating endpoints to attempt; `0` = worst endpoint only, `100` = all |
| `-max_passes <N>` | 10000 | Per-endpoint pass limit — max repair passes on each individual endpoint |
| `-max_iterations <N>` | unlimited | Global budget — total repair operations across ALL endpoints; once hit, repair_timing stops entirely |
| `-setup_margin <ps>` | 0 | Additional slack target beyond 0; tool stops when slack ≥ margin |
| `-skip_last_gasp` | false | Skip the final greedy optimization phase (VT swap + sizeup_match + sizeup + pin_swap) |
| `-skip_crit_vt_swap` | false | Skip critical-path VT swap to fastest variant (runs after last gasp) |

### What each knob controls

**`-repair_tns <0–100>`** — Breadth control. Determines what fraction of violating
endpoints (sorted worst to best) the tool will attempt. `100` works all of them;
`0` works only the single worst endpoint; values in between work a proportional
subset. Affects which endpoints are touched, not how hard each is worked.

**`-max_passes <N>`** — Per-endpoint depth cap. Maximum number of repair passes
the tool will run on any single endpoint. Each pass re-evaluates the current worst
path to the endpoint and attempts one round of driver fixes. Capped at 10000 by default.

**`-max_iterations <N>`** — Global work cap. Limits the total number of repair
operations across all endpoints combined. Unlimited by default. Endpoints are
processed in sorted order; if the cap is hit, remaining endpoints are left unrepaired.


**`-setup_margin <ps>`** — Slack target. The tool considers an endpoint repaired
when its slack meets or exceeds this margin. Default 0 means slack ≥ 0 is the
stopping condition. Positive values set a stricter stopping condition.

**`-skip_last_gasp`** — Skips the final greedy optimization phase that runs after
the main per-endpoint loop. Last gasp tries VT swap, sizeup_match, sizeup, and
pin_swap in up to 10 passes on remaining violating endpoints. In staged plans,
set this to true for all stages except the last — you want the intermediate stages
to apply only their designated move without the finishing passes interfering.

**`-skip_crit_vt_swap`** — Skips the critical-path VT swap phase that runs after
last gasp. This phase swaps the most critical cells on the worst path to the
fastest VT variant. Like last gasp, skip it in all staged plan stages except the
last — let the final stage handle VT finishing.

### Staged repair

A staged plan runs `repair_timing` multiple times in sequence, each with exactly
**one move**. This isolates each move type so its effect can be measured
independently. `detailed_placement` and `estimate_parasitics -placement` run
between each stage. All stages except the last MUST use `-skip_last_gasp` and
`-skip_crit_vt_swap` so that intermediate stages only apply their designated move.
The final stage performs the finishing passes. Minimum 2 stages, maximum 9.

---

## Available Moves (the 9 valid sequence moves)

| Move | Effect |
|------|--------|
| `sizeup` | Upsize to a larger drive-strength variant — reduces output resistance, improves slew on fanout |
| `sizedown` | Downsize — recovers area/power after aggressive sizing |
| `vt_swap` | Swap to a lower-Vt variant (faster, higher leakage) — direction is always R→L→SL |
| `buffer` | Insert a repeater to split a long net — reduces RC delay |
| `split` | Duplicate a high-fanout driver — reduces load per driver copy |
| `unbuffer` | Remove a redundant buffer — reduces wire delay when the buffer itself is the bottleneck |
| `clone` | Clone a gate to serve a subset of its fanout — reduces load capacitance per clone |
| `sizeup_match` | Upsize to match the footprint of a neighbor cell — more placement-friendly than unconstrained sizeup |
| `swap` | Swap to a logically equivalent cell with better timing characteristics |

Each move may appear **at most once** within a single sequence. Max sequence length: 9 moves.
In a staged plan, each stage uses exactly one move. The same move should not repeat across stages.

---

## Utilization Headroom

| Utilization | Cell insertion safety |
|-------------|----------------------|
| < 70% | Safe — buffer, clone, split freely |
| 70–80% | Moderate — prefer resizing over insertion |
| 80–85% | Tight — prioritize `sizeup`/`swap` over `buffer`/`clone` |
| > 85% | Critical — avoid insertion; in-place moves only (`sizeup`, `vt_swap`, `swap`) |

---

## Violation Severity

| `|WNS| / clock_period` | Severity | Interpretation |
|------------------------|----------|----------------|
| < 5% | Mild | A few targeted moves likely sufficient |
| 5–15% | Moderate | Careful sequence selection required |
| 15–30% | Severe | Multiple iterations likely needed |
| > 30% | Critical | May be structurally limited; set expectations accordingly |

---

## Docker / Environment Constants

| Item | Value |
|------|-------|
| Docker image | `openroad/flow-ubuntu22.04-builder:0b569c` |
| OpenROAD binary (inside Docker) | `/OpenROAD-flow-scripts/tools/install/OpenROAD/bin/openroad` |
| Workspace mount point | Host `cli_based_flow/4-agent-flow/` → `/workspace` inside Docker |
| Host root | `/home/atmadipd/new_openroad/context_scripts/cli_based_flow/4-agent-flow` |
| LEF files | `/workspace/OpenROAD-flow-scripts/flow/platforms/asap7/lef/` |
| Liberty files | `/workspace/OpenROAD-flow-scripts/flow/platforms/asap7/lib/NLDM/` |
| setRC TCL | `/workspace/OpenROAD-flow-scripts/flow/platforms/asap7/setRC.tcl` |

All host paths passed as Docker environment variables must be converted from
the host root to `/workspace/...` form before being passed into the container.
`STAGE_TAG` is a plain string label — do not convert it to a Docker path.
