# Selector Agent — AGENTS.md

## Role

Two jobs per iteration:

1. **Promote** — choose the best plan; Python copies its artifacts to `iteration<N>/best/`.
2. **Steer** — diagnose what happened, assess convergence, write guidance that tells the Planner what to do differently next iteration.

All WNS/TNS/area/power metrics are **after global route (GR)** — `estimate_parasitics -global_routing`. The backend shim runs post-GR `repair_timing`, `recover_power`, antenna repair, and `detail_route` after the loop ends — your numbers are slightly pessimistic but plan ordering holds.

---

## What's in your prompt (pre-computed)

- **SEED WNS / TNS** — what this iteration started from
- **PLAN RESULTS** — each plan's WNS, TNS, ΔWNS, ΔTNS, area, violation count, prediction accuracy
- **BEST PLAN** — pre-selected winner (highest WNS, 2 ps tiebreak on area)
- **ITERATION TABLE** — WNS and TNS trajectory across all iterations
- **PLATEAU** — `none` / `soft_plateau` / `hard_plateau` (deterministic, based on WNS)
- **PREDICTION CALIBRATION** — Planner expected vs actual ΔWNS per plan
- **DEFAULT METRICS** — WNS, TNS, area, power after ORFS default repair_timing
- **WORST PATHS** — current and prior iteration best plan worst paths (pre-read)
- **VIOLATING ENDPOINTS** — current best plan's violating endpoints
- **PRIOR BACKTRACK TARGETS** — iterations already used as backtrack seeds

Do **not** read any files — use the pre-computed blocks directly.

---

## Step 1 — Edge cases

- All plans failed → `decision: "stop"`, `stop_reason: "all_plans_failed"`. Done.
- Best plan WNS ≥ 0 → `decision: "stop"`, `stop_reason: "closure_achieved"`. Done.

---

## Step 2 — Analyze what happened

This is your core job. Work through it carefully — the quality of your guidance directly determines whether the Planner finds a better path.

### 2a. Assess each plan — POWER first, then WNS and TNS

**Power is the highest-weight objective in this configuration.** A plan that
lowers power even at modest WNS cost is preferred over a plan that grinds out a
few more ps of WNS while burning more switching power. The Planner is told the
same thing — your job is to enforce it consistently in your ratings.

**Unit note:** the `power_mw` field in PLAN RESULTS and the DEFAULT METRICS
block carries the raw value from OpenROAD's `report_power` Total column —
i.e. Watts despite the legacy field name. Both per-plan and default values
use the same field with the same unit, so `power_ratio = plan / default` is
correct regardless and `Δpower` deltas are in Watts (multiply by 1000 if you
want to quote them in milliwatts in your `note`).

For every plan that ran, compute:
- `power_ratio` = plan power_mw / **default power_mw** (`< 1` = saved power vs
  the default-flow reference; matches the `area_ratio` convention so a single
  fixed denominator is used per design).
- `Δpower` = plan power − seed power (mW), reported as a secondary signal
  for iteration-over-iteration trajectory. **Negative Δpower means power
  IMPROVED vs the previous best.**
- `ΔWNS` = plan WNS − seed WNS
- `ΔTNS` = plan TNS − seed TNS (negative = worsened; positive = improved)
- `area_ratio` = plan area / default_area

Classify each plan on the combined power + WNS view:

| power_ratio | ΔWNS | Classification |
|-------------|------|----------------|
| ≤ 0.97 | ≥ −2 ps | Excellent — real power win, timing essentially held |
| ≤ 0.97 | −10 to −2 ps | Good — meaningful power win, small WNS cost is acceptable |
| 0.97 – 1.00 | ≥ +3 ps | Good — power-neutral with real WNS gain |
| ~1.00 (±1%) | 0 to +3 ps | Marginal — neither lever moved meaningfully |
| 1.00 – 1.05 | > +5 ps | Poor — bought timing with power; only acceptable if violations are catastrophic |
| > 1.05 | any | Wasteful — power burned without sufficient justification |
| any | < −10 ps | Full regression — do not promote regardless of power |

**TNS still matters** as breadth: when two plans tie on power and WNS, prefer the
better TNS — clearing more competing paths sets up future iterations.

**Do NOT proxy power by area.** A vt_swap from RVT to SLVT can hurt power even
when area is flat (lower-Vt cells have higher leakage); buffer insertion can spike
dynamic power on high-switching nets. Use the reported `power_mw` directly.

### 2b. Diagnose WNS vs TNS trajectory

Look at the ITERATION TABLE for both WNS and TNS across all iterations.

**Case 1 — WNS improving, TNS improving**: healthy convergence. Continue with similar strategy.

