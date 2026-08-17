# Code Review Log

A living record of code reviews for the `simoscal` XDF/BIN library. Each review
is appended as a dated section below; findings are never deleted, only updated
in place as they are fixed or dismissed.

## How to use this file

- **Adding a review:** append a new `## Review YYYY-MM-DD — <scope>` section at
  the end of the file (newest last). Give every finding an ID of the form
  `CR-YYYYMMDD-NN` and add a row to the index table below.
- **Finding lifecycle:** update the `Status` column in the index (and a short
  note in the finding body) when a finding changes state. States:
  `Open` → `Fixed (YYYY-MM-DD)` / `Dismissed (reason)` / `Superseded (by CR-...)`.
- **Severity:** `High` = weakens a safety guarantee or breaks a user on first
  contact; `Medium` = wrong on realistic future inputs, or latent hazard;
  `Low` = cleanup, docs, efficiency.
- **Verdicts:** `CONFIRMED` = reproduced/proven against the code as written;
  `PLAUSIBLE` = mechanism verified but requires a realistic-but-not-current
  state to trigger.

## Findings index

| ID             | Severity | Verdict   | File                                                | Summary                                                                     | Status              |
|----------------|----------|-----------|-----------------------------------------------------|-----------------------------------------------------------------------------|---------------------|
| CR-20260706-01 | High     | CONFIRMED | tests/test_acceptance.py                            | AE3 diff blind to appended bytes; no length assert on edited save           | Open                |
| CR-20260706-02 | High     | CONFIRMED | tests/conftest.py                                   | Safety suite is skip-if-absent with no way to force a non-skipped run       | Open                |
| CR-20260706-03 | High     | CONFIRMED | tests/test_acceptance.py                            | AE3 offset math ignores `base_subtract`                                     | Open                |
| CR-20260706-04 | High     | CONFIRMED | tests/test_acceptance.py                            | AE1 tolerance is unbounded and controlled by the capture under test         | Open                |
| CR-20260706-05 | Medium   | CONFIRMED | tests/test_acceptance.py                            | AE4 precondition unsound for declared max in [120, 127)                     | Open                |
| CR-20260706-06 | High     | CONFIRMED | README.md                                           | Quick-start paths do not resolve from the documented cwd                    | Open                |
| CR-20260706-07 | Medium   | CONFIRMED | README.md                                           | Quick-start example writes out-of-range value, fires EditRangeWarning       | Open                |
| CR-20260706-08 | Medium   | CONFIRMED | tests/conftest.py                                   | `real_cal` fixture silently shadowed by test_read.py module fixture         | Open                |
| CR-20260706-09 | Medium   | CONFIRMED | tests/test_acceptance.py                            | AE2/AE3/AE5 duplicate pre-existing tests near-verbatim                      | Open                |
| CR-20260706-10 | Medium   | PLAUSIBLE | tests/test_acceptance.py                            | int8 wraparound in AE3 whole-table `+1` before clip                         | Open                |
| CR-20260706-11 | Low      | PLAUSIBLE | tests/test_acceptance.py                            | AE4 asserts on `rec[0]` without filtering warning category                  | Open                |
| CR-20260706-12 | Low      | PLAUSIBLE | tests/conftest.py                                   | Oracle JSON read without `utf-8-sig`; Windows BOM fails a valid capture     | Open                |
| CR-20260706-13 | Low      | CONFIRMED | tests/fixtures/README.md                            | mini.xdf documented as 3 tables; it contains 4                              | Open                |
| CR-20260706-14 | Low      | CONFIRMED | tests/conftest.py                                   | `requires_real_files` marker is dead code (fifth copy of the guard)         | Open                |
| CR-20260706-15 | Low      | CONFIRMED | tests/test_acceptance.py                            | No guard pins ORACLE_ID identity/shape/dtype                                | Open                |
| CR-20260706-16 | Low      | CONFIRMED | tests/ (multiple)                                   | Oracle JSON schema exists in three uncoordinated copies                     | Open                |
| CR-20260706-17 | Low      | CONFIRMED | tests/test_acceptance.py                            | Redundant `checked` counter duplicates fixture guard                        | Open                |
| CR-20260706-18 | Low      | CONFIRMED | README.md                                           | AE gating story told in three prose locations                               | Open                |
| CR-20260706-19 | Low      | CONFIRMED | tests/ (multiple)                                   | ~2.5 s/run avoidable XDF re-parsing and slow Python byte-diff loops         | Open                |
| CR-20260706-20 | Low      | CONFIRMED | tests/fixtures/README.md                            | AE1 capture procedure relies on error-prone hand transcription              | Open                |
| CR-20260706-21 | High     | CONFIRMED | simoscal/codec.py                                   | 2D table decode uses row-major reshape against column-major on-bin data     | Fixed (2026-07-06)  |
| CR-20260706-22 | High     | CONFIRMED | simoscal/xdf.py                                     | mmedtypeflags sign bit inverted for at least three real int16/int32 tables  | Fixed (2026-07-06)  |
| CR-20260707-01 | Medium   | PLAUSIBLE | simoscal/sop_recipe.py                              | Multi-cell writers leave a table partly written if a guard trips mid-loop   | Fixed (2026-07-07)  |
| CR-20260707-02 | Low      | CONFIRMED | simoscal/sop_recipe.py                              | Vestigial row_idx/col_idx locals only None-checked in _apply_literal_table  | Fixed (2026-07-07)  |
| CR-20260707-03 | High     | CONFIRMED | Tunes/TuningBasicsGuide/TUNE_Basics_Guide_R01.py    | Max allowed airmass written as TunerPro workaround value, not physical 2000 | Dismissed (invalid) |
| CR-20260707-04 | Medium   | CONFIRMED | Tunes/TuningBasicsGuide/TUNE_Basics_Guide_R01.py    | Merged report shows R01-covered guide items as both applied and skipped     | Fixed (2026-07-07)  |
| CR-20260707-05 | Medium   | CONFIRMED | Tunes/TuningBasicsGuide/TUNE_Basics_Guide_R01.py    | Coherence-passed banner hides unresolved in-scope guide fueling items       | Open                |
| CR-20260720-01 | High     | CONFIRMED | simoscal/tune/pipeline.py                           | Unverifiable checksums are reported CLEAN and do not fail the build         | Fixed (2026-07-20)  |
| CR-20260720-02 | High     | CONFIRMED | simoscal/tune/project.py                            | Intentional restoration to stock fails as unexplained bytes                 | Fixed (2026-07-20)  |
| CR-20260720-03 | Medium   | CONFIRMED | pyproject.toml                                      | Installed distributions omit all tune domain modules                        | Fixed (2026-07-20)  |
| CR-20260720-04 | Medium   | CONFIRMED | ../Tunes/TuningBasicsGuide/TUNE_Basics_Guide_R13.py | R13 template omits the required explicit intent on most domain calls        | Fixed (2026-07-20)  |
| CR-20260724-01 | High     | CONFIRMED | simoscal/tune/build_service.py                      | Extra allowances make unjournaled edits verified, shareable, and invisible  | Fixed (2026-07-24)  |
| CR-20260724-02 | High     | CONFIRMED | simoscal/tune/build_service.py                      | Public report assembler can emit internally contradictory shareable reports | Fixed (2026-07-24)  |
| CR-20260724-03 | Medium   | CONFIRMED | simoscal/tune/build_service.py                      | Unchanged declarations are reported as changed tables                       | Fixed (2026-07-24)  |
| CR-20260724-04 | High     | CONFIRMED | simoscal/export.py                                  | Plot extra imports undeclared openpyxl dependency                           | Fixed (2026-07-24)  |
| CR-20260724-05 | Medium   | CONFIRMED | android/engine/build.gradle.kts                     | Embedded NumPy runtime floats across unchanged APK builds                   | Fixed (2026-07-24)  |
| CR-20260724-06 | High     | CONFIRMED | simoscal/tune/recovery.py                           | Recovery accepts changed XDF table definitions                              | Fixed (2026-07-24)  |
| CR-20260724-07 | High     | CONFIRMED | simoscal/tune/recovery.py                           | Recovery silently drops finished-file safety gates                          | Fixed (2026-07-24)  |
| CR-20260724-08 | Medium   | CONFIRMED | simoscal/tune/recovery.py                           | Recovery ignores its within-engine-version marker                           | Fixed (2026-07-24)  |
| CR-20260724-09 | Medium   | CONFIRMED | simoscal/tune/recovery.py                           | Recovered sessions lose their undo/redo history                             | Fixed (2026-07-24)  |
| CR-20260724-10 | High     | CONFIRMED | simoscal/bridge.py                                  | Session creation can bypass compatibility preflight                         | Fixed (2026-07-24)  |
| CR-20260724-11 | High     | CONFIRMED | simoscal/tune/editing.py                            | Generic axis writes accept nonmonotonic breakpoints                         | Fixed (2026-07-24)  |
| CR-20260724-12 | Medium   | CONFIRMED | tests/test_bridge.py                                | Edit tests fail before reaching the bridge dispatcher                       | Fixed (2026-07-24)  |
| CR-20260724-13 | High     | CONFIRMED | simoscal/tune/recovery.py                           | Undo restores bytes but leaves profile views stale                          | Fixed (2026-07-24)  |
| CR-20260724-14 | Medium   | CONFIRMED | android/README.md                                   | V0 declared GO before physical-arm64 and x86_64 parity gates ran            | Fixed (2026-08-16)  |
| CR-20260724-15 | Low      | CONFIRMED | tests/test_packaging.py                             | No test installs a built wheel; the closure test imports the source tree    | Fixed (2026-07-24)  |
| CR-20260724-16 | Low      | PLAUSIBLE | simoscal/bridge.py                                  | `bridge_info` cannot discover a mismatched engine on its documented path    | Open                |
| CR-20260813-01 | High     | CONFIRMED | android/.../QuickEditViewModel.kt                   | Generic patch edits bypass domain guards and produce shareable bins         | Fixed (2026-08-14)  |
| CR-20260813-02 | High     | PLAUSIBLE | simoscal/tune/build_service.py                      | A later build can overwrite a candidate already granted to another app      | Fixed (2026-08-14)  |
| CR-20260813-03 | Medium   | CONFIRMED | android/.../QuickEditViewModel.kt                   | SAF imports and SHA-256 hashing run synchronously on the main thread        | Fixed (2026-08-14)  |
| CR-20260813-04 | Medium   | CONFIRMED | android/.../QuickEditViewModel.kt                   | Undo, redo, and rpm-axis apply silently discard unapplied editor drafts     | Fixed (2026-08-14)  |
| CR-20260813-05 | Medium   | PLAUSIBLE | simoscal/tune/build_service.py                      | Unsanitized provider display names can escape the staging directory         | Fixed (2026-08-14)  |
| CR-20260815-01 | High     | CONFIRMED | android/.../BridgeClient.kt                         | Every file-naming bridge op sends the path under the bare key, not `_path`  | Fixed (2026-08-15)  |
| CR-20260815-02 | Medium   | CONFIRMED | simoscal/bridge.py                                  | An unreadable switch-patch XDF is reported as an absent patch in the bin    | Fixed (2026-08-15)  |
| CR-20260815-03 | Low      | CONFIRMED | android/README.md                                   | V7 test-count table stale: claims 93 tests, actual 104 before this review   | Fixed (2026-08-15)  |
| CR-20260815-04 | High     | CONFIRMED | simoscal/tune/profiles/sc8s50.py                    | Generic editor can write the kg/stk airmass ceiling in its lying mg/stk unit | Fixed (2026-08-15)  |
| CR-20260815-05 | High     | CONFIRMED | simoscal/tune/domains/switchpatch.py                | Switch-patch build gate resolves a desktop BinToolz path; fails on any device | Fixed (2026-08-15)  |
| CR-20260815-06 | Medium   | PLAUSIBLE | simoscal/tune/profiles/sc8s50.py                    | An unmapped table reaches the generic editor with no guard tags or owner    | Fixed (2026-08-15)  |
| CR-20260816-01 | High     | CONFIRMED | simoscal/tune/recovery.py                           | A session that has been built can never be recovered — the build's checksum bytes are outside the byte diff | Fixed (2026-08-16)  |

---

## Review 2026-07-06 — U6 (fixtures + AE1–AE5 acceptance suite + READMEs)

- **Scope:** the four files shipped in U6, reviewed as whole-file diffs (all new):
  `tests/conftest.py`, `tests/test_acceptance.py`, `tests/fixtures/README.md`,
  `README.md`; plus the U6 edit to `Docs/plans/2026-07-05-001-feat-xdf-bin-library-plan.md`.
- **Method:** `/code-review high` — 8 independent finder angles (line-by-line,
  weakened-guarantees, cross-file tracer, reuse, simplification, efficiency,
  altitude, CLAUDE.md conventions), ~24 deduplicated candidates, each verified
  by an independent verifier pass (CONFIRMED / PLAUSIBLE / REFUTED).
- **Headline:** no library-code bugs found. Verifiers confirmed all `simoscal`
  API usage, the oracle-schema agreement between README and test, the
  `tunerpro` marker registration, and the ORACLE_ID identity (0x11F9C =
  ID_PORT_SP, 10×10 int8). All findings are in the new test/doc layer, in
  three clusters: (1) safety assertions weaker than they claim, (2) the safety
  spine can silently stop running, (3) the README quick start fails or
  mis-teaches on first contact.

### CR-20260706-01 — AE3 diff blind to appended bytes — High, CONFIRMED — Open

`tests/test_acceptance.py:124` (and `:150`). The minimal-diff check builds
`diff = [i for i in range(len(before)) if before[i] != after[i]]`, iterating
only over the original length, and no assertion pins `len(after) ==
len(before)` on the edited-save path (AE2's length-equality checks cover only
the zero-edit path).

**Failure scenario:** a writer regression that pads or appends data when edits
are staged produces an `out.bin` longer than the input; `diff` still equals
`[cell_offset]` and both AE3 tests pass while the saved bin is not
flash-equivalent. (A shorter output raises IndexError — loud; a longer one
passes silently.)

**Suggested fix:** `assert len(after) == len(before)` in both AE3 tests.

### CR-20260706-02 — Safety suite skip-if-absent with no strict mode — High, CONFIRMED — Open

`tests/conftest.py:44` (and `:52`). The real-file fixtures `pytest.skip()`
when the XDF/BIN are absent, by design (documented in the conftest docstring).
But no mechanism anywhere — env var, CI assertion, pytest addopts — can force
the skips to become failures, and default `pytest -q` output barely surfaces
them.

**Failure scenario:** the stock bin is renamed (new box code) or the repo is
cloned without the 4 MB binaries: AE2 byte-identical round-trip, AE3
minimal-diff, and AE4 warn-loud never execute again and the suite stays green
forever — a writer regression that corrupts adjacent bytes ships undetected.

**Suggested fix:** honor e.g. `SIMOSCAL_REQUIRE_REAL_FILES=1` in the fixtures
to turn skips into failures, and set it wherever a real-file run is expected.

### CR-20260706-03 — AE3 offset math ignores `base_subtract` — High, CONFIRMED — Open

`tests/test_acceptance.py:125` (and `:141`). The expected changed-byte offset
is hand-rolled as `emb.address + real_cal.model.base_offset`, but the library
itself uses `codec.file_offset_for(address, base_offset, base_subtract)`,
which computes `address - base_offset` when the subtract flag is set.
`file_offset_for` is publicly exported from `simoscal/__init__.py`.

**Failure scenario:** passes today only because SC8S50.V1.0.xdf declares
`BASEOFFSET ... subtract="0"`. Against any subtract-style XDF the library
correctly writes at `address − base` while the test expects `address + base`:
a correct one-byte edit fails AE3, or the divergence masks a real writer bug.

**Suggested fix:** call
`simoscal.file_offset_for(emb.address, model.base_offset, model.base_subtract)`.

### CR-20260706-04 — AE1 tolerance unbounded and self-controlled — High, CONFIRMED — Open

