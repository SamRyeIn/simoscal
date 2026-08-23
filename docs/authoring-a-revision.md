# Authoring a tune revision

This guide assumes you know Simos 18 tuning and have never seen `simoscal`. By
the end you can write a revision, run it, and read what it produced.

It does not teach tuning. Whether 26 psi on this turbo on this fuel is a good
idea is your judgment; this library's job is to make sure the number you decided
on is the number that reaches the ECU, and that you can prove it afterwards.

**Nothing here flashes an ECU.** A revision produces a verified `.bin` file. You
flash it yourself, with the tool built for that, after reading the report.

---

## 1. The model

A **revision** is one Python script that declares an entire calibration and
builds one bin. Revisions are numbered and never edited in place: to make R14,
copy R13, change the numbers, run it. Each script is the complete truth about
what it flashes — it never imports from another revision.

That means the unchanged bulk of the calibration is repeated in every revision.
That is deliberate. The alternative — layering changes on top of each other —
means reading five files to know what a bin contains, which is exactly what this
design replaced.

A revision has three parts:

```python
# 1. Open: which bin, which XDF(s), which patches
tune = Tune.open(SC8S50, xdf=XDF_PATH, bin=STOCK_BIN)

# 2. Declare: the whole calibration, in physical units, an intent on every call
tune.apply_basics_sop()
tune.boost.put_ceiling_psi(30.0, intent="park the base boost ceiling above every slot")
...

# 3. Build: one call, every verification gate
result = build(tune, "R14", out_root=OUT_ROOT, reference_bin=R13_BIN)
```

---

## 2. Your first revision

The smallest useful change: raise the boost target and nothing else.

```python
#!/usr/bin/env python3
"""MyTune R01 — raise the full-load boost target to 22 psi."""

from pathlib import Path

from simoscal.tune import SC8S50, Tune, build

CODE = Path(__file__).resolve().parents[2] / "Code"
XDF_PATH = CODE / "xdf" / "SC8S50.V1.0.xdf"
BIN_PATH = CODE / "bin" / "5G0906259L__0002.bin"
OUT_ROOT = Path(__file__).resolve().parent / "MyTune_out"

BOOST_TARGET_PSI = 22.0


def main() -> None:
    tune = Tune.open(SC8S50, xdf=XDF_PATH, bin=BIN_PATH)

    tune.boost.put_ceiling_psi(
        BOOST_TARGET_PSI,
        intent="raise the full-load boost target for the upgraded intercooler",
    )

    result = build(tune, "R01", out_root=OUT_ROOT, bin_name="mytune_r01.bin")
    print(f"saved  : {result.bin_path}")
    print(f"report : {result.report_path}")


if __name__ == "__main__":
    main()
```

Run it. You get a fresh timestamped folder `MyTune_out/R01_<stamp>/` containing
the bin, `report.md`, and `compare/` PNGs.

Write an `intent=` on **every** calibration-changing call — it is required by
the project's authoring rule (`CLAUDE.md`), not optional, and the R13 source
acceptance test (`test_r13_every_calibration_call_declares_intent`) enforces it
on the template. It is what appears in the report's "Why" column, and in six
months it is the only record of *why* the number is what it is. Without it the
journal falls back to the library's generic action text, which describes *what*
changed but never *why*. The only exemptions are the bulk `tune.apply_basics_sop()`
pass (journaled per table with its own reasons) and gates that move no bytes
such as `tune.switchpatch.require_sanity(...)`.

### Read the report before anything else

```
| Table                                       | Change        | Verdict | Before                | After        | Why / detail
| `IP_PUT_SP` — Pressure up throttle setpoint | table (row 3) | applied | 2501.04, … 2506.02    | flat 2532.96 | raise the full-load boost target…

- Checksums: **CLEAN** (CAL_CRC, ECM3).
- Final-bin readback: **PASS** — 1 table(s) re-read off the saved bin and matched the journal.
- Raw-diff audit: **not run** — no reference bin was declared, so no byte-level claim is made about what else may have changed.
```

Every table you touched, what it was, what it is now. If a row you did not
expect appears here, the revision did something you did not intend — that is
what this table is for.

### Then add the byte audit

Pass `reference_bin=` pointing at the previous revision's output, and the build
additionally compares the two bins **byte for byte**:

```
- Raw-diff audit vs `mytune_r01.bin`: **CLEAN** — 12 changed byte(s), all attributed; unexplained = 0.
    - 8 byte(s): journaled edits
    - 4 byte(s): stored checksums (CAL_CRC, ECM3)
```

This is the strongest check available and you should always use it after your
first revision. The allowance comes from the journal, so anything that changed
without being declared lands in *unexplained* and the build fails. A first
revision has no predecessor, which is why the gate says "not run" above rather
than implying a clean result.

---

## 3. What the build actually checks

`build()` runs these in order and fails if any of them does:

| Gate | What it proves |
|------|----------------|
| **Checksums** | `CAL_CRC` and `ECM3` are corrected and verify clean *on the written file*, not on the in-memory buffer. |
| **Readback** | Every table you edited is re-read off the saved bin and matches what the journal says. Staging a write and having written it are different claims. |
| **Blocked writes** | No guard rejected something you asked for. If one did, your intent did not happen and the build says so instead of shipping. |
| **Coherence** | (When the SOP ran.) A boost change without matching fuelling is **DO NOT FLASH** — the lean-risk rule. |
| **Post-save checks** | Things only the finished file can answer, e.g. the switch patch still loads and decodes. |
| **Byte audit** | Every byte differing from the reference is attributed to a declared edit or a stored checksum. |

A failed build **still writes `report.md`** before raising, so you can read what
went wrong rather than guessing from a traceback.

---

## 4. Domain call reference

All calls take physical units and are journaled. All accept `intent="…"`.

### `tune.boost`

| Call | Effect |
|------|--------|
| `put_ceiling_psi(psi, rounding="nearest")` | Flatten the full-load row of the boost setpoint. Part-load rows untouched. |
| `put_ceiling_hpa(hpa)` | Same, in stored hPa absolute. |
| `put_curve_hpa(curve)` | A per-rpm full-load curve — one value per breakpoint of the table's own axis. |
| `put_rpm_axis(breakpoints)` | Re-breakpoint the boost setpoint's private rpm axis. Rewrite the curve to match. |
| `pressure_quotient_max(plateau, low_rpm=None)` | The compressor pressure-quotient cap. Can silently trim a boost curve short if left low. |
| `manifold_pressure_max(hpa)` | Maximum requested manifold pressure. Float-bug table — written raw, deliberately. |
| `overboost_threshold(hpa)` | Raise the P0234 diagnosis threshold. Never lowers. |

### `tune.wastegate`

| Call | Effect |
|------|--------|
| `overlay({(row, col): delta})` | Add deltas to **both** VVL feedforward maps. Negative opens the wastegate sooner. Refuses to clamp at the physical [0, 1] range. |
| `exh_flow_axis_last(value)` | Move the top exhaust-flow-factor breakpoint of the axis shared by both maps. |

Cells are actuator position: **1 = closed** (max boost), **0 = open**. Rows are
intake flow factor, columns exhaust flow factor.

### `tune.fueling`

| Call | Effect |
|------|--------|
| `rebreakpoint_lambda_axes(rpm=…, load=…)` | Re-breakpoint the axes shared by the BAS/HPDI/MPI grids. Run **before** writing a grid. |
| `lambda_grid(cells, tables=…, rpm_keys=…, load_keys=…)` | Write a full lambda grid. With the `*_keys`, refuses if the table's live axes disagree. |
| `lambda_floors(value)` | Flatten the three lambda minimum-value floors. |
| `pedal_threshold(percent)` | Where full-load enrichment comes in, in pedal percent. |

### `tune.ignition`

| Call | Effect |
|------|--------|
| `retard_cells({(rpm, load): degrees})` | Set absolute timing at operating points; snaps to the nearest breakpoint. Writes all nine cam-position grids. |
| `offset_cells({(rpm, load): delta})` | The relative form. Prefer `retard_cells` — a delta applied twice is a mistake the bin cannot detect. |

### `tune.limits`

| Call | Effect |
|------|--------|
| `airmass_cap_mg(mg_per_stroke)` | The airmass setpoint ceiling, **in mg/stk**. Converts to the kg/stk the ECU stores. |
| `intake_air_max(mg_per_stroke)` | Both max-intake-air tables (genuine mg/stk). |
| `torque_reference_max(nm)` | Maximum reference indicated torque. |
| `raise_ceiling(name, target)` | Any mapped limiter, never lowering a higher cell. |

### `tune.switchpatch`

Needs a patched bin and the patch table space — see §5.

| Call | Effect |
|------|--------|
| `slot_curve(slot, psi=…)` or `(slot, hpa=…)` | A slot's boost cap, flat or per-rpm, tiled across all eight rows. psi is **floored**. |
| `slot_rpm_axis(breakpoints)` | The rpm axis shared by all five slot grids. |
| `traction_control(slots=…, enable=True)` | The patch's TC on/off per slot, pairing `Enable SL TC` with `Disable OEM TC`. |
| `require_sanity(stock_bin=…)` | Register a build gate: the patch must still load and decode on the finished file. |

### Escape hatch

For a table no domain module covers:

```python
values = tune.values("put_setpoint")
values[2] = 1800.0
tune.write("put_setpoint", values, intent="lower the third load row")
```

`tune.write` is what every domain call routes through, so this is journaled and
audited identically — you just lose the domain method's built-in rules. Use
`tune.note(name, "why not", intent="…")` to record a deliberate *non*-change;
a reviewer cannot see a decision you left as silence.

---

## 5. Patched bins and the switch patch

Patch-added tables live in the patch author's XDF, which knows nothing about the
base calibration's tables — so a patched tune has **two table spaces** sharing
one byte buffer.

