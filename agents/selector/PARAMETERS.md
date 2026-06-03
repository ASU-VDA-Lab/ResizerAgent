# Selector Parameters

Tunable thresholds used by the Selector for plateau detection and backtracking.
Edit values here; `AGENTS.md` defers to this file.

## Plateau Detection

| Parameter | Value | Meaning |
|-----------|-------|---------|
| `soft_plateau_delta_ps` | `3` | `|ΔWNS|` ceiling (ps) for a soft plateau |
| `hard_plateau_delta_ps` | `1` | `|ΔWNS|` ceiling (ps) for a hard plateau |
| `plateau_window_iters`  | `2` | Number of consecutive iterations the ΔWNS must stay within the ceiling for a plateau to be declared |

## Backtrack Policy

| Parameter | Value | Meaning |
|-----------|-------|---------|
| `backtrack_trigger_window` | `2` | Iterations of sustained plateau before escalating from `retry` to `backtrack` |
| `plateau_tail_delta_ps`    | `3` | Max `|ΔWNS|` that still counts as part of the plateau tail when locating the plateau's origin |
| `regression_budget_ps`     | `5` | Max allowed WNS regression when accepting `accept_regression` |

## Stop Conditions

| Parameter | Value | Meaning |
|-----------|-------|---------|
| `closure_wns_ps` | `0` | WNS ≥ this value triggers `stop_reason: "closure_achieved"` |

## Change Log

- 2026-04-20: `plateau_window_iters` and `backtrack_trigger_window` raised from `2` → `4`.
- 2026-04-21: reverted `plateau_window_iters` and `backtrack_trigger_window` back to `2`.