`tests/test_acceptance.py:52` (default), `:57` (per-table), `:67-71`
(comparison). The tolerance is read from the capture file itself (top-level
`"tolerance"`, per-table `"tol"`) with no upper bound, feeding
`assert_allclose(..., atol=tol, rtol=0)`. `tests/fixtures/README.md` even
suggests loosening (e.g. 0.5) with no ceiling guidance. The 0.01 default is
atol-only, which spans ~10 raw LSBs on a table with scaling m=0.001.

**Failure scenario:** a capture recorded with `"tolerance": 100` (someone
thinking percent) makes the comparison vacuous — a decode off by an entire
scaling factor passes, and Decision 6 (mmedtypeflags semantics) is declared
independently confirmed by a test that verified nothing.

**Suggested fix:** cap `tol` at a sane ceiling (fail the test if exceeded) and
default to exact match for integer tables.

### CR-20260706-05 — AE4 precondition unsound for zmax in [120, 127) — Medium, CONFIRMED — Open

`tests/test_acceptance.py:164` (guard) and `:167` (target). The guard asserts
`zmax < 127` but `target = min(zmax + 1.0, 120.0)` fails to exceed the
declared max whenever zmax is in [120, 127) — `safety.py:140` warns only when
`v > mx + tol`, so no warning fires. Currently harmless (ID_PORT_SP has
zmax = 1.0).

**Failure scenario:** a future XDF revision (or repointed ORACLE_ID) with
declared max e.g. 125: the guard passes, target = 120 is in range, and
`pytest.warns(EditRangeWarning)` fails "DID NOT WARN" — a spurious AE4
failure whose message doesn't name the real precondition.

**Suggested fix:** use `target = zmax + 1.0` unconditionally with a
raw-headroom guard, or tighten the guard to `zmax < 119`.

### CR-20260706-06 — README quick-start paths don't resolve — High, CONFIRMED — Open

`README.md:34`. The Install section (line 22) puts the user in `Code/`, but
the quick start opens `"xdf/SC8S50.V1.0.xdf"` / `"bin/5G0906259L__0002.bin"`,
which only resolve from the repo root — `Code/xdf` does not exist.

**Failure scenario:** a user follows the README verbatim and
`CalFile.open` raises `FileNotFoundError` on the very first example.

**Suggested fix:** use `../xdf/...` and `../bin/...`, or state the expected
working directory next to the snippet.

### CR-20260706-07 — README quick-start example writes out-of-range value — Medium, CONFIRMED — Open

`README.md:42`. `port.set_cell(0, 0, 12.5)` targets ID_PORT_SP, whose declared
display range in the real XDF is `<min>0.0</min> <max>1.0</max>` (a
semantically boolean port-flap map), so the canonical first-contact example
fires `EditRangeWarning` and writes 12 into a 0/1 table.

**Failure scenario:** every new user's first run demonstrates the library's
out-of-range safety warning firing on the official example — teaching from day
one that `EditRangeWarning` is noise to ignore, the opposite of the fail-loud
stance.

**Suggested fix:** set an in-range value (e.g. `1.0`), or pick a table where a
non-trivial edit is in range.

### CR-20260706-08 — `real_cal` fixture shadowed in test_read.py — Medium, CONFIRMED — Open

`tests/conftest.py:57` vs `tests/test_read.py:299`. The new function-scoped
`real_cal` (documented: "each test gets an independent, unedited image") is
silently shadowed for all of `test_read.py` by that module's pre-existing
module-scoped fixture of the same name, which returns one shared `CalFile`.
The `REPO_ROOT`/`REAL_XDF`/`REAL_BIN` constants are also duplicated between
conftest and four test modules.

**Failure scenario:** a developer adds an edit-performing test to
`test_read.py` relying on the conftest contract: the module-local fixture
wins, staged edits and mutated `BinImage` bytes leak into subsequent tests,
producing order-dependent failures. Latent today (test_read.py is read-only).

**Suggested fix:** delete `test_read.py`'s local `real_cal` and duplicated
constants (renaming if a shared module-scoped variant must stay).

### CR-20260706-09 — AE2/AE3/AE5 duplicate pre-existing tests — Medium, CONFIRMED — Open

`tests/test_acceptance.py:81-152` vs `tests/test_roundtrip.py:27-91`, and
`tests/test_acceptance.py:183-224` vs `tests/test_write.py:175-189`. The four
AE2/AE3 acceptance tests re-implement four `test_roundtrip.py` tests
near-verbatim — two share exact test names — and the AE5 block
(`_NONLINEAR_XDF`, fixture, both tests) duplicates `test_write.py`'s
non-linear tests, which already sit under a section header labeled "AE5".

**Cost:** the safety-critical assertions exist in two separately-maintained
copies; a fix to the diff/offset/clip logic or a change to non-linear error
semantics must land twice or one suite asserts stale behavior. Duplicate test
names across modules also confuse failure reports.

**Suggested fix:** keep one copy — port the older tests onto the conftest
fixtures and make the acceptance file the single owner, or drop the acceptance
duplicates and point the AE table at the existing tests.

### CR-20260706-10 — int8 wraparound in AE3 whole-table nudge — Medium, PLAUSIBLE — Open

`tests/test_acceptance.py:144`. `np.clip(np.array(view.raw) + 1, -128, 127)`
computes in the table's int8 dtype: a raw cell holding 127 wraps silently to
−128 *before* clip runs (verified: numpy 2.5.1 emits no warning), staging an
unintended −128 via `set_raw` — the exact silent-corruption pattern the suite
exists to catch. Latent: the table currently holds only raw 0/1.

**Failure scenario:** oracle table (or future replacement) contains a raw 127;
the test still passes its extent assertion while the staged bin holds a value
that changed by −255 instead of +1.

**Suggested fix:** widen before arithmetic:
`np.clip(np.asarray(view.raw, dtype=np.int64) + 1, -128, 127)`.

### Lower-severity findings (verified, below the top-10 cut line)

#### CR-20260706-11 — AE4 asserts on `rec[0]` unfiltered — Low, PLAUSIBLE — Open

`tests/test_acceptance.py:172`. `msg = str(rec[0].message)` — but
`pytest.warns` (verified on pytest 9.1.1) records *all* warnings in `rec`,
not just matches. If `set_cell` internals ever emit another warning first
(numpy DeprecationWarning, StaleChecksumWarning), the table/cell-naming
assertion checks the wrong message. Fix: select the `EditRangeWarning`
instance from `rec` explicitly.

#### CR-20260706-12 — Oracle JSON read without BOM handling — Low, PLAUSIBLE — Open

`tests/conftest.py:82`. `json.loads(TUNERPRO_ORACLE.read_text())` rejects a
UTF-8 BOM, and the capture procedure mandates the file be recorded on Windows,
where editors commonly write one. The existing `JSONDecodeError → pytest.fail`
at least fails loud. Fix: `read_text(encoding="utf-8-sig")`.

#### CR-20260706-13 — mini.xdf documented as 3 tables, contains 4 — Low, CONFIRMED — Open

`tests/fixtures/README.md:5` says "Hand-written 3-table XDF snippet";
`mini.xdf` contains four XDFTABLE entries (uniqueids 0x100–0x400). Fix the
count.

#### CR-20260706-14 — `requires_real_files` marker is dead code — Low, CONFIRMED — Open

`tests/conftest.py:35`. Zero usages anywhere; `test_checksum.py:28`,
`test_roundtrip.py:20`, `test_read.py:293`, `test_xdf.py:227` each define
their own local `requires_real` copy of the same guard, and
`test_acceptance.py` gates via fixtures. Fix: delete it, or migrate the four
local copies onto it.

#### CR-20260706-15 — ORACLE_ID identity unguarded — Low, CONFIRMED — Open

`tests/test_acceptance.py:37`. "ID_PORT_SP, 10×10 int8" lives only in a
comment; no assertion pins the resolved table's symbol, shape, or dtype before
AE3/AE4 bake in int8 clip bounds and headroom assumptions. On an XDF revision
reassigning 0x11F9C, tests die with unhelpful numpy errors or silently
exercise a different table. Fix: a session-scoped fixture that resolves the
table and fails loud with "reference table changed — update ORACLE_ID".

#### CR-20260706-16 — Oracle schema in three uncoordinated copies — Low, CONFIRMED — Open

Prose in `tests/fixtures/README.md:88-102`, partial validation in
`tests/conftest.py:85-87`, inline coercion in the AE1 body
(`tests/test_acceptance.py:54-57`). A malformed capture dies as a raw
KeyError; future capture tooling must re-implement the coercions. Fix: one
shared `load_tunerpro_oracle(path)` with per-field validation, used by the
fixture and any tooling.

#### CR-20260706-17 — Redundant `checked` counter — Low, CONFIRMED — Open

`tests/test_acceptance.py:53/:75`. `assert checked >= 1` duplicates the
fixture's non-empty-`tables` guard (`conftest.py:86-87`); the loop has no skip
path, so the counter cannot legitimately be 0. Fix: drop the counter (or move
ownership of the guard to one side).

#### CR-20260706-18 — AE gating story triplicated in prose — Low, CONFIRMED — Open

`README.md:165-179`, `test_acceptance.py` docstrings, and
`tests/fixtures/README.md:19-34` all restate the AE1/Decision-6 gating
rationale and the AE table. Fix: keep the top-level list plus the existing
link; make `tests/fixtures/README.md` the single canonical explanation.

#### CR-20260706-19 — Avoidable re-parsing and slow byte-diffs — Low, CONFIRMED — Open

(a) `tests/conftest.py:57`: function-scoped `real_cal` re-parses the 5.8 MB
XDF (~0.93 s) per test; `XdfModel` is frozen and `BinImage.__init__` copies,
so a session-scoped parsed model + per-test `BinImage` rebuild preserves
isolation at ~1/1000th cost. (b) `test_acceptance.py:123-124/:149-150`: the
4 MB per-byte Python diff loop (~0.22 s each) duplicated in both AE3 tests —
one shared helper using numpy/slice comparison is ~350× faster.
(c) `test_acceptance.py:130`: re-open via `CalFile.open` re-parses the XDF to
read one cell; `CalFile(real_cal.model, BinImage.from_path(out, ...))` is the
supported cheap path (the AE5 fixture already uses it). (d) four
`real_bin.read_bytes()` sites re-read the same 4 MB file.

#### CR-20260706-20 — AE1 capture procedure is hand transcription — Low, CONFIRMED — Open

`tests/fixtures/README.md:53-55`. The procedure asks a human to retype ~10
tables (100+ cells) of TunerPro-displayed values into JSON by eye — the sole
independent oracle for the mmedtypeflags semantics is the most typo-prone
artifact in the pipeline. Fix: a small checked-in converter from TunerPro's
own table export/clipboard CSV into the oracle JSON, validated through the
shared loader (CR-20260706-16).

### Not findings (checked and clean)

- All `simoscal` API usage in the new tests matches the source (signatures,
  attribute names, exception/warning classes, `BinImage` keyword args).
- The oracle JSON schema in `tests/fixtures/README.md` and the AE1 parser
  agree field-for-field.
- The `tunerpro` marker is registered in `pyproject.toml`; conftest correctly
  does not re-register it.
- ORACLE_ID 0x11F9C verified as ID_PORT_SP, 10×10 int8, non-uniform. **Correction
  (2026-07-06):** that verification covered identity only (uniqueid → symbol →
  shape/dtype), not decode correctness. A live TunerPro capture shows this
  table's *values* are actually mis-decoded — see CR-20260706-21.
- README table/equation counts verified against the real XDF.
- No applicable CLAUDE.md convention violations (no project-level CLAUDE.md;
  user-level rules are MATLAB-specific).
- Plan-doc status framing ("Phase 1 complete" + "optional" AE1 capture) was
  flagged PLAUSIBLE as a process concern but discloses the caveat explicitly;
  recorded here rather than as a tracked finding.

---

## Review 2026-07-06 — AE1 live TunerPro capture (first real run)

- **Scope:** not a diff review — this is the result of actually performing the
  one-time AE1 capture the U6 review above only reviewed on paper. 10 tables
  spanning the decode surface (8-bit/16-bit/32-bit int, float32, non-identity
  scaling, square and non-square multi-row/col shapes) were read from
  `SC8S50.V1.0.xdf` over `5G0906259L__0002.bin` in TunerPro and recorded in
  `tests/fixtures/tunerpro_oracle.json` (screenshots and a CSV transcription
  in `oracles/TunerPro_export/`).
- **Headline:** AE1 immediately fails — and it is right to. Two independent,
  real `simoscal` decode bugs were confirmed by comparing the library's own
  output against the TunerPro-displayed ground truth, both predating this
  session and undetected by the existing test suite because every prior
  real-file test happens to exercise values these bugs don't disturb (0/1
  cells, small positive magnitudes, square symmetric tables). This is the
  first check in the whole project that independently verifies decoded
  *values* against a source outside `simoscal` itself, and it caught real
  corruption on the first run.

### CR-20260706-21 — 2D table decode uses the wrong element order — High, CONFIRMED — Open

`simoscal/codec.py:127`, `decode_raw`:

```python
arr = np.frombuffer(raw, dtype=dtype).reshape(emb.rows, emb.cols)
```

