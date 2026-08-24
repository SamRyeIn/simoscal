# Porting to another file structure

How `simoscal` finds a calibration inside a Simos18 bin, what is genuinely
per-car about that, and how to work it out for a bin the library has never seen.

Written while porting to `SCGA05` (box code `3CN906259B`), the second structure
this library has met. Everything below was measured against real files, not
inferred from documentation.

---

## 1. The short version

A Simos18 CAL block has a fixed internal shape. Two checksum headers sit at
fixed CAL-relative offsets — the CAL CRC at `0x300`, the ECM3 monitor at `0x400`
— and both were found at exactly those offsets on both cars.

**What varies between cars is not the shape. It is where the CAL block sits in
the file, and what address the ECU maps it to.**

|                          | SC8S50 (`5G0906259L`) | SCGA05 (`3CN906259B`) |
|--------------------------|-----------------------|-----------------------|
| CAL block, file offset   | `0x200000`            | `0x220000`            |
| CAL base address         | `0xA0800000`          | `0xA0820000`          |
| CAL CRC header, CAL-rel  | `0x300`               | `0x300`               |
| ECM3 header, CAL-rel     | `0x400`               | `0x400`               |
| CAL CRC covers to        | `0x7FA00`             | `0x9FA00`             |
| ECM3 area addresses at   | file `0x040520`       | file `0x020540`       |
| ECM3 area count          | 1                     | 1                     |
| Full bin size            | `0x400000`            | `0x400000`            |

Both cars' checksums verify clean on their stock bins once those constants are
right. Nothing else in the checksum layer needed changing. Both are declared:
`SC8S50_STRUCTURE` and `SCGA05_STRUCTURE` in `simoscal/checksum.py`.

## 2. The one thing that made this findable

`verify_ecm3` seeds its 64-bit accumulator from a constant stored in the header
itself, at `header+8` and `header+12`:

```
01234567 89ABCDEF
```

That constant is identical on both cars. It is therefore a **searchable
signature**: every occurrence of those eight bytes is a candidate ECM3 header,
anywhere in the file, with no assumption about where the CAL block starts.

Each bin contains exactly two occurrences — one in the CAL block, one in the ASW
block — and the ASW one is rejected because there is no sane CAL CRC header
`0x100` bytes before it.

This is `simoscal.checksum.discover_structure()`. It returns a `StructureSpec`
or raises `StructureNotFound` — it never returns a guess:

```python
from simoscal.checksum import discover_structure, verify

spec = discover_structure(open("some.bin", "rb").read())
for report in verify(data, spec):
    print(report.message())
```

`probe_foreign.py` calls the same function rather than keeping its own copy.

## 3. Why a candidate is only accepted if it verifies

A byte pattern that looks like a header proves nothing. The search accepts a
candidate only when the ECM3 value **stored** at it equals the value
**recomputed** over the areas that header points at — a 64-bit exact match over
several kilobytes of calibration. That cannot be satisfied by coincidence.

Run the search on a known bin as a negative control before trusting it on an
unknown one. On the SC8S50 stock bin it must rediscover `CAL_FILE_OFFSET
0x200000`, `ECM3` at `0x200400`, base `0xA0800000`, and the area addresses at
file `0x040520` — which is exactly `ASW1_FILE_OFFSET + ECM3_ADDR_LOC` as the
library already declares them — and it must accept nothing else. It does.

## 4. The base address is read off the file, not guessed

The CAL CRC header's first area always starts at CAL offset 0, so its stored
start address *is* the CAL base address:

```
S50  area0 = 0xA0800000 .. 0xA08002FF   ->  base 0xA0800000
A05  area0 = 0xA0820000 .. 0xA08202FF   ->  base 0xA0820000
```

No arithmetic on the S50 value is involved. This matters because the wrong base
still produces plausible-looking offsets, as section 6 shows.

## 5. Where the ECM3 area addresses live

The header's area *count* is in the header; the area *addresses* are not. They
sit either inline in CAL (when `header+24` is non-zero) or in the ASW block. On
both cars they are in the ASW block, `0x20` bytes below that block's own ECM3
header:

