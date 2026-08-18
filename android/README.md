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
| Physical-arm64 parity verdict             | **PASS** — digest match on a Galaxy Tab A9+ (2026-08-15), re-proven against the arm64-only APK (2026-08-16) |
| x86_64 parity                             | **N/A — ABI dropped** (2026-08-16). Never proven, so no longer shipped |
| V7 Compose shell + Quick Edit flow        | Built; host-verifiable half green (see V7)    |
| V7 on-device legs (SAF, share, recovery)  | **Green** — full import → preflight → session → edit → build → export run on a Galaxy Tab A9+ (2026-08-15); process-death recovery exercised too. Rotation and low-storage still owed |
| V8 boost canvas + calibration editors     | Built; pure rules green (see V8)              |
| V8 on-device legs (drag, screenshots)     | Parity pull done (2026-08-15, see V8); fingertip drag and screenshot tests still owed |

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

### The look is the promo video's, from one palette

The app and `Docs/promo/`'s two cuts are the same product seen twice, so they are
painted from one list of colours rather than two that drift. `PromoPalette`
(`ui/Theme.kt`) is a transcription of `PALETTE` in `Docs/promo/config.py` — same
names, same eleven RGB triples — and `res/values/colors.xml` repeats the two the
platform needs before Compose exists. The video is built from the library's real
output and is the older of the two, which makes it the source: add a colour there
first.

What that buys, beyond looking of a piece:

- **The names are roles, and the roles are enforced by use.** `accent` is boost,
  heat, and the thing that changed — a staged proposal, an applied edit, a
  changed cell, the live slot curve. `accent_2` is verification and selection.
  `good`/`warn`/`danger` are a check that passed, something to read first, and a
  refusal. `PanelTone` is that vocabulary as a type, so a panel picks a meaning
  rather than a colour.
- **One scheme, always dark, no Material You.** `dynamicColorScheme` would repaint
  the app in the device wallpaper's hues, which is exactly wrong here: it would
  make "the engine refused" a different colour on every phone. The light scheme
  and the `darkTheme` parameter are gone with it — `QuickEditTheme` takes only
  its content.
- **Numbers are monospace, everywhere.** `PromoType.identifier` and
  `figureSmall` carry the video's Menlo-set figures across to grid cells, axis
  breakpoints, bin names, hashes, and the encoded-value receipt. A filename
  differing by one revision digit and a column of psi figures are both read
  glyph-by-glyph, which a proportional face makes harder for no gain.
- **Panels are flat and hairlined, not raised.** `Panel` replaces Material's
  `Card` throughout: `bg_alt` fill, a 1 dp `rule` border, no elevation. Every one
  of them is a readout — what the bin holds, what a gate found — and a raised
  surface implies a thing to pick up rather than a face to read.
- **The five slot colours are the video's `SLOT_STYLE`.** `BoostCanvas`, the slot
  chips, and the switchboard's column headings all draw slot *n* in the same hue
  the promo's slots beat draws it in, so "slot 4" means one thing across the app
  and the video both.

Two places deliberately did **not** move to the palette. The heatmap ramp
(`TableHeatmap.kt`) still runs deep blue → cyan → green → amber → red, because it
mirrors the ramp the library's own compare plots use — the ones the video itself
shows — and repainting it here would put the app and the report at odds. And the
launcher icon is generated, not hand-drawn: `Docs/promo/gen_app_icon.py` renders
it from the same palette into `res/mipmap-*/`.

The window background is set in `themes.xml` as well as in Compose. On a build
that starts a Python runtime, the gap between the launcher tap and the first
composed frame is long enough to see, and on the stock light parent it was a
white flash before a near-black app.

**How much of the repaint has been seen running (2026-08-17).** 158 unit tests
and the no-permissions gate are green and the APK builds, but those say nothing
about how it looks. On the `v0_arm64` emulator, the **import screen and the
passed-preflight verdict** were driven for real and screenshotted against the
promo stills: wordmark, hairline panels, tracked kickers, monospace filename and
hash, squared accent buttons, the green `good` verdict panel.

