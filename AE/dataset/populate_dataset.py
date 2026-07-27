#!/usr/bin/env python3
"""
populate_dataset.py — Build AE/dataset/ from the ORFS base designs + the
paper's configuration table (ASAP7 and NanGate45).

Layout produced:
    AE/dataset/<pdk>/<design>/<design>_<util>_<cp>/
        design/   <- RTL source, mirrors designs/src/<srcdir>/ (e.g. ibex -> ibex_sv)
        setup/    <- config.mk + SDC (copy of .../designs/<pdk>/<design>/, with
                     CORE_UTILIZATION set to <util> and the clock period set to <cp>)

Only paper configurations are generated (see CONFIGS).

Clock-period format follows ORFS exactly:
    * ASAP7  SDCs express `set clk_period` in PICOSECONDS  -> value = <cp>
    * NanGate45 SDCs express it in NANOSECONDS             -> value = <cp>/1000
      formatted as a clean decimal (620->0.62, 1000->1.0, 2100->2.1).
"""
from __future__ import annotations
import pathlib
import re
import shutil
from decimal import Decimal

ROOT    = pathlib.Path(__file__).resolve().parent.parent.parent      # ResizerAgent/
ORFS    = ROOT / "openroad-flow-scripts" / "flow"
DESIGNS = ORFS / "designs"
DATASET = ROOT / "AE" / "dataset"

# ---- paper configuration points (source of truth) -------------------------
# Every (util, cp) point the paper evaluates, grouped by the experiment that
# introduces it. Design files are experiment-independent inputs, so each unique
# point is stored ONCE (the experiment is a run-time choice, not a dataset
# property). Sources:
#   util     -> utilization sweep       (Table 3 ASAP7 / Table 9 NanGate45)
#   clock    -> clock-period sweep       (Fig 2 ASAP7 / Fig 3 NanGate45)
#   ablation -> ablation studies         (Table 4, ASAP7 only)
# Lexicographic (Table 7) reuses Table 3 points and AutoTuner runs the same
# sweep points as RA, so neither adds new configs.
_SWEEPS = {
    "asap7": {
        "util":     {"aes":  [(60, 240), (65, 240), (70, 240)],
                     "ibex": [(60, 750), (70, 750), (75, 750)],
                     "jpeg": [(60, 430), (70, 430), (80, 430)]},
        "clock":    {"ibex": [(70, cp) for cp in (550, 600, 650, 700, 750, 800)],
                     "jpeg": [(70, cp) for cp in (370, 390, 410, 430, 450, 470)]},
        "ablation": {"aes":  [(65, 160)], "jpeg": [(70, 330)], "ibex": [(40, 500)]},
    },
    "nangate45": {
        "util":     {"aes":  [(40, 620), (45, 620), (50, 620)],
                     "ibex": [(50, 2100), (60, 2100), (70, 2100)],
                     "jpeg": [(50, 1000), (60, 1000), (70, 1000)]},
        "clock":    {"ibex": [(50, cp) for cp in (1600, 1700, 1800, 1900, 2000, 2100)]},
    },
}

# Flatten to a de-duplicated per-(pdk, design) list of unique config points.
CONFIGS: dict[str, dict[str, list]] = {}
for _pdk, _groups in _SWEEPS.items():
    _designs: dict[str, list] = {}
    for _group in _groups.values():
        for _design, _pairs in _group.items():
            _u = _designs.setdefault(_design, [])
            for _p in _pairs:
                if _p not in _u:
                    _u.append(_p)
    CONFIGS[_pdk] = _designs


# ---- config.mk / SDC patch helpers ----------------------------------------
def active_sdc_name(config_text: str) -> str:
    names = re.findall(r"SDC_FILE\s*\??=.*?/([^/\s]+\.sdc)", config_text)
    return names[-1] if names else "constraint.sdc"


def design_nickname(config_text: str, fallback: str) -> str:
    m = re.search(r"export\s+DESIGN_NICKNAME\s*\??=\s*(\S+)", config_text)
    return m.group(1) if m else fallback


def rtl_src_dirs(config_text: str, nickname: str) -> list[str]:
    text = config_text.replace("$(DESIGN_NICKNAME)", nickname)
    comps: list[str] = []
    for c in re.findall(r"\bsrc/([A-Za-z0-9_\-]+)", text):
        if c not in comps:
            comps.append(c)
    return comps or [nickname]


def _ensure_kv(text: str, key: str, value: str) -> str:
    if re.search(rf"export\s+{re.escape(key)}\b", text):
        return text
    return text.rstrip() + f"\nexport {key} = {value}\n"