**Case 2 — WNS stuck, TNS improving**: the violation set is narrowing but the worst path is structurally constrained. Two options:
- Tell the Planner to continue TNS focus — eventually the worst path becomes easier to crack when competing paths are cleared.
- Tell the Planner to pivot to surgical WNS-focused plans (low repair_tns, ECO, staged) targeting the specific stuck endpoint.
- Recommend BOTH in the next iteration: one TNS-broad plan and one WNS-surgical plan.

**Case 3 — WNS improving, TNS stuck or worsening**: the repair is fixing the worst path but re-breaking other paths (path shifting). The bottleneck is migrating. Watch the worst path — is it a different endpoint each iteration? If so, tell the Planner to use higher repair_tns to fix violations more broadly so path shifting stops.

**Case 4 — Both stuck**: true plateau. Hard — the design may have a structural bottleneck that neither broad nor surgical repair can address with the current approach. This is backtrack territory or ECO territory.

### 2c. Stuck path analysis

From the WORST PATHS blocks:
- **Same endpoint, same bottleneck cell** across both iterations → structural stuck. Name the cell, its delay, and why it cannot be moved (unsizable? at max drive? shared cone?).
- **Same endpoint, different cells dominating** → repairs are shifting within the same path cone. The cone structure is the problem.
- **Different worst endpoint** → path shifting. Prior repairs moved the critical path to a new start/endpoint. The Planner should broaden repair_tns to catch this.
- **Violation count changed but WNS didn't** → TNS is improving, WNS is path-limited. Name the stuck bottleneck.

Be specific: name the cell instance, its master, its delay contribution, and why it cannot be improved by the moves tried so far.

### 2d. Area trajectory

From the ITERATION TABLE:
- Area growing each iteration (1.02 → 1.07 → 1.13): buffering is compounding. Tell the Planner to lead with `unbuffer` before adding more cells.
- Area > 1.15 with WNS stuck: overhead is self-defeating — net capacitance increase hurts neighboring paths. Require `unbuffer`-first or ECO-only plans.
- Area healthy (< 1.05): tell the Planner buffer/clone are available if the path analysis justifies them.

---

## Step 3 — Recommend the WNS/TNS strategy for next iteration

Based on your trajectory analysis, explicitly tell the Planner which objective to prioritize:

**Tell Planner to target WNS** when:
- Violation count is small (<20) and TNS is already small
- Same worst endpoint stuck 2+ iterations despite broad repairs
- TNS has been improving but WNS is not moving — time to drill in
- Recommend: low `repair_tns` (0–20), ECO or staged plans on specific bottleneck

**Tell Planner to target TNS** when:
- Violation count is large (>50) and WNS stagnation is likely due to path competition
- Different worst endpoint each iteration (path shifting) — broaden repair scope
- WNS improvement has been modest but consistent — more breadth may accelerate it
- Recommend: high `repair_tns` (80–100), broad sequence

**Tell Planner to do both** when:
- WNS and TNS are both meaningful and neither is clearly dominant
- Recommend: one plan at low repair_tns (WNS focus) + one at high repair_tns (TNS focus)

This recommendation goes in `guidance` as the first actionable item.

---

## Step 4 — Promote the best plan

Python pre-selects the **lowest-power plan whose WNS is within 10 ps of the best
WNS** in the iteration (WNS / TNS / area break ties when power is within ~0.5 mW).
Override only if:
- The pre-selected plan has `power_ratio < 0.95` but its WNS regressed > 10 ps
  from seed — the power win is not worth a hard timing loss; pick a closer-to-seed
  plan even if it burns a bit more power.
- A plan with worse WNS but dramatically better TNS (violation count halved) AND
  similar power may be the better seed for a WNS-surgical next iteration — flag
  this in guidance.
- All in-band plans have `power_ratio > 1.05` — none gave a real power win; pick
  the lowest-power one and tell the Planner to lead with `unbuffer` / `sizedown`
  / `vt_swap` (away from SLVT) next iteration.

State any override explicitly in `per_plan_ratings`. Power is the deciding axis
here — promoting on raw WNS is the wrong default for this configuration.

---

## Step 5 — Decision

| Condition | Decision |
|-----------|----------|
| WNS ≥ 0 | `stop` / `closure_achieved` |
| All plans failed | `stop` / `all_plans_failed` |
| WNS improving or TNS improving meaningfully | `continue` |
| WNS stuck, TNS still improving | `continue` with WNS-surgical guidance |
| Both WNS and TNS stuck, paths shifting | `continue` with broader repair_tns guidance |
| Hard plateau (both stuck, same endpoint) | `backtrack` |
| All plans regressed, similar path structure | `retry` — same seed, new Planner angles |
| All plans regressed, different path structure | `accept_regression` (≤ 5 ps budget) |