This assumes the on-bin bytes for a 2D table are laid out row-major (each
row's `cols` elements contiguous, i.e. X fastest within a fixed Y). The real
XDF/bin layout is column-major (each column's `rows` elements contiguous, i.e.
Y fastest within a fixed X). Proven two ways:

1. `IP_N_SP_IS_T_AST_STST` (uniqueid `0x22192`, 6×4, non-square): flattening
   the TunerPro-true 6×4 matrix in Fortran (column-major) order and reshaping
   it row-major into (6, 4) reproduces the library's actual buggy output
   element-for-element.
2. Running AE1 live against `ID_PORT_SP` (uniqueid `0x11F9C`, the `ORACLE_ID`
   used everywhere else in the test suite) fails with 24/100 cells wrong,
   e.g. `[0,6]`: library says `1.0`, TunerPro shows `0.0` — the classic
   symptom of a square matrix silently reading as its own transpose.

**Failure scenario:** every table in the real XDF with `rows > 1 and cols > 1`
(623 of them) decodes to the wrong physical layout — reads show TunerPro row
*i* data in the wrong cell, and `set_cell(row, col, value)` (which `test_write.py`,
`test_roundtrip.py`, and this session's AE2–AE5 tests all exercise against
`ID_PORT_SP`) **writes to the wrong physical cell**. Every existing test using
`ID_PORT_SP` passed only because those tests check byte-level round-trip and
diff extent, never a specific cell's real-world (row, col) identity against an
independent source — this is invisible without exactly the TunerPro
cross-check AE1 was designed to be.

**Suggested fix:** decode with the correct element order —
`np.frombuffer(raw, dtype=dtype).reshape(emb.cols, emb.rows).T` or equivalently
`.reshape(emb.rows, emb.cols, order="F")` — and audit the writer's inverse
path (`simoscal/writer.py`) for the matching encode-order bug before trusting
any multi-row/col write.

**Resolution (2026-07-06) — Fixed. Same root cause as CR-20260706-22.** The
column-major layout is not universal; it is the `mmedtypeflags` bit `0x04`,
which the parser had mis-assigned to *signed*. Correcting the flag map (`0x01`
= signed, `0x04` = column-major — verified against the live TunerPro capture
over `SC8S50.V1.0.xdf`) fixed both findings at once. Changes: `EmbeddedData`
gained a `column_major` field (`model.py`); `xdf.py` decodes bit `0x04` into it
and includes it in the duplicate-detection fingerprint; `codec.decode_raw`
reshapes `(cols, rows).T` when set; the writer's inverse path now matches —
`pack_block` serializes `tobytes(order="F")` and `stage_cell` computes the
cell's linear index as `col*rows + row` — both flag-conditional, so a genuine
row-major (`0x2`) table is unaffected. Verified: all 623 real 2D tables carry
`0x6`; single-cell and full-block writes round-trip byte-identically; AE1
(`IP_N_SP_IS_T_AST_STST` 6×4 and `ID_PORT_SP` 10×10) now matches TunerPro
element-for-element.

### CR-20260706-22 — mmedtypeflags sign bit inverted for real tables — High, CONFIRMED — Open

`simoscal/xdf.py` (embedded-data parse) / `simoscal/codec.py:numpy_dtype_for`.
The `signed` flag derived from `mmedtypeflags` produces the *opposite* of
TunerPro's own interpretation for at least three real tables whose raw
magnitude is large enough to expose it (small/positive-only values can't
distinguish signed from unsigned, which is why nothing caught this before).
Reinterpreting the identical raw bits as the opposite signedness reproduces
TunerPro exactly in every case:

| Table | uniqueid | Raw decode (as parsed) | Correct (opposite signedness) | TunerPro |
|---|---|---|---|---|
| `IP_PRS_UP_THR_DIF_WIDE_OPEN_THR` row 1 | `0x4D0D0` | −5184.09 | 250.00 | 250.00 |
| `C_FAC_POW_PUT_CTL_BOL` | `0x36EC` | −98.00 | 102.00 | 102.00 |
| `C_LF_CMB_MOD_INH_RED` | `0x2564` | −193.0 | 4294967103.0 | 4294967103.0 |

**Failure scenario:** any int16/int32 calibration whose raw value's top bit is
set decodes (and would re-encode on write) to a value differing from the real
one by roughly the element's full integer range — not a rounding error, a
different number entirely. `C_LF_CMB_MOD_INH_RED` is a bit-field inhibit mask;
misreading it as −193 instead of the correct bit pattern silently corrupts
mask semantics for anything built on the current (wrong) decode.

**Suggested fix:** re-derive the `signed` bit extraction from `mmedtypeflags`
against TunerPro's own bit (likely inverted polarity or wrong bit position),
re-run AE1 against the widened oracle, and audit `is_float_bug_table`'s
adjacent write-guard logic (`simoscal/safety.py`) for any assumption that
depends on the current (wrong) sign.

**Resolution (2026-07-06) — Fixed. Same root cause as CR-20260706-21.** It was
*wrong bit position*, not inverted polarity: bit `0x04` is TunerPro's
column-major flag, and the real sign bit is `0x01`, which is **never set** on
any table in `SC8S50.V1.0.xdf` — so every table is unsigned, exactly matching
the three oracle rows (`0x4D0D0` → 250.00, `0x36EC` → 102.00, `0x2564` →
4294967103). Fix: `_FLAG_SIGNED = 0x01`, `_FLAG_COLUMN_MAJOR = 0x04` in
`xdf.py`. `safety.py` audit: `check_raw_fits`/`raw_int_range` key off
`emb.signed`, which is now correct (unsigned range `[0, 2^bits−1]`), so
previously-signed raw values like 33423/35783 now fit their uint16 width and
round-trip; no sign-dependent assumption in `is_float_bug_table` (it matches on
symbol only). Verified: all 10 oracle tables pass AE1; full suite 136 passed.
Tests that had hardcoded the `0x04 = signed` assumption were corrected
(`test_xdf`, `test_read`, and the `SIGNED_TIGHT` fixture in `test_write`, which
now expresses signed as `0x3 = 0x01|0x02`).

### Not findings (checked and clean)

- The three float32 tables in the capture (`C_DPL`, `C_PRS_IM_SP_MAX`,
  `C_M_AIR_CYL_SP_MAX`) match the library's decode; two of the three (`C_DPL`,
  `C_M_AIR_CYL_SP_MAX`) round to `0.00` only because TunerPro's display
  truncates to 2 decimals on very small physical values — not a decode bug,
  just insufficient capture precision for those two entries (`tol: 0.005`
  used to keep AE1 meaningful there rather than vacuous).
- The two identity-scaling square/near-square tables with small positive
  values (`ID_PORT_SP_CH`, `IP_N_SP_IS_BAS[MT]`) decode correctly in value
  *content* even though `ID_PORT_SP_CH` shares CR-20260706-21's row/col bug —
  recorded as expected-fail in the oracle's `note` field pending the fix.

---

## Review 2026-07-07 — SOP tune recipe (`simoscal/sop_recipe.py` + demo + tests + docs)

- **Scope:** the six-commit `feat/sop-tune-recipe` branch (U1–U6) implementing
  `Docs/plans/2026-07-06-003-feat-sop-tune-recipe-plan.md`, reviewed as a whole-
  branch diff against `main` (merge-base `244061c`): `simoscal/sop_recipe.py`
  (new, 1396 lines), `demos/apply_sop_recipe.py` (new), `tests/test_sop_recipe.py`
  (new), `tests/test_acceptance_sop.py` (new), and the `simoscal/__init__.py` /
  `README.md` / `.gitignore` edits.
- **Method:** whole-file read of the new module against the live Phase 1–3 API it
  consumes (`calfile.py`, `safety.py`, `writer.py`); ran `test_sop_recipe.py` +
  `test_acceptance_sop.py` (**74 passed**) and the full `Code/` suite (**295
  passed**); ran the demo end-to-end against the real bin (checksums **CLEAN**,
  DO NOT FLASH raised, 118 outcomes, minimal-diff save); and — given the brick /
  engine-damage stake of flashing a real ECU — did an independent second pass on
  **every transcribed literal value** against the source `knowledge/ecu-tuning-
  basics.md`, plus a trace of the write-staging order in `writer.py`/`safety.py`.
- **Headline:** no correctness bugs in the applied edits, and no safety-guarantee
  regressions. Symbol-resolution failures are data not exceptions; the existing
  float-bug / range / raw-width guards are caught per-entry (never swallowed,
  never abort the run); every literal grid/curve/scalar matches the guide byte-
  for-byte (Max-Torque curve, IGA 16×16, PUT setpoint last row, Spark-IAT rows,
  all limiter targets); axis-matched writes fail loud on the lambda mismatch as
  designed; the coherence gate correctly self-raises DO NOT FLASH on this bin.
  Two low-impact findings only, both in the recipe module, neither reachable on
  the current symbol set.

### CR-20260707-01 — Multi-cell writers not atomic on a mid-loop guard — Medium, PLAUSIBLE — Fixed (2026-07-07)

`simoscal/sop_recipe.py:902` (`_apply_cut_transform`), `:930` (`_apply_iat_rowmap`),
`:971` (`_apply_axis_write`). These three writers stage edits cell-by-cell in a
loop — `_run_write(lambda: view.set_cell(...))` per cell — and on the first
failure `return … OUTCOME_GUARD_BLOCKED`. But `writer.stage_cell` checks
`check_raw_fits` and *then* writes bytes (`writer.py:128` before `:138`), so each
completed iteration has already staged its bytes irreversibly. A guard tripping
on cell *N* therefore leaves cells `0…N-1` written while the returned outcome is
`guard_blocked` — whose contract (docstring `:1032`, and the README's
"the table stays byte-identical and the recipe continues") promises the opposite.
This is the same silent-partial-corruption shape the library's fail-loud mandate
exists to prevent, and the analogue of CR-20260706-10.

**Failure scenario:** any future symbol routed to one of these three kinds whose
per-cell physical value inverts to an out-of-width raw (`RawRangeError`) — or a
float-bug-flagged symbol mapped to `cut_transform`/`iat_rowmap`/`axis_write`
(`FloatBugGuardError`) — trips the guard partway through, and the report records
`guard_blocked` on a table that is now half-written and neither stock nor the
intended target. Latent today: none of the three current targets
(`CoTE_tHdCtlSp_M_VW`, `IP_IGA_BAS_TEMP_N_32`, `IP_PUT_SP`) is float-bug-flagged,
and all of their values sit in range, so no guard fires in the loop.

**Suggested fix:** stage the whole grid atomically. Build the target array and
call `view.set(target)` once (as the `literal_table` / `broadcast` / `torque_curve`
paths already do — `set` range-checks the full array before staging any byte), or
snapshot `view.raw` at entry and restore it if any loop iteration reports
`guard_blocked`, so the `guard_blocked` outcome keeps its byte-identical contract.

**Fixed 2026-07-07:** all three writers now assemble the full target grid (rows/
cells left stock keep their decoded `view.values`) and stage it in a single
`_run_write(lambda: view.set(target))`. `set` range-checks the whole array before
staging any byte, so a `FloatBugGuardError`/`RawRangeError` now leaves the table
byte-identical — restoring the `guard_blocked` contract. `_apply_axis_write`'s
standalone breakpoint write (a different table) was already a single `set_cell`
and is unchanged. Verified: 74 SOP tests + full suite 295 passed; demo still
118 outcomes / checksums CLEAN / DO NOT FLASH.

### CR-20260707-02 — Vestigial `row_idx`/`col_idx` locals in `_apply_literal_table` — Low, CONFIRMED — Fixed (2026-07-07)

`simoscal/sop_recipe.py:853-854`. `row_idx = _positional_axis_match(...)` and
`col_idx = _positional_axis_match(...)` are computed but only ever consumed as
`None` checks (`:855`, and the `which` diagnostic `:857-859`); the actual write
uses the full `grid.cells` via `view.set(target)` (`:868-869`). The names read as
if the matched indices drive cell placement, when in fact a non-`None` result is
always `list(range(n))` and correctness rests on the count-and-alignment guarantee
`_positional_axis_match` provides. Harmless, but a small altitude trap for the
next reader.

**Suggested fix:** rebind to booleans — `x_ok = _positional_axis_match(view.axis_values("x"), grid.x_keys) is not None`
(and `y_ok`) — or keep the locals with a one-line comment that a non-`None`
result is always the identity index list, so the full-grid write is trivially
axis-aligned.

**Fixed 2026-07-07:** rebound to `x_ok`/`y_ok` booleans with a one-line comment
noting that a non-`None` match is always the identity index list, so the
`view.set(target)` full-grid write is trivially axis-aligned. Behavior identical
(the `which` diagnostic and axis-mismatch outcome are unchanged).

### Not findings (checked and clean)

- **Transcription integrity — verified independently.** Every literal payload in
  `SYMBOL_MAP` matches `knowledge/ecu-tuning-basics.md` cell-for-cell on a second
  pass: `_MAX_TORQUE_CURVE` (20 RPM/Nm pairs, guide line 82), `_IGA_CELLS`/`_IGA_X`/
  `_IGA_Y` (16×16, lines 238–255), `_PUT_SP_SPEC.last_row_values` and
  `axis_target` 2698.97 (line 157), `_IAT_ROWMAP` rows + `zero_below`=30 (lines
  265–276), and the limiter scalars 300 / 220000 / 3000 / 350000 / 2700 / 257.49
  (lines 345/349/363/367/353/401). No transcription error found.
