# Quick Edit v1 — Implementation Details

This is a living implementation record for the private Quick Edit v1 work in
`Code/`. It explains why the implementation is shaped the way it is, what was
verified, and what remains intentionally incomplete. Add a new dated entry at
the end whenever behavior, architecture, safety reasoning, or verification
status changes. Do not rewrite earlier entries; correct factual errors with a
dated follow-up entry that names the superseded statement.

## How future agents should use this file

Before changing Quick Edit code:

1. Read `CLAUDE.md`, `README.md`, the active plan in `../Docs/plans/`, and
   `code_review.md`.
2. Read the newest entry below and inspect the live nested-repository status.
3. Preserve the stock recovery image
   `bin/5G0906259L__0002.bin`; it must remain untouched.
4. Keep parameter references in the required form: `` `ID` — Description ``.
5. Add a dated entry describing the decision, the safety impact, and the exact
   verification performed. Include unresolved gates rather than implying that
   a partial verification is complete.

The document is explanatory evidence, not an additional policy source. If a
workflow or safety rule changes, update `../CLAUDE.md` and then record the
implementation consequence here.

## Current implementation boundary

Quick Edit v1 is a private, SC8S50-only Android editing tool. It does not flash
an ECU, communicate with a vehicle, analyze logs on-device, or provide a
generic write path for arbitrary XDFs. Kotlin owns Android lifecycle, file
selection, scheduling, and sharing. Python remains authoritative for:

- XDF and bin parsing;
- compatibility preflight;
- physical-unit decoding and encoding;
- journaled edits and boost-curve guards;
- checksum correction and independent verification;
- readback and byte-level audit; and
- the final verified/shareable decision.

The Android app must pass private file paths together with recorded SHA-256
hashes. It must never pass Android URI objects, raw bin bytes, or Python/numpy
objects across the bridge.

## Architecture and rationale

### V0 — Chaquopy feasibility

The Android engine embeds the existing Python safety kernel with Chaquopy. This
was selected because it preserves one implementation of the byte, checksum,
scaling, and boost safety logic while the product is personal and private.
The Android dependency closure is numpy plus `simoscal`; matplotlib and
openpyxl are desktop-only extras.

The parity payload is shared by the host runner and Android instrumentation
test. It compares parsed table data, decoded values, edited bytes, readback,
checksum verdicts, and the psi-floor behavior. Timing and environment metadata
are informational and are not part of the parity digest.

The arm64 emulator parity report matched the host digest, and so did a physical
Galaxy Tab A9+ run (2026-08-15), re-taken against the arm64-only APK on
2026-08-16 — three agreeing results at digest `9e6ee056…`.

The V0 gate is **closed**. The x86_64 leg it once owed no longer applies: that
ABI was dropped from `abiFilters` on 2026-08-16 rather than proven, because it
could not be exercised on an Apple-silicon host and the target device is arm64.
Shipping an unverified architecture was the thing worth avoiding. Re-adding
x86_64 re-opens the parity question — see `android/README.md` § "x86_64:
dropped, not proven".

### V1 — Portable package boundary

The core package imports without matplotlib or openpyxl. The plot surface may
reuse table-selection helpers, but the xlsx-only dependency is imported only
inside `write_xlsx()`. This prevents a plot-only installation from requiring
the export extra and gives an actionable export-specific error when xlsx
support is absent.

The Android Gradle configuration pins `numpy==1.26.2`, the NumPy runtime used
by the recorded V0 device result. The desktop requirement remains broader;
the embedded APK must not change behavior merely because a newer compatible
NumPy wheel is published.

The Android build itself constructs and installs a wheel from the working tree.
The standalone host packaging test still checks package declarations and import
closure rather than installing a wheel in a separate environment; this is a
known test-strength gap.

### V2 — Compatibility preflight

`preflight()` is read-only and returns a structured verdict. It checks file
existence, hashes, XDF parsing, region bounds, exact SC8S50 profile resolution,
checksum state, and optional switch-patch presence. A valid but non-SC8S50
layout is inspect-only; truncated, malformed, or otherwise unusable input is
blocked. There is no continue-anyway path.

