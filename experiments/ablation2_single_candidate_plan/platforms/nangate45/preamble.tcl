# Nangate45 preamble — LEF + Liberty loading.
# Sourced by main TCL scripts via `source $env(PDK_PREAMBLE)`.
# Requires env vars: LEF_DIR, LIB_DIR
#
# Single-VT PDK (no VT tier variants), single typical corner.

# LEF (tech + unified macro LEF)
read_lef [file join $::env(LEF_DIR) NangateOpenCellLibrary.tech.lef]
read_lef [file join $::env(LEF_DIR) NangateOpenCellLibrary.macro.lef]

# Liberty (single typical-corner library)
read_liberty [file join $::env(LIB_DIR) NangateOpenCellLibrary_typical.lib]
