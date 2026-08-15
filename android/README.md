# `Code/android` — Quick Edit v1 Android app (V0 parity gate + V7 shell + V8 editors)

Implements **V0**, **V7**, and **V8** of
`Docs/plans/2026-07-21-002-feat-simoscal-quickedit-v1-plan.md`: first prove the
Python engine runs under Chaquopy with **byte-for-byte parity** against host
Python, then build the Compose shell that drives it. Nothing here flashes an
ECU, and nothing here does bin math in Kotlin.

## Status

| Piece                                     | State                                         |
| ----------------------------------------- | --------------------------------------------- |
| Parity payload (`simoscal_v0_parity.py`)  | Done, deterministic, verified on host         |
| Host runner + golden                      | Done (`parity/run_host_parity.py`)            |
| Engine decoupled from matplotlib/openpyxl | Done (see "Ordering note")                    |
| Gradle/Chaquopy project                   | Builds (AGP 7.4.2 / Gradle 7.6.4 — see below) |
| Arm64-emulator parity verdict             | **PASS** — digest match (2026-07-23)          |
| Physical-arm64 / x86_64 parity            | Pending — required to close V0                |
| V7 Compose shell + Quick Edit flow        | Built; host-verifiable half green (see V7)    |
| V7 on-device legs (SAF, share, recovery)  | Pending — needs Sam's hardware                |
| V8 boost canvas + calibration editors     | Built; pure rules green (see V8)              |
| V8 on-device legs (drag, screenshots)     | Pending — needs Sam's hardware                |

## V7 — the Compose shell

The app lives in **one module**, `:engine`, alongside the Chaquopy runtime. That
is not tidiness lost: Chaquopy's Gradle plugin applies to the *application*
module, so a separate `:app` would have to either carry its own Python runtime or
demote `engine` to a library Chaquopy does not support. Keeping one module also
leaves the V0 parity evidence (taken against `applicationId com.simoscal.engine`)
describing the same artifact the UI ships in.

UI code is `com.simoscal.quickedit`; the V0/V6 engine plumbing stays in
`com.simoscal.engine`.

| File                    | What it is responsible for                                       |
| ----------------------- | ---------------------------------------------------------------- |
| `BridgeProtocol.kt`     | Envelope build/parse; version and call-identity checks. Pure.     |
| `BridgeClient.kt`       | Suspending front door; cancellation never aborts an in-flight op. |
| `ImportStore.kt`        | SAF URI → app-private content-addressed copy, hashed while streaming. |
| `QuickEditState.kt`     | Every gate rule, as pure data. Where the safety invariants live.  |
| `QuickEditViewModel.kt` | Sequences bridge calls; persists recovery after each mutation.    |
| `RecoveryStore.kt`      | DataStore pointer wrapping the engine's own session record.       |
| `ShareBin.kt`           | FileProvider grant; takes a `Verified` build and nothing else.    |
| `BoostCurve.kt`         | Boost read model + the two ceilings and every clamp. Pure.        |
| `BoostUiState.kt`       | Staged boost draft and its transitions. Pure.                     |
| `BoostPlot.kt`          | Canvas coordinate math, Compose-free so it is JVM-testable.       |
| `TablesUiState.kt`      | Catalog, table draft, selection, and batch operations. Pure.      |
| `ui/`                   | Compose shell, navigation, and the four screens.                  |

### The rules the shell enforces

- **No "continue anyway".** A blocked preflight renders a dialog that cannot be
  dismissed by back-press or outside-tap, offering only *Choose another bin* and
  *Cancel*. Both retract the verdict; neither opens a session, because
  `canOpenSession` requires `PreflightState.Passed`.
- **Export exists only in the verified state** — absent, not disabled. Any edit,
  undo, or redo invalidates a completed build (`invalidatingBuild()`), so a
  Share button can never point at a candidate bin that predates the current
  journal.
- **`Simple | Advanced` changes visible controls only.** No `canX` value reads
  `Mode`; a test sweeps the state space to keep it that way.
- **A gate that did not run is not a pass.** `GateResult.ran` is rendered as its
  own third state.
- **No permissions.** Enforced by `verifyDebugNoPermissions`, which reads the
  *merged* manifest — so a permission contributed by a library fails the build
  too — and is wired into `check`.

### Why DataStore rather than Room

