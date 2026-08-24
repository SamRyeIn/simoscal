# simoscal — Implementation Details

This is a living implementation record for the `simoscal` library. It explains
why the implementation is shaped the way it is, what was verified, and what
remains intentionally incomplete. Add a new dated entry at the end whenever
behavior, architecture, safety reasoning, or verification status changes. Do not
rewrite earlier entries; correct factual errors with a dated follow-up entry that
names the superseded statement.

> **Provenance.** Most of what is described below (the portable package
> boundary, preflight, the build service, recovery persistence, generic and
> boost-curve editing, and the bridge) was built between 2026-07-21 and
> 2026-08-16 as the engine behind a private GUI client, and this file was that
> programme's record. On 2026-08-18 the client moved to its own repository ahead
> of opening `simoscal` to beta testers, and this file was rewritten to cover
> only the library. The full original record — including the client's own layers
> and dated entries — moved with it. Nothing here was invented in the rewrite;
> where a design reads as though it were shaped by an embedded, non-desktop
> caller, that is because it was.

## How future agents should use this file

Before changing library code:

1. Read `CLAUDE.md`, `README.md`, and `code_review.md`.
2. Read the newest entry below.
3. Preserve the stock recovery image `bin/5G0906259L__0002.bin`; it must remain
   untouched.
4. Keep parameter references in the required form: `` `ID` — Description ``.
5. Add a dated entry describing the decision, the safety impact, and the exact
   verification performed. Include unresolved gates rather than implying that a
   partial verification is complete.

The document is explanatory evidence, not an additional policy source. If a
workflow or safety rule changes, update `../CLAUDE.md` and then record the
implementation consequence here.

## Current implementation boundary

`simoscal` parses an XDF and a Simos18 bin, edits tables in physical units, and
writes a minimal-diff, checksum-verified bin. It does not flash an ECU and does
not communicate with a vehicle. The write path covers the calibrations a shipped
profile maps — `SC8S50` and `SCGA05` today; a valid layout no profile matches is
inspect-only.

Python is authoritative for:

- XDF and bin parsing;
- compatibility preflight;
- physical-unit decoding and encoding;
- journaled edits and boost-curve guards;
- checksum correction and independent verification;
- readback and byte-level audit; and
- the final verified/shareable decision.

A client of the bridge owns scheduling, lifecycle, file selection, and sharing —
nothing else. It must pass private absolute file paths together with recorded
SHA-256 hashes, never platform URI objects, raw bin bytes, or Python/numpy
objects.

## Architecture and rationale

### Portable package boundary

The core package imports without matplotlib or openpyxl. The plot surface may
reuse table-selection helpers, but the xlsx-only dependency is imported only
inside `write_xlsx()`. This prevents a plot-only installation from requiring the
export extra and gives an actionable export-specific error when xlsx support is
absent.

An embedded runtime installs the package from a working-tree wheel and pins
`numpy==1.26.2`, the NumPy runtime used by the recorded cross-runtime parity
result. The desktop requirement remains broader; an embedded build must not
change behavior merely because a newer compatible NumPy wheel is published.

The standalone host packaging test checks package declarations and import
closure, and `tests/test_packaging.py` adds an installed-wheel closure test — the
strong form of the boundary, since import-closure checks against a source tree
can pass while a published wheel omits a subpackage.

### Compatibility preflight — `simoscal/preflight.py`

`preflight()` is read-only and returns a structured verdict. It checks file
existence, hashes, XDF parsing, region bounds, exact profile resolution against
the registry, checksum state, and optional switch-patch presence. A valid layout
no registered profile recognises is inspect-only; truncated, malformed, or
otherwise unusable input is blocked. There is no continue-anyway path.

The bridge repeats this decision in `session_create()` and `session_recover()`.
This is intentional defense in depth: a UI sequence that forgot to call preflight
cannot create an editable session over invalid bytes. Supplying a switch-patch
XDF for an unpatched bin produces `PREFLIGHT_BLOCKED`; it cannot expose patch
addresses as if they were present.