The bridge repeats this decision in `session_create()` and
`session_recover()`. This is intentional defense in depth: a UI sequence that
forgot to call preflight cannot create an editable session over invalid bytes.
Supplying a switch-patch XDF for an unpatched bin produces
`PREFLIGHT_BLOCKED`; it cannot expose patch addresses as if they were present.

### V3 — Renderer-independent build service

`build_revision()` reuses the existing gate spine without importing
matplotlib. The returned report model is the only object the mobile review
surface needs. Sharing is available only when the gate outcome is clean,
checksums are independently verifiable and clean, readback passed, the audit
ran, and the audit found no unexplained bytes.

The Quick Edit service does not accept caller-supplied extra audit allowances.
Allowances come from journaled declarations, legitimate restore-to-source
responsibility, and stored checksum bytes. This prevents an unjournaled write
from becoming shareable while remaining invisible in the report.

For v1, the imported bin is both the source and the byte-audit reference. The
bridge verifies that the build reference and source hashes match the session's
imported hash before calling the build service.

### V4 — Recovery persistence

The live session remains `Tune` plus its ordered journal. Recovery is not a new
recipe engine. Recovery format version 2 stores:

- exact engine version;
- source-bin SHA-256;
- base and extra-space XDF SHA-256 values;
- the byte diff needed to reconstruct the current buffer — every journaled table
  extent *plus* the stored checksums, which a build writes into the same live
  buffer without journaling a table write (CR-20260816-01: omitting them made a
  built session permanently unrecoverable);
- the ordered journal;
- declarative finished-file safety checks; and
- compact undo/redo snapshots and cursor when a `SessionHistory` is attached.

Restore reopens the source and table spaces, verifies every recorded
provenance hash, reapplies exact bytes, verifies the reconstructed full-buffer
hash, restores the journal and known safety checks, then invalidates both
`CalFile` caches and profile-held `TableView` caches. Clearing only the
`CalFile` lookup cache is insufficient because resolved profile objects retain
their own decoded-value caches.

The undo cursor is re-checked against the restored buffer on the same terms:
equal but for the stored checksums, because a build corrects them without
committing an undo point, so a built session's top snapshot legitimately predates
them.

Switch-patch sanity is represented as a recoverable check. An unknown or
non-describable post-build check prevents serialization rather than silently
dropping a safety gate. Bulk SOP recipe coherence is not yet a recovery format;
sessions carrying a recipe report are refused for recovery persistence instead
of being restored with missing coherence state.

### V5 — Generic and boost-curve editing

Generic edits operate on profile-resolved, reversible tables and are atomic:
the full target is computed before the write, a blocked write leaves the table
and journal unchanged, and requested-versus-encoded values are returned.

Axis tables are explicitly tagged with `TAG_AXIS`. Generic writes to them are
journaled as `axis` and must remain strictly increasing. This covers shared
breakpoints such as:

- `ldp_n_ip_put_sp` — Pressure up throttle setpoint x axis (engine speed);
- `ldpm_n_32_1_lasp` — Basic lambda setpoint x axis (engine speed);
- `ldpm_maf_1_lasp` — Basic lambda setpoint y axis (airmass load); and
- the switch-patch slot RPM axis.

The boost model exposes five slot curves, the shared RPM axis, and the base
`IP_PUT_SP` — Pressure up throttle setpoint ceiling. Slot edits use the existing
switch-patch domain, which tiles a curve across the eight rows, floors psi-to-
hPa conversion so the encoded cap never exceeds the requested psi, and rejects
a cap at or above the live base ceiling.

### V6 — Python/Kotlin bridge

`simoscal.bridge.dispatch()` is a versioned JSON-in/JSON-out boundary. The
closed operation table includes preflight, session create/recover/serialize,
catalog/detail, generic edit, boost read/edit, undo/redo, and build.

The boundary provides stable error codes for bad requests, version mismatch,
missing or changed files, preflight blocks, unknown sessions, rejected edits,
recovery failures, profile/tune failures, busy calls, and unexpected internal
errors. Tracebacks are logged privately and are not returned as UI payloads.

A process-global non-blocking lock prevents concurrent mutation. The Kotlin
`SimoscalBridge` facade additionally serializes calls on one background executor
so Compose does not wait on Python and two operations cannot race a session.
The Kotlin layer returns immutable result types and never performs bin math.

