# `Code/android` — Quick Edit v1 Android engine (V0 feasibility gate)

Implements **V0** of `Docs/plans/2026-07-21-002-feat-simoscal-quickedit-v1-plan.md`:
prove the Python engine runs under Chaquopy with **byte-for-byte parity** against
host Python before any UI work exists. Nothing here flashes an ECU, and nothing
here does bin math in Kotlin.

## Status

| Piece                                     | State                                         |
| ----------------------------------------- | --------------------------------------------- |
| Parity payload (`simoscal_v0_parity.py`)  | Done, deterministic, verified on host         |
| Host runner + golden                      | Done (`parity/run_host_parity.py`)            |
| Engine decoupled from matplotlib/openpyxl | Done (see "Ordering note")                    |
| Gradle/Chaquopy project                   | Builds (AGP 7.4.2 / Gradle 7.6.4 — see below) |
| Arm64-emulator parity verdict             | **PASS** — digest match (2026-07-23)          |
| Physical-arm64 / x86_64 parity            | Pending — required to close V0                |

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