There is exactly one recovery record, with no relations and no queries. The hard
half of recovery is the engine's (`session_serialize` / `session_recover`); the
app only stores that record plus verified pointers to the input files. Room would
add an annotation processor to persist a single JSON string. Revisit at Phase 2
if projects and revision lineage arrive.

### Verifying V7 on this machine

```bash
export JAVA_HOME=/opt/homebrew/opt/openjdk@17
export ANDROID_HOME="$HOME/Library/Android/sdk"
./gradlew :engine:testDebugUnitTest :engine:verifyDebugNoPermissions
```

Expect **93 unit tests passing** (13 `BridgeProtocolTest`, 17
`QuickEditStateTest`, 6 `ImportNamingTest`, 18 `BoostCurveTest`, 13
`BoostUiStateTest`, 7 `BoostPlotTest`, 19 `TablesUiStateTest`) and a receipt at
`engine/build/reports/permissions/debug.txt`.

The unit tests are deliberately JVM-only and cover the pure layers: the envelope
contract against the real `org.json`, every state gate, and the import naming and
hashing rules. They need no device.

`./gradlew :engine:assembleDebug` builds the whole APK (Compose + Chaquopy):
**65.8 MB** for arm64 + x86_64 — unchanged by V8, which adds no dependency — against V0's 54 MB — Compose costs ~12 MB. The
`material-icons-extended` artifact accounted for 5.4 MB of that on its own for
three glyphs, so the navigation bar uses `material-icons-core` instead.

> **`lintDebug` is not a meaningful gate here.** AGP 7.4.2's lint bundles a
> Kotlin 1.7.1 UAST analyzer, and the project compiles with Kotlin 1.9.24, so
> lint emits `Module was compiled with an incompatible version of Kotlin` and its
> Kotlin *source* analysis does not run. It still checks Gradle, manifest, and
> resources (currently 18 warnings, all deliberate pins or targetSdk 33). Do not
> read a clean `lintDebug` as a statement about the Kotlin code.

### What V7 still owes, and why it needs hardware

None of these have a host-side substitute; they are listed rather than claimed:

- airplane-mode import → edit → export on a real phone;
- the SAF picker and the share hand-off to SimosTools with the real bin and XDF;
- process-death recovery during copy, edit, and build;
- rotation and low-storage behaviour.

## V8 — the boost canvas and the calibration editors

### Two ceilings, not one

The single most important thing in this unit. `min(base ceiling, slot)` produces
**two different limits**, and the editor draws both because conflating them
would either block edits the engine accepts or forward edits it rejects:

| Limit                                | What it is                                                        | In the UI                    |
| ------------------------------------ | ----------------------------------------------------------------- | ---------------------------- |
| `BoostCurveModel.baseCeilingPsi`     | `IP_PUT_SP` — Pressure up throttle setpoint full-load row, interpolated onto the slot rpm axis. Per-rpm. | Solid line + shaded band above it |
| `BoostCurveModel.refusalCeilingPsi`  | The **scalar maximum** of that same row — what `switchpatch._check_below_base_ceiling` compares against. | Dashed error-coloured line   |

Between them lies a real region where an edit is **accepted and then ignored**:
the base caps the slot at that rpm, so the change never shows on a log. That band
is shaded and counted in words on the screen, because a silently ineffective
boost edit is the failure a person would otherwise diagnose from a datalog.

`maxSettablePsi` backs off one `PSI_STEP` below refusal. The engine's test is
`>=`, and psi is *floored* on its way to stored hPa, so any value strictly below
the ceiling in psi is also strictly below it in hPa — one step is sufficient,
not merely cautious. `BoostCurveTest` sweeps the drag range and asserts no
reachable fingertip position produces a cap the engine would refuse.

### Dragged values are clamped; typed values are refused

The distinction is deliberate and applies to both editors:

- A **drag** never stated an exact number, so snapping it into the legal range
  alters nothing anyone asked for.
- A **typed** number is a stated intent. Silently storing 20.99 for a typed 21.00
  would be this library's cardinal sin — quietly altering a value — committed one
  layer up. So typed entry is *validated* and the entry is left untouched with a
  message naming the ceiling.

### Edits are staged, and one deliberate change is one journal entry

A drag moves a local draft; **Apply** sends it as a single engine op. Continuous
committing would turn one gesture into dozens of journal entries and undo points,
on a bin that gets flashed to a real ECU. The table editor works the same way:
cell edits and the batch operations (fill / offset / scale / ramp) compose a
draft, and Apply sends one `paste` over the whole grid.

