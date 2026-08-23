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
right. Nothing else in the checksum layer needed changing.

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

`probe_foreign.py` implements this as `discover_structure()`.

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

## 7. The A05 base XDF declares a wrong `BASEOFFSET`

`SCGa05_cal.xdf` declares `BASEOFFSET offset="0"`, but its addresses are
CAL-relative — 4,048 of them, spanning `0x96F` to `0x8F8C3`, all inside the
`0x9FC00` CAL block. Taken literally, every read lands near the start of the
file instead of in the calibration:

```
ID_PORT_SP x-axis, declared address 0x1336, 10 x u8, value = raw * 32 rpm

  at 0x1336              [0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
  at 0x220000 + 0x1336   [1344, 1376, 1504, 2016, 2240, 2496, 3008, 3200, 4000, 4512]
```

The second is an rpm breakpoint axis. The first is padding.

This is a defect in that XDF file, not a property of the car: the A05 *switch
patch* XDF, for the same bin, correctly declares `BASEOFFSET 0x220000`. Profile
resolution matches on name and shape and so is unaffected, but **any value read
through this XDF at its declared offset is meaningless**. A port must not treat
a declared `BASEOFFSET` as authoritative — cross-check it against the CAL block
offset discovered from the file.

## 8. Procedure for the next structure

1. Run `probe_foreign.py <bin> <xdf> [patch-xdf]`. Read the STRUCTURE DISCOVERY
   section.
2. Run it on `bin/5G0906259L__0002.bin` too. If the SC8S50 result is not
   rediscovered exactly, the search is broken — fix it before believing
   anything it says about the new bin.
3. Confirm the XDF's `BASEOFFSET` equals the discovered CAL file offset. If it
   does not, the XDF is wrong; check a known breakpoint axis both ways as in
   section 7 before deciding which to trust.
4. Only then write the profile's `StructureSpec`.

## 9. Still open

- **Declared `CAL_BLOCK_LENGTH`.** The probe reports how far the CAL CRC
  covers, which is not the same number. SC8S50 declares `0x7FC00` where its CRC
  covers to `0x7FA00` — `0x200` more. If that relationship holds, A05's is
  `0x9FC00`; two samples is not a rule, so confirm it independently rather than
  assuming the offset.
- **The ASW block's file offset** (`0x40000` on S50, `0x20000` on A05) is
  inferred from where the ECM3 addresses were found. It should be confirmed
  against the block's own header before being written into a profile.