def set_utilization(text: str, util: int) -> str:
    new, n = re.subn(r"(export\s+CORE_UTILIZATION\s*)\??=\s*\S+",
                     lambda m: f"{m.group(1)}= {util}", text)
    if n == 0:
        new = text.rstrip() + f"\nexport CORE_UTILIZATION = {util}\n"
    return new


def drop_floorplan_def(text: str) -> tuple[str, bool]:
    """A fixed FLOORPLAN_DEF pins die/core area, overriding CORE_UTILIZATION.
    For a utilization sweep we remove it and fall back to util-based floorplan,
    ensuring aspect ratio + margin are present (ORFS floorplan requirements)."""
    lines = [ln for ln in text.splitlines()
             if not re.match(r"\s*export\s+FLOORPLAN_DEF\b", ln)]
    if len(lines) == len(text.splitlines()):
        return text, False
    out = "\n".join(lines)
    out = _ensure_kv(out, "CORE_ASPECT_RATIO", "1")
    out = _ensure_kv(out, "CORE_MARGIN", "2")
    return out, True


def bump_place_density(text: str, util: int) -> str:
    """Raise a hardcoded PLACE_DENSITY so global placement legalizes at higher
    utilizations. Designs using PLACE_DENSITY_LB_ADDON (auto density) are left."""
    target = min(0.99, round(util / 100.0 + 0.05, 2))
    new, _ = re.subn(r"(export\s+PLACE_DENSITY\s*)=\s*([0-9.]+)",
                     lambda m: f"{m.group(1)}= {max(float(m.group(2)), target)}", text)
    return new


def clk_period_value(cp_ps: int, pdk: str) -> str:
    """Format the clock period as ORFS does for the PDK: ps for asap7,
    ns (decimal) for nangate45."""
    if pdk == "asap7":
        return str(cp_ps)
    ns = (Decimal(cp_ps) / Decimal(1000)).normalize()
    s = format(ns, "f")
    return s if "." in s else s + ".0"


def set_clock_period(text: str, cp_ps: int, pdk: str) -> tuple[str, int]:
    return re.subn(r"(set\s+clk_period\s+)\S+",
                   rf"\g<1>{clk_period_value(cp_ps, pdk)}", text)


# ---- build one config -----------------------------------------------------
def build_one(pdk: str, design: str, util: int, cp: int) -> str:
    src_design = DESIGNS / pdk / design
    cfgname    = f"{design}_{util}_{cp}"
    base       = DATASET / pdk / design / cfgname
    ddir, sdir = base / "design", base / "setup"

    ctext0   = (src_design / "config.mk").read_text()
    nickname = design_nickname(ctext0, design)
    src_dirs = rtl_src_dirs(ctext0, nickname)

    # design/ mirrors designs/src/<srcdir>/
    if ddir.exists():
        shutil.rmtree(ddir)
    ddir.mkdir(parents=True)
    for sd in src_dirs:
        shutil.copytree(DESIGNS / "src" / sd, ddir / sd)

    # setup/ = pdk design dir (drop bulky cached synth netlists)
    if sdir.exists():
        shutil.rmtree(sdir)
    shutil.copytree(src_design, sdir, ignore=shutil.ignore_patterns("*_synth.v"))

    # patch config.mk
    cfgmk = sdir / "config.mk"
    ctext = cfgmk.read_text()
    sdc_name = active_sdc_name(ctext)
    ctext, fp_dropped = drop_floorplan_def(ctext)
    ctext = set_utilization(ctext, util)
    ctext = bump_place_density(ctext, util)
    cfgmk.write_text(ctext)

    # patch the active SDC (clock period, ORFS units per PDK)
    note = "  [+fp->util]" if fp_dropped else ""
    sdc_path = sdir / sdc_name
    if sdc_path.exists():
        stext, n = set_clock_period(sdc_path.read_text(), cp, pdk)
        if n:
            sdc_path.write_text(stext)
        else:
            note += f"  [WARN] no 'set clk_period' in {sdc_name}"
    else:
        note += f"  [WARN] active SDC {sdc_name} not found"
    return (f"  {pdk}/{design}/{cfgname}  "
            f"(util={util}, cp={cp}ps->{clk_period_value(cp, pdk)}, sdc={sdc_name}){note}")


def main() -> None:
    # Wipe only the per-PDK data dirs, never the whole dataset/ (which now
    # also holds these scripts + README).
    for _pdk in CONFIGS:
        _d = DATASET / _pdk
        if _d.exists():
            shutil.rmtree(_d)
    total = 0
    for pdk, designs in CONFIGS.items():
        for design, cfgs in designs.items():
            for util, cp in cfgs:
                print(build_one(pdk, design, util, cp))
                total += 1
    print(f"\nDone. {total} config(s) generated under {DATASET}")


if __name__ == "__main__":
    main()