Two consequences worth knowing:

- **Switching slots with an unapplied draft is refused**, not auto-discarded. The
  alternative silently drops an edit on the way to another slot.
- **Undo and redo re-read whatever editor is open** (`refreshOpenViews`). Without
  it a grid would keep showing values the session no longer holds, and the diff
  someone reviewed before Apply would not be the diff they made.

### What each editor will not let you do

- A **non-reversible** table (non-linear scaling, or no embedded data) is
  presented read-only. `canApply` is gated on `reversible`, so a proposal that
  could only ever end in an engine refusal cannot be composed and sent.
- An **axis** edit that breaks strict monotonicity is refused at the keystroke.
  The engine enforces it regardless — this only makes the refusal arrive while
  the value is still on screen.
- **Restore** is a real `restore` op, never a local reset: only the journal knows
  what a table held at session start, and it is journaled like any other change
  so the build's byte audit can still explain it.

### One bridge op was added: `boost_rpm_axis`

The shared slot rpm axis is routed through `switchpatch.slot_rpm_axis` rather
than reached with the generic `edit` op, because only the domain call enforces
strictly-increasing breakpoints *and* checks the patch's separate axis-length
header. One axis serves all five slots, so a bad breakpoint reinterprets every
slot curve at once — silently, since the stored grids do not change.

Adding an op does **not** bump `BRIDGE_VERSION`: an older app never names it, and
a newer app against an older engine gets a clean `UNKNOWN_OP` rather than a field
read two different ways, which is what the version gate exists to prevent.

`simoscal.tune.catalog.TableInfo` also gained `is_axis`, so the editor can label
an axis and pre-validate it.

### Domain-owned tables are unreachable from the generic editor

Routing the rpm axis through its own op is not enough on its own: the generic
`edit` op could still reach the same table, and the five slot `PUT setpoint`
grids too. A `TableSpec` now carries an `owner` — the domain call that is the
only legitimate way to write it — and the whole switch-patch profile declares
one. Three things follow, and all three are engine-side:

- `apply_op()` refuses an owned table for **every** generic op, RESTORE included:
  a partial restore breaks the eight-row tiling exactly as a partial write does.
- `catalog()` omits owned tables, so the browser never offers a grid the engine
  will refuse. `table_detail` still reads them — reading was never the hazard.
- Every app build of a patched bin registers the switch-patch sanity gate, so the
  finished file is re-checked whether the session was created or recovered.

Without this, a one-cell edit to a slot grid — or a `12 → 13` write to the axis
length header — was accepted, then reported `verified=True` with a share path
(CR-20260813-01).

### What V8 still owes, and why it needs hardware

- Dragging on a real touchscreen: whether 12 breakpoints across a phone-width
  plot are separable by a fingertip, and whether the grab-at-touch-down rule
  feels right in practice.
- **No Compose screenshot tests.** The plan asks for light/dark screenshot tests;
  the pure state and coordinate math are covered instead. This is a known gap,
  carried over from V7's decision not to stand up a Compose test harness.
- The on-device parity pull: a boost-only diff on the real SC8S50 bin,
  hand-reviewed against a desktop `simoscal` build of the same edit.

## V0 verdict: provisional GO for implementation

On a `Pixel 6 / API 35 / arm64` emulator, the embedded Chaquopy engine produced a
parity report whose compared digest is **byte-for-byte identical** to the host
golden:

```
host digest:   9e6ee056b54dda3352b3049795c3e855e14b7810072e6652b63045d4d8e9719c
device digest: 9e6ee056b54dda3352b3049795c3e855e14b7810072e6652b63045d4d8e9719c
PARITY: MATCH — host and device agree on every compared field.
```

That means, across all 3,814 tables decoded, the single-cell edit
(`IP_PUT_SP` — Pressure up throttle setpoint, 1500 hPa → encoded
1499.9780270084689), both checksum verdicts, the psi→hPa floor (10 psi → 1705
hPa), and the `slot_curve()` boost edit, **the arm64 emulator computes the same
bytes as the desktop.** This is enough to continue implementation with
Chaquopy, but it does not close the plan's full V0 gate: physical-arm64 and
x86_64 parity runs remain required before v1 is declared runtime-complete.

