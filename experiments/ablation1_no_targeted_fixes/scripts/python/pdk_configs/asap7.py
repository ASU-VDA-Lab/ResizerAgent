"""ASAP7 PDK configuration.

Points to PDK-specific artifacts under platforms/asap7/ at the workspace root.
"""
from dataclasses import dataclass
from typing import List


@dataclass(frozen=True)
class _PDK:
    name: str
    lib_subdir: str          # subdirectory under platforms/<pdk>/lib/ (e.g. "NLDM")
    setrc_tcl: str           # name of setRC.tcl inside the ORFS platform dir
    preamble_tcl: str        # path (relative to workspace) to the PDK's LEF/LIB TCL preamble
    cell_catalog_xml: str    # path (relative to workspace) to the cell catalog XML
    cell_name_regex: str     # regex: group 1=family, 2=size, 3=vt (may be "")
    vt_tiers: List[str]      # fast → slow; empty list means single-VT PDK
    unsizable_cells: List[str]
    size_notation: str       # human-readable description of size format
    time_unit_ps: float      # multiplier to convert the PDK's native time unit → ps
                             # asap7 reports in ps (1.0), nangate45 reports in ns (1000.0)
    # Global-route signal layer range + default track-capacity adjustment.
    # GRT state doesn't persist across write_db/read_db, so every standalone
    # TCL that calls global_route must re-apply these via env vars.
    min_routing_layer: str
    max_routing_layer: str
    routing_layer_adjustment: float


PDK = _PDK(
    name="asap7",
    lib_subdir="NLDM",
    setrc_tcl="setRC.tcl",
    preamble_tcl="platforms/asap7/preamble.tcl",
    cell_catalog_xml="platforms/asap7/cell_catalog.xml",
    # Format: FAMILY + xSIZE[suffix] + _ASAP7_75t_ + VT
    # Examples: BUFx16f_ASAP7_75t_R, HAxp5_ASAP7_75t_SL, NAND2x1p5_ASAP7_75t_L
    cell_name_regex=r"([A-Z][A-Za-z0-9]*?)(x[p\d]+[a-z]?)_ASAP7_75t_([A-Z]+)",
    vt_tiers=["SL", "L", "R"],  # fast → slow (SLVT → LVT → RVT)
    unsizable_cells=["HA", "FA"],
    size_notation=(
        "xN or xNpF or xNpFsuffix (e.g. x1, xp5, x1p5, x16f). "
        "'xp5' = 0.5x, 'x1p5' = 1.5x. Trailing lowercase letter (f) is a variant tag."
    ),
    time_unit_ps=1.0,  # asap7 reports timing in ps directly
    # ASAP7 platform defaults from OpenROAD-flow-scripts/flow/platforms/asap7/config.mk
    min_routing_layer="M2",
    max_routing_layer="M7",
    routing_layer_adjustment=0.5,  # ORFS default from variables.yaml
)