**Tables, Boost, Slots, and Build have not been seen rendered** — only compiled.
Reaching them needs an open session, and on an emulated arm64 device the engine's
XDF parse runs about four minutes per call (preflight, then again on session
open); the first attempt was killed when the emulator ran out of memory partway
through. Nothing in those four screens is new logic — they use the same `Panel`,
`Kicker`, and `PromoType` as the two that were verified — but the boost canvas's
five slot colours and the grid's cell/heatmap interplay are worth a look on the
tablet before this is called done. That is the same gap V8 already records as
"no Compose screenshot tests", and this repaint is the second unit to be stopped
by it.

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

Expect **158 unit tests passing** and a receipt at
`engine/build/reports/permissions/debug.txt`:

| Test class            | Cases |
| --------------------- | ----- |
| `BoostCurveTest`      | 18    |
| `BoostPlotTest`       | 7     |
| `BoostUiStateTest`    | 13    |
| `BridgeProtocolTest`  | 13    |
| `FormattingTest`      | 15    |
| `ImportNamingTest`    | 9     |
| `QuickEditStateTest`  | 24    |
| `SlotsUiStateTest`    | 12    |
| `TableHeatmapTest`    | 18    |
| `TablesUiStateTest`   | 24    |
| `VerifiedParamsTest`  | 5     |

Keep these current. The total is this document's stated pass criterion, so a
stale number cannot distinguish a complete run from a partial one. The figure has
now gone stale twice: 93 → 109 when the 2026-08-14 review fixes added cases
without updating it (CR-20260815-03), and 109 → 158 when V8/V9's own suites
landed the same way, caught while re-running the gate for the 2026-08-17
repaint. If you add a test, add it here.

The unit tests are deliberately JVM-only and cover the pure layers: the envelope
contract against the real `org.json`, every state gate, the import naming and
hashing rules, and the file-param key names. They need no device.

> **They do not cover the Kotlin → Python boundary itself.** The two halves agree
> on an envelope contract *and* a params contract; only the first was tested
> until `VerifiedParamsTest`, and the gap hid a defect that broke every
> file-naming op from V7 until 2026-08-15 (CR-20260815-01). A green JVM suite is
> not evidence that the engine can be reached.

`./gradlew :engine:assembleDebug` builds the whole APK (Compose + Chaquopy).
Measured 2026-08-16, both configurations built back-to-back from the same tree:
**60.8 MB** for arm64 + x86_64, **50.3 MB** for the arm64-only build that now
ships. Dropping the ABI is worth 10.5 MB — not just the 10.3 MB of `lib/x86_64`
`.so` files, because Chaquopy also ships per-ABI Python assets
(`stdlib-x86_64.imy`, `requirements-x86_64.imy`, `bootstrap-native`). An earlier
note here estimated an arm64-only split at "~30 MB"; that was a guess, and the
measurement above replaces it. V8 adds no dependency and did not move the
number. The `material-icons-extended` artifact cost 5.4 MB on its own for three
glyphs, so the navigation bar uses `material-icons-core` instead.

> **`lintDebug` is not a meaningful gate here.** AGP 7.4.2's lint bundles a
> Kotlin 1.7.1 UAST analyzer, and the project compiles with Kotlin 1.9.24, so
> lint emits `Module was compiled with an incompatible version of Kotlin` and its
> Kotlin *source* analysis does not run. It still checks Gradle, manifest, and
> resources (currently 18 warnings, all deliberate pins or targetSdk 33). Do not
> read a clean `lintDebug` as a statement about the Kotlin code.

### What V7 still owes, and why it needs hardware

None of these have a host-side substitute; they are listed rather than claimed:

- ~~the SAF picker~~ — **done 2026-08-15**: picked the real bin and both XDFs
  through DocumentsUI on a Galaxy Tab A9+ (`SM-X210`, Android 16, `arm64-v8a`),
  and preflight returned *"Ready to edit — recognised SC8S50 bin with valid
  checksums"* against the R14 patched bin, both embedded checksums clean;
- the share hand-off to SimosTools with the real bin;
- airplane-mode import → edit → export;
- process-death recovery during copy, edit, and build;
- rotation and low-storage behaviour;
- **tablet layout** — the shell renders as a phone-width column on a 1200 px
  tablet, leaving most of the screen empty. The v1 plan deferred tablet layout as
  a phone-first call; the actual target device is a tablet, and SimosTools runs
  on the same one, so the whole loop closes there.

That first item is the one worth reading twice: reaching it took a one-line fix
(CR-20260815-01), because *no* file-naming op had ever succeeded on any device.
Everything above the line in this README was true of code that could not be
reached from the UI.

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

### Every table says what it is, and every axis says what it measures

An XDF axis carries breakpoints and a unit string. That decodes a table and does
not begin to label one: "4000" across the top of a grid is engine speed on
`IP_PQ_CHA_MAX` and turbocharger air mass flow on
`IP_PUT_AMP_DIF_MAX_PRS_DIF_THR`, and the screen said neither. Three pieces close
that, all engine-side so the app and any report describe a table the same way:

- **The parser keeps the axis link.** A breakpoint axis is stored once and shared
  by every table that uses it, and only that standalone table carries the axis's
  own A2L symbol. `Axis.link_uniqueid` now holds the `<embedinfo linkobjid>` that
  points at it. Metadata only — it names a sibling table and changes no decode.
- **`simoscal.tune.quantities` maps symbol → English**, curated the way the
  profile is: every entry was checked against its decoded breakpoints before
  being written down, and an axis with no entry falls back to its symbol rather
  than to a guess. Inferring the quantity from the *unit* was rejected outright —
  it is right most of the time, and the times it is wrong put the wrong name over
  the wrong column. `test_quantities.py` fails if a profile addition brings an
  uncurated axis with it.
- **`TableInfo.signature`** is the one-line "what is this table": `hPa vs. Engine
  speed [rpm] and Manifold pressure setpoint [hPa]`. `units_description` spells a
  bare XDF `-` as "dimensionless", which is a statement where a lone dash reads
  as missing metadata.

The editor prints the signature under the title, the x-axis label over the column
headers, and the y-axis label in the corner beside the rows; the edit dialog adds
the breakpoints the cell sits on, because indices identify a cell and only the
breakpoints say what operating point it governs.

### Numbers are formatted for the table they are in, never in exponent form

`%.6g` put `4.40536e-13` on screen for a y-axis breakpoint of exactly 0 °C — the
XDF's scaling equation evaluated in floating point, so the residue is the
arithmetic's and not the calibration's — and `3.10010` for a cell quantized to
1/4096. An exponent on a calibration screen is never information: it is either
noise dressed as a number or a real value made unreadable.

`ValueFormat` (`Formatting.kt`) replaces it. One precision is chosen for a whole
set and applied to all of it — the cells share one, each axis gets its own —
starting at roughly four significant digits of the largest value present, which
is what puts that residue back as `0.00` and a `79.9891` breakpoint back as the
`80` the calibrator meant. It is then *raised* until every meaningfully different
value in the set formats differently, and that guard is what makes the rounding
safe: a changed cell is drawn above its old value, and two different numbers
rendering identically would turn a real edit into an invisible one. Differences
below one part in a billion are excluded from that test, or a 6000-rpm axis would
chase residue in its twelfth digit forever.

The numeric dialog also asks the IME for `KeyboardType.Decimal` rather than
`Number`. `Number` requests digits only, and every value the dialog edits is a
Double — on the Tab A9+ that meant a cell holding `3.100097`, or a `0.05` Offset,
could not be typed at all, and Set stayed disabled because `toDoubleOrNull` never
saw a parseable string.