## Verification record for the 2026-07-24 continuation

The continuation was performed against the nested branch
`feat/quickedit-v1`, which already contained commits through V3 review fixes.
The following checks completed:

- Focused bridge, preflight, recovery, editing, build-service, and packaging
  suites passed while the new failure cases were being added.
- Complete Python suite: **655 passed**, with four expected
  `StaleChecksumWarning` cases from minimal-diff tests.
- Android debug Kotlin and Android instrumentation sources compiled under the
  project-documented JDK 17.
- Chaquopy built the wheel and installed the pinned NumPy runtime for both
  `arm64-v8a` and `x86_64` during the Gradle build.
- `git diff --check` passed.
- The recovery image hash remained
  `d61a6e297b3ac1d25f60ec8cb3bb504ff47f2db603a960a56e6a6e34074ad69b`.

No ECU was flashed. No bin was edited in place. No root-repository user work
was reverted.

## Remaining work and explicit non-claims

Last reconciled against the dated entries below on 2026-08-13. V0–V8 are all
implemented and committed on `feat/quickedit-v1`; everything still owed needs
either a real device or a Compose test harness. The following are not complete
and must not be described as complete by a future agent:

- ~~physical-arm64 and x86_64 V0 parity execution~~ — **closed 2026-08-16**:
  physical arm64 passed and was re-proven against the arm64-only APK; x86_64 was
  dropped rather than proven;
- cold-start measurements (physical-device timings are now recorded in
  `android/README.md`);
- full V6 device execution and per-operation host/Android golden fixtures —
  still the next byte-critical unit;
- the boost-only parity pull: an edit made on the phone against the real SC8S50
  bin, hand-reviewed against a desktop `simoscal` build of the same edit;
- on-device gesture verification — dragging the 12 boost breakpoints on a
  phone-width plot with a real fingertip;
- Compose UI and screenshot tests (light + dark) for the V7 shell and the V8
  editors; only the pure state and coordinate math are covered today;
- airplane-mode, process-death, rotation, low-storage, SAF-picker, and real
  share-to-SimosTools device tests.

Passing unit tests on the host prove the rules, not the phone: no leg of this
list may be inferred from a green `:engine:testDebugUnitTest` run.

The implementation verifies software integrity only. It does not establish
mechanical safety, and only human review plus real driving logs can validate a
tune before any human flashing step.

## Dated entries

### 2026-07-24 — V1/V4/V5/V6 continuation and adversarial review

Inspected the committed Quick Edit foundation and Claude's untracked V6 draft.
The first bridge run exposed eight tests that failed in the test helper before
dispatch because `call(op, ...)` collided with edit requests carrying
`op="set"`. Renaming the helper argument allowed the intended edit, build, and
recovery paths to execute.

Adversarial checks then found that a stock bin plus the switch-patch XDF could
open a patch-space session without a positive patch-presence verdict. The bridge
now repeats preflight and blocks that combination. Recovery was also hardened
against changed XDF definitions, engine-version drift, missing safety checks,
and lost undo/redo state. A cache invalidation bug discovered during testing
showed that undo changed bytes but left profile-held decoded values stale; all
resolved views are now invalidated.

The portable-boundary review found that the plot extra imported openpyxl
eagerly and that Android NumPy was floating. The xlsx import is lazy and the
Android dependency is pinned. Axis profile tags and strict monotonicity checks
were added to make generic axis writes carry the same invariant as the
switch-patch domain path.

The Python/Kotlin V6 facade and bridge instrumentation test were added, and the
Android source compiled under JDK 17. The review log records these findings as
`CR-20260724-04` through `CR-20260724-14`; all except the external V0 device
gate are fixed.

### 2026-07-24 — Independent cross-family review + installed-wheel closure

Context: picking up the committed V4/V5/V6 continuation (`7f03e68`), which
carried Codex's own review of its bridge/recovery work. The v1 plan reserves a
cross-vendor adversarial pass on the byte-critical units (V0/V2/V3/V5/V6); this
entry records that pass, performed by a different model family (Opus) than the
author.