```
S50  ASW ECM3 header at file 0x040540, addresses at file 0x040520
A05  ASW ECM3 header at file 0x020560, addresses at file 0x020540
```

Under an ASW block starting at `0x40000` (S50) and `0x20000` (A05), those are
ASW-relative `0x520` and `0x540` — the two values `checksum.py` already tries as
`ECM3_ADDR_LOC` and `ECM3_ADDR_LOC_EARLY`. The existing fallback happens to
cover both cars, but the ASW block's own file offset does differ and must become
a per-profile field rather than the module constant it is today.

## 6. Two corrections to earlier findings

An earlier characterisation pass reached two conclusions that are wrong. They
are recorded here because both are the kind of mistake the next port could
repeat.

**"A05's CAL CRC is one constant away — base `0x80800000` instead of
`0xA0800000`."** The measurement was real but it was taken on the wrong block.
There is a second CRC-headered block at file `0x200000` on the A05 bin whose
base genuinely is `0x80800000` and whose CRC genuinely verifies clean — it is
simply not the block the XDF addresses. The calibration lives at `0x220000`
under base `0xA0820000`. Three constants differ, not one, and searching for
"the constant that makes it verify" found a true statement about the wrong
region. Locate the block first; verify second.

**"ECM3 is genuinely relocated — an ASCII part number sits at the S50 offset."**
The observation is correct and the inference is not. Nothing moved *within* the
CAL block. The whole CAL block moved `0x20000` further into the file, so reading
at `0x200400` reads `0x20000` before A05's CAL block starts, which lands in a
region holding part-number strings. ECM3 is at CAL-relative `0x400` on both
cars.

## 7. Two address conventions, and holding a file to the one it uses

`SCGa05_cal.xdf` declares `BASEOFFSET offset="0"`, but its addresses are
CAL-relative — 4,048 of them, spanning `0x96F` to `0x8F8C3`, all inside the
`0x9FC00` CAL block. Taken literally against a full 4 MB bin, every read lands
near the start of the file instead of in the calibration:

```
ID_PORT_SP x-axis, declared address 0x1336, 10 x u8, value = raw * 32 rpm

  at 0x1336              [0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
  at 0x220000 + 0x1336   [1344, 1376, 1504, 2016, 2240, 2496, 3008, 3200, 4000, 4512]
```

The second is an rpm breakpoint axis. The first is padding.

The first reading of this was "that XDF is faulty". It is not. The file numbers
its tables from the start of the **calibration block** rather than the start of
the bin — the `_cal` in its name — which is a second legitimate convention, and
the one you get from any definition written against an extracted CAL block.
`SC8S50.V1.0.xdf` uses the other: it declares `0x200000` and is written against
a whole bin. Both files are internally consistent; what varies is which image
they were authored for.

Three signals tell the conventions apart, and they agree:

| Signal                             | Full-bin (`SC8S50.V1.0.xdf`) | CAL-relative (`SCGa05_cal.xdf`)    |
|------------------------------------|------------------------------|------------------------------------|
| declared `BASEOFFSET`              | `0x200000` = CAL file offset | `0`                                |
| address span vs `cal_block_length` | exceeds it                   | `0xad4..0x8f8c3`, inside `0x9FC00` |
| values at the declared offset      | real calibration             | padding                            |

Measured on A05: rebased by `0x220000`, 214 of 270 candidate breakpoint axes
read strictly monotonic against 3 at the declared base, and `C_PRS_IM_SP_MAX` —
Maximum requested intake-manifold pressure setpoint reads 2399.96 hPa against 0.

**The library never infers which convention a file uses.** It could — the middle
row of that table is a reliable structural tell — but a wrong guess here decides
where every write lands, so the convention is a *declaration* on the profile
(`Profile.xdf_addresses_cal_relative`) sitting beside that car's other per-car
facts, and `preflight` holds the file to it:

```
preflight(3CN906259B__0002_SCGA05.bin, SCGa05_cal.xdf)
  -> READY, profile_name=SCGA05, writable=True

preflight(3CN906259B__0002_SCGA05.bin, <same file, BASEOFFSET 0x200000>)
  -> BLOCKED, profile_name=SCGA05, advanced.profile_resolved=True
     "This XDF's tables match the SCGA05 profile, but it does not count
      addresses from where that profile expects."
```

Recognition and permission stay separate results, and both are reported. A file
declaring anything other than what the profile expects is refused rather than
accommodated — including one declaring `0x220000`, which is self-consistent for
a full bin and still not the file SCGA05 was authored against. That is what
stops the declaration becoming a licence to accept whatever arrives.

Two things follow from the convention and are derived, not separately declared:

- **The addressable region.** A CAL-relative XDF describes the CAL block in its
  `REGION` header too, so its declared region is in the same short coordinates
  and cannot bound reads into a full bin — `SCGa05_cal.xdf` declares
  `[0x0, 0x7d000)`, which does not even contain its own highest table at
  `0x8F8C3`. `CalFile.open` takes the region from the `StructureSpec` instead
  whenever a base offset is overridden: a CAL-relative file may address the CAL
  block and nothing else.
- **Every reopen.** The build pipeline reopens the saved bin to read tables back,
  audit bytes and render comparisons; each of those carries the base offset from
  the `CalFile` the tune already holds, so a rebased space stays rebased all the
  way through. `_open_shared_space` does the same, because a patch XDF sharing
  one buffer with the base XDF must share its address arithmetic.

Why the check earns its lines: `IP_PUT_SP` — Pressure up throttle setpoint is
declared at `0x1F054`, so an edit through this file at its declared offset writes
the boost grid to file `0x1F054` instead of `0x23F054`. That lands outside every
range the CAL checksums cover, so `build()` would correct the checksums, verify
them clean, and hand back a flashable bin with the boost table untouched and 48
bytes of unrelated flash overwritten. Rebased, the same edit produces exactly two
changed byte runs — the CAL CRC at `0x220304` and the 48-byte table at `0x23F054`
— and both checksums verify clean, which is what the acceptance test asserts.

## 8. Procedure for the next structure

1. Run `probe_foreign.py <bin> <xdf> [patch-xdf]`. Read the STRUCTURE DISCOVERY
   section.
2. Run it on `bin/5G0906259L__0002.bin` too. If the SC8S50 result is not
   rediscovered exactly, the search is broken — fix it before believing
   anything it says about the new bin.
3. Work out which address convention the XDF uses, by the three signals in
   section 7 — declared `BASEOFFSET`, address span against `cal_block_length`,
   and whether a known breakpoint axis reads as breakpoints or as padding. If it
   is CAL-relative, the profile declares `xdf_addresses_cal_relative=True`; if
   it is full-bin, it declares nothing and its `BASEOFFSET` must equal the
   discovered CAL file offset. Do this before writing any specs: preflight
   enforces the declaration once the profile exists, so getting it wrong
   surfaces as a `BLOCKED` verdict rather than as wrong bytes, but a map written
   against the wrong reading of the file is wasted either way.
4. Only then write the profile's `StructureSpec`. A declared spec is worth
   having even though discovery works: it lets a bin that does not match the
   profile the user selected be caught, instead of being silently accepted
   because it happens to be a valid bin of some other car.
5. Set it as the profile's `structure=`, and settle the profile's other two
   per-car facts at the same time (section 9): which tables carry
   `TAG_FLOAT_BUG`, and which — if any — stock values the SOP guidance may
   quote. Declaring none of the latter is the correct answer until someone has
   actually read them off this car's bin.
6. Add the profile to `PROFILES` in `simoscal/tune/profiles/__init__.py`. That
   is the whole registration: `BASE_PROFILES` — what preflight tries — is
   derived from it by "has a `structure`", so step 5 is what makes the new car
   recognisable and there is no second list to forget. Check the port with
   `preflight(new_bin, new_xdf)`: it should now name the profile and report
   `writable=True`, and `preflight(sc8s50_bin, sc8s50_xdf)` must still say
   `SC8S50` — two profiles both resolving against one file raises
   `AmbiguousProfileError` rather than picking one.