`Decimal` gets the decimal point and stops there: it never sets
`TYPE_NUMBER_FLAG_SIGNED`, Android has no signed-decimal input type, and Compose
exposes none — so the Tab A9+ keypad renders no `-` key at all. Ignition `°CRK`,
a downward Offset, and a sub-zero temperature breakpoint are all ordinary values
here, and none of them could be entered. Hence the **± button** beside the field,
which flips the sign of the text (`String.withFlippedSign`) and depends on no IME
behaviour whatsoever. It operates on text rather than a parsed Double so it works
mid-type — press ± on an empty field, then key the digits. Verified on the tablet:
`3.10009765625` → ± → `-3.10009765625`, with Set still enabled.

The edit dialog does **not** round. It seeds with `displayExact`, the shortest
fixed-point text that reads back within `CHANGE_EPSILON` — the same threshold
that decides whether a cell counts as changed. Were it looser, opening a cell and
pressing Set while changing nothing would manufacture a diff out of a rounding
step. Verified on the tablet: a `3.10009765625` cell opened and Set unchanged
still reports "0 changed".

## V9 — the per-slot switchboard

The switch patch is one shared tune plus a per-slot decision about which features
are on: **24 tables differ between map slots**, and until now the app exposed
exactly one of them (`PUT setpoint`, via the boost editor). Sixteen of the other
23 are 1×1 scalars, and the question they answer is comparative — "which slots
have launch control enabled" — which the generic table editor answers badly: you
open five tables, and the one you skipped is the one that surprises you in the
car. The **Slots** destination puts all five slots on one screen, one row per
setting.

### Sixteen settings, twelve of them writable

`SLOT_SETTINGS` in the switch-patch profile is one registry feeding two
consumers: it generates the `TableSpec`s *and* it is what the screen renders, so
a setting cannot be toggleable in the app and unmapped in the profile.
`test_every_switchboard_setting_is_mapped_and_owned` asserts the generation.

Twelve are 0/1 flags — SL TC, OEM TC, LC, NLS, RAL, pops, and six flex-fuel
modifier enables. The other four are **read, described, and refused**, and shown
anyway rather than hidden, because a screen listing twelve would be claiming the
patch has twelve per-slot settings:

| Setting | Why it has no write path |
|---------------------------|----------------------------------------------------------------------|
| `RPM limiter` | Reads 0 in every slot of the as-patched bin, so what a non-zero value means — and whether 0 means "leave the OEM limiter alone" — is inferred, not established. Writing an uncharacterised override is how you get a rev limit you did not intend. |
| `Speed limiter` | Same: 0 everywhere, meaning of non-zero unestablished. |
| `Manual AFU` | Not a flag at all. A 0–1 fraction stored **/128**, so a "toggle" writes 128× what anyone meant. The XDF says it "only adjusts the value"; the patch's own logging category mentions "manual e content", so it is *probably* hand-set ethanol fraction — probably is not good enough for a fuel-composition input the engine trusts. |
| `Gauge settings (bitmask)` | No source we have says what any individual bit means, and a bitmask written as a whole number sets seven bits you did not choose. |

Tapping a row's name opens the description, the units, the caution, and — for
these four — the reason verbatim. `set_slot_flag()` refuses all four engine-side
regardless of what any UI does.

### Three refusals, not one

`switchpatch.set_slot_flag()` rejects an unknown key (a typo that wrote nothing
looks exactly like a flag that does not work), a non-flag or read-only setting,
and — the last line of defence — **a flag whose stored byte is neither 0 nor 1**.
These sixteen tables sit within a few bytes of each other, so a mis-bound
uniqueid lands on a neighbour holding something else; reading before writing is
what makes that a loud failure instead of a silent corruption. The tables are
`owner`-tagged like every other patch table, so the generic `edit` op cannot
reach them either.

### Toggles are not staged