Measurements (emulator, arm64):

| Metric                     | Value                                    |
| -------------------------- | ---------------------------------------- |
| Runtime                    | Python 3.13.9, numpy 1.26.2, Android 15  |
| Full-XDF parse (5.8 MB)    | ~1.9 s                                   |
| Decode all 3,814 tables    | ~0.17 s                                  |
| Edit + save + checksum     | ~0.21 s                                  |
| `slot_curve()` boost edit  | ~1.96 s                                  |
| APK size (arm64 + x86_64)  | 54 MB (an arm64-only split is ~30 MB)    |

All well within the "tolerable on your own phone in a garage" bar the plan set.
Cold-start, physical-arm64 parity, and x86_64 parity remain to be measured.

## The gate, in one sentence

`simoscal_v0_parity.run_parity()` is imported by *both* the host runner and the
on-device instrumentation test, each writes a report, and the reports' compared
sections must be identical — one sha256 either matches or it does not.

The report deliberately splits in two. `environment` and `timings` are
**informational**: the Python build, ABI, and wall-clock are *expected* to differ
between a Mac and a phone, and comparing them would fail the gate for the wrong
reason. Everything else is compared exactly.

What it exercises, and why each is the narrowest thing that could break:

| Leg           | What would break if a runtime differed                     |
| ------------- | ---------------------------------------------------------- |
| `parse`       | 5.8 MB XML → table model                                    |
| `enumerate`   | numpy decode math over all 3,814 tables                     |
| `edit_and_save` | inverse scaling, minimal-diff writer, checksum correction |
| `readback`    | what actually landed on disk, reopened from scratch         |
| `psi_floor`   | psi→hPa **floors, never rounds up** (boost safety property) |
| `boost_curve` | `slot_curve()` incl. the below-base-ceiling guard           |

A skipped leg is recorded **inside the compared section**, so a host golden that
ran the boost leg cannot silently "match" a device run that skipped it.

## Running the host half

```bash
cd Code
./.venv313/bin/python android/parity/run_host_parity.py --out android/parity/golden_host.json
```

Must run on **Python 3.13** (the version Chaquopy embeds); the runner refuses a
mismatch rather than producing a golden that answers the wrong question.

Verified host results (2026-07-23, Python 3.13.14, numpy 2.5.1):

- digest stable across repeated runs (byte-identical reports)
- `IP_PUT_SP` — Pressure up throttle setpoint: requested 1500 hPa → encoded
  1499.9780270084689, and read back off the saved file as exactly that
- `CAL_CRC` and `ECM3` both verify clean after correction
- 10.0 psi → **1705 hPa** floored, matching the documented `slot_curve` contract
- source bin sha256 unchanged by the run (immutable-source rule holds)
- comparator proven in both directions: a tampered field is caught and named

## Running the device half

```bash
adb push Code/xdf/SC8S50.V1.0.xdf /data/local/tmp/v0/
adb push Code/bin/5G0906259L__0002.bin /data/local/tmp/v0/
# optional, enables the boost leg:
adb push "BinToolz-main/definitions/S50 Switch Patch.29.33.V2.xdf" /data/local/tmp/v0/
adb push Tunes/TuningBasicsGuide/BinToolz-patched/CB_HSL_SP2933_5G0906259L_0002_BasicsGuide_R04.bin /data/local/tmp/v0/

./gradlew :engine:connectedAndroidTest
adb pull /sdcard/Android/data/com.simoscal.engine.test/files/v0_device_report.json

cd Code && ./.venv313/bin/python android/parity/run_host_parity.py --compare v0_device_report.json
```

Fixtures are **not committed** — the bin and XDFs are Sam's own and the repo
gitignores `*.bin`. Absent fixtures make the test *skip*, matching the repo-wide
convention for tests that touch the real bin/XDF.

## Build path (what actually worked)

Generate the `gradlew` wrapper once via **Android Studio** (open `Code/android`,
choose **Use Android Studio's SDK** at the prompt, let the sync finish, dismiss
any "update AGP / migrate Gradle" nudges). From then on the CLI works:

```bash
export JAVA_HOME=/opt/homebrew/opt/openjdk@17
export ANDROID_HOME="$HOME/Library/Android/sdk"
./gradlew :engine:connectedAndroidTest \
  -Pandroid.testInstrumentationRunnerArguments.fixtureDir=/data/local/tmp/v0
```

