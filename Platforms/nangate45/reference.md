# Nangate45 Cell Library Reference

This is a shared reference file. All agents in this framework must read it
before analyzing cell names, proposing cell changes, or interpreting timing
reports when the active PDK is Nangate45.

---

## Cell Naming Convention

Nangate45 cells follow a simple naming pattern:

```
<FunctionFamily>_X<DriveStrength>
```

| Component | Meaning | Example |
|-----------|---------|---------|
| `FunctionFamily` | Logic function (BUF, INV, NAND2, DFF, etc.) | `NAND2` |
| `_X` | Fixed separator | `_X` |
| `DriveStrength` | Drive strength multiplier (integer) | `2` |

Full example: `NAND2_X2` — 2-input NAND, 2× drive strength.

**No VT suffix** — Nangate45 is a single-VT PDK. Do not append `_LVT`/`_SVT`/`_HVT`.

---

## Drive Strength Encoding

Drive strengths are **integers only** (no fractional or variant forms):

| Size | Typical Meaning |
|------|-----------------|
| `X1` | Baseline (minimum drive) |
| `X2` | 2× drive |
| `X4` | 4× drive — common strong driver |
| `X8` | 8× drive |
| `X16` | 16× drive — used for buffers on high-load nets |
| `X32` | 32× drive — maximum for INV/BUF |

Sizes monotonically increase: `X1 < X2 < X4 < X8 < X16 < X32`.
Not all cell families have all sizes — most logic gates stop at `X4`.
Use the cell catalog XML (`Platforms/nangate45/cell_catalog.xml`) or query utility
to check what is available for a specific family before proposing a resize.

---

## VT (Threshold Voltage) Tiers

**Nangate45 is single-VT.** There are no VT variants.

- The `vt_swap` repair_timing move is a **no-op** on this PDK.
- Do NOT propose VT swap as a primary strategy; it will produce no benefit.
- Size optimization is the dominant lever for timing improvement.

---

## Common Cell Families

| Family | Function | Sizes Available |
|--------|----------|-----------------|
| `BUF` | Buffer | X1, X2, X4, X8, X16, X32 |
| `INV` | Inverter | X1, X2, X4, X8, X16, X32 |
| `CLKBUF` | Clock buffer | X1, X2, X3 |
| `CLKGATE` / `CLKGATETST` | Clock gating cell | X1 |
| `NAND2` / `NAND3` / `NAND4` | NAND gate | X1, X2, X4 |
| `NOR2` / `NOR3` / `NOR4` | NOR gate | X1, X2, X4 |
| `AND2` / `AND3` / `AND4` | AND gate | X1, X2, X4 |
| `OR2` / `OR3` / `OR4` | OR gate | X1, X2, X4 |
| `XOR2` / `XNOR2` | 2-input XOR / XNOR | X1, X2 |
| `AOI21` / `AOI22` / `AOI211` / `AOI221` / `AOI222` | AND-OR-INVERT complex gates | X1, X2, X4 |
| `OAI21` / `OAI22` / `OAI211` / `OAI221` / `OAI222` | OR-AND-INVERT complex gates | X1, X2, X4 |
| `MUX2` | 2:1 multiplexer | X1, X2 |
| `DFF` / `DFFR` / `DFFS` / `DFFRS` | D flip-flop variants (plain, reset, set, reset+set) | X1, X2 |
| `DLH` / `DLL` | D latch (high / low enable) | X1, X2 |
| `FA` | Full adder | X1 |
| `HA` | Half adder | X1 |
| `LOGIC0` / `LOGIC1` | Constant tie-off cells | X1 |
| `TBUF` / `TINV` | Tristate buffer / inverter | X1, X2, X4, X8, X16 |
| `ANTENNA` | Antenna diode | X1 |
| `FILLCELL` | Filler cells | X1, X2, X4, X8, X16, X32 |

---

## Unsizable Cells

**Nangate45 has no special unsizable cells** like asap7's `HAxp5`. Every cell
in a family with multiple sizes can be resized. Cells like `FA_X1`, `HA_X1`,
`LOGIC0_X1` exist at only one size, but they are not typically on critical
timing paths.

When a single-size cell appears as the dominant delay on a critical path, the
correct strategy is either:
1. Reduce input slew by upsizing the upstream driver
2. Insert a buffer downstream to split output load

---

## Practical Implications for the Agentic Flow

- **`vt_swap` is useless** — skip it in plan sequences; include only if the Planner
  wants to mark it as tried-and-harmless.
- **Sizing has a wider range** than asap7 — `INV_X1` can go all the way to `INV_X32`,
  giving much more headroom per cell than asap7's typical 4–6 sizes per family.
- **Fewer gate types** — no XOR3, no complex AO/OA beyond AOI222/OAI222. Synthesis
  may produce deeper logic trees than on asap7 for the same RTL.
- **Clock periods are typically longer** — Nangate45 gcd uses 0.46 ns (460 ps) vs
  asap7's 310 ps. Slack thresholds should be interpreted relative to the clock.
- **Size notation parsing**: regex is `([A-Z][A-Z0-9]*?)_(X\d+)` — family + size.
  The size token always begins with `X`.

---

## Checking Valid Sizes for a Cell

Use the cell catalog XML directly, or run:

```bash
python3 Scripts/python/utils/query_cell_catalog.py --cells INV_X16
python3 Scripts/python/utils/query_cell_catalog.py --cells "BUF_X32,NAND2_X2"
```

(The query utility must support the PDK's regex; see `Scripts/python/pdk_configs/nangate45.py`.)