Unlike the table and boost editors, there is no draft and no Apply. A flag has
two states and no shape to review, so an Apply step would gate the write on a
review of nothing. Each tap is one bridge call, one journal entry, one undo
point — which is the granularity a person actually wants to step back through.
Nothing flips optimistically: the reply carries the whole board and the grid
redraws from it, so a refused write never looked, even for a frame, like it
worked.

`refreshOpenViews()` re-reads the switchboard after undo/redo for the same
reason it re-reads the other editors. Caught on-device: before that, undoing a
toggle left the row still reading "on" — this screen's entire job is to say which
slots have a feature on, so being stale about that is being wrong about the only
thing it claims to know.

### What the switchboard does not cover

Seven per-slot tables remain unexposed: `Spark modifier` (16×16 °), `Lambda
modifier` (8×12), and five `Torque Request` maps (9×20 / 7×20 Nm). The two
modifiers are grids the generic editor could show if they were mapped; the torque
maps are held deliberately — 25 grids of 180 cells whose Type 1/2/3 semantics are
not established anywhere, and no revision in the lineage has touched them.
Offering a table this project cannot describe cuts against how the catalog is
built.

### What V8 still owes, and why it needs hardware

- Dragging on a real touchscreen: whether 12 breakpoints across a phone-width
  plot are separable by a fingertip, and whether the grab-at-touch-down rule
  feels right in practice.
- **No Compose screenshot tests.** The plan asks for light/dark screenshot tests;
  the pure state and coordinate math are covered instead. This is a known gap,
  carried over from V7's decision not to stand up a Compose test harness.
- ~~The on-device parity pull~~ — **done 2026-08-15**, see below.

### The on-device pull (2026-08-15)

The first bin this app ever produced, built on a Galaxy Tab A9+ from the R14
patched bin and exported through the share sheet, then verified on the desktop
against the app's own claims. **Not flashed** — this is a `TEST00` artifact.

The whole point is the *byte diff*, because it is the one thing the app cannot
grade itself on:

| Region       | Bytes | What it is                                                |
| ------------ | ----- | --------------------------------------------------------- |
| `0x27d71a`   | 192   | `slot5_put_setpoint` — the boost edit (8 × 12 int16)       |
| `0x200304`   | 4     | `CAL_CRC` stored value — the checksum correction           |
| everything else | 0  | byte-identical to the R14 source                          |

196 bytes in two contiguous runs, and nothing else in a 4 MB file. The edit is
map slot 5 flattened from 1705 hPa to **2050 hPa** (9.99 → 15.00 psi gauge),
tiled identically across all eight uncharacterized Y rows — the tiling invariant
holding through the domain call, on a phone. Slots 1–4 are untouched.

Re-checked on the exported file with desktop `simoscal`:

- `CAL_CRC` and `ECM3` both verify clean, neither stale;
- `ECM3`'s stored value correctly did **not** change — the edit at `0x27d71a`
  falls outside its covered range (`0x20DFAC`–`0x2101B0`), so an ECM3 delta here
  would have been the bug;
- `preflight`: `ok_to_edit=True`, `switch_patch_present=True`;
- `switch_patch_sanity`: `plausible=True`, 123 tables resolved, 123 decoded,
  0 decode errors.

Reaching this took three fixes found by running on hardware, all on the same
day: the bridge could not name a file to the engine at all (CR-20260815-01), the
build gate resolved a desktop-only BinToolz path so no patched bin could ever be
built on a device (CR-20260815-05), and an unreadable patch XDF was reported as
an absent patch, which cost three attempts to diagnose (CR-20260815-02). None of
the three was reachable from the host test suite.

**This is a different claim from the V0 parity gate below**, which ran
separately on the same tablet later that day. This run shows the app produces a
correct bin through its real workflow; the parity gate shows the engine computes
byte-identical results to the host across the payload's six legs. Both now hold
on physical hardware, but neither implies the other.

