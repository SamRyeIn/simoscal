# simoscal — Simos18 XDF/BIN tuning library (Phase 1)

A pure-Python library that parses a TunerPro `.xdf`, maps its tables against a
Simos18 `.bin`, reads and edits table values **in physical units**, and writes a
**minimal-diff, flashable** `.bin`. It runs entirely on the Mac — no Windows, no
TunerPro dependency for day-to-day work.

Phase 1 is the read/edit/write substrate; Phase 2 adds CSV/xlsx export (see
[Export](#export-phase-2--csv--xlsx-physical-units-read-only) below) and Phase 3
adds static-PNG visualization (see
[Visualization](#visualization-phase-3--surface--heatmap--line-pngs--comparison)
below). Later phases (datalog-driven auto-tuning) consume this library
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
./.venv/bin/pip install -e ".[dev]"     # numpy + openpyxl + matplotlib runtime, pytest dev
```

Requires Python ≥ 3.11. Runtime dependencies: `numpy`, `openpyxl` (xlsx export,
Phase 2), and `matplotlib` (PNG visualization, Phase 3).

### You supply the bin and the XDF

This repository ships **source only**. It does not distribute an OEM calibration
image (VW's copyright) or the SC8S50 XDF definitions (community-authored, not
ours to relicense) — see [`LICENSE-THIRD-PARTY`](LICENSE-THIRD-PARTY). `bin/`
and `xdf/` are gitignored; drop your own files there and every path in the
examples below resolves. Tests that need them skip cleanly when they're absent.

Read the bin off your own car. Even on the same box code, build from the file
your ECU actually has, and keep that stock read untouched as your recovery image.

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

## Authoring a revision — `simoscal.tune`

Everything above is the substrate. **`simoscal.tune` is the layer you actually
write a tune revision in**, and it is where you should start if your goal is to
change a calibration rather than to build a tool.

A revision is one flat, self-contained script: it declares the whole calibration
in physical units through domain-level calls, then hands the entire verification
pipeline to a single `build()`.

```python
from simoscal.tune import SC8S50, Tune, build

tune = Tune.open(SC8S50, xdf=XDF_PATH, bin=STOCK_BIN)

tune.apply_basics_sop()                         # the whole ecu-tuning-basics SOP
tune.boost.put_ceiling_psi(30.0)                # full-load row only
tune.wastegate.overlay({(7, 14): -0.06})        # both VVL maps, identical deltas
tune.limits.airmass_cap_mg(2000)                # mg/stk in, kg/stk stored

result = build(tune, "R14", out_root=OUT_ROOT, reference_bin=PREVIOUS_BIN)
```

That produces the standard artifact set — saved bin, `report.md`, `compare/`
PNGs — with every gate run: checksums corrected and independently verified,
every edited table read back off the saved file, and a **byte-level audit**
against the previous revision.

**→ Full walkthrough: [`docs/authoring-a-revision.md`](docs/authoring-a-revision.md)**
— how to write your first revision, the complete domain-call reference, and how
to add a profile for a different Simos 18 XDF.

Three properties are what make this safe to hand to someone with no simoscal
history:

1. **Every edit is journaled.** A domain call moves bytes *and* records a typed
   entry — logical name, resolved `` `ID` — Description ``, units, before/after
   values, guard verdict. `report.md` is rendered *from* that journal, so it
   cannot drift from what the code did.
2. **The byte audit is driven by the journal.** The allowance set comes from the
   edits that were recorded, so a change made outside the journal shows up as
   *unexplained bytes* and fails the build. Forgetting to declare something is
   loud rather than silent.
3. **Table references go through a profile.** Logical names resolve against an
   explicit per-XDF map, exactly — never fuzzily. A name that does not resolve
   fails before any bin is opened, listing every miss with suggestions.

Safety-critical unit handling lives in the library rather than in each script,
so the traps are unavailable rather than merely documented:

| Trap | How the API removes it |
|------|------------------------|
| `C_M_AIR_CYL_SP_MAX` — Maximum allowed airmass setpoint stores **kg/stk** behind an mg/stk label; writing `2000` removes the limiter | `limits.airmass_cap_mg(2000)` takes mg/stk and writes `0.002`; a sub-1.0 argument is rejected as a raw value passed by mistake |
| A psi→hPa boost cap that rounds **up** encodes above the number you asked for | `switchpatch.slot_curve(5, psi=10.0)` floors — 1705 hPa, never 1706 |
| Timing pulled from only some cam-position grids leaves the knock cell reachable | `ignition.retard_cells(...)` writes all nine by default |
| A lambda grid written against the wrong breakpoints is lean at full load | `fueling.lambda_grid(...)` refuses unless the declared breakpoints match the table's live axes |
| A per-slot boost cap above the base ceiling is capped by the base instead | `switchpatch.slot_curve(...)` checks against the live base table and refuses |

`Tunes/TuningBasicsGuide/TUNE_Basics_Guide_R13.py` is the worked example: the
complete R00–R12 calibration in one page of domain calls, verified byte-identical
to the hand-written R12 output (`tests/test_acceptance_tune.py`).

### Renderer-independent build service — `simoscal.tune.build_service`

`build()` above is the *desktop* build: it runs the gate chain and then renders
it — comparison PNGs (matplotlib), `report.md`, `report.html`. An embedded
runtime (Android/Chaquopy) carries no matplotlib and cannot open a browser, so it
needs the same gates and the same verdicts returned as **one machine-readable
model**, not as files.

`build_revision()` does exactly that. It runs `run_gates()` — the identical
save → checksum-verify → readback → blocked-write → coherence → post-check →
byte-audit spine, now factored out of `build()` so the safety gates live once —
and returns a `BuildReport`: a frozen, JSON-serializable object a UI (or a
bridge, or a test) reads. Like `preflight()`, it returns a verdict rather
than raising on a failed gate.

```python
from simoscal.tune import SC8S50, Tune, build_revision

tune = Tune.open(SC8S50, xdf=XDF_PATH, bin=IMPORTED_BIN)
tune.boost.put_ceiling_psi(24.0, intent="park the full-load ceiling")

# For v1 the imported bin is both the edit baseline and the byte-audit reference.
report = build_revision(tune, "R01", staging_dir=STAGING,
                        reference_bin=IMPORTED_BIN, source_bin=IMPORTED_BIN)

if report.verified:
    share(report.share_path)     # the staged bin, or None on any gate failure
print(report.to_json())          # deterministic; the bridge/golden-gate wire form
```

Three properties are structural, not conventions:

- the model is **derived from the journal** (same source as
  `report.md`/`report.html`, so it cannot describe something other than what the
  build did);
- **sharing is gated on the verdict** — `report.share_path` is the staged bin
  only when every gate passed *and* the byte audit ran, else `None`. A failed
  build has no shareable bin;
- **a shared candidate is immutable** — each build writes into its own fresh
  `staging_dir/<revision>-<id>/` directory and never reuses a path, so bytes
  already handed to another app (Android grants a content URI that cannot be
  revoked) can never be rewritten by a later or failing build. `revision` and
  `bin_name` must each be a bare file name; anything carrying a path separator
  raises rather than being sanitized, because on the phone `bin_name` originates
  as an untrusted document-provider display name.

The module imports no matplotlib, so it runs in the on-device engine unchanged.

### The bridge and recoverable sessions

`simoscal.bridge.dispatch()` is the sole Python boundary an embedded client
calls. It accepts and returns deterministic, versioned JSON envelopes; files
cross as private absolute paths plus SHA-256 hashes, never as base64 or Python
objects. Session creation re-runs compatibility preflight itself. Supplying a
switch-patch XDF for an unpatched bin is therefore a hard
`PREFLIGHT_BLOCKED` result rather than a live session over invalid addresses.

Recovery records pin the engine version, source-bin hash, and every XDF hash.
They preserve the ordered journal, compact undo/redo snapshots, and registered
finished-file safety gates. Restore refuses changed provenance or an unknown
gate instead of silently weakening the reopened session. Where the imported bin
is both the source and the byte-audit reference, the bridge enforces that
identity before `build_revision()` can expose a share path.

A client owns scheduling and lifecycle only; Python remains authoritative for
preflight, edits, checksums, readback, byte audit, and the share verdict.

The read-only `journal` op hands back a live session's whole edit journal as flat
text, so a client can show a person what they have changed so far. It is
pointedly **not** a report: no verified flag, no gate rows, no checksum state, no
share path. A report is only ever the atomic product of a `build` gate run
(CR-20260724-02), and re-deriving one from the live journal is the drift that
finding closed — a client rendering this op owes its reader a plain statement
that the list is unverified. Because undo and redo restore the journal wholesale,
re-reading this op is also the only way a client stays correct about what a
session holds; a tally accumulated from edit replies drifts on the first undo.

The read-only `analyze_logs` op runs the whole analysis battery over a set of
verified datalog CSVs and returns the findings document plus `plot_payload()`.
It is **sessionless on purpose**: reading a datalog has nothing to do with
editing a calibration, and requiring an open session would be a gate with no
safety behind it. It writes no file — the desktop entry point's
`analysis_findings.{json,md}` and `plots/` are folder artifacts, and an embedded
client has neither a folder nor a reason for them.

Two differences from `analyze_folder` are deliberate. Bin **autolocation does
not happen**: there is no project tree on a phone, and a check that quietly found
some other bin would be worse than one that skipped, so the calibration is passed
explicitly or the two `needs_cal` checks report SKIPPED. And plots cross as
*series*, not images: matplotlib is outside the embedded dependency closure, so
the client draws them from `PLOT_SPECS`' own declarations rather than deciding
for itself what belongs on a panel. `analyze_logs` is additive and does not bump
`BRIDGE_VERSION`, for the same reason the V8 ops did not.

The read-only `log_overlay` op is the editing surface's counterpart: the detected
pulls and, per pull, the gauge-boost actual and setpoint traces, so a client can
draw a real pull *behind* the boost curves being edited. It is sessionless and
runs no battery — it needs pulls and two series, and coupling it to `analyze_logs`
would make one screen's lifecycle a dependency of another's. Three things stay
the engine's job: which samples belong on the trace (`series_segments` plus
`gear_trim_mask`, so the DSG's early gear flip cannot reach the canvas), what
"boost" means (the `boost` `PlotSpec`'s own reframe, computed once), and which
gear a pull was in (already resolved to an *actual* gear by the channel-header
rule, so no client does gear arithmetic). A log that parses but lacks the boost
channels returns `available: false` with the missing channel names rather than an
error — the file was read fine, it simply has nothing to draw with.

The `limiters` / `limiters_edit` and `lambda_fl` / `lambda_fl_edit` pairs back the
Limiters and Lambda screens. Both edit ops route to domain calls rather than the
generic `edit` op, because each write carries an invariant no single-table grid
edit can see: the road-speed quartet is four tables holding one number, the
cylinder-cut trio must escalate, and the full-load enrichment map has a lean
*direction*. `lambda_fl` sends the engine's own `lean_max`, so the danger band a
client draws is the bound the engine refuses on rather than a UI constant that
can drift from it. The Pedal screen deliberately gets no op: its maps are
ordinary independent grids, so it rides on `catalog`/`table_detail`/`edit`. A
screen is not a reason for an op; an invariant is.

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
| `render_table(view)` → `RenderedTable` | The shared table→grid rendering layer (`symbol`, `title`, `units`, `categories`, `x_labels`, `y_labels`, `x_units`, `y_units`, `values`). Public so Phase 3 (visualization) can reuse it directly. |
| `write_csv(tables, path)` | All tables in **one file**, stacked as labeled grid blocks. |
| `write_xlsx(tables, path)` | Tables grouped onto sheets **by XDF category**; a multi-category table is written onto every one of its categories' sheets. |

### Visualization (Phase 3) — surface / heatmap / line PNGs + comparison

Render any selection of tables to **static PNGs** so a map can be *seen* without
TunerPro. Read-only and additive (no bin-mutation path), built on the same
`RenderedTable`/`render_table()` layer and `select_tables()` selection model as
export. matplotlib is used headless via the object API (no `pyplot`), so import
has no window/backend side effects.

- **2D** table → a 3D **surface** *and* a value-overlaid **heatmap** (every cell
  labeled, TunerPro-style).
- **1D** table → a **line** plot. **Scalar** (1×1) → nothing produced.
- **`compare_tables(a, b)`** is provenance-agnostic — two `.bin`s *or*
  before/after one edit. Delta is `b − a`. 2D → a 3-panel composite (A and B on
  a shared scale, delta on its own zero-centered diverging scale); 1D → a 2-panel
  composite (overlay + delta). Mismatched shapes/axes **hard-fail** with
  `TableMismatchError` naming both tables — never a misleading plot.

```python
from simoscal import CalFile, plot_tables, compare_bins, compare_tables, render_table

cal = CalFile.open("xdf/SC8S50.V1.0.xdf", "bin/5G0906259L__0002.bin")
plot_tables(cal, "plots/", category="Boost Control")   # PNGs under plots/Boost Control/

# Compare the same tables across two bins (stock vs tuned):
stock = CalFile.open("xdf/SC8S50.V1.0.xdf", "bin/stock.bin")
tuned = CalFile.open("xdf/SC8S50.V1.0.xdf", "bin/tuned.bin")
compare_bins(stock, tuned, "diffs/", category="Boost Control")

# Before/after one in-session edit — no second bin (render_table snapshots):
view = cal.get("ID_PORT_SP")
before = render_table(view)          # holds the pre-edit values
view.set_cell(0, 0, 12.5)
compare_tables(before, view, "review/")
```

| Member | Description |
|--------|-------------|
| `plot_table(source, out_dir, *, surface=True, heatmap=True, value_cmap="viridis", fmt="{:.4g}", elev=30, azim=-120)` | Render one table (`TableView` or `RenderedTable`) to PNG(s), flat into `out_dir`. Returns written paths. |
| `compare_tables(a, b, out_dir, *, surface=True, heatmap=True, value_cmap="viridis", delta_cmap="RdBu_r", ...)` | Composite comparison of two views of one table (`b − a`). Raises `TableMismatchError` on shape/axis mismatch. |
| `plot_tables(cal, out_dir, *, symbols=None, category=None, all_tables=False, ...)` | Batch-plot a selection into per-category subfolders (`_uncategorized/` for a category-less table). |
| `compare_bins(cal_a, cal_b, out_dir, *, symbols=None, category=None, all_tables=False, ...)` | Batch-compare a selection across two bins, matched by `uniqueid` (fails loud if `cal_b` lacks a match). |
| `TableMismatchError` | Raised by the compare path when two tables are not comparable. |

Output model: one PNG set per table under `out_dir/<category>/`; a multi-category
table is duplicated under each of its categories (mirroring Phase 2 xlsx). Files
are `<name>__<kind>.png` where `name` = symbol → title → uniqueid and `kind` ∈
{`surface`, `heatmap`, `line`, `compare_surface`, `compare_heatmap`,
`compare_line`}. Defaults: `viridis` values / `RdBu_r` delta, both overridable
(e.g. `value_cmap="turbo"` for a TunerPro-like look); surfaces bake in a fixed
camera (`elev=30, azim=-120`, tunable) since the PNG is non-interactive.

### SOP tune recipe — `apply_basics_sop`

Scripts the concrete, log-independent instructions from
`knowledge/ecu-tuning-basics.md` onto the stock bin via the read/edit/write API
above — no new safety, checksum, or plotting logic, and **no flashing**. It
produces **revision 0 of a tune: a starting point, not a finished calibration**
(the intended loop is recipe → review → flash → log → review → iterate).

The one source of truth is `SYMBOL_MAP`: one reviewable entry per guide
instruction (in-scope *and* explicitly-skipped), each mapping a guide section to
its XDF symbol(s), a target value/curve/rule, and a *treatment*. `apply_basics_sop`
resolves the map against a live `CalFile`, applies each entry in guide order, and
returns a `RecipeReport` — it **stages edits in memory and does not touch disk**
(saving, verifying, and PNG generation are the caller's job; see
`demos/apply_sop_recipe.py`).

```python
from simoscal import CalFile, apply_basics_sop, format_report

cal = CalFile.open("xdf/SC8S50.V1.0.xdf", "bin/5G0906259L__0002.bin")
report = apply_basics_sop(cal)                 # stages edits, returns the report
print(format_report(report))                   # DO NOT FLASH banner first, if any
cal.save("tuned.bin", correct_checksums=True)  # then verify + review before flashing
```

Fail-loud, per the library mandate — the recipe never guesses:

- **axis-matched literal writes** — a literal grid is written only when the
  table's own axis breakpoints match the guide's; a bin whose axes differ (e.g.
  the lambda tables, whose stock breakpoints differ from the guide's example bin)
  is reported `axis_mismatch` and left byte-identical, never written to the wrong
  cells;
- **guarded ceiling raises** never write a lower value over a higher one
  (`guarded_skip`), and float-bug-flagged limiter writes that trip the existing
  `FloatBugGuardError` are caught per-entry as `guard_blocked` — the table stays
  byte-identical and the recipe continues;
- **unresolved / vague / out-of-scope** instructions are reported, never guessed;
- a **coherence check** opens the report with **DO NOT FLASH** when dependent
  entries diverge (e.g. boost curve applied without lambda enrichment → lean
  risk). The bin still saves — the human review gate decides.

| Member | Description |
|--------|-------------|
| `apply_basics_sop(cal, symbol_map=SYMBOL_MAP)` → `RecipeReport` | Resolve + apply the whole SOP to an open `CalFile`, staging edits in memory. Deterministic and re-runnable from the stock bin. |
| `resolve_symbol_map(cal, symbol_map=SYMBOL_MAP)` → `list[ResolvedEntry]` | Resolve every entry's symbol(s) against the bin; failures are data (`resolved=False` + reason), never exceptions. |
| `RecipeReport` | Frozen wrapper over per-table `TableOutcome`s; `.by_outcome()`, `.counts()`, `.coherence()`, `.do_not_flash()`. |
| `format_report(report)` → `str` | Aligned Markdown grouped by outcome; DO NOT FLASH coherence section first; scalar old→new always shown. |
| `SYMBOL_MAP` | The reviewable guide-instruction → symbol(s) → target/treatment table. |

Outcomes: `applied` · `applied_buildout` (TTA/ATT linear build-out) ·
`already_satisfied` · `guarded_skip` · `guard_blocked` · `axis_mismatch` ·
`poor_fit` · `unresolved` · `skipped`. Scalar `(1,1)` edits produce no
comparison PNG by design (Phase 3 `compare_tables`) — they are reviewed via the
report's old→new values instead; every changed non-scalar table gets a
before/after PNG from the demo.

### BTP patching — `simoscal.btp` (BinToolz `.btp` adapter)

Applies BinToolz `.btp` patches (e.g. the 5-slot on-the-fly map **switch patch**)
to a bin with the same fail-loud guarantees as every other bin operation. It
**wraps** BinToolz's Qt-free byte layer (imported at runtime from
`../BinToolz-main/source`, never ported — the license carries no derivation grant)
and layers `simoscal` guards on top. **Never flashes; never patches in place** —
the input bin is read-only and each apply/remove writes a *new* file.

```python
from simoscal import btp

pre = btp.check("bin/5G0906259L__0002.bin", ".../SL PATCH.29.33 - S50.btp")
print(pre.readiness)                       # READY_TO_ACCEPT / PATCH_FOUND / NOT_READY

res = btp.apply(stock_bin, patch, "patched.bin")   # requires READY_TO_ACCEPT
print(res.changed_bytes, res.confined)     # confined to the patch's declared blocks
print(res.cal_crc.is_stale, res.ecm3.is_stale)     # checksum state, reported not assumed
print(btp.format_change_report(res))       # markdown review report

btp.remove("patched.bin", patch, "back.bin")       # round-trips to byte-identical stock
```

| Member | Description |
|--------|-------------|
| `check(bin, patch, *, bintoolz_root=None)` → `PatchCheckResult` | Read-only readiness + identity guards; never writes. |
| `apply(bin, patch, out, *, bintoolz_root=None)` → `ChangeResult` | Apply on a copy (requires READY_TO_ACCEPT); post-verify confined diff + checksum report. |
| `remove(bin, patch, out, *, bintoolz_root=None)` → `ChangeResult` | Remove on a copy (requires PATCH_FOUND); round-trips apply. |
| `switch_patch_sanity(bin, *, xdf_path=None, stock_bin_path=None, ...)` → `SanityResult` | Load the patched bin against the switch-patch XDF; slot/switch tables resolve, decode, and differ from stock. |
| `format_change_report(result)` → `str` | Markdown apply/remove report for the review gate. |

Guarantees (fail loud, never guess): identity guards hard-fail on hardware /
software-code / **file-size** mismatch (`PatchIdentityError`) before any write;
the patch's own CRC32 self-check surfaces as `PatchIntegrityError`; apply/remove
refuse a bin not in the required state (`PatchStateError`); a post-apply full-bin
diff asserts every changed byte lies inside the patch's declared blocks
(`PatchConfinementError`); and a missing / drifted BinToolz tree raises
`BinToolzNotFound` (`AE7`). Applying the switch patch leaves **`CAL_CRC` stale**
(the `.btp` carries no corrected CAL CRC — correct it before flashing) and
**`ECM3` clean**; ASW/code block checksums are **not-verifiable** here (outside
`simoscal`'s scope — SimosTools/VW_Flash compute them at full-flash time). All
three states are reported explicitly, never assumed. See
`demos/apply_btp_patch.py` for the canonical stock→patch→verify pipeline and
`knowledge/bintoolz-btp-patching.md` "U1 findings" for the checksum/XDF evidence.

### Log analysis battery — `simoscal.analysis`

Runs an identical, enumerable battery of checks against a `Logs/<Tune>_R<NN>/`
folder of SimosTools datalog CSVs and writes a machine-readable findings file, a
rendered Markdown summary, an explicit SKIPPED list, evidence plots, and
per-table coverage maps into that folder. It is **findings-only and read-only**:
Claude consumes the output to write `log_review.md` — the tool **never writes
`log_review.md` and never proposes or writes a calibration change**. It consumes
the rest of `simoscal` read-only (opening the flashed bin via `CalFile` for the
calibration-aware checks) and inherits the fail-loud mandate: a channel it cannot
confidently resolve is reported unmapped rather than mis-scaled, and a check
whose required channels (or bin) are absent lands in SKIPPED rather than firing
on wrong data.

```python
from simoscal.analysis import analyze_folder

out = analyze_folder("Logs/BasicsGuide_R04")   # autolocates the flashed bin
print(out.result.high_findings)                # ranked findings
print(out.json_path, out.md_path)              # written into the folder
```

```bash
python -m simoscal.analysis Logs/BasicsGuide_R04     # writes findings + plots
python -m simoscal.analysis --print-battery          # enumerate the battery, run nothing
```

| Member | Description |
|--------|-------------|
| `analyze_folder(folder, *, xdf_path=None, bin_path=None, make_plots=True)` → `AnalyzeResult` | Load CSVs, detect pulls, autolocate the bin, run the battery + coverage, write `analysis_findings.{json,md}` and `plots/analysis_*.png` into the folder. |
| `load_logset(folder)` → `LogSet` | Parse `simostools-*.csv` into canonical, unit-normalized channels (airmass→mg/stk, rail→bar) with header-rule gear resolution and a non-mutating quality preflight; dedups trimmed re-exports of one capture. |
| `load_logset_files(paths, *, folder=None, dedup=True, names=None)` → `LogSet` | The explicit-path form `load_logset` delegates to, for a caller with no folder to glob — the Android app, whose copy of each CSV is content-addressed. `names` carries the display name the picker showed, since the filename on disk is a hash. |
| `plot_payload(ctx)` → `list[dict]` | Every evidence plot in `PLOT_SPECS` as JSON-safe series — the same masked, segmented, rpm-sorted samples the PNGs are drawn from. What the bridge's `analyze_logs` op sends a client that cannot render matplotlib. |
| `overlay_payload(ctx)` → `dict` | The detected pulls, each with its gauge-boost actual and setpoint traces, for drawing a logged pull behind the boost curves being edited. Organised by pull rather than by panel, and gear-trimmed. What the bridge's `log_overlay` op sends. |
| `gear_trim_mask(ctx, pull)` → `ndarray` | Samples whose logged gear is the pull's attributed gear — drops the tail the DSG's gear channel mislabels before a shift lands. All-True when gear is unresolved. |
| `detect_pulls(logset)` → `list[Pull]` | Segment WOT pulls + per-pull summary with environment context. |
| `default_battery()` → `list[Check]` · `run_battery(checks, ctx)` → `BatteryResult` | The v1 battery (knock, boost, wastegate, lambda, rail, timing, turbo/heat, torque limiter, data quality, + a `needs_cal` boost-ceiling check) and its runner. |
| `compute_coverage(ctx)` → `(results, skipped)` | Per-cell hit-count maps (whole-log + WOT-only) for the primary tuning tables via ECU-lookup simulation. |
| `format_battery(checks)` → `str` | Print the enumerable battery (ids, channels, thresholds) without running it. |

Output contract per folder: `analysis_findings.json` (sorted keys, fixed float
formatting — byte-identical across identical reruns), `analysis_findings.md`
(findings by severity, SKIPPED, aligned pull table + environment, coverage,
battery enumeration), and `plots/analysis_*.png`. Evidence plots follow one
encoding rule — **quantity = line style, pull = color** (each pull an
RPM-sorted solid line, setpoint/base/table dashed dark gray). The six per-check
plots (`boost`, `knock`, `lambda`, `rail_pressure`, `turbo_heat`, `wastegate`)
are referenced from their findings; three standalone plots are additive:
`ignition` (delivered vs table timing vs RPM), `overview_<log>` (one whole-log
panel-stack per CSV vs time with detected pull windows shaded), and
`tc_activity_<log>` (per CSV, inferring the switch-patch slip-based TC — wheel
slip, ignition, wastegate, torque — skipped when no wheel-speed channel is
present).
**The plot inventory is data, in one place.** `simoscal/analysis/series.py`
declares every rpm-axis evidence plot — panels, series and their roles, threshold
lines, and the description/tip prose printed above each one — as `PLOT_SPECS`.
`evidence.py` renders those declarations to PNG and `bridge.analyze_logs`
serializes them to JSON, both drawing their samples from the one shared
`series_segments()`. The reason is drift: matplotlib is outside the Android
dependency closure, so the app must draw its own plots, and an inventory decided
twice is an inventory that ends up describing the same log two ways.
`test_plot_payload_matches_the_png_inventory` pins the two together. The
per-file time-axis plots (`overview`, `tc_activity`) stay imperative and
desktop-only.

Thresholds are seeded from the R01/R04 reviews and live as inspectable registry
data. Acceptance replay (`tests/test_acceptance_analysis.py`) reproduces the
R01/R04 headline findings with **no false High** — every High the tool emits is
one the human review also called High.

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
./.venv/bin/python -m pytest tests/test_acceptance_plot.py -v     # AE1–AE9 (Phase 3 viz)
./.venv/bin/python -m pytest tests/test_acceptance_sop.py -v      # AE1–AE5 (SOP tune recipe)
./.venv/bin/python -m pytest tests/test_acceptance_btp.py -v      # AE1–AE7 (BTP patching)
./.venv/bin/python -m pytest tests/test_acceptance_analysis.py -v # R01/R04 log-analysis replay
./.venv/bin/python -m pytest tests/test_acceptance_tune.py -v     # AE1 (R13 ≡ R12, byte-identical)
```

`test_btp.py` (synthetic fixtures) and `test_acceptance_btp.py` (real files) skip
cleanly when the vendored `BinToolz-main/` tree, the real switch patch, or the
stock bin are absent — the BTP adapter wraps BinToolz at runtime.

The `simoscal.analysis` unit tests (`test_analysis_*.py`) run entirely on
synthetic logs built by `tests/faultinject.py` (fault injection) and
`tests/synthlog.py`. `test_acceptance_analysis.py` replays the real
human-reviewed `Logs/BasicsGuide_R01`/`_R04` folders and skips cleanly when they
are absent from a lean `Code/` checkout.

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
(Phase 2); static-PNG visualization (surface/heatmap/line) and provenance-
agnostic comparison composites (Phase 3); check / apply / remove of BinToolz
`.btp` patches with confined-diff post-verification and checksum reporting
(`simoscal.btp`, wrapping BinToolz); a read-only, findings-only log-analysis
battery over SimosTools datalog folders with evidence plots and per-table
coverage maps (`simoscal.analysis`).
**Out:** flashing (SimosTools/VW_Flash), checksum *recompute* beyond the
optional correction path, CBOOT/ASW editing, `.btp` *creation* (`patchCreate`)
and BinToolz's ignore-data CAL-skip mode, FRF→BIN extraction,
GUI/CLI, import/round-trip from an exported file back into a `.bin`;
interactive/on-screen viewing, vector (SVG/PDF) output, >2-bin comparison
(Phase 3 out-of-scope); for `simoscal.analysis`, authoring `log_review.md`
(Claude's job) and any calibration proposer/orchestration/bin-writing
(deferred). Datalog-driven auto-tuning (Phase 4) is a later phase
that consumes this library read-only and writes *through* this writer,
inheriting its guards.