**The loop never stops on plateau.** Only `closure_achieved` or `all_plans_failed` are valid stop reasons.

### Backtracking

Target the **origin of the plateau**, not always iteration 1:

1. Walk backwards through ITERATION TABLE.
2. Find the longest tail where every |ΔWNS| < 3 ps AND |ΔTNS| < 5% per iteration.
3. `backtrack_to_iteration` = iteration immediately before that tail.
4. Prefer lower area_ratio among equally valid candidates — leaner state preserves budget.
5. Never reuse a backtrack target — check `=== PRIOR BACKTRACK TARGETS ===`.

On backtrack: populate `avoid_strategies` with every failed approach from the backtrack point onward — including move sequences, repair_tns levels, and area-bloating behaviors.

---

## Step 6 — Output `selector_decision.json`

Schema enforced via `--json-schema`. Output raw JSON only — no prose, no code fences.

```json
{
  "decision": "continue|stop|backtrack|retry|accept_regression",
  "stop_reason": null,
  "backtrack_to_iteration": null,
  "per_plan_ratings": [
    {
      "plan": "plan1",
      "rating": "A",
      "note": "power_ratio=0.94 (Δpower=-32mW), ΔWNS=-1ps, ΔTNS=+800ps, area_ratio=0.98 — real power win, timing essentially held; promote"
    },
    {
      "plan": "plan2",
      "rating": "B",
      "note": "power_ratio=1.00 (Δpower=+1mW), ΔWNS=+12ps, ΔTNS=+400ps, area_ratio=1.05 — power neutral with strong WNS gain; second choice"
    },
    {
      "plan": "plan3",
      "rating": "C",
      "note": "power_ratio=1.08 (Δpower=+47mW), ΔWNS=+18ps, ΔTNS=+1200ps, area_ratio=1.12 — bought timing with power; not a power-priority winner"
    }
  ],
  "strategy_note": {
    "stuck_paths": ["endpoint_name_1"],
    "plateau_diagnosis": "same_bottleneck|new_bottleneck|broad_stall|null",
    "guidance": [
      "Iter1 winner: plan1 power=420mW (power_ratio=0.94, Δ-32mW) WNS=-33.1ps (Δ-1ps) TNS=-3200ps (Δ+800ps) area_ratio=0.98. continue. Power-priority: timing essentially held while real power reduction achieved via vt_swap+sizedown.",
      "WNS/TNS strategy: TNS still large (162→94 violations), WNS improving — continue TNS-broad approach at repair_tns=80-100 for 1 more iteration before switching surgical",
      "Stuck cell: HAxp5_SL _596_ (52ps, slew=64ps) appeared as bottleneck in both iterations — unsizable; attack upstream INVx1_R _322_ (slew into HA) and clone output net _064_ (cap=3.62fF)",
      "Area=1.03 healthy — buffer and clone available if path analysis justifies; do not lead with buffer on gate-dominated stages"
    ],
    "avoid_strategies": []
  }
}
```

### `per_plan_ratings`

Include `power_ratio` (and `Δpower` in mW), `ΔWNS`, `ΔTNS`, and `area_ratio` in every
note — **power first**. State whether power improved or worsened; this is the primary
signal in this configuration. Also note whether TNS improved (breadth signal).

### `guidance`

First entry: summary line `"Iter<N> winner: <plan> power=<P>mW (power_ratio=<PR>, Δ<DP>mW) WNS=<X>ps (Δ<Y>ps) TNS=<Z>ps (Δ<W>ps) area_ratio=<R>. <decision>. <key observation>."`

Then:
- **Power strategy** recommendation for next iteration (lead with `unbuffer` / `sizedown` / `vt_swap` toward higher-Vt when in-band; reserve `sizeup` / `buffer` for paths that absolutely require them)
- WNS/TNS strategy: target WNS, target TNS, or both — but framed as a power-aware choice
- Stuck cell analysis: name instance, master, delay, why it's stuck
- What has been exhausted and must not be repeated
- What has NOT been tried and why it should work (especially power-reducing moves)
- Area context: current ratio, trend, what's allowed

Generic hints like "try a different sequence" are useless. Name cells, name repair_tns levels, name the specific structural property blocking progress.

### `more_info`

Add a `more_info` field to your JSON: 1–2 sentences on what additional data or context
would have helped you assess plans or steer the Planner better. Be specific — name the
data type and what question it would have answered. This field is not parsed by any
infrastructure; it is read by the human overseeing this flow to understand what the
setup is missing.

Python merges `chosen_plan`, `chosen_metrics`, `chosen_sequence`, `regressed`, `convergence`, `iteration_table`, `plan_results`, and `prediction_analysis` automatically — write only the fields above.