## V0 verdict: CLOSED — arm64 PASS (2026-08-15/16), x86_64 dropped rather than proven

### Physical arm64 — Galaxy Tab A9+ (`SM-X210`, Android 16, `arm64-v8a`)

```
host   digest: 9e6ee056b54dda3352b3049795c3e855e14b7810072e6652b63045d4d8e9719c
device digest: 9e6ee056b54dda3352b3049795c3e855e14b7810072e6652b63045d4d8e9719c
PARITY: MATCH — host and device agree on every compared field.
```

`diff_count: 0`, digests self-consistent on both sides, and **zero `SKIPPED`
occurrences anywhere in the report** — so every leg ran and was compared,
including the boost leg (its switch-patch fixtures were pushed). Same digest as
the host and the emulator, which makes three-way agreement: desktop, emulator,
and real hardware compute the same bytes.

The host golden was regenerated from current source immediately beforehand and
produced that same digest, so the day's three engine fixes (CR-20260815-01, -05,
-06 and the -02 message change) are demonstrably parity-neutral.

`parity/v0_device_report.json` in this repo is now the physical run, and its diff
against the emulator run it replaced is the design working as intended: the only
changed lines are `platform` (`Android-15` → `Android-16`) and the four
`timings`. Those are exactly the two sections the report holds outside the
compared digest. Everything compared is byte-identical between an emulator and a
real tablet.

Measured on the tablet, informational and deliberately outside the compared
section — a slow phone must not fail the gate:

| Metric                    | Emulator | Physical A9+ |
| ------------------------- | -------- | ------------ |
| Full-XDF parse (5.8 MB)   | 1.9 s    | **6.7 s**    |
| Decode all 3,814 tables   | 0.17 s   | **0.53 s**   |
| Edit + save + checksum    | 0.21 s   | **0.42 s**   |
| `slot_curve()` boost edit | 1.96 s   | **7.0 s**    |

Runtime: Python 3.13.9, numpy 1.26.2, `Android-16-aarch64-64bit-ELF`. The tablet
is roughly 3.5× slower than the emulator, which is the number to design waits
around: parse and boost edit are each ~7 s, long enough that preflight reads as
a hang without a spinner.

### Re-proven against the shipping APK (2026-08-16)

The APK measured above was the two-ABI build. Dropping x86_64 changed the
shipping artifact, so the verdict was re-taken rather than inherited: rebuilt
arm64-only, reinstalled both APKs on the same tablet, and re-ran the
instrumentation. Same digest `9e6ee056…`, `diff_count: 0`, `digests_self_consistent`
on both sides, and all seven report steps carried real content — `boost_curve`
included, which is the leg that goes `{"skipped": …}` when its fixtures are
missing. 109 unit tests and the no-permissions gate green, and
`unzip -l … | grep x86` returns nothing.

### x86_64: dropped, not proven

x86_64 parity was the last leg owed for V0, and it is now moot because the ABI is
gone from `abiFilters`. The reasoning, recorded here because the alternative was
half-built:

The leg cannot run on this machine. The Apple-silicon Android emulator ships
`qemu-system-aarch64` and `qemu-system-armel` and **no x86_64 backend** in either
SDK install (`~/Library/Android/sdk`, `/opt/homebrew/share/android-commandlinetools`),
so an x86_64 system image has nothing to boot on no matter which images are
installed. A GitHub Actions workflow on a Linux x86_64 runner was written and its
host half passed, then abandoned: it buys a proof for an architecture nobody here
runs, and pays a permanent second CI path for it. The target device is arm64.

So the honest close is to stop making the claim rather than to keep shipping an
unverified one. **Re-adding `x86_64` to `abiFilters` re-opens the parity question**
and must not be done without running the leg on a real x86_64 host.

### Arm64 emulator (2026-07-23)

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
bytes as the desktop.** This was the provisional GO for implementation; the
physical-arm64 leg above has since closed, and x86_64 was dropped rather than
proven — so this is now the earliest of three agreeing arm64 results, not a
partial gate.