```python
from simoscal.tune import SWITCH_PATCH_2933, PatchSpec, Tune
from simoscal.tune.domains.switchpatch import PATCH_SPACE

PATCHES = (
    PatchSpec("SL CBRICK v1.2 - S50", BINTOOLZ / "patches" / "SL CBRICK v1.2 - S50.btp",
              "anti-brick patch"),
    PatchSpec("SL PATCH.29.33 - S50", BINTOOLZ / "patches" / "SL PATCH.29.33 - S50.btp",
              "5-slot map switch"),
)

tune = Tune.open(
    SC8S50, xdf=XDF_PATH, bin=STOCK_BIN,
    patches=PATCHES,
    extra_spaces={PATCH_SPACE: (SWITCH_PATCH_2933, SWITCH_XDF)},
)
```

Patches are applied first (copy-on-write; your stock bin is never touched), each
gated on `READY_TO_ACCEPT` and verified confined to its declared blocks. Never
forced.

Two things to know about slots:

- The effective boost target is the **minimum** of the base setpoint and the
  selected slot's grid. So the base ceiling is normally parked high and
  non-binding, and each slot's grid is what actually caps boost. Park the base
  ceiling *before* declaring slot curves, or the guard will (correctly) refuse a
  slot that sits above it.
- **A switch-patched bin must be flashed FULL, not CAL-only.** The patches
  modify ASW/code blocks. This library cannot check ASW checksums — those are
  computed at full-flash time by the flashing tool.

---

## 6. Using a different Simos 18 XDF

Every table reference goes through a **profile**: a map from a logical name to
an XDF symbol (or uniqueid), with a plain-English description, units, expected
shape, and any guard tags. Supporting another XDF means writing one map file.

```python
# simoscal/tune/profiles/my_xdf.py
from ..profile import GROUP_AIRFLOW, GROUP_BOOST, Profile, TableSpec, TAG_KG_PER_STROKE

_SPECS = [
    TableSpec("put_setpoint", "IP_PUT_SP",
              "Pressure up throttle setpoint", "hPa", (4, 6),
              group=GROUP_BOOST),
    TableSpec("airmass_setpoint_max", "C_M_AIR_CYL_SP_MAX",
              "Maximum allowed airmass setpoint", "mg/stk", (1, 1),
              frozenset({TAG_KG_PER_STROKE}), group=GROUP_AIRFLOW),
    # …
]

MY_XDF = Profile(name="MyXdf", xdf="my-file.xdf",
                 specs={s.name: s for s in _SPECS})
```

Use the same logical names as `profiles/sc8s50.py` and every domain module works
unchanged against your XDF. Notes:

- **`shape` is a real guard.** A same-named symbol with different geometry in
  another XDF is not the same table, and resolution refuses it.
- **Tags carry safety facts as data**, not as code. `TAG_KG_PER_STROKE` is what
  makes `airmass_cap_mg` convert instead of trusting the XDF's label;
  `TAG_FLOAT_BUG` marks a table whose display maximum is a TunerPro artifact.
- **Bind by uniqueid** when a table has no symbol or shares a title with others,
  as the switch patch's five `PUT setpoint` grids do.
- **`group` is the heading the app's table browser files it under**, and it is
  required for any spec with no `owner` — those are exactly the tables the generic
  browser offers, and one with no group is a table nobody can find. Pick from
  `profile.GROUPS`; anything else is refused at construction. It is curated rather
  than taken from the XDF's own categories, which file every breakpoint vector
  under "Axis" — so an axis takes the group of the map it indexes, not one of its
  own. Owner-locked tables may go without: they are reached through their domain
  screen, never browsed.

Resolution happens at `Tune.open`, before any byte can be written, and reports
*every* unresolved name at once with suggestions:

```
profile 'MyXdf' could not resolve 2 logical name(s) against my-file.xdf:
  - 'put_setpoint' (key 'IP_PUT_SP_2'): no table with this symbol, title, or uniqueid in the XDF
      did you mean: IP_PUT_SP — Pressure up throttle setpoint; …
Nothing was edited. Fix the map file (or point at the intended XDF) —
names are never resolved by guessing.
```

Suggestions are for a human to act on. Nothing is ever auto-substituted: a wrong
table is a wrong byte in an ECU.

---

## 7. Safety

The full model is in [`../README.md` § Safety](../README.md#safety-read-this).
The rules that bear on writing a revision:

- **Never flash from a script.** Nothing in this library flashes, and no
  revision should try to. Build, review, then flash with SimosTools or VW_Flash.
- **Keep a known-good stock bin** as your recovery image, untouched.
- **The human review gate is not optional.** A clean build means every
  *automated* check passed. It does not mean the calibration is safe — read the
  report and the compare plots.
- **Fail loud, never clamp.** If a guard refuses your value, it is telling you
  the declaration is wrong. Do not route around it; the guards encode failures
  that already happened once.
- **A revision is a starting point, not a finished calibration.** Only logs
  validate a tune. Flash → log → review → iterate.

---

## 8. Worked example

`Tunes/TuningBasicsGuide/TUNE_Basics_Guide_R13.py` is the reference: the
complete R00–R12 calibration — SOP, lambda, timing, wastegate, boost, limiters,
five switch-patch slots, traction control — as one flat script with no imports
from any other revision.

It is verified **byte-identical** to the hand-written R12 output it replaced
(`tests/test_acceptance_tune.py`), which is the evidence that this authoring
path does exactly what the old one did.
