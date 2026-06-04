"""PDK-specific configuration bundles.

Each submodule (e.g., asap7, nangate45) exports a PDK dataclass with:
  name              : PDK identifier string (matches ORFS platforms/<name>/)
  lef_files         : list of LEF filenames (relative to platforms/<pdk>/lef/)
  lib_files         : list of Liberty filenames (relative to platforms/<pdk>/lib/<subdir>)
  lib_subdir        : subdirectory under lib/ (e.g. "NLDM" for asap7, "" for nangate45)
  setrc_tcl         : name of setRC.tcl (usually "setRC.tcl")
  cell_name_regex   : regex to extract (family, size, vt) from cell masters;
                      groups: 1=family, 2=size, 3=vt (may be "" for single-VT PDKs)
  vt_tiers          : list of VT tier names, fast→slow ("" if single-VT)
  unsizable_cells   : cell families that cannot be resized (e.g., asap7 HA, FA)
  size_notation     : human-readable description of the size format
  catalog_xml       : filename of the cell catalog XML at workspace root
"""
from .asap7 import PDK as ASAP7
from .nangate45 import PDK as NANGATE45

REGISTRY = {
    "asap7":     ASAP7,
    "nangate45": NANGATE45,
}


def get(name: str):
    if name not in REGISTRY:
        raise ValueError(
            f"Unknown PDK '{name}'. Supported: {', '.join(REGISTRY.keys())}"
        )
    return REGISTRY[name]