Measurements (emulator, arm64):

| Metric                     | Value                                    |
| -------------------------- | ---------------------------------------- |
| Runtime                    | Python 3.13.9, numpy 1.26.2, Android 15  |
| Full-XDF parse (5.8 MB)    | ~1.9 s                                   |
| Decode all 3,814 tables    | ~0.17 s                                  |
| Edit + save + checksum     | ~0.21 s                                  |
| `slot_curve()` boost edit  | ~1.96 s                                  |
| APK size (arm64 + x86_64)  | 54 MB (V0-era, pre-Compose — see § V7 for current) |

All well within the "tolerable on your own phone in a garage" bar the plan set.
Cold-start remains to be measured; x86_64 parity no longer applies.

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

This is the sequence that actually worked on a physical tablet (2026-08-15).
`parity/push_fixtures_and_compare.sh` wraps the first and last steps.

```bash
cd Code/android/parity && ./push_fixtures_and_compare.sh push   # all four fixtures

cd Code/android
export JAVA_HOME=/opt/homebrew/opt/openjdk@17
export ANDROID_HOME="$HOME/Library/Android/sdk"
./gradlew :engine:assembleDebug :engine:assembleDebugAndroidTest
adb install -r -g engine/build/outputs/apk/debug/engine-debug.apk
adb install -r -g engine/build/outputs/apk/androidTest/debug/engine-debug-androidTest.apk

adb shell am instrument -w -e fixtureDir /data/local/tmp/v0 \
  com.simoscal.engine.test/androidx.test.runner.AndroidJUnitRunner   # → OK (2 tests)

cd parity && ./push_fixtures_and_compare.sh compare
```

Fixtures are **not committed** — the bin and XDFs are Sam's own and the repo
gitignores `*.bin`. Absent fixtures make the test *skip*, matching the repo-wide
convention for tests that touch the real bin/XDF. A skipped leg is recorded
inside the compared section, so a host golden that ran the boost leg cannot
silently "match" a device run that skipped it — check the report has zero
`SKIPPED` before believing a MATCH.

### Play Protect blocks the test APK on a stock device

**The first install of `com.simoscal.engine.test` will hang, not fail.** Google
Play Protect refuses it with an on-device dialog:

> **Unsafe app blocked** — `com.simoscal.engine.test`
> This app was built for an older version of Android and doesn't include the
> latest privacy protections.

`adb install` blocks on the verifier's verdict, which never arrives while the
dialog sits unanswered — so it looks like a hung install, with no error, until
whatever timeout kills it. The dialog can easily be on a screen nobody is
looking at.

The objection is `targetSdk 33`, which is pinned deliberately (see the AGP
7.4.2 / Gradle 7.6.4 note below). It bites the *test* APK and not the app APK
only because the app package is usually already installed, making its installs
*updates*; the block applies to new packages.

```bash
adb shell settings put global verifier_verify_adb_installs 0   # before the first install
adb shell settings delete global verifier_verify_adb_installs  # restore afterwards
```

Scoped to adb sideloads and nothing else. Only needed once per package: after
`com.simoscal.engine.test` exists, rebuilds are updates and install normally, so
put the setting back. **The V0 gate is therefore not runnable on a stock
consumer device without either this setting or a `targetSdk` bump** — worth
knowing for a gate whose entire purpose is physical hardware.

### `connectedAndroidTest` still reports `tests=0`

Reconfirmed on the A9+ on 2026-08-15: `./gradlew :engine:connectedDebugAndroidTest`
builds and installs both APKs, runs nothing, and fails the build. The JUnit XML
it leaves behind says so plainly:

```xml
<testsuite tests="0" failures="0" errors="0" skipped="0" ...>
```

Read the XML before believing the verdict in either direction — a red Gradle
build here is not a failing test, and it is certainly not a parity result. Use
`am instrument` (above) or Android Studio's green ▶.

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
