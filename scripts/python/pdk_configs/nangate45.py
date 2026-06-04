"""Nangate45 PDK configuration (FreePDK45 / Si2 Nangate Open Cell Library).

Single-VT PDK — no VT tiers. FAMILY_Xsize naming.
Points to PDK-specific artifacts under Platforms/nangate45/.
"""
from dataclasses import dataclass
from typing import List


@dataclass(frozen=True)
class _PDK:
    name: str
    lib_subdir: str
    setrc_tcl: str
    preamble_tcl: str
    cell_catalog_xml: str
    reference_md: str
    cell_name_regex: str
    vt_tiers: List[str]
    unsizable_cells: List[str]
    size_notation: str
    time_unit_ps: float
    # Global-route signal layer range + default track-capacity adjustment.
    # GRT state doesn't persist across write_db/read_db, so every standalone
    # TCL that calls global_route must re-apply these via env vars.
    min_routing_layer: str
    max_routing_layer: str
    routing_layer_adjustment: float


PDK = _PDK(
    name="nangate45",
    lib_subdir="",           # Nangate45 lib files live directly in lib/, no subdirectory
    setrc_tcl="setRC.tcl",
    preamble_tcl="Platforms/nangate45/preamble.tcl",
    cell_catalog_xml="Platforms/nangate45/cell_catalog.xml",
    reference_md="agents/nangate45.md",
    # Format: FAMILY + _ + Xn
    # Examples: NAND2_X1, AOI21_X4, INV_X16, BUF_X32
    # Size capture includes the 'X' prefix to match catalog XML keys ("X1", "X2", ...)
    # No VT suffix → third capture group is always empty
    cell_name_regex=r"([A-Z][A-Z0-9]*?)_(X\d+)()",
    vt_tiers=[],  # single VT only
    unsizable_cells=[],
    size_notation=(
        "_Xn where n is the drive strength multiplier (1, 2, 4, 8, 16, 32). "
        "Higher = stronger drive."
    ),
    time_unit_ps=1000.0,  # Nangate45 Liberty uses ns; multiply values by 1000 to get ps
    # Nangate45 platform defaults from OpenROAD-flow-scripts/flow/platforms/nangate45/config.mk
    min_routing_layer="metal2",
    max_routing_layer="metal10",
    routing_layer_adjustment=0.5,  # ORFS default from variables.yaml
)