Decision and rationale: the full suite was re-run and confirmed green (655
passed, four expected `StaleChecksumWarning`s) rather than trusted from the
prior writeup, then `simoscal/bridge.py` and `simoscal/tune/recovery.py` were
read independently. No confirmed safety or provenance defect surfaced — the
recovery reconstruction is content-addressed and fail-loud at every step
(source-bin hash, per-space XDF hash, engine version, full-buffer hash, and
per-undo-snapshot hash all raise `RecoveryError` on mismatch), generic edits are
atomic, axis writes carry the strictly-increasing invariant, and `build` refuses
any reference/source bin whose hash is not the session's imported bin.

Two Low findings resulted (`code_review.md`, CR-20260724-15/16):

- CR-15 (Fixed) — the mobile-closure tests imported from the source checkout, so
  no test proved a *built and installed* wheel still carries the whole on-device
  closure. Added `test_built_wheel_installs_and_imports_the_whole_mobile_closure`
  in `tests/test_packaging.py`: it builds a wheel, installs only `simoscal`
  (`--no-deps`) into an isolated target, and imports the numpy-only closure from
  that installed tree, asserting each module's `__file__` resolves under the
  target. This closes the V1 installed-wheel strength gap this document
  previously listed as remaining, at the blast radius of CR-20260720-03.
- CR-16 (Open, Low) — `_op_bridge_info` is gated by the same version check it is
  meant to bootstrap; it fails closed and the engine version is still recoverable
  from the `VERSION_MISMATCH` error's advanced detail, so this is left as a
  contract decision for the author, not fixed unilaterally.

Safety/provenance impact: none negative. The installed-wheel test strengthens
the guarantee that the on-device artifact cannot silently omit a safety-relevant
module. No source bin or generated bin was modified; the recovery image stayed
`d61a6e29…`.

Files changed: `tests/test_packaging.py` (new test + `os` import),
`code_review.md` (review section + two index rows), this file. Commits
`7f03e68` (the reviewed continuation) and `1edb59d` (this review + test).

Verification: `tests/test_packaging.py` 8 passed; the new test passes in ~2.6 s;
full suite green at 655 as above.

Remaining risks or follow-up: the plan's cross-runtime golden gate (per-op host-
vs-Android byte-identical fixtures) still does not exist; its host side needs the
bridge's nondeterministic response fields (uuid `session_id`, temp paths)
normalized before capture, and its Android side needs the open CR-20260724-14
device runs. That is the next byte-critical unit; V7/V8 Android UI remains the
next visible unit and is Sonnet-delegated under Opus review.

### 2026-07-25 — V7: Compose shell and the Quick Edit flow

Context: V0 (Chaquopy parity) and V6 (bridge) were done and committed, leaving
V7 — "Import → preflight → edit → review → share, on a phone" — as the next
visible unit of the Quick Edit v1 plan. Sam chose V7 over the deferred
golden-fixture normalization, and authorized pushing the branch.

Decision and rationale:

- **One module, not two.** The UI lives in `:engine` alongside the Chaquopy
  runtime, in package `com.simoscal.quickedit`. Chaquopy's Gradle plugin applies
  to the *application* module, so a separate `:app` would have to carry its own
  Python runtime or demote `engine` to a library Chaquopy does not support.
  Keeping one module also leaves the V0 parity evidence — taken against
  `applicationId com.simoscal.engine` — describing the artifact the UI ships in.
- **Safety rules live in pure data, not in composables.** `QuickEditState.kt`
  holds every gate (`canOpenSession`, `exportVisible`, `destinationEnabled`,
  `invalidatingBuild`, `retractingBlocker`) as pure Kotlin, so they are pinned by
  JVM tests rather than by a screen test that only proves a button was drawn.
- **DataStore, not Room**, for recovery: one record, no relations, no queries.
  The hard half is already the engine's (`session_serialize`/`session_recover`);
  the app persists that record plus verified path+hash pointers.
- **`material-icons-core`, not `-extended`**: the extended artifact measured
  5.4 MB of APK (71.2 → 65.8 MB) for three navigation glyphs.