### The version pin that matters: pre-8.0 Gradle

**AGP 7.4.2 + Gradle 7.6.4 + compileSdk 33**, pinned in the build files. This is
deliberate, not stale. Chaquopy 17.0.0's own Python tasks read from the JNI-libs
and asset directories without declaring dependencies on the AGP tasks that write
there. **Gradle 8.0 turned that class of undeclared-dependency into a hard build
failure**; the overlaps are numerous and some are with `installPythonRequirements`'s
own *descendants* (a spurious directory overlap that no `dependsOn`/`mustRunAfter`
can fix without a cycle). Gradle **7.6.4** treats the same overlaps as deprecation
*warnings* and builds through them. Chaquopy 17 supports AGP 7.3–9.2, so 7.4.2 is
in range, and the *runtime under test* (Python 3.13 + numpy) is fixed by the
Chaquopy version, unaffected by the AGP/Gradle choice.

This was arrived at empirically: every Gradle 8.x attempt (9.3.0, 8.11.1) failed
the validation, and per-edge `dependsOn`/`mustRunAfter`/`doNotTrackState` fixes
either whack-a-moled or cycled. Public-release tooling is revisited at the Phase 2
gate (where a Kotlin kernel, if adopted, sidesteps Chaquopy's Gradle plugin
entirely).

### Gradle `connectedAndroidTest` orchestration flake

`./gradlew :engine:connectedAndroidTest` built and packaged both APKs correctly
but its *connected-run step* reported `tests=0` / "failing tests" — an
install/run orchestration flake, **not** a real test failure. Installing the two
built APKs and running the instrumentation directly passes cleanly:

```bash
ADB="$HOME/Library/Android/sdk/platform-tools/adb"
APK=engine/build/outputs/apk
"$ADB" install -r -g "$APK/debug/engine-debug.apk"
"$ADB" install -r -g "$APK/androidTest/debug/engine-debug-androidTest.apk"
"$ADB" shell am instrument -w -e fixtureDir /data/local/tmp/v0 \
  com.simoscal.engine.test/androidx.test.runner.AndroidJUnitRunner
# → OK (1 test); report at /sdcard/Android/data/com.simoscal.engine/files/
```

Then `parity/push_fixtures_and_compare.sh compare` pulls the report and diffs it
against the host golden. Running the test from Android Studio's green ▶ is the
GUI equivalent and also works. The flake is worth revisiting but did not block the
gate.

## Environment / toolchain

Installed on this machine while setting up: `openjdk@17`
(`/opt/homebrew/opt/openjdk@17`, keg-only — `~/.zshrc` was **not** modified),
`android-commandlinetools` and **Android Studio** (which adopted its own SDK at
`~/Library/Android/sdk` — platform 33 & 35, build-tools, platform-tools, emulator,
`system-images;android-35;google_apis;arm64-v8a`), `python@3.13`, and `gradle`.
An AVD named `v0_arm64` (Pixel 6 / API 35 / arm64) exists. `~/.zshrc` was not
modified; `JAVA_HOME` is set per-invocation.

## Ordering note — V1 came before V0, necessarily

The plan lists V1 (portable package boundary) as depending on V0. In practice the
dependency is the other way round: V0 cannot run at all until the engine imports
without matplotlib, because `simoscal/__init__.py` eagerly imported `.export`
(openpyxl) and `.plot` (matplotlib). So V1's decoupling was done first:

- `simoscal/__init__.py` now resolves the export/plot names lazily (PEP 562
  `__getattr__`), and raises an ImportError naming the missing extra rather than
  a bare "No module named matplotlib".
- `simoscal/tune/pipeline.py` imports `compare_tables` inside the one function
  that renders PNGs, so building/verifying/auditing a bin needs no matplotlib.
- `pyproject.toml` core deps are now **numpy-only**, with `plot`, `export`,
  `desktop`, and `dev` extras.

Verified: with `matplotlib` and `openpyxl` forced to fail on import, `import
simoscal` and `import simoscal.tune` both succeed, a real XDF/bin parses and
decodes, and checksums verify — with matplotlib never imported. Full suite: **554
passed**.

What remains of V1 for its own unit: installed-wheel packaging tests, and the
same decoupling for `simoscal.analysis` (`evidence.py` still imports matplotlib
at module scope).
