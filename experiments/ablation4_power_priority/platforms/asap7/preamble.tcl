# ASAP7 preamble — LEF + Liberty loading.
# Sourced by main TCL scripts via `source $env(PDK_PREAMBLE)`.
# Requires env vars: LEF_DIR, LIB_DIR

# LEF (tech + 3 VT-separated standard cell LEFs)
read_lef [file join $::env(LEF_DIR) asap7_tech_1x_201209.lef]
read_lef [file join $::env(LEF_DIR) asap7sc7p5t_28_L_1x_220121a.lef]
read_lef [file join $::env(LEF_DIR) asap7sc7p5t_28_R_1x_220121a.lef]
read_lef [file join $::env(LEF_DIR) asap7sc7p5t_28_SL_1x_220121a.lef]

# Liberty (NLDM, FF corner; .lib.gz supported natively by OpenROAD)
read_liberty [file join $::env(LIB_DIR) asap7sc7p5t_AO_LVT_FF_nldm_211120.lib.gz]
read_liberty [file join $::env(LIB_DIR) asap7sc7p5t_AO_RVT_FF_nldm_211120.lib.gz]
read_liberty [file join $::env(LIB_DIR) asap7sc7p5t_AO_SLVT_FF_nldm_211120.lib.gz]
read_liberty [file join $::env(LIB_DIR) asap7sc7p5t_INVBUF_LVT_FF_nldm_220122.lib.gz]
read_liberty [file join $::env(LIB_DIR) asap7sc7p5t_INVBUF_RVT_FF_nldm_220122.lib.gz]
read_liberty [file join $::env(LIB_DIR) asap7sc7p5t_INVBUF_SLVT_FF_nldm_220122.lib.gz]
read_liberty [file join $::env(LIB_DIR) asap7sc7p5t_OA_LVT_FF_nldm_211120.lib.gz]
read_liberty [file join $::env(LIB_DIR) asap7sc7p5t_OA_RVT_FF_nldm_211120.lib.gz]
read_liberty [file join $::env(LIB_DIR) asap7sc7p5t_OA_SLVT_FF_nldm_211120.lib.gz]
read_liberty [file join $::env(LIB_DIR) asap7sc7p5t_SEQ_LVT_FF_nldm_220123.lib]
read_liberty [file join $::env(LIB_DIR) asap7sc7p5t_SEQ_RVT_FF_nldm_220123.lib]
read_liberty [file join $::env(LIB_DIR) asap7sc7p5t_SEQ_SLVT_FF_nldm_220123.lib]
read_liberty [file join $::env(LIB_DIR) asap7sc7p5t_SIMPLE_LVT_FF_nldm_211120.lib.gz]
read_liberty [file join $::env(LIB_DIR) asap7sc7p5t_SIMPLE_RVT_FF_nldm_211120.lib.gz]
read_liberty [file join $::env(LIB_DIR) asap7sc7p5t_SIMPLE_SLVT_FF_nldm_211120.lib.gz]