Two bridge-contract mismatches were found while wiring the ViewModel and fixed
against the engine rather than papered over: no op returns an edit count (an
assumed `entries` field does not exist), so `hasEdits` now derives from the
engine's `can_undo`; and the engine's bad-request envelope carries no `op` or
`request_id`, which the app's call-identity check would have discarded as a
mismatch — it now delivers the real `BAD_REQUEST` reason.

Safety/provenance impact: positive, and no Python touched. The manifest declares
no permissions, enforced by a new `verify<Variant>NoPermissions` Gradle task that
reads the **merged** manifest (so a library-contributed permission fails too) and
is wired into `check`. Its first real run caught
`DYNAMIC_RECEIVER_NOT_EXPORTED_PERMISSION`, an AndroidX-Core signature permission
the app defines for itself; it is allowed by exact name with the reasoning
recorded, not by pattern. The FileProvider exposes only `staging/`, so imported
bins and XDFs cannot leave through the share sheet. Imports stream into
app-private content-addressed copies hashed from the bytes actually written, and
a picked URI is never edited in place. The recovery image stayed `d61a6e29…`.

One genuine defect was found in review of the delegated UI work and fixed: the
non-dismissible blocked-preflight dialog had no action that cleared
`PreflightState.Blocked`, so both its buttons left it on screen covering the file
pickers — a permanent trap rather than a dead end. `retractingBlocker()` now
returns the state to un-checked (granting nothing, since `canOpenSession` still
requires `Passed`), the dialog owns its own picker launcher, and a test pins it.

Files changed: new `android/engine/src/main/java/com/simoscal/quickedit/`
(`BridgeProtocol.kt`, `BridgeClient.kt`, `ImportStore.kt`, `QuickEditState.kt`,
`QuickEditViewModel.kt`, `RecoveryStore.kt`, `ShareBin.kt`, `MainActivity.kt`,
`ui/` shell + four screens); new `src/main/res/` and `src/test/`; modified
`android/engine/build.gradle.kts`, `android/engine/src/main/AndroidManifest.xml`,
`android/README.md`, this file.

Verification: `:engine:testDebugUnitTest` **36 passed** (13 `BridgeProtocolTest`,
17 `QuickEditStateTest`, 6 `ImportNamingTest`); `:engine:verifyDebugNoPermissions`
passes with a receipt; `:engine:assembleDebug` produces a 65.8 MB APK. Note that
`lintDebug` is **not** a meaningful gate here: AGP 7.4.2's lint bundles a Kotlin
1.7.1 UAST analyzer against this project's Kotlin 1.9.24, so its Kotlin source
analysis does not run (18 warnings remain, all deliberate version pins or
targetSdk 33). A Gradle fix was also needed: `.kts` scripts compile at Kotlin
apiVersion 1.4, so `replaceFirstChar` is unavailable in the build script.

Remaining risks or follow-up: every V7 leg that needs hardware is unverified and
is listed, not claimed — airplane-mode import/edit/export, the SAF picker and the
real share hand-off to SimosTools, process-death recovery during copy/edit/build,
and rotation/low-storage behaviour. No Compose UI test exists yet; the pure rules
are covered instead. V8 (the boost-curve canvas and the real editors) is the next
visible unit, and the cross-runtime golden fixtures remain the next byte-critical
one.

### 2026-07-25 — V8: the boost canvas and the calibration editors

Context: V7 left `TablesScreen` and `BoostScreen` as honest placeholders. V8 is
the plan's next visible unit — the hero boost-curve surface plus the generic
scalar/line/grid/axis editors — built on the V6 bridge and the V7 shell.

Decision and rationale: the unit's real content turned out to be a *distinction*,
not a drawing. `min(base ceiling, slot)` yields **two** limits, and the engine's
guard uses only one of them: `switchpatch._check_below_base_ceiling` compares
every point against the **scalar maximum** of the base full-load row, while what
actually caps boost at a given rpm is the *per-rpm* interpolated value. Between
them is a band where an edit is accepted and then ignored — the change never
shows on a log. The canvas therefore draws both (solid per-rpm ceiling with a
shaded band above it; dashed scalar refusal line) and the screen counts the
capped breakpoints in words. A UI that enforced the per-rpm value as if it were
the refusal limit would have blocked edits the engine accepts.