Which profiles preflight tries is `tune.profiles.BASE_PROFILES` — every shipped
profile that declares a `structure`, so registration is derived rather than
hand-listed. `Verdict.profile_name` names whichever matched and
`Verdict.writable` follows from that match. Every profile is attempted, not just
until the first success: two matches raise `AmbiguousProfileError` rather than
returning a verdict, because no file the user picks can fix a registry that
ships two maps for one calibration, and a first-match win would mean editing
under one car's safety rules on a file that might be another's. When none match,
the refusal quotes the XDF's `deftitle` so it names what the file *is* — the
title is evidence for the reader and never an input to matching.

A matched profile is also the second opinion on *where* the XDF reads. Profile
resolution matches on symbol and shape, which says nothing about addressing, so
preflight compares the XDF's declared base offset against the matched profile's
`structure.cal_file_offset` and blocks the pairing when they disagree. This is
not hypothetical: `SCGa05_cal.xdf` names every A05 table correctly and declares
`BASEOFFSET 0` for addresses that are CAL-relative to `0x220000`, so an edit
through it would write `0x220000` short of its table — outside every range the
CAL checksums cover, producing a bin that builds clean and flashes wrong. The
verdict keeps the two facts separate: `profile_name` and
`advanced.profile_resolved` say the car was recognised, while `profile_matched`
and `writable` stay false.

What is **not** implemented is the *graduated trust model* on top of this: every
registered profile is equally writable once it matches, so there is no
"contributed, readable on arrival, writable only once validated" tier. That
remains beta-program work.

### Renderer-independent build service — `simoscal/tune/build_service.py`

`build_revision()` reuses the existing gate spine without importing matplotlib.
The returned report model is the only object a non-desktop review surface needs.
Sharing is available only when the gate outcome is clean, checksums are
independently verifiable and clean, readback passed, the audit ran, and the audit
found no unexplained bytes.

The service does not accept caller-supplied extra audit allowances. Allowances
come from journaled declarations, legitimate restore-to-source responsibility,
and stored checksum bytes. This prevents an unjournaled write from becoming
shareable while remaining invisible in the report.

Where the imported bin is both the source and the byte-audit reference, the
bridge verifies that the build reference and source hashes match the session's
imported hash before calling the build service.

### Recovery persistence — `simoscal/tune/recovery.py`

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

Restore reopens the source and table spaces, verifies every recorded provenance
hash, reapplies exact bytes, verifies the reconstructed full-buffer hash,
restores the journal and known safety checks, then invalidates both `CalFile`
caches and profile-held `TableView` caches. Clearing only the `CalFile` lookup
cache is insufficient because resolved profile objects retain their own
decoded-value caches.

The undo cursor is re-checked against the restored buffer on the same terms:
equal but for the stored checksums, because a build corrects them without
committing an undo point, so a built session's top snapshot legitimately predates
them.

Switch-patch sanity is represented as a recoverable check. An unknown or
non-describable post-build check prevents serialization rather than silently
dropping a safety gate. Bulk SOP recipe coherence is not yet a recovery format;
sessions carrying a recipe report are refused for recovery persistence instead of
being restored with missing coherence state.

### Generic and boost-curve editing — `simoscal/tune/editing.py`, `boostcurve.py`

Generic edits operate on profile-resolved, reversible tables and are atomic: the
full target is computed before the write, a blocked write leaves the table and
journal unchanged, and requested-versus-encoded values are returned.

Axis tables are explicitly tagged with `TAG_AXIS`. Generic writes to them are
journaled as `axis` and must remain strictly increasing. This covers shared
breakpoints such as:

- `ldp_n_ip_put_sp` — Pressure up throttle setpoint x axis (engine speed);
- `ldpm_n_32_1_lasp` — Basic lambda setpoint x axis (engine speed);
- `ldpm_maf_1_lasp` — Basic lambda setpoint y axis (airmass load); and
- the switch-patch slot RPM axis.

The boost model exposes five slot curves, the shared RPM axis, and the base
`IP_PUT_SP` — Pressure up throttle setpoint ceiling. Slot edits use the existing
switch-patch domain, which tiles a curve across the eight rows, floors psi-to-hPa
conversion so the encoded cap never exceeds the requested psi, and rejects a cap
at or above the live base ceiling.

### The bridge — `simoscal/bridge.py`

`simoscal.bridge.dispatch()` is a versioned JSON-in/JSON-out boundary. The closed
operation table includes preflight, session create/recover/serialize,
catalog/detail, generic edit, boost read/edit, undo/redo, and build.