## 9. What the library does with this

`StructureSpec` carries the eight per-car numbers and is passed explicitly to
every checksum call — `verify`, `correct`, `verify_cal_crc`, `verify_ecm3`,
`stored_checksum_ranges`, `correction_patches` — and to `CalFile.open`. There is
no default and no module-level "current structure": SC8S50 is
`SC8S50_STRUCTURE`, one declared instance among several.

The structure also decides whether a profile can *identify* a bin.
`BASE_PROFILES` is every profile that declares one, and `preflight` tries each
in turn: `Verdict.profile_name` is whichever matched and `Verdict.writable`
follows from that match, where it used to be a hardcoded SC8S50 resolve. A
profile with `structure=None` — the switch patch — only adds tables to another
profile's space, so it is excluded by the rule rather than by a hand-kept list.

The loop tries every profile rather than stopping at the first success, because
two matches must be distinguishable from one. When two do match, preflight
raises `AmbiguousProfileError` instead of returning a verdict: no choice of file
fixes a registry shipping two maps for one calibration, and taking the first
would mean editing under one car's safety rules on a file that might be
another's. A file no profile matches is `INSPECT_ONLY`, and the refusal quotes
the XDF's `deftitle` (`SCGA0531_C_OEM.a2l` for A05) so it says what the file is,
not only what it is not — evidence for the reader, never an input to matching.

The structure is one of three per-car facts a `Profile` now carries; the other
two used to be globals, and porting had no way to override either:

- **`Profile.float_bug_symbols`** — the tables whose XDF-declared display
  maximum is an editor artifact rather than an ECU limit, so a write above it is
  legitimate and must go through a raw write. It is *derived* from the specs
  tagged `TAG_FLOAT_BUG`, so a port flags a table in exactly one place. This
  replaced `safety.FLOAT_BUG_SYMBOLS`, a module global that had already drifted:
  it named `C_PRS_IM_SP_LIM` — Offset to the pressure behind the air cleaner for
  the limitation of the manifold setpoint, which no SC8S50 spec tagged.
- **`Profile.stock_references`** — sentences describing what stock reads on this
  car, for the SOP guidance strings that compare the guide's instruction against
  it. A profile that declares none renders the guidance *without* the comparison
  clause. That silence is the point: telling an A05 owner what a `5G0906259L`
  reads is worse than saying nothing.

`CalFile` carries the first two of these — `float_bug_symbols` and
`stock_references` — as the writer-facing form of "which car is this?".
`float_bug_symbols` defaults to `None`, meaning *no profile was supplied*: reads
still work, and a physical-unit write raises `FloatBugPolicyUnset` rather than
skipping a guard it cannot evaluate. An empty `frozenset()` is a different and
perfectly valid answer — "this calibration flags nothing" — and it must be
stated, never inferred from silence.

`correct()` raises `ChecksumNotLocatable` when either checksum cannot be located
under the spec it was given. It used to return the data unchanged and raise
nothing, which meant the caller held an uncorrected bin with no sign of it — on
the one operation whose entire job is making a bin flash-ready.

A fourth per-car fact is **`Profile.xdf_addresses_cal_relative`** — whether this
car's XDF numbers its tables from the calibration block or from the whole bin
(section 7). It is a declaration the file is checked against, never inferred from
the file, because it decides where every write lands. `Profile.xdf_base_offset`
derives the override to hand `CalFile.open`, which is `None` for the full-bin
convention so that path is byte-for-byte what it was before this field existed.

A fifth arrived with the A05 map: **`Profile.unavailable`** —
logical names this car does not have, each with the reason. It exists because
leaving a name out and declaring it absent produce the same profile but not the
same knowledge: an omission cannot be told apart from an oversight, and the
`KeyError` it produces says nothing about whether anyone looked. A declared gap
raises `TableUnavailableError` carrying the reason, and the two causes are
worded differently because they have different fixes — *absent from the
calibration* (no definition file could supply it) versus *absent from this XDF*
(the data is in the bin, embedded in the map that uses it, with no standalone
table to bind). A05 has five of each.

