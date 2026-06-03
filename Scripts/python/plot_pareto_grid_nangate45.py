#!/usr/bin/env python3
"""Pareto grid for the nangate45 clock-sweep designs.

Thin wrapper around plot_pareto_grid.py that retargets the CLOCK_SWEEP path
to clock_sweep/orfs_fix/nangate45 and defaults --designs to the four bare
names (gcd / aes / jpeg / ibex — no _w variants on nangate45).

Usage (all flags from plot_pareto_grid.py still work):
    python3 plot_pareto_grid_nangate45.py
    python3 plot_pareto_grid_nangate45.py --best-of-ranks
    python3 plot_pareto_grid_nangate45.py --vs-rank 2 --designs aes ibex

Outputs land in clock_sweep/orfs_fix/nangate45/ unless --out-dir is passed.
"""
import pathlib
import sys

# Retarget CLOCK_SWEEP to the nangate45 tree before plot_pareto_grid binds it.
import plot_pareto
plot_pareto.CLOCK_SWEEP = (
    pathlib.Path(__file__).resolve().parents[3]
    / "clock_sweep" / "orfs_fix" / "nangate45"
)

import plot_pareto_grid as pg  # noqa: E402  — must come after the patch
pg.CURRENT_PDK = "nangate45"


def main() -> int:
    if not any(a == "--designs" for a in sys.argv[1:]):
        sys.argv.extend(["--designs", "gcd", "aes", "jpeg", "ibex"])
    return pg.main()


if __name__ == "__main__":
    sys.exit(main())