Two more rules were made explicit and shared by both editors. **Dragged values
are clamped, typed values are refused** — a fingertip never stated an exact
number, so snapping it alters nothing anyone asked for, whereas silently storing
20.99 for a typed 21.00 would be this library's cardinal sin committed one layer
up. And **edits are staged**: a drag moves a local draft and Apply sends one op,
so one deliberate change stays one journal entry and one undo point instead of a
gesture becoming fifty. Switching slots with an unapplied draft is refused rather
than auto-discarded, and undo/redo re-read whatever editor is open, because a
grid still showing undone values would let someone Apply a draft built on numbers
the session no longer holds.

`maxSettablePsi` backs off exactly one `PSI_STEP` below refusal, which is
sufficient rather than merely cautious: the engine's test is `>=` and psi is
floored on its way to stored hPa, so strictly-below in psi implies
strictly-below in hPa. A test sweeps the whole drag range and asserts no
reachable fingertip position yields a cap the engine would refuse.

Safety/provenance impact: two additive Python changes. A new bridge op
`boost_rpm_axis` routes the shared slot rpm axis through
`switchpatch.slot_rpm_axis` rather than the generic `edit` path, because only the
domain call enforces strictly-increasing breakpoints *and* checks the patch's
separate axis-length header — and one axis serves all five slots, so a bad
breakpoint reinterprets every slot curve at once while the stored grids sit
unchanged. `BRIDGE_VERSION` is deliberately **not** bumped: an older app never
names a new op, and a newer app against an older engine gets a clean `UNKNOWN_OP`
rather than a field read two ways. `TableInfo` gained `is_axis` so the editor can
pre-validate monotonicity. Non-reversible tables are read-only in the UI
(`canApply` is gated on `reversible`), Restore is a real journaled `restore` op
rather than a local reset, and the manifest still declares no permissions. The
recovery image stayed `d61a6e29…074ad69b` and the APK stayed 65.8 MB — V8 adds no
dependency.

Files changed: new `android/engine/src/main/java/com/simoscal/quickedit/`
`BoostCurve.kt`, `BoostUiState.kt`, `BoostPlot.kt`, `TablesUiState.kt` and
`ui/BoostCanvas.kt`; rewritten `ui/BoostScreen.kt` and `ui/TablesScreen.kt`;
modified `QuickEditState.kt` (boost/tables state, and the three input setters now
share one `forgettingPreviousInputs()` so a stale grid cannot survive a new bin)
and `QuickEditViewModel.kt`; new tests `BoostCurveTest.kt`, `BoostUiStateTest.kt`,
`BoostPlotTest.kt`, `TablesUiStateTest.kt`; modified `simoscal/bridge.py`,
`simoscal/tune/catalog.py`, `tests/test_bridge.py`, `android/README.md`, this
file.

Verification: `:engine:testDebugUnitTest` **93 passed** (up from 36 — 18
`BoostCurveTest`, 13 `BoostUiStateTest`, 7 `BoostPlotTest`, 19
`TablesUiStateTest` added); `:engine:verifyDebugNoPermissions` passes with a
receipt; `:engine:assembleDebug` produces a 65.8 MB APK. Python: the full
`Code/tests` suite **662 passed, 4 `StaleChecksumWarning`** in ~10 min, up from a
656-test baseline by the six new `boost_rpm_axis` cases. The canvas coordinate math was deliberately moved out
of the `ui` package into `BoostPlot.kt` with plain floats so `psiAt` could be
tested as the exact inverse of `y` — the step where a fingertip becomes a number
written to a bin.

Remaining risks or follow-up: **no Compose screenshot tests**, which the plan's
V8 asks for; the pure state and coordinate math are covered instead, carrying
forward V7's decision not to stand up a Compose test harness. Every on-device leg
is owed and listed rather than claimed: dragging 12 breakpoints on a phone-width
plot with a real fingertip, and the boost-only parity pull on the real SC8S50 bin
hand-reviewed against a desktop `simoscal` build of the same edit. The
cross-runtime golden fixtures remain the next byte-critical unit.

### Future entry template

```markdown
### YYYY-MM-DD — Short implementation change

Context:

Decision and rationale:

Safety/provenance impact:

Files changed:

Verification:

Remaining risks or follow-up:
```