## 9a. What the A05 map actually measured

Every "obviously universal" fact this port touched turned out to be a fact about
one XDF. All three below are checked by tests rather than asserted here.

| Fact | SC8S50 | SCGA05 |
|-------------------------------------------------|--------------------------|--------------------------|
| `IP_IGA_BAS_IVVT_VVL_PORT_L[STND][i][e]` shape   | (16, 16)                 | (16, 18)                 |
| `C_M_AIR_CYL_SP_MAX` scaling                     | identity — stores kg/stk behind an mg/stk label | `m = 1e6` — reads mg/stk correctly |
| `C_PRS_IM_SP_MAX` / `_LIM` scaling               | identity — stock is 24x the declared max | `m = 0.01` — stock is inside it |
| Tables tagged `TAG_FLOAT_BUG`                    | 4                        | **0**                    |
| Tables tagged `TAG_KG_PER_STROKE`                | 1                        | **0**                    |

The middle rows are the important ones, and they run *opposite* to intuition: it
is the tag that is dangerous to copy, not to omit. `TAG_KG_PER_STROKE` tells
`tune.limits.airmass_cap_mg()` to divide by a million before writing. On SC8S50
that converts an mg/stk figure into the kg/stk the store actually holds; carried
to A05, whose XDF already carries the factor, it would divide a value that is
already correct — the same millionfold error, in the other direction, produced by
copying the very tag that exists to prevent it. `TAG_FLOAT_BUG` is the same
shape of hazard: it *disables* a range guard, so an empty set is the safe answer
and it must be reached by measurement — does stock already sit outside the
declared range? — rather than by analogy.

Because A05's ceiling needs no conversion, its `airmass_setpoint_max` is left
generically editable. Pointing it at `airmass_cap_mg()` would be worse than
useless: that method refuses an untagged table, so the owner would leave the
ceiling with no write path at all.

## 10. Still open

- **A05 has no usable base definition file.** This is the one that blocks the
  port from meaning anything in practice. The `SCGA05` profile is written,
  registered, and resolves cleanly — but the only base XDF anyone has for this
  car is `SCGa05_cal.xdf`, whose `BASEOFFSET` defect (section 7) makes every
  read and write through it land in the wrong place, so preflight blocks the
  pairing. Nothing about the profile changes when a corrected file arrives: fix
  the `BASEOFFSET` to `0x220000`, or obtain a definition that already declares
  it, and the same profile goes `READY` with no code change. Until then A05 is
  mapped but not editable, and no A05 edit path has ever been exercised on a
  real read.
- **Declared `CAL_BLOCK_LENGTH`, still two samples.** `SCGA05_STRUCTURE`
  declares `0x9FC00` on the SC8S50 relationship — `0x200` past where the bin's
  own CAL CRC reaches (`0x9FA00`) — because it is used only as an upper bound
  for area range checks, where the looser of two candidate values costs nothing
  and the tighter one could reject a legitimate area. That is a defensible
  default, not a confirmation; the relationship is still unproven.
- **The ASW block's file offset** (`0x40000` on S50, `0x20000` on A05) is
  inferred from where the ECM3 addresses were found, and is now declared in both
  structures. It should still be confirmed against the block's own header.
  `SCGA05_STRUCTURE` leaves `ecm3_addr_locs` at the default `(0x520, 0x540)`
  rather than pinning A05's `0x540`, so the claim stays as narrow as the
  evidence: the fallback pair is what located it.
- **Whether A05 mirrors the `full-profile-coverage` specs.** That effort adds 93
  SC8S50 names. The A05 map covers the same 70-name vocabulary the SC8S50 map
  did at the time of the port, minus its ten declared gaps. Extending it is an
  open question, not a commitment.
