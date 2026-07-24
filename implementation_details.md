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

The arm64 emulator parity report matched the host digest. That establishes a
provisional implementation GO for Chaquopy, but it does not close the full V0
plan gate: a physical arm64 run and an x86_64 run are still required. The
status is deliberately documented as provisional in `android/README.md`.

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
- the byte diff needed to reconstruct the current buffer;
- the ordered journal;
- declarative finished-file safety checks; and
- compact undo/redo snapshots and cursor when a `SessionHistory` is attached.

Restore reopens the source and table spaces, verifies every recorded
provenance hash, reapplies exact bytes, verifies the reconstructed full-buffer
hash, restores the journal and known safety checks, then invalidates both
`CalFile` caches and profile-held `TableView` caches. Clearing only the
`CalFile` lookup cache is insufficient because resolved profile objects retain
their own decoded-value caches.

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

The following are not complete and must not be described as complete by a
future agent:

- physical-arm64 and x86_64 V0 parity execution;
- cold-start and physical-device measurements;
- full V6 device execution and per-operation host/Android golden fixtures;
- V7 Android import, navigation, review, and share UI;
- V8 Compose boost-curve and generic calibration editors;
- airplane-mode/process-death/device UI tests; and
- a stronger standalone installed-wheel test boundary for V1.

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
