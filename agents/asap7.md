## Cell Naming Convention

ASAP7 cells follow a fixed naming pattern:

```
<FunctionFamily><DriveStrength>_ASAP7_75t_<VT>
```

| Component | Meaning | Example |
|-----------|---------|---------|
| `FunctionFamily` | Logic function (BUF, INV, NAND2, HA, DFF, etc.) | `NAND2` |
| `DriveStrength` | Drive strength multiplier (see table below) | `x2` |
| `ASAP7` | PDK identifier — always present | `ASAP7` |
| `75t` | 7.5-track standard cell height — always present | `75t` |
| `VT` | Threshold voltage tier (see VT table below) | `SL` |

Full example: `NAND2x2_ASAP7_75t_SL` — 2-input NAND, 2× drive strength, SLVT

---

## Drive Strength Encoding

| Suffix | Multiplier | Notes |
|--------|-----------|-------|
| `xp5` | 0.5× | Weakest — minimum drive strength available for some families |
| `x1` | 1× | Baseline drive strength |
| `x1p5` | 1.5× | |
| `x2` | 2× | Common general-purpose size |
| `x4` | 4× | Strong driver |
| `x6f` | 6× fast | Used for buffers on high-load nets |
| `x8` | 8× | Largest available for some cell types |

Sizes increase monotonically: `xp5 < x1 < x1p5 < x2 < x4 < x6f < x8`.
Not all cell families have all sizes — use `query_cell_catalog.py` to check
what is available for a specific family before proposing a resize.

---

## VT (Threshold Voltage) Tiers

ASAP7 VT naming is **counter-intuitive** — "Regular" (R) is the **slowest**,
not the fastest:

| Suffix | Full Name | Speed | Leakage |
|--------|-----------|-------|---------|
| `SL` | SLVT — Super-Low Threshold Voltage | **Fastest** | Highest |
| `L` | LVT — Low Threshold Voltage | Middle | Middle |
| `R` | RVT — Regular Threshold Voltage | **Slowest** | Lowest |

`vt_swap` always moves a cell **toward lower Vt** (toward faster, higher leakage):
`R → L → SL`. A cell already at `SL` **cannot be swapped further** — the move
returns false immediately.

---

## Common Cell Families

| Family | Function | Notes |
|--------|----------|-------|
| `BUF` | Buffer | All sizes; `BUFx6f` is the largest repeater |
| `INV` | Inverter | Common on all path types |
| `NAND2`, `NAND3` | NAND gate | `NAND2` most common in datapaths |
| `NOR2`, `NOR3` | NOR gate | |
| `XOR2` | 2-input XOR | Appears heavily in AES/crypto datapaths |
| `XNOR2` | 2-input XNOR | |
| `AOI21`, `OAI21` | AND-OR-INVERT / OR-AND-INVERT | 3-input complex gate |
| `HAxp5` | Half adder | **Only one size (`xp5`)** — cannot be upsized |
| `FAx1` | Full adder | **Only one size (`x1`)** — cannot be upsized |
| `DFFHQN`, `DFFHQ` | D flip-flop | Sequential element — endpoint of most timing paths |
| `MUX2` | 2:1 multiplexer | |

---

## Unsizable Cells

Some cells exist at only one drive strength. `repair_timing` and `sizeup` ECO
actions cannot help them directly. The most common:

| Cell | Only Size | Correct ECO approach |
|------|-----------|----------------------|
| `HAxp5` | xp5 only | Target upstream drivers (reduce input slew) or buffer output net |
| `FAx1` | x1 only | Same — buffer output net or upsize the cell feeding it |

When an unsizable cell appears as the dominant delay contributor on a path,
the bottleneck is the **load on its output net** or the **slew on its input**,
not the cell itself.

---

## Checking Valid Sizes for a Cell

Use `Scripts/python/utils/query_cell_catalog.py`:

```bash
# Look up one cell
python3 Scripts/python/utils/query_cell_catalog.py --cells NAND2x2_ASAP7_75t_SL

# Look up multiple cells
python3 Scripts/python/utils/query_cell_catalog.py --cells "BUFx4_ASAP7_75t_R,HAxp5_ASAP7_75t_SL"

# Look up all cells on a timing path
python3 Scripts/python/utils/query_cell_catalog.py \
    --from-timing work_dir/<agent>/<design>/base/base_worst_paths.txt
```

Output shows available sizes (weak→strong) and VT tiers for each cell family.
Always check before proposing a `resize` ECO action or a `new_master` field.