The boundary provides stable error codes for bad requests, version mismatch,
missing or changed files, preflight blocks, unknown sessions, rejected edits,
recovery failures, profile/tune failures, busy calls, and unexpected internal
errors. Tracebacks are logged privately and are not returned as UI payloads.

A process-global non-blocking lock prevents concurrent mutation. A client is
expected to serialize calls on its own single-threaded executor as well; this
guard is the last line, not the only one.

The bridge is called by an out-of-tree client. Its contract is therefore a
**cross-repo** contract: changing an operation name, an error code, or an
envelope shape breaks a caller this repository cannot see. `tests/test_bridge.py`
is the executable half of that contract.

## Verification record for the 2026-07-24 continuation

- Focused bridge, preflight, recovery, editing, build-service, and packaging
  suites passed while the new failure cases were being added.
- Complete Python suite: **655 passed**, with four expected
  `StaleChecksumWarning` cases from minimal-diff tests.
- `git diff --check` passed.
- The recovery image hash remained
  `d61a6e297b3ac1d25f60ec8cb3bb504ff47f2db603a960a56e6a6e34074ad69b`.

No ECU was flashed. No bin was edited in place.

## Remaining work and explicit non-claims

The following are not complete and must not be described as complete by a future
agent:

- **A graduated trust model over the profile registry.** The registry itself is
  built — `preflight` resolves against `BASE_PROFILES` and writability follows
  from the match, not from a hardcoded name — but every registered profile is
  equally trusted. A contributed profile that is readable on arrival and
  writable only once marked validated is still beta-program work.
- **A05 is mapped but has never been edited.** The `SCGA05` profile is
  registered and resolves cleanly against `SCGa05_cal.xdf`, but that file's
  `BASEOFFSET` is wrong (see above), so preflight blocks the pairing and no A05
  read or write has ever produced a real value. Everything downstream of
  resolution — the domain calls, the build gates, the byte audit — is therefore
  unexercised on this car. A corrected definition file, not a code change, is
  what would change that. Do not describe A05 as a supported car.
- ~~**Per-car safety knowledge is not yet consolidated onto the profile.**~~
  Done. `Profile` now carries the car's `StructureSpec`, derives
  `float_bug_symbols` from the specs tagged `TAG_FLOAT_BUG`, and supplies the
  `stock_references` that `sop_recipe.py`'s guidance strings used to hardcode.
  `safety.FLOAT_BUG_SYMBOLS` is deleted; the guard takes the set from its caller.
  Deriving the set rather than declaring it twice caught a live drift: the global
  named `C_PRS_IM_SP_LIM` — Offset to the pressure behind the air cleaner for the
  limitation of the manifold setpoint, which no spec tagged, so the profile-side
  view of "flagged" was one table short.
- **No preflight CLI.** `preflight()` is a Python function; a newcomer has no
  one-command way to ask whether their bin and XDF are supported.
- **Per-operation cross-runtime golden fixtures for the bridge.** Host tests
  prove the rules, not another runtime's execution of them.

The implementation verifies software integrity only. It does not establish
mechanical safety, and only human review plus real driving logs can validate a
tune before any human flashing step.

## Dated entries

### 2026-08-18 — repository separation

The GUI client that this library was built to serve moved to its own private
repository, and the promo-video build scripts left the `gti-tune` repository the
same way, so that no unpublished client source is reachable from any ref a beta
tester can see.

What changed *in this repository*: the library work that had accumulated on the
client's feature branch — preflight, the bridge, the build service, recovery,
generic and boost-curve editing, quantities, and the catalog, with their tests —
landed on `main` as a single commit, and prose naming the unpublished client was
rewritten to name the library capability instead. Runtime facts that explain a
real design constraint (an embedded runtime carrying no matplotlib; a content URI
that cannot be revoked once granted; an untrusted document-provider display name)
were kept, because deleting them would delete the reason the code is shaped the
way it is.

No behavior changed. Verification: the full Python suite, run on `main` after the
land and the rewrite.

### Future entry template

### YYYY-MM-DD — Short implementation change

- **What changed:**
- **Why:**
- **Safety impact:**
- **Verification performed:**
- **Still owed:**