- **The float-bug and ceiling guards behave as the guide demands.**
  `C_PRS_IM_SP_MAX → 350000` correctly returns `guard_blocked` (float-bug flagged,
  over declared max) and leaves the table stock; `C_PRS_IM_SP_LIM → 2700`
  correctly `guarded_skip`s (stock ~271695 > target, "if already >2700 don't
  touch"). Both are byte-identical after the run — asserted by the acceptance
  suite.
- **By-design, recorded so it isn't mistaken for a defect:** the Overboost limit
  (P0234, the guide's single most safety-relevant limiter) is *not* actually
  landed by the recipe — `C_PRS_IM_SP_LIM` is an unconfirmed offset-to-baro
  candidate whose stock value trips the ceiling guard, so it stays stock and is
  reported `guarded_skip` with a "flagged for manual confirmation before flashing"
  reason. This is the intended fail-safe (the plan flags the symbol as a
  candidate only), but it means overboost must be set by hand. The report line +
  the revision-0 iteration model cover it.
- **By-design:** the coherence gate raises DO NOT FLASH on every run against
  *this* bin, because the lambda tables can never resolve (their stock axes differ
  from the guide's example bin → `axis_mismatch`), so the lean-risk rule always
  fires. Correct and safe — the recipe genuinely cannot apply enrichment here —
  but the "coherence passed" state is unreachable via the recipe alone on this
  bin; the human gate is the only path, exactly as designed and tested
  (`test_full_report_accounts_for_every_instruction` asserts `do_not_flash() is
  True`).
- All `simoscal` API usage in the new module and tests matches the source
  (`CalFile.get`/`search`, `TableView.set`/`set_cell`/`axis_values`/`values`/
  `shape`/`units`, the `AmbiguousTableError`/`FloatBugGuardError`/`RawRangeError`/
  `EditRangeWarning` classes, `render_table`/`compare_tables`/`TableMismatchError`).
- AE1–AE5 are each exercised end-to-end against the real bin (value match,
  guard behaviour, checksum-clean save, complete accounting, PNG coverage), and
  every guide instruction — in-scope and explicitly-skipped — has exactly one
  `SYMBOL_MAP` entry (`report_sections == map_sections`).
- No applicable CLAUDE.md convention violations (no project-level CLAUDE.md; the
  user-level rules are MATLAB-specific and don't bind this Python module).

---

## Review 2026-07-07 — TuningBasicsGuide R01 tune script + generated output

- **Scope:** `Tunes/TuningBasicsGuide/TUNE_Basics_Guide_R01.py`, its R00 lineage
  script, `Tunes/TuningBasicsGuide/REV_LOG.md`, existing and freshly-generated
  R01 output under `Tunes/TuningBasicsGuide/TUNE_Basics_Guide_out/`, the source
  guide docs (`knowledge/ecu-tuning-basics.md`, `knowledge/tuning-getting-started.md`,
  and `Docs/3. ECU Tuning - Basics.docx` converted to text), and the live
  `simoscal` code/XDF path used by the tune script.
- **Method:** code review performed with **GPT-5.5**. Read the script against the
  guide, XDF metadata, and library contracts (`calfile.py`, `writer.py`, `codec.py`,
  `safety.py`, `model.py`, `sop_recipe.py`); ran the script end-to-end with
  `PYTHONPATH="/Users/sam/SimosTools/Code"`; inspected the fresh report at
  `Tunes/TuningBasicsGuide/TUNE_Basics_Guide_out/R01_20260707-201402/report.md`;
  and re-opened the saved bin through `CalFile` to confirm the six R01-added
  decoded values.
- **Headline:** the script runs successfully, saves a checksum-clean bin (CAL_CRC
  + ECM3), writes the intended bytes for five of the six R01-added targets, and
  generates reports/PNGs. One high-severity calibration/value-contract issue was
  confirmed on `C_M_AIR_CYL_SP_MAX` — Maximum allowed M_AIR_CYL_SP: the saved bin
  decodes to `0.002`, while the Python/XDF documentation says non-TunerPro tools
  should write the intended physical value (`2000`) rather than the TunerPro
  workaround literal. Two medium findings are report-gate issues: R01-covered
  guide items still appear as skipped, and the top-level coherence banner can be
  read as broader SOP completeness than it actually proves.

### CR-20260707-03 — Max allowed airmass written as TunerPro workaround value — High, OVERTURNED — Dismissed (2026-07-07, invalid)

> **RESOLUTION (2026-07-07) — Dismissed, finding invalid. No code change; the
> script was correct as written.**
>
> The finding assumed the XDF's identity/`mg/stk` scaling for `C_M_AIR_CYL_SP_MAX`
> — Maximum allowed M_AIR_CYL_SP is literal. It is not. The stock bin decodes this
> symbol to `0.001389`, and a stock airmass-request ceiling of 0.0014 mg/stk is
> impossible when the engine breathes 515–1275 mg/stk — so the label is wrong. The
> ECU stores this value in **kg/stk**; the XDF (both `SC8S50.V1.0.xdf` and
> `SC8S50.ALL.xdf`) mislabels it identity `mg/stk`. The correct raw value for a
> 2000 mg/stk ceiling is therefore `0.002` kg/stk — exactly what the script writes.
> Stock `0.001389` (= 1389 mg/stk) → R01 `0.002` (= 2000 mg/stk) is a ~1.44× raise,
> in line with the intake-air tables. Confirmed by exporting the R01 bin: the symbol
> decodes `0.002`.
>
> The **suggested fix below is dangerous and must not be applied**: writing `2000.0`
> raw = 2000 kg/stk = 2,000,000 mg/stk (~1.44M× stock), effectively removing the
> limiter. See `knowledge/ecu-tuning-basics.md` note (2) and the memory
> `air-cyl-sp-max-kg-not-mg`. Original finding text retained below for the record.

`Tunes/TuningBasicsGuide/TUNE_Basics_Guide_R01.py:151-154` sets
`C_M_AIR_CYL_SP_MAX` — Maximum allowed M_AIR_CYL_SP to `0.002` through the normal
physical-unit `.set(...)` path:

```python
AIR_CYL_SP_MAX_SYMBOL = "C_M_AIR_CYL_SP_MAX"
AIR_CYL_SP_MAX_VALUE = 0.002
```

The script's comment treats `0.002` as the correct stored value because the guide
says to type `0.002` if TunerPro displays the value wrong. That conflicts with
the project documentation at `knowledge/ecu-tuning-basics.md:367-369`, which says
the float-bug is a TunerPro editor artifact and tools that write raw float bytes
directly, like this Python library, should write the intended physical value and
ignore the TunerPro workaround. The XDF entry confirms the library's decoded
contract for `C_M_AIR_CYL_SP_MAX` — Maximum allowed M_AIR_CYL_SP is identity
scaling with units `mg/stk` and display max `20000` (`SC8S50.V1.0.xdf:2117-2148`),
so `2000` is in range and should not trip the float-bug guard.

The fresh run confirms the saved output follows the script rather than the guide
contract: re-opening `R01_20260707-201402/5G0906259L_0002_BasicsGuide_R01.bin`
decodes `C_M_AIR_CYL_SP_MAX` — Maximum allowed M_AIR_CYL_SP as
`0.0020000000949949026`, and the report records `0.001389 → 0.002`.

**Failure scenario:** the tune leaves the maximum allowed requested airmass
ceiling effectively at the TunerPro workaround literal as interpreted by
`simoscal`, not at the guide's intended `2000 mg/stk`. Since the rest of R01
raises boost, torque, and intake-air ceilings, this is a real limiter/intervention
risk and a direct contradiction between the tune script and the codebase's stated
Python-library float-bug policy.

**Suggested fix:** change `AIR_CYL_SP_MAX_VALUE` to `2000.0` and write
`C_M_AIR_CYL_SP_MAX` — Maximum allowed M_AIR_CYL_SP in physical units through
`.set(...)` / `.set_cell(...)`. If the project intentionally wants the raw/stored
literal `0.002` here, update `knowledge/ecu-tuning-basics.md`, `simoscal`'s
float-bug policy comments, and the report wording together, because they
currently say the opposite.

### CR-20260707-04 — Report shows R01-covered guide items as both applied and skipped — Medium, CONFIRMED — Fixed (2026-07-07)

`Tunes/TuningBasicsGuide/TUNE_Basics_Guide_R01.py:156-159` defines
`R01_SUPERSEDES` only by concrete symbol:

```python
R01_SUPERSEDES = frozenset({PRS_MAX_SYMBOL, TQ_REF_MAX_SYMBOL, AIR_CYL_SP_MAX_SYMBOL})
```

That is sufficient to replace recipe rows for `C_PRS_IM_SP_MAX` — Maximum allowed
PRS_IM_SP, `IP_TQI_REF_MAX_MON` — Maximum reference indicated engine torque, and
`C_M_AIR_CYL_SP_MAX` — Maximum allowed M_AIR_CYL_SP. It cannot replace the
placeholder `skip_vague` rows whose symbol is `—`, so the merged report lists two
R01-covered guide requirements as both done and not done.

Confirmed in the fresh report:

- `ID_PV_AV_FL` — Pedal value threshold for the determination of LV_FL_RAW appears
  as applied at `report.md:79`, but the guide row `Fueling — heavy-throttle table
  ~70–75` still appears as skipped at `report.md:152`.
- `IP_M_AIR_CYL_MAX_STND_VVL[STND]` — Maximum intake air of the engine at
  standardized ambient pressure for different valve lifts and
  `IP_M_AIR_CYL_MAX_STND_VVL[LFT_1]` — Maximum intake air of the engine at
  standardized ambient pressure for different valve lifts appear as applied at
  `report.md:81-82`, but `Limiters — two max intake air tables → 2000` still
  appears as skipped at `report.md:155`.

**Failure scenario:** a human review gate sees the same guide work in both the
applied and skipped sections. That weakens trust in the report as the artifact
deciding whether the generated bin is complete enough to flash or iterate.

**Suggested fix:** supersede by guide section as well as symbol for R01-covered
placeholder rows. For example, filter recipe skip outcomes with guide sections
`Fueling — heavy-throttle table ~70–75` and `Limiters — two max intake air tables
→ 2000` before merging the R01 outcomes.

**Fixed (2026-07-07):** `simoscal/sop_recipe.py` reclassified all 7 `skip_vague`
placeholder entries to a new `KIND_SKIP_STOCK` kind with real symbols and honest
per-entry reasons, so the two guide sections above now carry concrete symbols
instead of `—`. `Tunes/TuningBasicsGuide/TUNE_Basics_Guide_R02.py` supersedes
recipe rows by `guide_section` (`R02_SUPERSEDES_SECTIONS`) rather than by symbol
alone, so a section is fully replaced by the script's applied outcome instead of
leaving a stale skipped duplicate. Verified: R02 report shows 0 `skip_vague`
occurrences and no guide section appears as both applied and skipped; the R02
bin is byte-identical to R01 (report-honesty fix only, no calibration change).

### CR-20260707-05 — Coherence-passed banner hides unresolved in-scope guide fueling items — Medium, CONFIRMED — Open

`simoscal/sop_recipe.py:1223-1241` defines the coherence gate as a dependency
check only: boost must be paired with basic lambda enrichment, Max PR flattening,
and the Option 3 selector. It does not encode SOP completeness or unresolved
in-scope guide work. Because R01 re-breakpoints lambda axes and writes the basic
lambda family, the report starts with:

```markdown
## ✅ Coherence check passed

No dependent-entry divergence detected. (Still pass the human review gate +
checksum verify before flashing.)
```

That statement is locally true for the declared dependency rules, but the same
report still lists unresolved in-scope fueling instructions from the guide,
including `Fueling — fueling-influence tables → 0.80` and `Fueling — two tables
set entirely to 1` (`report.md:151`, `report.md:153`). The source guide presents
those as part of the fueling setup before the lambda curves (`knowledge/ecu-tuning-
basics.md:280-316`).

**Failure scenario:** a reviewer reads the green top banner as a broad flash-readiness
signal and misses that in-scope fueling work remains unresolved/skipped lower in
the report. The current wording is especially easy to over-read because the script
also prints `Coherence check passed` on the terminal after saving a checksum-clean
bin.

**Suggested fix:** make the banner scope explicit, e.g. `Dependency coherence check
passed`, and add an early `Incomplete guide items` / `Not full SOP complete`
section whenever any in-scope `skip_vague` remains. If unresolved in-scope fueling
items should block flashing, add a `DO NOT FLASH` or warning-level rule for those
sections.

**Partially addressed (2026-07-07), still Open:** the `skip_vague` reclassification
in `simoscal/sop_recipe.py` (see CR-20260707-04's fix note) removed this finding's
literal trigger — the entries it cites now carry real symbols and reasons, not the
`skip_vague` placeholder. The banner-wording change itself (renaming to `Dependency
coherence check passed` and adding an early `Incomplete guide items` section) was
not done, so a green top banner can still coexist with unresolved in-scope guide
items lower in the report. Left Open pending that wording/section change.

### Not findings (checked and clean)

- The script ran end-to-end with the existing `simoscal` package and generated
  `Tunes/TuningBasicsGuide/TUNE_Basics_Guide_out/R01_20260707-201402/` with a
  checksum-clean saved bin (`CAL_CRC`, `ECM3`), `report.md`, and 188 comparison
  PNGs.
- Re-opening the saved bin confirmed five R01-added decoded targets as expected:
  `ID_PV_AV_FL` — Pedal value threshold for the determination of LV_FL_RAW at
  `71.97265625%` flat from target `72`; `C_PRS_IM_SP_MAX` — Maximum allowed
  PRS_IM_SP at `350000`; `IP_M_AIR_CYL_MAX_STND_VVL[STND]` — Maximum intake air
  of the engine at standardized ambient pressure for different valve lifts and
  `IP_M_AIR_CYL_MAX_STND_VVL[LFT_1]` — Maximum intake air of the engine at
  standardized ambient pressure for different valve lifts at `1999.9819638361182`
  flat from target `2000`; and `IP_TQI_REF_MAX_MON` — Maximum reference indicated
  engine torque at `1000` flat.
- `C_PRS_IM_SP_MAX` — Maximum allowed PRS_IM_SP uses `set_raw`, and that is
  technically effective in this codebase: `TableView.set_raw(...)` bypasses the
  display-range/float-bug guard and writes the float bytes directly; re-opening
  the saved bin decodes `350000`.
- The R00 lambda axis re-breakpoint still clears the base recipe's basic-lambda
  axis mismatch for the recipe-targeted HPDI/MPI tables and writes the third
  shared table, `IP_LAMB_BAS[1]` — Basic lambda setpoint, to keep the shared-axis
  family coherent.

---

## Review 2026-07-20 — human-friendly tune API (`simoscal.tune`) and R13

- **Scope:** the seven implementation units on `feat/human-friendly-tune-api`
  relative to `Code` commit `2b2eff6`, plus
  `Tunes/TuningBasicsGuide/TUNE_Basics_Guide_R13.py` in the root repository.
  The requirements and completed plan were reviewed alongside the surrounding
  calibration, checksum, patch, packaging, and test code.
- **Method:** inspected both repository histories and diffs, traced profile
  resolution, journaling, checksum/save/readback, raw-diff attribution, domain
  calls, and switch-patch handling; ran the complete test suite; and executed
  focused reproductions for the previous-revision restoration and unverifiable-
  checksum paths.
- **Verification:** `pytest -q` completed with **540 passed** and 4 expected
  `StaleChecksumWarning` warnings in 346.83 seconds. Both focused reproductions
  triggered the behaviors described below. The repositories were clean after
  review; no source changes were made.
- **Headline:** R13 reproduces R12 byte-for-byte and the complete existing suite
  passes, but two core build guarantees have uncovered failure modes: a checksum
  that cannot be verified is treated as clean, and a declared restoration to
  stock cannot pass the previous-revision byte audit. Packaging omits the domain
  package, and the R13 template does not follow the repository's explicit-intent
  authoring rule.

### CR-20260720-01 — Unverifiable checksums are reported CLEAN — High, CONFIRMED — Fixed (2026-07-20)

> **RESOLUTION (2026-07-20) — Fixed.** Independently reproduced first: two
> `ChecksumReport`s with `can_verify=False` returned `checksums_clean=True`, and
> an empty report tuple did too (`all()` over nothing is vacuously true).
> `simoscal/tune/pipeline.py` now classifies reports through one
> `_checksum_state()` helper into three verdicts — `CHECKSUM_CLEAN`,
> `CHECKSUM_STALE`, `CHECKSUM_UNVERIFIABLE`. Clean requires the reports to be
> *present* and every one `can_verify=True` and `is_stale=False`; a missing,
> stale, or unverifiable report fails the build and renders
> `Checksums: **UNVERIFIABLE — DO NOT FLASH**` (or `STALE — …`). Tests added
> (`test_tune_build.py`): one and both unverifiable reports, and the empty-set
> case, each asserting `BuildFailed` + the report banner. The pre-existing stale
> test's match string was updated to the new label. Full suite: 548 passed.


`simoscal/tune/pipeline.py:93-96` defines `checksums_clean` as true when every
checksum is either unverifiable or non-stale. The build gate repeats the same
logic at `:139-142`, so `can_verify=False` is a passing vote rather than a failed
verification. `render_report()` then labels that state `CLEAN` at `:320-324`.

**Reproduction:** replaced the final-file verification response with two
`ChecksumReport`s for CAL_CRC and ECM3 whose `can_verify` fields were false.
`build()` returned successfully with `result.ok == True` and
`result.checksums_clean == True` even though neither checksum could be verified.
The existing checksum layer deliberately uses `can_verify=False` for malformed,
short, or unsupported layouts, so this is a real API state rather than an
impossible mock value.

**Failure scenario:** a full bin with a malformed or unsupported checksum layout
is saved and presented as having passed every automated gate. The human-facing
report says `Checksums: CLEAN`, even though the requirement says checksum
verification must complete before any bin is declared buildable. Combined with
the checksum-byte audit allowance, this weakens the last automated gate before
human review of a safety-critical artifact.

**Suggested fix:** require the expected checksum reports to be present,
`can_verify=True`, and `is_stale=False`. Add a distinct `UNVERIFIABLE — DO NOT
FLASH` report state and a build test covering one and both unverifiable reports.

### CR-20260720-02 — Restoring a table to stock fails the byte audit — High, CONFIRMED — Fixed (2026-07-20)

> **RESOLUTION (2026-07-20) — Fixed.** Reproduced verbatim first (tuned one cell
> of `IP_PQ_CHA_MAX` — Maximum allowed pressure quotient at turbo charger
> compressor in one revision, restored it to stock in the next → `2 unexplained
> changed byte(s)`). Fix: `EditEntry` now carries a `declared` byte extent (the
> table's full z-data extent, set by `Tune.write` regardless of whether any byte
> moved) alongside the measured `offsets`; `Journal.declared_offsets()` and
> `touching()` expose it. `build()` adds a second, *tight* allowance —
> `audit.restore_to_source_allowance()` — that authorises only the declared bytes
> whose candidate value equals the build's pre-write source snapshot
> (`Tune.source_snapshot`, captured post-patch at construction). A restored byte
> (candidate == source, but differs from the prior revision) is attributed; a
> byte moved *away* from source stays accountable, so an undeclared change
> smuggled into a *different cell of a declared table* still fails the audit. The
> restored table is additionally read back off the saved bin (via the widened
> `touching()`), which independently pins its contents. Tests added: a
> restore-to-stock revision that now builds clean, and the negative
> smuggled-cell case that must still fail. Full suite: 548 passed.


`simoscal/tune/project.py:233-260` measures each journal entry's offsets by
comparing the write against the tune's source buffer, which is normally the
stock bin plus declared patches. When the target already equals that source,
the entry is marked `unchanged` with no offsets at `:261-262`. The final audit,
however, compares the candidate against the previous revision and permits only
`journal.changed_offsets()` at `simoscal/tune/pipeline.py:176-186`.

**Reproduction:** built a prior revision that changed one cell of
`IP_PQ_CHA_MAX` — Maximum allowed pressure quotient at turbo charger compressor,
then opened a fresh stock-based tune and explicitly wrote that table's stock
values with intent `restore stock`. The journal entry was `unchanged` with zero
offsets. Building against the tuned prior revision failed with `2 unexplained
changed byte(s)`.

**Failure scenario:** a later flat revision intentionally removes or backs out a
previous calibration change. Its candidate bytes are correct and its declaration
is explicit, but `build()` rejects the revision, omits the table from readback and
comparison plots, and calls the legitimate reversion unexplained. Omitting the
old domain call has the same result because every revision rebuilds from stock.
This makes a normal and safety-relevant rollback operation impossible through
the standard pipeline.

**Suggested fix:** derive audit responsibility from the declared table/cell
extent as it differs from `reference_bin`, while retaining the measured
source-buffer offsets as evidence of what the current write staged. At minimum,
explicitly declared tables must be able to authorize candidate-versus-reference
bytes that restore source values. Add a regression test that tunes one revision
and restores the affected cell or table in the next.

### CR-20260720-03 — Installed distributions omit tune domains — Medium, CONFIRMED — Fixed (2026-07-20)

> **RESOLUTION (2026-07-20) — Fixed.** Added `simoscal.tune.domains` to the
> `[tool.setuptools] packages` list in `pyproject.toml`. Verified by building a
> wheel (`pip wheel . --no-deps`): all eight domain modules
> (`__init__`, `_common`, `boost`, `fueling`, `ignition`, `limits`, `switchpatch`,
> `wastegate`) are now included. Added `tests/test_packaging.py`, which pins the
> declared package list to the packages actually on disk (a dir with
> `__init__.py`) in both directions, so the next omission fails in CI rather than
> at a user's first `tune.boost` access.


`pyproject.toml:23-29` configures an explicit setuptools package list containing
`simoscal`, `simoscal.analysis`, `simoscal.tune`, and
`simoscal.tune.profiles`, but not `simoscal.tune.domains`. The new domain package
and all six domain modules therefore fall outside the declared distribution.

**Failure scenario:** a wheel or other non-source-path installation can import
`simoscal.tune`, but the first lazy property access such as `tune.boost`,
`tune.fueling`, or `tune.switchpatch` imports an omitted module and raises
`ModuleNotFoundError`. Source-tree and current editable testing do not expose the
packaging boundary, so all 540 tests remain green.

**Suggested fix:** include `simoscal.tune.domains` in the package list or switch
to setuptools package discovery, then add a wheel-build/install smoke test that
imports and instantiates every public domain facade outside the source tree.

### CR-20260720-04 — R13 template omits explicit `intent=` arguments — Medium, CONFIRMED — Fixed (2026-07-20)

> **RESOLUTION (2026-07-20) — Fixed.** Added an explicit, revision-specific
> `intent=` to every calibration-changing domain call in
> `TUNE_Basics_Guide_R13.py` (previously only the two wastegate overlays and the
> valet slot supplied one). Exempt, correctly: `tune.apply_basics_sop()` (bulk
> SOP, journaled per table with its own reasons) and `require_sanity()` (a gate
> that moves no bytes) — neither accepts `intent`. Updated
> `docs/authoring-a-revision.md` to state the rule is required, not optional, and
> demonstrate it in the quick-start snippet. Extended the R13 acceptance test with
> `test_r13_every_calibration_call_declares_intent`, which walks `declare()`'s AST
> and asserts an `intent` keyword on every `tune.<domain>.<method>(...)` call. R13
> still reproduces the R12 bin byte-for-byte (intent strings change no bytes).


The canonical project workflow at `../CLAUDE.md:78-85` names
`TUNE_Basics_Guide_R13.py` as the template for R13 onward and requires physical
units, named constants, and an explicit `intent=` on every call. Most domain
calls in `../Tunes/TuningBasicsGuide/TUNE_Basics_Guide_R13.py:227-273` omit
`intent=` and rely on the library's generic fallback text; only the two
wastegate overlays and the slot-5 valet call supply it explicitly.

**Failure scenario:** a human copies the designated template for R14 and follows
its demonstrated style. The edit journal contains mechanically generated action
descriptions instead of the author's revision-specific reason, weakening the
audit trail precisely where the API is intended to make a tune understandable
without mentally executing earlier revisions. The acceptance test checks only
the count of `tune.` calls, not the required keyword.

**Suggested fix:** add `intent=` to every R13 domain call, update the authoring
guide examples to demonstrate the same convention, and extend the R13 source
acceptance test to require an explicit `intent` keyword on every calibration-
changing domain call.

### Open questions / assumptions

- Review scope used `2b2eff6` as the `Code/` feature base and treated the root
  R11 log-analysis commit as unrelated except where it supplied historical
  artifacts used by R13 acceptance.
- Public PyPI distribution is deferred, but `pyproject.toml` already defines the
  installation boundary and the authoring goal includes eventual use outside the
  source checkout; CR-20260720-03 is therefore retained as a current packaging
  defect rather than deferred documentation work.

### Summary

Four findings: 2 High and 2 Medium, all CONFIRMED and Open. The complete existing
suite passes, but it has no regression coverage for restoring a previous edit to
stock, treating unverifiable checksums as a failed build, or importing the domain
facades from an installed distribution.

---

## Review 2026-07-24 — Quick Edit V3 renderer-independent build service

- **Scope:** commit `fa59007` relative to parent `d9f43eb`, reviewed as one V3
  unit. Priority was the extraction in `simoscal/tune/pipeline.py`, followed by
  `simoscal/tune/build_service.py` and `tests/test_build_service.py`; the V3
  plan unit, `README.md` safety/build-service sections, and this findings index
  supplied the acceptance contract.
- **Method:** compared the old inline `build()` spine statement-for-statement
  with `run_gates()`, traced all `BuildReport` construction and sharing paths,
  inspected journal/audit semantics, ran focused adversarial reproductions, ran
  the focused build suites, and ran the complete test suite.
- **Verification:** the extraction preserved the complete gate set, order,
  problem strings, accumulate-all behavior, and final raise-after-render
  behavior. Focused suites completed with **42 passed**. The complete suite
  completed with **597 passed** and the same 4 expected
  `StaleChecksumWarning`s.
- **Headline:** the gate extraction itself is behavior-identical, but the new
  public model boundary does not enforce its own journal-derived/shareable
  claims. An arbitrary audit allowance can make an unjournaled edit shareable
  while the model lists no edit, and direct `build_report()` use can return a
  share path for an audit its own gate row says failed. The existing ten service
  tests do not exercise either path.

### CR-20260724-01 — Extra allowances make unjournaled edits verified, shareable, and invisible — High, CONFIRMED — Fixed (2026-07-24)

`simoscal/tune/build_service.py:246-270` exposes `extra_allowances` on the Quick
Edit service and passes them unchanged into the raw-diff audit. The later model
is derived only from `tune.journal` (`:320-322`), while shareability depends on
the resulting outcome being problem-free and having any audit (`:295-312`).
Nothing requires an extra allowance to correspond to a journal entry or causes
its attributed bytes to appear in `edits` or `changed_tables`.

**Reproduction:** edited `IP_TQI_REF_MAX_MON` — Maximum reference indicated
engine torque directly through the underlying `CalFile`, bypassing `Tune.write`,
then supplied that table's byte extent as a caller-defined `Allowance`. The
service returned `verified=True`, a non-null `share_path`, and a clean audit
covering 22 changed bytes (8 checksum-storage bytes plus 14 caller-allowed
bytes), while reporting zero edits and zero changed tables.

**Failure scenario:** a bridge caller, future app integration, or mistaken
internal caller covers an unjournaled calibration write with an extra allowance.
The byte audit calls it attributed, so the bin becomes shareable, but the review
model gives the human no table-level claim to inspect. This violates V3's
requirement that allowances come from journal entries plus stored checksum
bytes, and reopens the exact “bin changed but report did not” hole the service
claims to close.

**Suggested fix:** remove arbitrary `extra_allowances` from `build_revision()`,
or replace it with typed, journal/model-visible allowance evidence whose changed
bytes are included in the report. Add the reproduced zero-journal smuggled-write
case and require `verified=False` plus `share_path=None`.

### CR-20260724-02 — Public report assembler can emit internally contradictory shareable reports — High, CONFIRMED — Fixed (2026-07-24)

`simoscal/tune/build_service.py:282-324` exports `build_report()` as a public
assembler over a caller-supplied, mutable `GateOutcome` and the tune's current,
mutable journal. It defines `verified` only as `not outcome.problems` and
`shareable` only as `verified and outcome.diff is not None` (`:295-300`); it
does not require a clean audit or revalidate the other recorded gate facts.
The gate rows and edit model are then re-derived later from the potentially
inconsistent outcome and live tune (`:316-322`).

**Reproductions:**

1. Took a clean no-reference outcome, attached a `RawDiffAudit` with one
   unexplained byte, and called `build_report()`. The result had
   `verified=True` and a non-null `share_path` while `audit.clean=False` and its
   own `Raw-diff audit` gate row had `passed=False`.
2. Ran a clean audited gate chain, wrote
   `IP_PQ_CHA_MAX` — Maximum allowed pressure quotient at turbo charger
   compressor to the tune *after* the gates, then called `build_report()`. The
   staged bin/audit had zero changed bytes, but the verified, shareable report
   listed the post-gate journal entry and table as changes.

**Failure scenario:** direct use—the path the public API and its dedicated test
explicitly support—produces a report whose action fields disagree with its own
evidence, or whose claimed changes were never verified against the staged bin.
`share_path = verified AND audit.ran` therefore does not close the no-byte-level-
claim hole; a failed audit can still count as “ran.”

**Suggested fix:** make a gate outcome immutable and self-contained, snapshot
the journal-derived model during `run_gates()`, and derive both `verified` and
`share_path` from explicit immutable gate verdicts. At minimum, sharing must
require `outcome.ok`, checksum-clean, no readback failures, `diff is not None`,
and `diff.clean`; reject a tune/journal state that differs from the one gated.
Add adversarial direct-assembler tests for an unclean audit and post-gate journal
mutation.

### CR-20260724-03 — Unchanged declarations are reported as changed tables — Medium, CONFIRMED — Fixed (2026-07-24)

`simoscal/tune/build_service.py:491-504` builds `changed_tables` from
`Journal.tables_touched()`. That method intentionally includes declarations
that moved no bytes so restore-to-source readback remains safe, but the desktop
HTML renderer already distinguishes those declarations from actual changes by
intersecting their extent with `RawDiffAudit.changed_offsets`
(`simoscal/tune/report_html.py:267-301`). The service model does not.

**Reproduction:** journaled a write of the current value back to
`IP_PQ_CHA_MAX` — Maximum allowed pressure quotient at turbo charger compressor
and built against the unchanged source. The entry verdict was `unchanged`, its
`moved_bytes` was 0, and the audit found 0 changed bytes, yet
`changed_tables` contained that table.

**Failure scenario:** the Compose review UI presents a carried-forward/no-op
declaration as a table changed in this flash, contradicting the byte audit and
adding review noise to the highest-value list. The model can therefore describe
a change the build did not make even on the normal `build_revision()` path.

**Suggested fix:** when an audit ran, include a table in `changed_tables` only
when its journaled offset/declared extent intersects `outcome.diff.changed_offsets`,
matching the desktop HTML logic. Retain a documented no-reference fallback if
the lower-level assembler continues to support one, and add a no-op declaration
regression test.

### Open questions / assumptions

- `extra_allowances` may be needed by the desktop build for explicit patch
  transitions, but V3's app service contract says the imported bin is both
  source and reference and names only journal/checksum allowances. This review
  therefore treats arbitrary service-level allowances as outside that contract,
  not as an accepted escape hatch.
- `verified=True` without an audit remains the documented lower-level
  `build_report()` meaning, provided `share_path` stays `None`. The High finding
  is the stronger contradiction: an audit that ran and failed can still yield
  both `verified=True` and a share path through that same direct API.

### Summary

Three findings: 2 High and 1 Medium, all CONFIRMED and Open. The
`run_gates()` extraction is behavior-identical to the old inline build spine;
the defects are in the new report/share boundary and are not caught by the
otherwise-green 597-test suite.

### Resolution (2026-07-24)

All three fixed in `simoscal/tune/build_service.py` (+ a supporting snapshot in
`simoscal/tune/pipeline.py`), with an adversarial regression test per finding in
`tests/test_build_service.py`. Full suite green at **601 passed** (was 597; +4
new tests, same 4 expected `StaleChecksumWarning`s), and the service import path
stays matplotlib-free.

- **CR-20260724-01** — Removed the `extra_allowances` parameter from
  `build_revision()` entirely. The app service now derives every audit allowance
  itself (journaled edits, declared restores, stored checksums); there is no
  caller-supplied door through which an unjournaled write could be forgiven and
  hidden from the model. The desktop `build()` / `run_gates()` keep their
  `extra_allowances` for legitimate patch transitions. Test:
  `test_service_offers_no_caller_supplied_allowance`.
- **CR-20260724-02** — Two changes. (a) `share_path` is now derived from the
  explicit gate facts — `outcome.ok` **and** checksum `CLEAN` **and** no readback
  failures **and** the audit ran **and** `diff.clean` — not from an empty problem
  list, so an audit that *ran and failed* can no longer license a share path.
  (b) `run_gates()` now captures a `JournalFingerprint` (entry count + measured +
  declared offset sets) of the journal it gated, stored on `GateOutcome.journal`.
  `build_report()` compares it to the live journal and, on any drift, marks the
  build neither verified nor shareable and records why — a journal mutated after
  the gates ran can no longer produce a report describing an unverified bin.
  Tests: `test_an_unclean_audit_is_never_shareable`,
  `test_journal_mutated_after_gates_is_rejected`.
- **CR-20260724-03** — `_changed_tables()` now takes the audit diff and, when one
  ran, keeps a table only where its measured/declared byte extent intersects
  `diff.changed_offsets` — the same logic `report_html._changed_tables_section`
  uses — so a re-declared table that moved no bytes is no longer listed as
  changed. The no-op edit still appears in the full `edits` journal as a 0-byte
  move; the no-reference path still lists every touched table. Test:
  `test_a_noop_declaration_is_not_a_changed_table`.

---

## Review 2026-07-24 — Quick Edit V1/V4/V5/V6 continuation

- **Scope:** committed Quick Edit foundation through `284af5a`, plus Claude's
  untracked `simoscal/bridge.py` and `tests/test_bridge.py` V6 draft.
- **Method:** traced the V1/V4/V5/V6 acceptance criteria through packaging,
  recovery, generic editing, the Python JSON boundary, and the Android facade;
  ran focused suites and adversarial reproductions against the real SC8S50
  files. No source or generated bin was modified.
- **Headline:** eleven confirmed gaps. Six weakened a safety or provenance
  guarantee; five broke reproducibility or executable acceptance coverage. Ten
  were fixed in this continuation; the physical-arm64/x86_64 V0 gate remains
  open because code cannot substitute for those device runs.
- **Verification:** complete Python suite **655 passed** with the same four
  expected `StaleChecksumWarning`s. Gradle compiled both the debug Kotlin source
  and Android instrumentation source under the documented JDK 17, building the
  embedded wheel and pinned NumPy runtime for arm64-v8a and x86_64.

### CR-20260724-04 — Plot extra required undeclared openpyxl — High, CONFIRMED — Fixed (2026-07-24)

`simoscal.plot` imported `select_tables` from `simoscal.export`, whose eager
`openpyxl` import made `simoscal[plot]` unusable without the separate export
extra. The lazy wrapper then incorrectly recommended installing the already
installed plot extra. `openpyxl` now imports only inside `write_xlsx()`, with an
export-specific actionable error, and an isolated dependency test blocks only
openpyxl while importing the plot surface.

### CR-20260724-05 — Android NumPy runtime was not reproducibly pinned — Medium, CONFIRMED — Fixed (2026-07-24)

The Android build installed the working tree with `numpy>=1.24`; an unchanged
APK build could therefore select a newer compatible wheel than V0 tested.
Chaquopy now pins `numpy==1.26.2`, the device runtime recorded by the V0 parity
report, and the packaging suite pins that declaration.

### CR-20260724-06 — Recovery accepted different XDF definitions — High, CONFIRMED — Fixed (2026-07-24)

Recovery pinned the source bin but stored only XDF paths. Replacing
`SC8S50.V1.0.xdf` with a different parseable XDF successfully restored the same
bytes under different table semantics. Format v2 now records and verifies the
base and every extra-space XDF SHA-256 before opening a tune.

### CR-20260724-07 — Recovery dropped finished-file safety gates — High, CONFIRMED — Fixed (2026-07-24)

`restore_session()` recreated bytes and journal entries but reset
`Tune.post_checks`, including switch-patch sanity. Recoverable checks now carry
a declarative descriptor and are re-registered on restore; an unknown or
non-recoverable check blocks serialization/restoration rather than disappearing.
The optional sanity-reference bin is hash-pinned too.

### CR-20260724-08 — Recovery ignored its engine-version marker — Medium, CONFIRMED — Fixed (2026-07-24)

The serializer wrote `engine_version`, but restore validated only
`format_version`. Recovery now requires the exact engine version promised by
the v1 within-version contract.

### CR-20260724-09 — Recovery reset undo/redo — Medium, CONFIRMED — Fixed (2026-07-24)

A restored edit session opened at its current bytes with a fresh history, so
the first post-restart Undo did nothing. Recovery format v2 now stores compact
byte-diff snapshots plus their journal states and cursor, each hash-verified,
and restores both undo and redo.

### CR-20260724-10 — Session creation bypassed compatibility preflight — High, CONFIRMED — Fixed (2026-07-24)

The bridge trusted the UI to have called preflight earlier. A stock, unpatched
bin plus the switch-patch XDF produced a live session exposing patch addresses.
`session_create` and `session_recover` now independently require an editable
SC8S50 verdict and positive patch detection before opening the patch space.
Build also requires the session's imported bin as both source and reference.

### CR-20260724-11 — Generic axis writes accepted nonmonotonic breakpoints — High, CONFIRMED — Fixed (2026-07-24)

Profile axis tables were editable through the generic cell path without the
strictly-increasing rule used by the switch-patch domain. Profiles now tag axis
vectors explicitly; generic editing rejects duplicate or descending
breakpoints atomically and journals accepted writes as `axis`.

### CR-20260724-12 — Bridge edit tests never reached dispatch — Medium, CONFIRMED — Fixed (2026-07-24)

The test helper's positional argument was named `op`, colliding with the edit
operation's `op="set"` parameter. Eight tests raised `TypeError` in the helper.
Renaming it to `operation` made the intended bridge/edit/build/recovery paths
execute; malformed nested parameter cases were added too.

### CR-20260724-13 — Undo left profile-held decoded values stale — High, CONFIRMED — Fixed (2026-07-24)

History restore wrote the correct prior bytes and cleared `CalFile._views`, but
the resolved profile retained separate `TableView` objects with cached decoded
values. The UI—and a following edit—could therefore see the pre-undo values
despite the buffer having changed. Recovery now invalidates every profile-held
view as well as the lookup cache; the bridge recovery test asserts decoded
values change after Undo.

### CR-20260724-14 — V0 GO declared before all runtime legs ran — Medium, CONFIRMED — Fixed (2026-08-16)

The arm64 emulator produced a byte-identical parity digest, but the plan requires
one physical-arm64 run and an x86_64 run before closing V0. The Android README
called the go/no-go clause satisfied while also saying the physical run remained.
It now records a provisional implementation GO and leaves the two objective
runtime legs open. No code change can substitute for those device executions.

**Closed 2026-08-16.** Both legs are resolved, by opposite routes:

- *Physical arm64* — executed. Galaxy Tab A9+ (`SM-X210`) matched the host at
  digest `9e6ee056…` on 2026-08-15, and the run was re-taken on 2026-08-16
  against the arm64-only APK after the ABI change altered the shipping artifact.
- *x86_64* — retired rather than executed. The ABI was dropped from
  `abiFilters` (`199acc6`), so there is no unproven architecture left to prove.

This is worth stating plainly because the finding could otherwise be read as
half-satisfied: the gate closed because the claim shrank to what was measured,
not because every original leg ran. The README's § "x86_64: dropped, not proven"
carries the reasoning and the warning that re-adding the ABI re-opens this
finding.

---

## Review 2026-07-24 — Quick Edit V6 bridge independent cross-family pass (Opus)

- **Scope:** the committed V4/V5/V6 continuation (`7f03e68`), read as an
  independent second pass by a different model family from the author (the
  cross-vendor review the v1 plan reserves for byte-critical units V0/V2/V3/V5/
  V6). Focus: `simoscal/bridge.py` and `simoscal/tune/recovery.py` — the two
  surfaces every phone byte decision flows through.
- **Method:** traced the dispatch boundary, the path+hash file contract, the
  session/edit/boost ops, and the recovery reconstruction (pristine-buffer
  reopen → verbatim byte-diff → full-buffer SHA-256 verify → journal/undo
  rebuild). Ran the full suite (655 passed) and the focused bridge/recovery/
  packaging suites (67 passed). No source or generated bin was modified during
  review; the recovery image stayed `d61a6e29…`.
- **Headline:** no confirmed safety or provenance defect. The byte-critical
  reconstruction is content-addressed and fail-loud at every step — a moved
  source bin, a changed XDF, an engine-version drift, a mismatched full-buffer
  hash, or a per-snapshot hash mismatch each raise `RecoveryError` rather than
  restoring wrong bytes; generic edits are atomic and axis writes carry the
  strictly-increasing invariant; `build` refuses any reference/source bin whose
  hash is not the session's imported bin. Two Low findings only, one already
  fixed here.

### CR-20260724-15 — No installed-wheel closure test — Low, CONFIRMED — Fixed (2026-07-24)

The mobile-closure tests import from the source checkout (`cwd=CODE_ROOT`),
where every subpackage is on disk regardless of what a real wheel carries.
`test_every_source_package_is_declared_for_distribution` catches a *declaration*
mismatch, but nothing built and installed a wheel into a clean location and
imported the on-device closure from it — the exact strength gap
`implementation_details.md` flagged for V1, and the blast radius of the earlier
CR-20260720-03 (`simoscal.tune.domains` dropped from the wheel). Added
`test_built_wheel_installs_and_imports_the_whole_mobile_closure`: it builds a
wheel, installs only `simoscal` (`--no-deps`) into an isolated `--target`, then
imports the whole numpy-only closure (incl. `simoscal.bridge`,
`simoscal.preflight`, `simoscal.tune.domains`, `simoscal.tune.recovery`) from a
neutral cwd, asserting each module's `__file__` resolves under the install
target so the source tree cannot satisfy the import. Passes in ~2.6 s.

### CR-20260724-16 — `bridge_info` handshake is gated by the version check it bootstraps — Low, PLAUSIBLE — Open

`_op_bridge_info` is documented as the handshake "the app checks it speaks this
engine's version before anything," but `dispatch_obj` rejects every op —
including `bridge_info` — with `VERSION_MISMATCH` when `request.bridge_version`
does not equal `BRIDGE_VERSION`. So an app on a different bridge version cannot
reach `bridge_info` on the happy path; it can only recover the engine's version
from the `VERSION_MISMATCH` error's `advanced` string (`…engine={BRIDGE_VERSION}`),
which the error path does expose. Not a safety issue — the boundary still fails
closed — but the intended handshake op is partially defeated by the global gate.
Recommendation (a contract decision, left to the author): exempt `bridge_info`
(and only it) from the version gate so version discovery works on the happy
path, keeping its response shape frozen forever as the price of that exemption.

### Remaining objective gate (unchanged, not a new finding)

The plan's cross-runtime golden gate — per-operation host-vs-Android byte-
identical request/response fixtures — still does not exist, and its Android leg
needs the open CR-20260724-14 device runs. Building the host-side canonical
fixtures first requires normalizing the bridge's nondeterministic response
fields (uuid `session_id`, temp paths) before they can be frozen; noted here as
the next byte-critical unit after the device gate, not a defect in this code.

---

## Review 2026-08-13 — Quick Edit V7/V8 Android app

- **Scope:** V7 commit `b7e0224`, V8 commit `44c6d5f`, and the README-only
  reconciliation in `46a4bbb`; reviewed the shipped V8 state with the V7 → V8
  delta, surrounding Python bridge/build code, and existing tests.
- **Method:** traced import → preflight → session → edit → recovery → build →
  FileProvider share, reviewed the Compose state transitions and both editor
  paths, ran the Android JVM/manifest/APK gates, ran the focused Python bridge
  suite, and exercised adversarial edits against the real patched SC8S50 bin.
- **Headline:** five findings: two High and three Medium. The release-blocking
  issue is that V8's generic table editor exposes switch-patch implementation
  tables and routes them around the domain guards, allowing structurally invalid
  boost-patch edits to receive a verified, shareable build report.
- **Verification:** `:engine:testDebugUnitTest` passed all **93** tests;
  `:engine:verifyDebugNoPermissions` passed; `:engine:assembleDebug` produced a
  65,775,186-byte APK; `tests/test_bridge.py` passed all **23** tests. The two
  domain-bypass reproductions and the staging-escape reproduction wrote only to
  temporary directories. The untouched recovery image remained
  `d61a6e297b3ac1d25f60ec8cb3bb504ff47f2db603a960a56e6a6e34074ad69b`.

### CR-20260813-01 — Generic patch-table edits bypass domain guards — High, CONFIRMED — Fixed (2026-08-14)

`android/engine/src/main/java/com/simoscal/quickedit/QuickEditViewModel.kt:501-568`
loads the catalog across every tune space and sends every table draft through
the generic `edit` / `paste` operation. The catalog includes the switch-patch
implementation tables, but `simoscal/bridge.py:520-564` routes that generic op
to `apply_op()`, which enforces only reversibility, finite values, and generic
axis monotonicity. It does not invoke `SwitchPatch.slot_curve()`, its eight-row
tiling and below-base-ceiling rules, or `SwitchPatch.slot_rpm_axis()` and its
separate header check.

Two real-file reproductions confirmed the consequence:

- `0x7d41a` — PUT setpoint, boost target grid for map slot 1: changed only the
  first row/first column from about 4000 hPa to 5000 hPa, breaking the required
  identical eight-row tiling. `build_revision()` returned `verified=True`, an
  empty problem list, and a non-null `share_path`.
- `0x7d7da` — PUT SP RPM Axis Header, breakpoint count, must remain 12: changed
  the header from 12 to 13. The build again returned `verified=True`, an empty
  problem list, and a non-null `share_path`.

This is not merely a UI pre-validation gap: the engine accepts and certifies the
unsafe route. The app can therefore hand SimosTools a bin that violates the
switch patch's structural contract. Block generic bridge edits to domain-owned
patch tables, remove those tables from the generic catalog, and require the
switch-patch sanity check on every app build as defense in depth. Domain-owned
edits should be reachable only through their guarded bridge operations.

### CR-20260813-02 — A granted candidate remains mutable — High, PLAUSIBLE — Fixed (2026-08-14)

`android/engine/src/main/java/com/simoscal/quickedit/QuickEditViewModel.kt:285-302`
uses the imported bin's display name as `bin_name` for every build in a session,
and `simoscal/tune/build_service.py:270-274` writes each candidate to the same
`staging_dir / bin_name` path. `ShareBin.kt:26-43` grants another app a content
URI to that path, not to an immutable snapshot.

After a verified candidate is shared, a later edit correctly hides the Share
button but cannot revoke a URI already granted to the receiver. A subsequent
build overwrites the same file before its new gates finish. If SimosTools (or any
selected receiver) defers opening the URI while its grant is live, it can read
the later candidate, including during a failed or partial build, rather than the
verified bytes the user chose to share. The overwrite is confirmed by the path
construction; the receiver timing needs an on-device test, hence PLAUSIBLE.

Give every build a unique, immutable output name independent of the imported
display name, write through a temporary file plus atomic rename, and never reuse
an exported path. A test should retain the first URI/file, run a second build,
and prove the first bytes remain unchanged.

### CR-20260813-03 — Imports block the Android main thread — Medium, CONFIRMED — Fixed (2026-08-14)

`android/engine/src/main/java/com/simoscal/quickedit/QuickEditViewModel.kt:55-59`
launches in `viewModelScope` and immediately calls `ImportStore.importFile()`.
`viewModelScope` uses the main dispatcher by default, while
`ImportStore.kt:66-81` opens the SAF stream, copies it, and computes SHA-256
synchronously. No `withContext(Dispatchers.IO)` or other dispatcher handoff
exists.

A normal 4–6 MB local file freezes composition while it is copied; a cloud or
slow-document provider can hold the UI long enough for an ANR. The busy state is
set immediately beforehand but cannot render while the same thread is blocked.
Run the entire open/copy/hash/rename operation on `Dispatchers.IO`, returning
only the immutable `ImportedFile` to the main dispatcher, and add a ViewModel or
instrumentation test with a deliberately blocking provider stream.

### CR-20260813-04 — State-changing actions silently discard drafts — Medium, CONFIRMED — Fixed (2026-08-14)

The editors refuse a slot switch while a boost draft is dirty, but other
state-changing actions have no equivalent guard. `BoostScreen.kt:169-176` keeps
Undo, Redo, and shared-rpm-axis Apply reachable while a draft exists;
`TablesScreen.kt:272-274` likewise leaves Undo and Redo reachable with a dirty
grid. A successful history operation calls `refreshOpenViews()` at
`QuickEditViewModel.kt:264`, and a successful rpm-axis edit calls
`loadBoostCurve()` at line 468. The resulting `withModel()` / `withDetail()`
transition replaces the draft with freshly committed values.

Thus a user can stage and review a curve or grid, tap Undo/Redo—or apply a shared
axis—and lose the entire proposal without confirmation. Gate these operations
while the open editor is dirty, or present an explicit Apply / Discard / Cancel
decision before refreshing. Add state tests covering dirty drafts across every
engine-mutating action, not only slot selection.

### CR-20260813-05 — Provider display names escape the staging directory — Medium, PLAUSIBLE — Fixed (2026-08-14)

`ImportStore.kt:61` accepts `OpenableColumns.DISPLAY_NAME` as untrusted display
text, `QuickEditViewModel.kt:299` forwards it unchanged as `bin_name`, and
`simoscal/tune/build_service.py:272` joins it directly to `staging_dir`. A
provider can return `../escaped.bin` or an absolute path; `pathlib` then resolves
the candidate outside the FileProvider staging tree.

A direct reproduction with `bin_name="../escaped.bin"` returned
`verified=True`, no problems, and a `share_path` whose resolved parent was
outside `staging_dir`. On Android a hostile document provider could target other
app-private files, including an imported source copy. The write happens before
FileProvider later rejects sharing an out-of-root file. The path escape is
confirmed; the malicious-provider prerequisite makes the app exploit PLAUSIBLE.

Treat the provider name as display-only. Generate the physical candidate name
from a safe revision/build identifier, reject path separators and absolute
names at the Python boundary, and assert the resolved output remains a direct
child of the resolved staging directory before writing.

### Remaining gaps and assumptions

- No Compose screenshot/UI tests or ViewModel tests exercise the interaction
  paths above; the current 93 JVM tests cover pure rules and protocol parsing.
- The documented physical-arm64, x86_64, SAF/share, process-death, low-storage,
  touchscreen-drag, and on-device boost-parity legs remain open. This review did
  not treat those already-declared hardware gaps as new findings.
- The Gradle run emitted Chaquopy/Gradle implicit-task-dependency warnings and
  disabled execution optimizations for affected tasks. The build completed, but
  parallel/incremental task correctness remains a tooling risk outside the V7/V8
  code findings above.

### Resolution (2026-08-14)

All five fixed. Every structural rule landed in the **engine**, with the app-side
change as the second layer rather than the fix itself — a UI that merely stops
offering a bad route is not a guard.

Verification after the fixes: complete Python suite **681 passed** with the same
four expected `StaleChecksumWarning`s (was 662; +19 regression tests, at least
one adversarial reproduction per finding, all run against the real stock and
patched SC8S50 bins). `:engine:testDebugUnitTest` **104 passed** (was 93; +11),
`:engine:verifyDebugNoPermissions` passed, `:engine:assembleDebug` produced a
65,775,188-byte APK under JDK 17.

- **CR-20260813-01** — `TableSpec` gained an `owner`: the domain call that is the
  only legitimate way to write that table. The whole `SwitchPatch2933` profile
  declares one (`slot_curve()` for the five grids, `slot_rpm_axis()` for the
  shared axis, `traction_control()` for the flags, and "never written" for the
  axis-length header). `apply_op()` now refuses an owned table for **every**
  generic op — RESTORE included, since a partial restore breaks the eight-row
  tiling exactly as a partial write does — and `catalog()` omits owned tables, so
  the app is never handed a grid the engine will refuse. `table_detail` still
  reads them; reading was never the hazard, and the boost editor needs the
  values. Defense in depth: `_op_build` registers the switch-patch sanity gate on
  any session holding the patch space, idempotently, so a patched build re-checks
  the finished file whether the session was created or recovered. The two
  reproductions in the finding — the `0x7d41a` first-cell write and the
  `0x7d7da` `12 → 13` header write — are now `EDIT_REJECTED` with no journal
  entry and no undo point. Tests: `test_generic_edit_of_a_slot_grid_is_refused`,
  `test_generic_edit_of_the_slot_rpm_axis_is_refused`,
  `test_generic_edit_of_the_axis_length_header_is_refused`,
  `test_generic_restore_of_a_domain_owned_table_is_refused`,
  `test_the_catalog_does_not_offer_domain_owned_tables`,
  `test_a_domain_owned_table_is_still_readable`,
  `test_the_domain_call_still_writes_the_table_the_generic_path_cannot`,
  `test_a_patched_build_always_runs_the_switch_patch_gate`.
- **CR-20260813-02** — `build_revision()` now writes each build into its own
  fresh `staging_dir/<revision>-<uuid12>/` directory (created with
  `exist_ok=False`) and never reuses a path; `build_report.json` moved beside its
  own candidate. That is a stronger guarantee than temp-file-plus-rename, which
  would only have made one path's *replacement* atomic: no later build writes the
  granted path at all, so a receiver holding a live URI cannot read a subsequent
  or mid-gate candidate. Still under the `staging/` FileProvider root, which
  matches by prefix, so sharing is unaffected. Test:
  `test_a_second_build_cannot_rewrite_the_first_candidate`,
  `test_two_builds_never_share_a_candidate_path`.
- **CR-20260813-03** — `ImportStore.importFile` is now `suspend` and wraps the
  whole open/copy/hash/rename in `withContext(io)` itself, with the dispatcher
  injectable and defaulting to `Dispatchers.IO`, so a caller cannot forget. The
  streaming copy was extracted as a pure `copyAndHash(InputStream, File)` and is
  now covered on the JVM, including a provider that returns one byte per read —
  a copy loop treating a short read as EOF would truncate the file *and* record a
  hash matching the truncation. Tests: `the hash describes the bytes actually
  written`, `a stream that dribbles bytes still hashes to the same value`. That
  the work leaves the main thread still needs an on-device test; see the
  remaining gap below.
- **CR-20260813-04** — `QuickEditUiState` gained `dirtyDraft`, `canMutateSession`,
  and `dirtyDraftRefusal`. Undo, Redo, Restore, and the shared-rpm-axis Apply are
  now refused while either editor holds an unapplied draft — in the view model
  (`refusingWhileDirty()`, which places the reason in the blocking editor) and in
  both screens, which disable the controls and say why. A refusal rather than a
  confirmation dialog: Apply and Discard are already on screen one tap away, so
  there is nothing for a dialog to ask. Restore was not named in the finding but
  discards a draft the same way, so it obeys the same rule. Tests: five new cases
  in `QuickEditStateTest`.
- **CR-20260813-05** — The provider display name no longer names any file. The
  app stopped sending `bin_name` altogether (the build screen shows the
  *candidate's* own name, read off the returned share path, which is the file
  actually handed to SimosTools), and the engine validates both `bin_name` and
  `revision` as bare filename components — separator, absolute, `.`/`..`, NUL,
  and empty are all refused loudly, never sanitized, with a final assertion that
  the candidate resolves to a direct child of this build's directory. The
  finding's `bin_name="../escaped.bin"` reproduction is now `BAD_PARAMS` with no
  file written anywhere under the staging root. Tests:
  `test_a_candidate_name_that_is_not_a_bare_file_name_is_refused` (six traversal
  and degenerate forms), `test_a_revision_label_that_is_not_a_bare_file_name_is_refused`,
  `test_build_refuses_a_name_that_escapes_the_staging_directory`,
  `test_build_refuses_a_revision_that_is_not_a_bare_file_name`.

Still open after this pass: the dispatcher hand-off in CR-20260813-03 and the
FileProvider grant timing in CR-20260813-02 are both proven only at the level the
JVM can reach — the path and copy logic. Confirming them as *device* behavior
(a deliberately blocking SAF provider; a receiver that defers opening a granted
URI across a second build) still needs the instrumentation legs already listed
as open above. Neither is a reason to keep the findings open: the mechanisms they
depended on — a reused output path, a blocking call on the main dispatcher — are
gone.

---

## Review 2026-08-15 — First run on physical hardware (Galaxy Tab A9+)

- **Scope:** the first execution of the Quick Edit app on real arm64 hardware —
  a Samsung Galaxy Tab A9+ (`SM-X210`, Android 16, `arm64-v8a`) — exercising
  import → preflight with the stock `5G0906259L__0002.bin`, the R14 patched bin,
  `SC8S50.V1.0.xdf`, and both switch-patch XDF candidates.
- **Method:** device-driven rather than code-driven. The APK built at V8 was
  installed and used; each failure seen on the screen was traced back to the
  code, then reproduced on the host with the same files before being called a
  finding.
- **Headline:** three findings: one High, one Medium, one Low. The High is the
  reason this review exists — **every bridge operation that names a file has
  been broken since V7**, so the app could import files and do nothing else. No
  host-side test crossed the Kotlin → Python param-naming boundary, so 93 green
  tests and the 2026-08-13 review both passed over it. The lesson is narrow and
  worth stating: the two halves of this app agree on an envelope contract that is
  tested, and a *params* contract that was not.
- **Verification:** `:engine:testDebugUnitTest` passed all **109** tests (104
  pre-existing + 5 new); `:engine:verifyDebugNoPermissions` passed;
  `:engine:assembleDebug` produced a 65,775,186-byte APK, installed to the
  tablet. The CR-20260815-01 fix was proven by reverting it and re-running the
  new test — 4 of its 5 cases fail against the pre-fix code. Post-fix, preflight
  returns a real verdict on the device: *"Ready to edit — recognised SC8S50 bin
  with valid checksums"*, both embedded checksums clean, against the R14 bin.
  Nothing was flashed. The untouched recovery image remained
  `d61a6e297b3ac1d25f60ec8cb3bb504ff47f2db603a960a56e6a6e34074ad69b`.

### CR-20260815-01 — Every file-naming bridge op sends the path under the wrong key — High, CONFIRMED — Fixed (2026-08-15)

`android/engine/src/main/java/com/simoscal/quickedit/BridgeClient.kt:61-64`
(as shipped in `b7e0224`, untouched since) built the verified path+hash pair as:

```kotlin
put(name, file.path)                // "bin"
put("${name}_sha256", file.sha256)  // "bin_sha256"
```

The engine resolves a file from **two suffixed keys**. `simoscal/bridge.py:190-194`
(`_verified_path`) reads `f"{name}_path"` and `f"{name}_sha256"`, and treats a
missing `<name>_path` as a hard `BAD_PARAMS` refusal rather than a degraded
request — deliberately, since the bridge will not open a file it cannot also
hash-verify. The hash therefore arrived under the key the engine wanted and the
path did not, so the file was invisible to it.

`putVerified` is the only way a file is named to the engine, so this broke every
op that takes one: `preflight`, `session_create`, `session_recover`, and `build`
(the `_verified_path` call sites at `bridge.py:300-302`, `316-318`, `380`, and
`715-717`). Import itself is pure Kotlin and worked, which is what made the
failure look like a file problem rather than a protocol one.

Observed on the device as a snackbar reading `missing required parameter
'bin_path'` on the first tap of **Check this bin**, with a valid bin and XDF
selected.

Nothing host-side could catch it. `BridgeProtocolTest` covers the envelope —
version, op, request id, and the *presence* of a params object — and
`QuickEditStateTest` covers the gates; neither asserts a key name inside
`params`, and `putVerified` had no test of its own. `BridgeProtocolTest:25` even
uses a bare `"bin"` key in its sample params, mirroring the wrong convention
into the fixture.

### CR-20260815-02 — An unreadable switch-patch XDF is reported as an absent patch — Medium, CONFIRMED — Fixed (2026-08-15)

`simoscal/preflight.py:233-236` catches any failure to open the switch-patch XDF
and returns `(None, {"switch_patch_error": ...})`, where `None` documents "an
internal error, never a guess" and is correctly distinguished from `False`
("resolved, but the patch is not in this bin"). The distinction is then lost at
the only place a person sees it: `simoscal/bridge.py:327-335` refuses session
creation whenever `switch_patch_present is not True`, with the single message

> The switch-patch tables are not present in this bin.

That sentence is *false* for the `None` case, and misdirects: it describes the
bin, when the problem is the XDF. `switch_patch_error` already holds the true
cause and is discarded.

Reproduced on host against the R14 patched bin, whose patch **is** present:

| Switch-patch XDF                     | `switch_patch_present` | Detail                                    |
|--------------------------------------|------------------------|-------------------------------------------|
| `SC8S50_switchpatch29.33_v1.006.xdf` | `None`                 | `could not open patch XDF: uniqueid 0x11f9c reused with DIFFERENT data` |
| `S50 Switch Patch.29.33.V2.xdf`      | `True`                 | slot 1 decodes 2502..2506 hPa             |

Both runs returned `ok_to_edit=True`; only the patch leg differs. A person
handed the first message would go looking for a missing patch in a bin that has
one, or re-patch a bin that does not need it.

Suggested fix: branch on `None` versus `False` in `_op_session_create` and
surface `switch_patch_error` in the advanced detail — "the switch-patch XDF
could not be read" is a different remedy (choose another XDF) from "the patch is
not in this bin" (patch the bin). The verdict logic itself is right and should
not change.

### CR-20260815-03 — V7 test-count table in the Android README is stale — Low, CONFIRMED — Fixed (2026-08-15)

`android/README.md` § "Verifying V7 on this machine" claimed **93 unit tests**
with a per-class breakdown (`ImportNamingTest` 6, `QuickEditStateTest` 17,
`TablesUiStateTest` 19). The actual counts before this review's change were 9,
22, and 22, totalling 104.

The 93 was correct when written and at the 2026-08-13 review. It went stale on
2026-08-14, when that review's own fixes added 11 cases — `ImportNamingTest` +3
(CR-20260813-03), `QuickEditStateTest` +5 (CR-20260813-04), and
`TablesUiStateTest` +3 — without updating the table. The failure mode is
ordinary: a fix pass adds tests and the count is documented elsewhere.

Minor on its own, but the number is the README's stated pass criterion, so a
reader cannot use it to tell a complete run from a partial one.

## Fixes applied 2026-08-15

- **CR-20260815-01** — `putVerified` now writes `"${name}_path"`, matching
  `_verified_path`'s contract, with a comment naming the engine function so the
  coupling is visible from the Kotlin side. The gap that allowed it is closed by
  `VerifiedParamsTest` (5 cases): the path lands under `<name>_path`, the bare
  `<name>` key is asserted *absent*, a verified file contributes exactly two
  keys, several files coexist without collision, and the suffixing rule is swept
  over all five names the engine passes to `_verified_path` (`bin`, `xdf`,
  `switch_patch_xdf`, `source_bin`, `reference_bin`) so a new call site there
  without a matching name here fails. Proven to catch the defect: against the
  pre-fix line, 4 of the 5 cases fail (the fifth checks `_sha256`, which was
  always correct).
- **CR-20260815-03** — The README table now lists the eight test classes with
  their current counts and totals **109**.

Deferred at the time, then fixed the same day: CR-20260815-02 was held back on
the reasoning that the right wording would be easier to judge once the session
leg had run on the device. The device answered it — the message cost three
separate attempts before the real cause (the wrong file in the switch-patch
slot) became apparent, which is exactly the failure the wording invites. See
"Fixes applied 2026-08-15 (third pass)" at the end of this file.

---

## Review 2026-08-15 — Domain-owned base tables and the on-device build gate

- **Scope:** triggered by the first real session on the tablet. Two things came
  out of it: a sweep of every tagged spec in both profiles for tables the
  generic editor can write but shouldn't, and the build failure that ended the
  session.
- **Method:** the sweep was mechanical, not by eye — every `TableSpec` in
  `SC8S50` and `SwitchPatch2933` enumerated with its tags and owner, then each
  candidate checked against the real stock and R14 bins for what the editor
  would actually display and what a plausible correction would write.
- **Headline:** three findings, two High. The generic table editor could write
  the one table in this calibration whose XDF units are actively wrong, with no
  guard in the way; and no build of a patched bin could ever pass its safety
  gate on a phone, because the gate resolved a path inside a desktop BinToolz
  checkout.
- **Verification:** full Python suite **686 passed** (681 before, plus the five
  new cases); `tests/test_bridge.py` 68 passed, up from 63. Each fix was proven
  to be load-bearing by reverting
  it and re-running — CR-20260815-05's test fails against the old code with the
  device's exact error string. Nothing was flashed; no bin outside `tmp_path`
  was written. The untouched recovery image remained
  `d61a6e297b3ac1d25f60ec8cb3bb504ff47f2db603a960a56e6a6e34074ad69b`.

### CR-20260815-04 — The kg/stk airmass ceiling is generically writable — High, CONFIRMED — Fixed (2026-08-15)

`C_M_AIR_CYL_SP_MAX` — Maximum allowed M_AIR_CYL_SP (maximum allowed airmass
setpoint) is labelled `mg/stk` by the XDF and **stores kg/stk**. Its profile
spec carried `TAG_KG_PER_STROKE` but no `owner`, so `catalog()` offered it and
`apply_op()` accepted a generic write. On the tablet it was the *first row* of
the table browser.

The conversion exists in exactly one place — `simoscal/tune/domains/limits.py:49-73`
— and the generic edit path never consults the tag. That module's own docstring
says the mistake "is not guarded against, it is unavailable"; the generic editor
had quietly made the second half false.

What the person sees and what it does:

| | |
|-------------------------|--------------------------------------------------|
| Displayed (stock bin)   | `0.001389`, beside the XDF's `mg/stk` label       |
| Intended ceiling        | 2000 mg/stk → `0.002` stored                      |
| What typing `2000` does | 2000 kg/stk — about **1.44 million times** stock  |

No guard catches it. The table *is* on `FLOAT_BUG_SYMBOLS`, so a value above the
declared max raises `FloatBugGuardError` even with override — but the declared
max is `20000`, and `2000` breaches nothing. The value is neither out of range
nor structurally invalid; it is simply in the wrong unit, and nothing in the
generic path knows the unit is a lie.

Severity is High on the safety definition rather than the usability one: the
resulting bin verifies, reports clean, and is shareable to SimosTools for
flashing, with the airmass limiter effectively removed.

### CR-20260815-05 — The switch-patch build gate cannot resolve its XDF on a device — High, CONFIRMED — Fixed (2026-08-15)

`SwitchPatch.require_sanity()` registered a post-build check that called
`btp.switch_patch_sanity(bin_path, stock_bin_path=...)` without an
`xdf_path`, so it fell through to `btp.default_switch_patch_xdf()` — a path
inside a **BinToolz checkout**. That directory exists on a development Mac and
on no phone, ever.

Observed on the tablet, building a patched R14 bin:

```
TEST00: NOT verified — 1 gate(s) failed. DO NOT FLASH: switch-patch sanity
failed: check raised BtpError: switch-patch XDF not found:
/data/data/com.simoscal.engine/files/chaquopy/AssetFinder/BinToolz-main/
definitions/S50 Switch Patch.29.33.V2.xdf
```

The gate is registered on *every* build of a patched session (CR-20260813-01,
defense in depth), so this is unconditional: **no patched bin could ever be
built on the app.** The base-only path was unaffected, which is why the failure
did not surface until a switch-patch session was opened on hardware.

It failed safe — loud, unverified, no `share_path`, an explicit DO NOT FLASH —
so nothing unsafe was produced. But a gate that cannot pass is not a gate; it is
a wall, and the app's entire purpose is on the other side of it.

The session's own patch XDF is the correct reference regardless of platform: it
is the definition the edits were made through, so the gate re-reads the finished
file exactly as the editor wrote it. The previous default was pinning a
*curated* definition instead (per `switch_patch_sanity`'s docstring, because the
v1.005/v1.006 XDFs reuse a uniqueid and do not load), which is a real property
worth keeping — but it cannot be bought at the price of the gate never running.
A session whose patch XDF does not load never opens in the first place, so the
XDF reaching the gate is always one that parsed.

### CR-20260815-06 — An unmapped table carries no guard tags — Medium, PLAUSIBLE — Fixed (2026-08-15)

`C_M_AIR_CYL_FL` was absent from the SC8S50 profile entirely. Tags and owners
live only on profile specs, and `catalog()` enumerates every table in the space —
so an unmapped table arrives at the generic editor with no tags, no owner, and
no description beyond the XDF title. That is the structural reason
CR-20260815-04 was reachable, stated as its own finding because the mapping gap
outlives that one table.

`C_M_AIR_CYL_FL` shares its sibling's `mg/stk` label, its `0..20000` declared
range, its float32 store, and its place on `FLOAT_BUG_SYMBOLS`. It reads `0.0`
in both the stock and every patched bin, so **nothing available here proves
whether it stores kg/stk**, which is why this is PLAUSIBLE rather than
CONFIRMED: the mechanism is identical to CR-20260815-04, but triggering it
requires the units to actually be kg/stk.

Mapped and refused rather than left open. No domain call writes it and no
revision in the lineage ever has, so refusing costs nothing today; if a use
appears, settle the units first and give it a real writer. The alternative was
leaving a possibly-millionfold-wrong write one tap away on the strength of an
unverified assumption.

### Not findings (checked and clean)

- **`C_PRS_IM_SP_MAX` and `C_PRS_IM_SP_LIM`** — both float-bug flagged, both
  reading far above their declared maxima (350000 and 271695.84 against 10000).
  Left generically writable deliberately: their units are correct, a write above
  the declared max is refused outright by `FloatBugGuardError`, and any write
  below it *lowers* a ceiling — the safe direction. Unlike the airmass cap,
  there is no value a person could plausibly type that silently means a
  millionfold different thing.
- **The four axis-tagged specs** — generic writes already enforce strict
  monotonicity (CR-20260724-11), which is the invariant that matters.
- **The switch-patch profile** — all 17 specs already declare an owner.

## Fixes applied 2026-08-15 (second pass)

- **CR-20260815-04** — `airmass_setpoint_max` now declares
  `owner="tune.limits.airmass_cap_mg(), …"`, so the table leaves the generic
  catalog and `apply_op()` refuses every generic op against it, RESTORE
  included. `_spec()` in the SC8S50 profile gained an `owner` parameter to carry
  it. Reading is untouched. Tests:
  `test_generic_edit_of_the_airmass_ceiling_is_refused`,
  `test_the_catalog_does_not_offer_the_airmass_tables` (which also asserts the
  genuine-mg/stk `intake_air_max_vvl0` stays editable, so the fix cannot be
  satisfied by hiding everything), and
  `test_the_airmass_ceiling_is_still_readable_and_names_its_owner`.
- **CR-20260815-05** — `require_sanity()` passes
  `xdf_path=self._tune.space(PATCH_SPACE).xdf`, resolved *inside* the check
  rather than at registration, so a session recovered after a process kill uses
  the path it was rehydrated with. No new recovery parameter, and therefore no
  stale absolute path to survive a kill. Test:
  `test_the_switch_patch_gate_does_not_need_a_bintoolz_checkout`, which
  monkeypatches `default_switch_patch_xdf` to an unresolvable path — the
  device's condition — and asserts the gate still runs, passes, and yields a
  verified shareable report.
- **CR-20260815-06** — `C_M_AIR_CYL_FL` mapped as `airmass_full_load` with an
  owner naming the doubt: "no verified write path — this table's units are
  unconfirmed and may be kg/stk like C_M_AIR_CYL_SP_MAX". Test:
  `test_generic_edit_of_the_unconfirmed_airmass_table_is_refused`.

Still open after this pass, and worth stating because both are consequences of
these fixes rather than leftovers:

- **A refused table is now an absent table.** `catalog()` omits owned tables, so
  the airmass ceiling simply is not in the browser — and unlike the switch-patch
  internals, this is a table the tuning guide explicitly tells a person to edit.
  They will look for it and find nothing. The engine already surfaces
  `TableInfo.owner` and `catalog(include_domain_owned=True)`, so the app could
  list owned tables read-only with the owner as the explanation; that is a UI
  change, not an engine one, and is not attempted here.
- **The app has no mg/stk entry point.** With the generic route closed, there is
  no way to set the airmass ceiling from the app at all — `airmass_cap_mg()` has
  no bridge op. Closing the unsafe path was the urgent half; adding the safe one
  is a v1 feature decision.

## Fixes applied 2026-08-15 (third pass)

- **CR-20260815-02** — `_op_session_create` now branches on `None` before the
  `is not True` catch-all. `None` (preflight could not open the patch XDF)
  raises **"The switch-patch XDF could not be read."** and passes on the
  `switch_patch_error` the verdict already carried, telling the person to choose
  a different switch-patch XDF; `False` (opened, patch genuinely absent) keeps
  the original wording, which is correct for that case. The verdict logic did
  not change — it already drew the distinction, and only the message collapsed
  it.

  Field evidence for the severity, recorded because it is easy to underrate a
  wording bug: with a correctly patched R14 bin and the unreadable v1.006 XDF in
  the slot, the message sent the reader to inspect the bin three separate times.
  The actual cause was that the file picker's *Recent* list kept re-offering the
  unreadable XDF, which was not even in the folder the working one was in. A
  message naming the XDF would have ended it on the first attempt.

  Tests: `test_an_unreadable_switch_patch_xdf_blames_the_xdf_not_the_bin`
  (asserts the new message, asserts the old sentence is *absent*, and asserts
  the underlying uniqueid cause is forwarded rather than discarded) and
  `test_an_unpatched_bin_still_says_the_patch_is_absent` (the contrast case, so
  the fix cannot be satisfied by renaming both branches). Proven load-bearing:
  against the pre-fix code the first test fails with the exact sentence seen on
  the device, "The switch-patch tables are not present in this bin."

`tests/test_bridge.py`: **70 passed**.

---

## Review 2026-08-16 — Closing V0 by dropping the x86_64 ABI

- **Scope:** `199acc6` — the reversal of `4adc78c`. Removes
  `.github/workflows/v0-parity-x86_64.yml` and narrows `abiFilters` to
  `arm64-v8a` alone. Plus the documentation reconciliation that followed
  (`android/README.md`, `implementation_details.md`, this file).
- **Verification run before the commit, not asserted after it:** 109 unit tests
  and `verifyDebugNoPermissions` green; the built APK contains `lib/arm64-v8a`
  and no `x86` entry of any kind; fixtures re-pushed and the on-device
  instrumentation re-run on the tablet, reproducing digest `9e6ee056…` with
  `diff_count: 0` and `digests_self_consistent` on both sides.

No new findings. Two notes recorded because they are the kind of thing a future
agent would otherwise have to rediscover:

**The re-run was not ceremonial.** The previous session's `9e6ee056…` claim was
taken against an APK that included x86_64. Changing `abiFilters` changes the
shipping artifact, so inheriting the old verdict would have described a build
that no longer existed. The digest came back identical — as expected, since
dropping an unused ABI should not move arm64 numerics — but "expected" is not
"measured", and the gate's whole value is that it is measured.

**The skipped-leg trap held.** All seven report steps carried real content,
`boost_curve` included. That leg records `{"skipped": …}` rather than failing
when its switch-patch fixtures are absent, and because the digest is a sha256
over the whole `compared` dict, a host and device that both skip agree at a
self-consistent `41f7e2cb…` and print `PARITY: MATCH`. A green banner is
therefore not sufficient evidence; the step contents are. `push_fixtures_and_compare.sh`
now fails on a skipped leg, and the fixtures committed in `4adc78c` are what
keep the leg from skipping in the first place — that commit's CI motivation was
abandoned, but those two changes remain load-bearing for every arm64 run.

**Correction to a prior measurement.** The previous session recorded the APK as
67.9 MB → 38.2 MB across the ABI change. Re-measured on 2026-08-16, both
configurations built back-to-back from the same tree: **60.8 MB → 50.3 MB**, a
10.5 MB saving. The README's older "~30 MB" arm64-only estimate was a guess and
has been replaced with the measurement. Neither prior figure should be quoted.

---

## Review 2026-08-16 — Why the 2026-08-15 session would not recover

- **Scope:** a field failure, not a diff. Tapping *Resume session* on the
  tablet's saved 2026-08-15 21:34 session dropped to the import screen with no
  session. Investigated from the device's own state and reproduced on the host
  through `bridge.dispatch_obj`, the same path the app takes.
- **Not** what it looked like. The first guess was that reinstalling the APK
  revokes the SAF grants the pointer depends on. That is wrong: a session saved
  today recovered cleanly across a reinstall, because the app recovers from its
  own copies under `files/imports/`, not from the original content URIs.

### CR-20260816-01 — A built session can never be recovered — High, CONFIRMED — Fixed (2026-08-16)

`recovery._byte_diff()` pins only the bytes over `journal.declared_offsets()` —
"the extents of every physical/raw table write". `build_revision()` also writes
the **corrected stored checksums** into the live buffer, and those offsets are
not a journaled table extent. So `serialize_session()` records a
`buffer_sha256` taken *after* the build while shipping a diff that cannot
reproduce it, and `restore_session()` fails its own whole-buffer gate:

```
restored buffer does not match the saved session
(recorded 8f21ed8f03ea…, got 0895fdda8488…).
The source bin or a patch may differ from when it was saved.
```

The message is a red herring — nothing about the source bin or the patch
differs. The record is internally inconsistent, and every restore of it fails
identically and permanently.

Measured on the failed session's own inputs (R14 bin, `SC8S50.V1.0.xdf`, the
29.33 V2 switch-patch XDF, all pulled by hash from the tablet's imports dir):
the build changed exactly **4 bytes, `0x200304`–`0x200307`** — the stored
CAL_CRC — and **none** of them are in `declared_offsets()`, which stays at 192
(the boost edit's own extent) before and after the build.

Reproduced as a 2×2, which also scopes it:

| session shape | built | recovers |
| ------------- | ----- | -------- |
| patch space   | no    | OK       |
| patch space   | yes   | **FAILED** |
| base only     | no    | OK       |
| base only     | yes   | **FAILED** |

So it is the build, not the patch space, and not that session's contents. Any
session recovers until it is built, and never afterwards. This matches the
field evidence exactly: that session's `build_report.json` is stamped 21:34 and
the recovery pointer's save time was 21:34:36.

Severity is High because of what the failure costs. `QuickEditViewModel
.recoverSession` clears the pointer on any failure — deliberately, so a bad
record cannot offer the same dead end every launch — so the session is
destroyed by the one attempt to restore it. And the reason goes out as a
transient snackbar, so the person sees an unexplained empty import screen.
Building is the normal end of a session, which makes *the sessions most worth
recovering the only ones that cannot be*.

Fix directions, none applied here:

- Make the diff cover every byte that differs from the pristine patched buffer
  rather than only journaled extents. Self-correcting and makes the
  `buffer_sha256` gate meaningful instead of self-tripping, but it needs the
  pristine buffer at serialize time — a re-open, ~7 s on the tablet.
- Or add the stored-checksum field extents to `_byte_diff()`'s offset set.
  Cheap and targeted, but it re-states the "what can the build touch" question
  in a second place, where it can drift.
- Or journal the checksum write as an edit entry so `declared_offsets()` covers
  it by construction. Probably the most honest — the build *is* writing bytes
  and the journal claims to be the record of writes — but it changes what a
  journal entry means and would show up in reports and undo.

Whichever is chosen, a regression test belongs with it: build, serialize,
restore, assert OK. There is currently no test that serializes a *built*
session, which is why a High-severity break in the recovery path shipped.

#### Fix, 2026-08-16 — the second direction, on a correction to its trade-off

`_byte_diff()` now pins `declared_offsets() | _checksum_offsets(buf)`. The stated
objection to this direction — that it re-states "what can the build touch" in a
second place — does not hold: the offsets come from
`checksum.stored_checksum_ranges()`, which is the *same single* statement of
where the stored values live that the build's own
`audit.checksum_storage_allowance()` reads. There is nothing to drift from.

Chosen over the alternatives because the first costs a ~7 s re-open on the
tablet for every save, and the third would have widened
`Journal.declared_offsets()` — which also feeds the build's
`restore_to_source_allowance` — so making recovery honest would have quietly
loosened a flash-gating audit. Pinning the bytes unconditionally is safe on an
unbuilt session too: they still hold the source's own values, so restore writes
them back unchanged.

**A second failure mode of the same root cause, found while fixing the first.**
`SessionHistory.__init__` re-checks that the recovered undo cursor's snapshot
equals the restored buffer. A build corrects checksums into the live buffer
*without committing an undo point*, so on a built session the top snapshot
legitimately holds the pre-build checksum bytes and that check rejected the
session one step after the byte diff had rebuilt it correctly — same error class,
different message (`recovered undo cursor does not match the restored session
buffer`). The comparison is now `_equal_but_for_checksums()`: a difference
confined to derived bytes is not a difference in the session. Undo/redo semantics
are untouched — snapshots still store and restore whole buffers, so an undo
leaves stale checksums that the next build corrects, exactly as before.

Regression tests, both of which fail on the pre-fix code with the field error and
pass after:

- `tests/test_recovery.py::test_built_session_still_recovers` — build, serialize,
  restore, assert byte-equal. Asserts the build actually moved the live buffer
  first, so it cannot pass vacuously if that ever stops being true.
- `tests/test_recovery.py::test_built_patched_session_still_recovers` — the same
  through `save_session`/`load_session` on a patched bin with a raw slot edit.
- `tests/test_bridge.py::test_a_built_session_survives_serialize_and_recover` —
  the field sequence at the boundary the app calls: `edit` → `build` →
  `session_serialize` → `bridge.reset()` → `session_recover`, then `undo`. This
  is the one that covers the undo-cursor path, since the bridge builds a
  `SessionHistory` for every recovered session.

Full Python suite green (691 tests).

**Verified on the tablet, 2026-08-17** (Galaxy Tab A9+ `SM-X210`, transport
`R92X9086X0D`, arm64-only debug APK rebuilt from this tree). Chaquopy installs
simoscal via `install("../..")`, so the fix ships from the working tree — checked
rather than assumed, by extracting `assets/chaquopy/requirements-common.imy` from
the APK and finding `_checksum_offsets` and `_equal_but_for_checksums` in the
packaged `recovery.pyc`. The sequence:

1. Resumed the leftover 2026-08-16 pointer, written by the **pre-fix** engine —
   it restored cleanly, so the change is backward compatible with an existing
   unbuilt record.
2. Edited `IP_PQ_CHA_MAX` — Maximum allowed pressure quotient at turbo charger
   compressor, cell [0][0], 1.69995 → 1.75. An edit is *required* for this test:
   with none, the checksums stay valid, `correction_patches()` returns empty, and
   the build never writes the bytes that caused the bug.
3. Built revision R00 — Verified, all gates PASSED.
4. Confirmed the recovery pointer's own save stamp (7:21:30) was *after* the
   build, i.e. the same post-build record shape that failed in the field.
5. `am force-stop`, confirmed the process was gone, relaunched, tapped
   *Resume session*.

It restored. The edited cell came back as 1.75000 and **Undo was live** — that
second fact is what exercises the `SessionHistory` cursor path, since the undo
stack had to survive alongside the buffer.

Left on the device: that session now carries a test edit and a built `R00.bin`
in staging. Both are throwaway artifacts of this check — the R00 candidate must
never be flashed.
