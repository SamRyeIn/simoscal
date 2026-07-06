# simoscal — Simos18 XDF/BIN tuning library (Phase 1)

A pure-Python library that parses a TunerPro `.xdf`, maps its tables against a
Simos18 `.bin`, reads and edits table values **in physical units**, and writes a
**minimal-diff, flashable** `.bin`. It runs entirely on the Mac — no Windows, no
TunerPro dependency for day-to-day work.

Phase 1 is the read/edit/write substrate; Phase 2 adds CSV/xlsx export (see
[Export](#export-phase-2--csv--xlsx-physical-units-read-only) below). Later
phases (visualization, datalog-driven auto-tuning) consume this library
read-only. It **does not flash** and it **does not recompute** checksums by
default — it *verifies and reports* them so a stale bin is caught before it
reaches the flasher.

> **⚠ This is not a sandbox.** The output of this pipeline is a calibration that
> gets flashed to the engine controller of a real, driven car. A wrong byte can
> brick the ECU or encode a dangerous tune (over-boost, lean lambda, excess
> timing → knock, melted pistons, blown turbo). The library's correctness
> guarantees are **safety mechanisms**. See [Safety](#safety-read-this).

## Install

```bash
cd Code
python -m venv .venv
./.venv/bin/pip install -e ".[dev]"     # numpy + openpyxl runtime, pytest dev
```

Requires Python ≥ 3.11. Runtime dependencies: `numpy` and `openpyxl` (the
latter for xlsx export, Phase 2).

## Quick start

```python
from simoscal import CalFile

cal = CalFile.open("xdf/SC8S50.V1.0.xdf", "bin/5G0906259L__0002.bin")

# Look up a table by A2L symbol (or by title, or by uniqueid int).
port = cal.get("ID_PORT_SP")
print(port.shape, port.units)     # (10, 10) '-'
print(port.values)                # numpy array of physical values

# Edit in physical units.
port.set_cell(0, 0, 12.5)         # inverse-scaled, range-checked, staged

# Save minimal-diff, then verify checksums (never silently rewritten).
reports = cal.save("out.bin")
for r in reports:
    print(r.name, "stale" if r.is_stale else "ok")
```

## The workflow: load → edit → save → verify → **flash externally**

```
  CalFile.open(xdf, bin)          parse XDF + load 4 MB bin (region-checked)
        │
        ▼
  table = cal.get(symbol)         look up by symbol / title / uniqueid
  table.values                    lazy-decode to physical units (cached)
        │
        ▼
  table.set(...) / set_cell(...)  edit in physical units; inverse MATH,
                                  range-checked, staged minimal-diff
        │
        ▼
  reports = cal.save("out.bin")   write only changed bytes; verify checksums
        │                         → StaleChecksumWarning if an edit left a
        │                           checksummed range stale
        ▼
  human review gate               visually confirm changed tables (TunerPro /
                                  Phase 3 viz) + pass checksum verification
        │
        ▼
  flash externally                SimosTools / VW_Flash  ← NOT this library
```

## API surface

### `CalFile`
| Member | Description |
|--------|-------------|
| `CalFile.open(xdf_path, bin_path)` | Parse the XDF and load the bin; region taken from the XDF `REGION` header. |
| `.get(key)` | Fetch the single `TableView` by symbol, title, or `uniqueid` int. Raises `AmbiguousTableError` if a name maps to genuinely distinct tables. |
| `.search(substring)` | List `TableView`s whose symbol/title contains `substring`. |
| `.unique_tables()` | Dedup-by-`uniqueid` view (3,814 tables) for sweeps. |
| `.categories()` | The XDF category names. |
| `.edited` / `.edited_ranges` | Whether/which byte ranges were staged this session. |
| `.save(path, *, correct_checksums=False, warn_stale=True)` | Write buffer (original bytes + staged edits) minimal-diff. Returns `list[ChecksumReport]`. |
| `.verify_checksums()` | Verify without writing; returns `list[ChecksumReport]`. |

### `TableView`
| Member | Description |
|--------|-------------|
| `.values` / `.raw` | Physical (scaled) / raw-integer numpy arrays, lazily decoded and cached. |
| `.shape`, `.units`, `.symbol`, `.title`, `.uniqueid_hex` | Metadata. |
| `.axis_values("x"\|"y")` | Decoded axis breakpoints, if embedded. |
| `.set(values, *, override=False)` | Write a full array in physical units. |
| `.set_cell(r, c, value, *, override=False)` | Write one cell in physical units. |
| `.set_raw(arr)` / `.set_raw_cell(r, c, v)` | Write raw integers directly (the only write path for non-linear tables). |

### Export (Phase 2) — CSV / xlsx, physical units, read-only

Turns any selection of tables into flat-file output — archiving, cross-tune
diffing, or handing values to another tool. Grid-shaped like TunerPro (X
across, Y down, Z fills the matrix); 1D tables and scalars degrade naturally.
One-way (no import back into a `.bin`) and library-only (no CLI).

```python
from simoscal import CalFile, export_tables

cal = CalFile.open("xdf/SC8S50.V1.0.xdf", "bin/5G0906259L__0002.bin")
export_tables(cal, "boost.csv", category="Boost Control")
export_tables(cal, "full_dump.xlsx", all_tables=True)  # one sheet per category
```

| Member | Description |
|--------|-------------|
| `export_tables(cal, path, *, symbols=None, category=None, all_tables=False)` | Select, render, and write in one call. Dispatches to CSV/xlsx by `path`'s suffix. |
| `select_tables(cal, *, symbols=None, category=None, all_tables=False)` | Resolve a selection spec into a deduplicated `list[TableView]`, unioned by `uniqueid`. |
| `render_table(view)` → `RenderedTable` | The shared table→grid rendering layer (`symbol`, `title`, `units`, `categories`, `x_labels`, `y_labels`, `values`). Public so Phase 3 (visualization) can reuse it directly. |
| `write_csv(tables, path)` | All tables in **one file**, stacked as labeled grid blocks. |
| `write_xlsx(tables, path)` | Tables grouped onto sheets **by XDF category**; a multi-category table is written onto every one of its categories' sheets. |

### Checksums — `ChecksumReport`
`name` · `can_verify` · `is_stale` · `stored` · `computed` · `covered` (half-open
full-bin byte ranges) · `detail`. Two checksums are reported: **`CAL_CRC`**
(32-bit CRC over the whole CAL block minus its header) and **`ECM3`** (64-bit
summation; needs a full bin because its area addresses live in ASW1 — a CAL-only
image degrades to `can_verify=False`).

## The flash / checksum boundary

The library **never flashes** and, by default, **never rewrites checksums**:

- **`save()` default** (`correct_checksums=False`) writes the bin as-is. If an
  edit this session touched a checksummed range and left it stale, it emits a
  `StaleChecksumWarning` — the saved bin is **not flash-ready**.
- **`save(..., correct_checksums=True)`** corrects `CAL_CRC` and `ECM3` in place
  (ECM3 first — its stored value sits inside CRC coverage) so the saved bin
  verifies clean. Only the few stored-checksum bytes change, keeping the diff
  minimal.

Flashing itself is delegated to the tools built for it (**SimosTools /
VW_Flash**), which carry their own recovery and checksum handling. The checksum
algorithm here is *adapted from* VW_Flash (`lib/checksum.py` + `lib/fastcrc.py`,
BSD-2-Clause) and reimplemented with attribution — no third-party files are
vendored.

## Safety (read this)

Operating principle for the whole pipeline: **fail loud, change nothing
silently, keep every modified bin verifiable before it is flashed.** How the
library upholds it:

- **Minimal-diff writes + round-trip byte-equality** — the output differs from
  the input *only* where you intended (AE2/AE3).
- **Warn-loud, never-silent edits** — an out-of-declared-range value is written
  *and reported* via `EditRangeWarning`, never quietly clamped (AE4). A raw-width
  overflow hard-fails (`RawRangeError`) rather than wrapping.
- **Float-bug hard guard** — a small flagged list of boost/airmass-ceiling
  float tables rejects over-limit writes (`FloatBugGuardError`) even with
  `override=True` (plan Decision 9).
- **Non-linear fallback** — a table whose MATH is not linear refuses
  `set(physical)` (`NonLinearEquationError`) and exposes only `set_raw` (AE5).
- **Checksum verify** — a stale-checksum bin is flagged before it reaches the
  flasher.

Workflow corollaries the operator owns:

- **Always retain a known-good stock bin** (`bin/5G0906259L__0002.bin`) as the
  recovery image.
- **A human review gate before every flash** — visually confirm changed tables
  and pass checksum verification. The library never flashes.
- **First flash full + unlock, battery on a charger** (per the SOP) so recovery
  is possible.

The library owns **software-fidelity** risk (right byte, right cell, right
scaling). **Tuning-decision** risk — whether a given boost/timing target is safe
for this engine on this fuel — is the human's judgment and is out of scope for
Phase 1.

## Tests

```bash
cd Code
./.venv/bin/python -m pytest tests -q            # full suite
./.venv/bin/python -m pytest tests/test_acceptance.py -v          # AE1–AE5 (Phase 1)
./.venv/bin/python -m pytest tests/test_acceptance_export.py -v   # AE1–AE7 (Phase 2 export)
```

The acceptance suite (`tests/test_acceptance.py`) encodes the AE1–AE5 examples:

| | Example | Runs on Mac? |
|-|---------|--------------|
| AE1 | Read parity with TunerPro | Only with the captured oracle (else **skips**) |
| AE2 | load → save-unchanged → byte-identical | ✅ |
| AE3 | one-cell edit → exactly that cell's bytes change | ✅ |
| AE4 | out-of-range write succeeds **and** warns | ✅ |
| AE5 | non-linear table rejects `set(physical)`, allows `set_raw` | ✅ |

AE1 is the one independent confirmation of the `mmedtypeflags` bit semantics
(plan Decision 6). It needs a one-time Windows capture,
`tests/fixtures/tunerpro_oracle.json` — see
[`tests/fixtures/README.md`](tests/fixtures/README.md) for the capture procedure
and schema. Until then AE1 skips cleanly; everything else runs on the Mac.

Tests that touch the real bin/xdf skip (never fail) when those files are absent
from a checkout.

## Scope

**In:** XDF parse, bin read/edit/write in physical units, minimal-diff save,
checksum verify/report, acceptance suite (Phase 1); CSV/xlsx export of any
table selection in physical units, a public `RenderedTable` rendering layer
(Phase 2).
**Out:** flashing (SimosTools/VW_Flash), checksum *recompute* beyond the
optional correction path, CBOOT/ASW editing, bin patching, FRF→BIN extraction,
GUI/CLI, import/round-trip from an exported file back into a `.bin`.
Visualization (Phase 3) and datalog-driven auto-tuning (Phase 4) are later
phases that consume this library read-only (Phase 4 writes *through* this
writer, inheriting its guards).
