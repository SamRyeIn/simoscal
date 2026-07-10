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

| ID             | Severity | Verdict   | File                                            | Summary                                                                    | Status             |
|----------------|----------|-----------|-------------------------------------------------|----------------------------------------------------------------------------|--------------------|
| CR-20260706-01 | High     | CONFIRMED | tests/test_acceptance.py                        | AE3 diff blind to appended bytes; no length assert on edited save          | Open               |
| CR-20260706-02 | High     | CONFIRMED | tests/conftest.py                               | Safety suite is skip-if-absent with no way to force a non-skipped run      | Open               |
| CR-20260706-03 | High     | CONFIRMED | tests/test_acceptance.py                        | AE3 offset math ignores `base_subtract`                                    | Open               |
| CR-20260706-04 | High     | CONFIRMED | tests/test_acceptance.py                        | AE1 tolerance is unbounded and controlled by the capture under test        | Open               |
| CR-20260706-05 | Medium   | CONFIRMED | tests/test_acceptance.py                        | AE4 precondition unsound for declared max in [120, 127)                    | Open               |
| CR-20260706-06 | High     | CONFIRMED | README.md                                       | Quick-start paths do not resolve from the documented cwd                   | Open               |
| CR-20260706-07 | Medium   | CONFIRMED | README.md                                       | Quick-start example writes out-of-range value, fires EditRangeWarning      | Open               |
| CR-20260706-08 | Medium   | CONFIRMED | tests/conftest.py                               | `real_cal` fixture silently shadowed by test_read.py module fixture        | Open               |
| CR-20260706-09 | Medium   | CONFIRMED | tests/test_acceptance.py                        | AE2/AE3/AE5 duplicate pre-existing tests near-verbatim                     | Open               |
| CR-20260706-10 | Medium   | PLAUSIBLE | tests/test_acceptance.py                        | int8 wraparound in AE3 whole-table `+1` before clip                        | Open               |
| CR-20260706-11 | Low      | PLAUSIBLE | tests/test_acceptance.py                        | AE4 asserts on `rec[0]` without filtering warning category                 | Open               |
| CR-20260706-12 | Low      | PLAUSIBLE | tests/conftest.py                               | Oracle JSON read without `utf-8-sig`; Windows BOM fails a valid capture    | Open               |
| CR-20260706-13 | Low      | CONFIRMED | tests/fixtures/README.md                        | mini.xdf documented as 3 tables; it contains 4                             | Open               |
| CR-20260706-14 | Low      | CONFIRMED | tests/conftest.py                               | `requires_real_files` marker is dead code (fifth copy of the guard)        | Open               |
| CR-20260706-15 | Low      | CONFIRMED | tests/test_acceptance.py                        | No guard pins ORACLE_ID identity/shape/dtype                               | Open               |
| CR-20260706-16 | Low      | CONFIRMED | tests/ (multiple)                               | Oracle JSON schema exists in three uncoordinated copies                    | Open               |
| CR-20260706-17 | Low      | CONFIRMED | tests/test_acceptance.py                        | Redundant `checked` counter duplicates fixture guard                       | Open               |
| CR-20260706-18 | Low      | CONFIRMED | README.md                                       | AE gating story told in three prose locations                              | Open               |
| CR-20260706-19 | Low      | CONFIRMED | tests/ (multiple)                               | ~2.5 s/run avoidable XDF re-parsing and slow Python byte-diff loops        | Open               |
| CR-20260706-20 | Low      | CONFIRMED | tests/fixtures/README.md                        | AE1 capture procedure relies on error-prone hand transcription             | Open               |
| CR-20260706-21 | High     | CONFIRMED | simoscal/codec.py                               | 2D table decode uses row-major reshape against column-major on-bin data    | Fixed (2026-07-06) |
| CR-20260706-22 | High     | CONFIRMED | simoscal/xdf.py                                 | mmedtypeflags sign bit inverted for at least three real int16/int32 tables | Fixed (2026-07-06) |
| CR-20260707-01 | Medium   | PLAUSIBLE | simoscal/sop_recipe.py                          | Multi-cell writers leave a table partly written if a guard trips mid-loop  | Fixed (2026-07-07) |
| CR-20260707-02 | Low      | CONFIRMED | simoscal/sop_recipe.py                          | Vestigial row_idx/col_idx locals only None-checked in _apply_literal_table | Fixed (2026-07-07) |
| CR-20260707-03 | High     | CONFIRMED | Tunes/TuningBasicsGuide/TUNE_Basics_Guide_R01.py | Max allowed airmass written as TunerPro workaround value, not physical 2000 | Dismissed (invalid) |
| CR-20260707-04 | Medium   | CONFIRMED | Tunes/TuningBasicsGuide/TUNE_Basics_Guide_R01.py | Merged report shows R01-covered guide items as both applied and skipped    | Fixed (2026-07-07) |
| CR-20260707-05 | Medium   | CONFIRMED | Tunes/TuningBasicsGuide/TUNE_Basics_Guide_R01.py | Coherence-passed banner hides unresolved in-scope guide fueling items      | Open               |

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
