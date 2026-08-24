"""Checksum verify/report for the Simos18 CAL block (U5).

Detects and reports that a written CAL block has **stale checksums** — a bin
whose calibration bytes were edited but whose embedded checksums no longer match.
Such a bin must be corrected (by this module or the flasher) before it is flashed;
flashing a stale-checksum CAL can leave the ECU rejecting the block.

Two checksums protect the Simos18 CAL block, both implemented here:

* **CAL CRC** — a 32-bit CRC (poly ``0x04C11DB7``, initial value 0, no input/
  output reflection, no final xor) over the CAL block, described by a header at CAL
  offset ``0x300``. In the stock bin it covers ``[0x0, 0x2FF] + [0x400, 0x7F9FF]``
  — the whole CAL block *except* its own checksum header — so essentially any
  calibration edit makes it stale.
* **ECM3 monitor** — a 64-bit running summation of 32-bit little-endian words
  over a small CAL area, checked continuously by the ECU. Its *area addresses*
  live in the ASW1 block (moved there in newer ECUs for protection), so ECM3 can
  only be verified from a **full bin** that contains ASW1; a CAL-only image
  degrades to a "cannot verify" report rather than crashing.

Scope (Decision, plan U5): **verify + report by default; never silently rewrite.**
An optional ``correct=True`` path writes corrected checksums using this same
reference, but callers must opt in.

Algorithm and header layout adapted from VW_Flash (``lib/checksum.py`` +
``lib/fastcrc.py``, © Brian Ledbetter, **BSD-2-Clause**) — the authoritative
Simos18 implementation, by the flasher's author. The CRC lookup table is
generated at import time rather than vendored; it reproduces VW_Flash's table
byte-for-byte, and the CRC is pinned by both a fixed regression vector and the
stored CRC of the real stock bin (see ``tests/test_checksum.py``).
"""

from __future__ import annotations

import struct
import warnings
from dataclasses import dataclass, replace
from typing import Optional, Union

__all__ = [
    "StaleChecksumWarning",
    "ChecksumReport",
    "crc32_simos",
    "verify_cal_crc",
    "verify_ecm3",
    "verify",
    "correct",
    "correction_patches",
    "ranges_overlap",
    "stored_checksum_ranges",
    "StructureSpec",
    "SC8S50_STRUCTURE",
    "SCGA05_STRUCTURE",
    "discover_structure",
    "verify_discovered",
    "StructureNotFound",
    "ChecksumNotLocatable",
]

# ---- Simos18 CAL layout, as data ---------------------------------------------- #
# These numbers are per-car. They used to be module constants, which made the
# library structurally single-car: a second calibration could not be supported
# without editing this file. They are now a value object that a caller supplies,
# and there is deliberately no default — "the SC8S50 one" being the fallback is
# the defect, not the fix.

ECM3_OFFSET_CACHED = 0x20000000  # cached-alias offset applied when uncached < base
_MAX_SANE_AREAS = 16             # more than this ⇒ a mis-located header; bail out

# The ECM3 monitor seeds its accumulator from this constant, stored in the header
# itself at +8/+12. It is identical across calibrations, which makes it the
# signature :func:`discover_structure` searches for.
ECM3_SEED_HI = 0x01234567
ECM3_SEED_LO = 0x89ABCDEF
_ECM3_SEED = struct.pack("<II", ECM3_SEED_HI, ECM3_SEED_LO)


class StructureNotFound(Exception):
    """No CAL block with a verifiable ECM3 header could be found in a bin."""


class ChecksumNotLocatable(Exception):
    """A checksum could not be located, so it cannot be corrected.

    Raised by :func:`correct`. Returning the data unchanged instead would be a
    silent no-op on the one operation whose entire job is to make a bin
    flash-ready — the caller would hold an uncorrected bin and no indication of
    it. A caller that legitimately tolerates an unverifiable bin should ask
    :func:`verify` and branch on ``can_verify``.
    """


@dataclass(frozen=True)
class StructureSpec:
    """Where the CAL block sits in one car's bin, and how it is addressed.

    The *shape* inside a CAL block is fixed across the calibrations seen so far —
    CAL CRC header at ``0x300``, ECM3 header at ``0x400``. What varies is where
    the block sits in the file and what address the ECU maps it to, which is why
    those two carry no default while the header offsets do.

    ``cal_block_length`` is an upper bound used for range checks. A declared spec
    may carry the official block length; one returned by :func:`discover_structure`
    carries how far that bin's own CAL CRC reaches, which is all a file can prove.
    """

    #: Human-readable label, for error messages. Not an identifier.
    name: str
    #: CAL block start, offset into the full bin.
    cal_file_offset: int
    #: CAL absolute ECU address — subtract it to get a CAL-relative offset.
    cal_base_address: int
    #: CAL block length; an upper bound for area range checks.
    cal_block_length: int
    #: CAL CRC header, CAL-relative: +0 initial, +4 stored crc, +8 area count, +12 addrs.
    cal_crc_header: int = 0x300
    #: ECM3 header, CAL-relative: +0 stored (64-bit), +8/+12 seed, +16 area count.
    ecm3_header: int = 0x400
    #: ASW block start, offset into the full bin — where ECM3 area addresses live.
    asw_file_offset: int = 0x40000
    #: Candidate ECM3 area-address locations, ASW-relative, tried in order.
    #: Late cars use 0x520, early cars 0x540.
    ecm3_addr_locs: tuple[int, ...] = (0x520, 0x540)
    #: Expected full-bin size.
    full_bin_size: int = 0x400000

    @property
    def cal_crc_file_offset(self) -> int:
        return self.cal_file_offset + self.cal_crc_header

    @property
    def ecm3_file_offset(self) -> int:
        return self.cal_file_offset + self.ecm3_header


#: The structure this library was originally written against — box code
#: ``5G0906259L``, Simos 18.1/18.6, SC8S50 file structure. One instance among
#: several, not a default: nothing in this module falls back to it.
SC8S50_STRUCTURE = StructureSpec(
    name="SC8S50",
    cal_file_offset=0x200000,
    cal_base_address=0xA0800000,
    cal_block_length=0x7FC00,
    asw_file_offset=0x40000,
)


#: Box code ``3CN906259B``, software ``SCGA05`` — the second structure this
#: library was ported to. Located by measurement, not by analogy: the whole CAL
#: block sits ``0x20000`` further into the file than SC8S50's and is mapped
#: ``0x20000`` higher in the address space, while the layout *inside* the block
#: is identical (CAL CRC at +0x300, ECM3 at +0x400).
#:
#: ``ecm3_addr_locs`` is left at the default pair: A05's area addresses live at
#: ASW-relative ``0x540``, which the default already tries second. Stating the
#: single value would be a narrower claim than the evidence supports — the
#: fallback is what proved it.
#:
#: ``cal_block_length`` is the declared block length, ``0x200`` past where this
#: bin's own CAL CRC reaches (``0x9FA00``) — the same relationship SC8S50 shows.
#: Two samples is not a rule; it is used only as an upper bound for area range
#: checks, where the looser of the two candidate values costs nothing and the
#: tighter one could reject a legitimate area.
SCGA05_STRUCTURE = StructureSpec(
    name="SCGA05",
    cal_file_offset=0x220000,
    cal_base_address=0xA0820000,
    cal_block_length=0x9FC00,
    asw_file_offset=0x20000,
)


# ---- CRC-32/MPEG-2 table (generated; matches VW_Flash fastcrc.crctab) --------- #
def _make_crc_table(poly: int = 0x04C11DB7) -> tuple[int, ...]:
    table = []
    for i in range(256):
        c = i << 24
        for _ in range(8):
            c = ((c << 1) ^ poly) if (c & 0x80000000) else (c << 1)
            c &= 0xFFFFFFFF
        table.append(c)
    return tuple(table)


_CRC_TABLE = _make_crc_table()


def crc32_simos(data: Union[bytes, bytearray, memoryview]) -> int:
    """The Simos CAL block CRC of ``data``.

    A 32-bit CRC with poly ``0x04C11DB7``, initial value ``0``, no input/output
    reflection, and no final xor (per VW_Flash's ``fastcrc``). Note the zero
    initial value distinguishes it from canonical CRC-32/MPEG-2 (init all-ones).
    """
    crc = 0
    for byte in data:
        crc = ((crc << 8) & 0xFFFFFF00) ^ _CRC_TABLE[((crc >> 24) & 0xFF) ^ byte]
    return crc & 0xFFFFFFFF


# ---- report ------------------------------------------------------------------- #
class StaleChecksumWarning(UserWarning):
    """A saved bin has a stale embedded checksum (edited but not corrected)."""


@dataclass(frozen=True)
class ChecksumReport:
    """The verdict for one embedded checksum.

    ``covered`` is the list of ``(start, end)`` **half-open, full-bin** byte
    ranges the checksum protects (so it can be intersected with
    :attr:`CalFile.edited_ranges`). ``stored``/``computed`` are the embedded and
    freshly-computed values (``None`` when it could not be verified).
    """

    name: str
    can_verify: bool
    is_stale: bool
    stored: Optional[int] = None
    computed: Optional[int] = None
    covered: tuple[tuple[int, int], ...] = ()
    detail: str = ""

    def message(self) -> str:
        if not self.can_verify:
            return f"{self.name}: cannot verify — {self.detail}"
        state = "STALE" if self.is_stale else "valid"
        return (
            f"{self.name}: {state} (stored={self.stored:#x} "
            f"computed={self.computed:#x})"
        )


def _require_full_bin(data: Union[bytes, bytearray], spec: StructureSpec) -> Optional[str]:
    """Return an explanatory string if ``data`` is not a usable full bin, else None."""
    need = spec.cal_file_offset + spec.cal_block_length
    if len(data) < need:
        return (
            f"bin is {len(data):#x} bytes; need at least {need:#x} to reach the "
            f"{spec.name} CAL block"
        )
    return None


def _u32(buf, off: int) -> int:
    return struct.unpack_from("<I", buf, off)[0]


# ---- CAL CRC ------------------------------------------------------------------ #
def _cal_crc_areas(data, spec: StructureSpec) -> Optional[list[tuple[int, int]]]:
    """CAL-relative inclusive ``[start, end]`` areas the CRC covers, or None."""
    base = spec.cal_crc_file_offset
    area_count = data[base + 8]
    if area_count == 0 or area_count > _MAX_SANE_AREAS:
        return None
    areas: list[tuple[int, int]] = []
    for i in range(area_count):
        start = _u32(data, base + 12 + (2 * i) * 4) - spec.cal_base_address
        end = _u32(data, base + 12 + (2 * i + 1) * 4) - spec.cal_base_address
        if not (0 <= start <= end < spec.cal_block_length):
            return None
        areas.append((start, end))
    return areas


def verify_cal_crc(
    data: Union[bytes, bytearray],
    spec: StructureSpec,
    *,
    correct: bool = False,
) -> tuple[ChecksumReport, Union[bytes, bytearray]]:
    """Verify (optionally correct) the CAL block CRC over a full bin.

    Returns ``(report, data)``. With ``correct=True`` and a stale CRC, ``data``
    is a new ``bytearray`` with the corrected CRC written; otherwise ``data`` is
    returned unchanged.
    """
    why = _require_full_bin(data, spec)
    if why is not None:
        return ChecksumReport("CAL_CRC", can_verify=False, is_stale=False, detail=why), data
    hdr = spec.cal_crc_file_offset
    areas = _cal_crc_areas(data, spec)
    if areas is None:
        return (
            ChecksumReport(
                "CAL_CRC", can_verify=False, is_stale=False,
                detail=(
                    f"CAL CRC header at {spec.cal_crc_header:#x} is not a sane area "
                    f"table under the {spec.name} structure"
                ),
            ),
            data,
        )
    stored = _u32(data, hdr + 4)
    buf = bytearray()
    covered: list[tuple[int, int]] = []
    for start, end in areas:
        f_start = spec.cal_file_offset + start
        f_end = spec.cal_file_offset + end + 1  # half-open
        buf += data[f_start:f_end]
        covered.append((f_start, f_end))
    computed = crc32_simos(buf)
    is_stale = computed != stored
    report = ChecksumReport(
        "CAL_CRC", can_verify=True, is_stale=is_stale,
        stored=stored, computed=computed, covered=tuple(covered),
    )
    if correct and is_stale:
        data = bytearray(data)
        struct.pack_into("<I", data, hdr + 4, computed)
    return report, data


# ---- ECM3 monitor ------------------------------------------------------------- #
def _ecm3_areas(data, spec: StructureSpec) -> tuple[Optional[list[tuple[int, int]]], str]:
    """CAL-relative inclusive ``[start, end]`` areas ECM3 sums, plus a detail note.

    The area *addresses* are read from CAL if present there, otherwise from the
    ASW block at each of the spec's candidate locations in turn. Returns
    ``(areas, note)`` — ``areas`` is None when the addresses cannot be resolved.
    """
    ecm3 = spec.ecm3_file_offset
    area_count = _u32(data, ecm3 + 16)
    if area_count == 0 or area_count > _MAX_SANE_AREAS:
        return None, f"ECM3 area count {area_count} is out of range"

    # Newer CALs hold the addresses in the ASW block; older ones inline them in CAL.
    cal_inline = _u32(data, ecm3 + 24)

    def resolve(src, addr_loc: int) -> Optional[list[tuple[int, int]]]:
        out: list[tuple[int, int]] = []
        for i in range(area_count):
            pair = []
            for k in range(2):
                a = _u32(src, addr_loc + (2 * i + k) * 4)
                off = a - spec.cal_base_address
                if off < 0:
                    off = a + ECM3_OFFSET_CACHED - spec.cal_base_address
                if not (0 <= off <= spec.cal_block_length):
                    return None
                pair.append(off)
            if pair[0] > pair[1]:
                return None
            out.append((pair[0], pair[1]))
        return out

    if cal_inline > 0:
        areas = resolve(data, ecm3 + 24)
        if areas is not None:
            return areas, "addresses inline in CAL"
        return None, "inline CAL ECM3 addresses did not resolve"

    widest = max(spec.ecm3_addr_locs)
    if len(data) < spec.asw_file_offset + widest + area_count * 8:
        return None, "bin has no ASW block — ECM3 addresses unavailable (CAL-only image?)"
    for loc in spec.ecm3_addr_locs:
        areas = resolve(data, spec.asw_file_offset + loc)
        if areas is not None:
            return areas, f"addresses from ASW block at {loc:#x}"
    return None, "ECM3 addresses in the ASW block did not resolve"


def _ecm3_stored_location(data, spec: StructureSpec) -> int:
    """CAL-relative offset of the ECM3 stored value (moved +56 on old-school CALs)."""
    ecm3 = spec.ecm3_header
    if data[spec.cal_file_offset + ecm3 + 56] > 0:
        return ecm3 + 56
    return ecm3


def stored_checksum_ranges(
    data: Union[bytes, bytearray],
    spec: StructureSpec,
) -> list[tuple[str, int, int]]:
    """Where each checksum's *stored value* lives: ``(name, offset, length)``.

    Full-bin offsets. A byte-level diff of two revisions always finds these
    changed — they are computed over the calibration, so any real edit moves
    them — and a consumer needs to say so explicitly rather than treat them as
    a mystery. Their *correctness* is a separate question, answered by
    :func:`verify`.
    """
    return [
        ("CAL_CRC", spec.cal_crc_file_offset + 4, 4),
        ("ECM3", spec.cal_file_offset + _ecm3_stored_location(data, spec), 8),
    ]


def verify_ecm3(
    data: Union[bytes, bytearray],
    spec: StructureSpec,
    *,
    correct: bool = False,
) -> tuple[ChecksumReport, Union[bytes, bytearray]]:
    """Verify (optionally correct) the 64-bit ECM3 monitor summation."""
    why = _require_full_bin(data, spec)
    if why is not None:
        return ChecksumReport("ECM3", can_verify=False, is_stale=False, detail=why), data
    ecm3 = spec.ecm3_file_offset
    areas, note = _ecm3_areas(data, spec)
    if areas is None:
        return ChecksumReport("ECM3", can_verify=False, is_stale=False, detail=note), data

    # 64-bit accumulator seeded from the header's initial hi/lo words.
    acc = (_u32(data, ecm3 + 8) << 32) + _u32(data, ecm3 + 12)
    covered: list[tuple[int, int]] = []
    for start, end in areas:
        f_start = spec.cal_file_offset + start
        f_end = spec.cal_file_offset + end  # ECM3 sums words in [start, end), step 4
        for j in range(f_start, f_end, 4):
            acc += _u32(data, j)
        covered.append((f_start, f_end))
    acc &= 0xFFFFFFFFFFFFFFFF

    stored_rel = _ecm3_stored_location(data, spec)
    sloc = spec.cal_file_offset + stored_rel
    stored = (_u32(data, sloc) << 32) + _u32(data, sloc + 4)
    is_stale = acc != stored
    report = ChecksumReport(
        "ECM3", can_verify=True, is_stale=is_stale,
        stored=stored, computed=acc, covered=tuple(covered),
        detail=note,
    )
    if correct and is_stale:
        data = bytearray(data)
        struct.pack_into("<I", data, sloc, acc >> 32)
        struct.pack_into("<I", data, sloc + 4, acc & 0xFFFFFFFF)
    return report, data


# ---- structure discovery ------------------------------------------------------ #
def discover_structure(
    data: Union[bytes, bytearray],
    *,
    name: str = "discovered",
) -> StructureSpec:
    """Work out a bin's CAL layout from the bin itself. Raises if it cannot.

    Candidates come from the ECM3 accumulator seed, which is a fixed constant
    stored in the header at +8/+12 — so every occurrence is a candidate header,
    with no assumption about where the CAL block starts. A candidate is accepted
    only when the ECM3 value **stored** at it equals the value **recomputed**
    over the areas it points at: a 64-bit exact match over kilobytes of
    calibration, which a merely plausible-looking byte pattern will not satisfy.

    The CAL base address is read off the file rather than guessed — the CAL CRC
    header's first area always starts at CAL offset 0, so its stored start
    address *is* the base address.

    Run this against a known bin as a control before trusting it on an unknown
    one: on an SC8S50 image it must reproduce :data:`SC8S50_STRUCTURE`'s offsets
    and accept nothing else.
    """
    data = bytes(data)
    candidates: list[int] = []
    i = data.find(_ECM3_SEED)
    while i != -1:
        candidates.append(i - 8)
        i = data.find(_ECM3_SEED, i + 1)

    rejected: list[str] = []
    for hdr in candidates:
        cal_off = hdr - 0x400
        if cal_off < 0:
            rejected.append(f"{hdr:#08x}: implies a negative CAL offset")
            continue
        crc_hdr = cal_off + 0x300
        if crc_hdr + 12 > len(data):
            rejected.append(f"{hdr:#08x}: no room for a CAL CRC header")
            continue
        count = _u32(data, crc_hdr + 8)
        if not 1 <= count <= _MAX_SANE_AREAS:
            rejected.append(f"{hdr:#08x}: CAL CRC area count {count} out of range")
            continue
        if crc_hdr + 12 + count * 8 > len(data):
            rejected.append(f"{hdr:#08x}: CAL CRC area table runs past the end of the bin")
            continue
        addrs = [_u32(data, crc_hdr + 12 + k * 4) for k in range(2 * count)]
        base = addrs[0]
        spans = [(addrs[2 * i] - base, addrs[2 * i + 1] - base) for i in range(count)]
        if any(not 0 <= s <= e for s, e in spans):
            rejected.append(f"{hdr:#08x}: CAL CRC areas do not resolve under {base:#010x}")
            continue

        candidate = StructureSpec(
            name=name,
            cal_file_offset=cal_off,
            cal_base_address=base,
            cal_block_length=spans[-1][1] + 1,
            asw_file_offset=0,
        )
        # The ASW block's own ECM3 header sits just above the area addresses, so
        # every other seed hit supplies a candidate location. Arithmetic decides.
        for other in candidates:
            if other == hdr:
                continue
            for delta in (0x20, 0x40):
                if other - delta < 0:
                    continue
                # The ASW block's own base is not recoverable from the file, so a
                # discovered spec states the address location as a file offset and
                # leaves the block base at 0 rather than inventing one.
                probe = replace(candidate, asw_file_offset=0,
                                ecm3_addr_locs=(other - delta,))
                report, _ = verify_ecm3(data, probe)
                if report.can_verify and not report.is_stale:
                    return probe
        if _u32(data, hdr + 24) > 0:
            report, _ = verify_ecm3(data, candidate)
            if report.can_verify and not report.is_stale:
                return candidate
        rejected.append(f"{hdr:#08x}: ECM3 did not verify at any address location")

    detail = "; ".join(rejected) if rejected else "no ECM3 seed found in the bin"
    raise StructureNotFound(
        f"no CAL block with a verifiable ECM3 header in this {len(data):#x}-byte "
        f"bin ({detail})"
    )


# ---- combined ----------------------------------------------------------------- #
def verify(
    data: Union[bytes, bytearray],
    spec: StructureSpec,
) -> list[ChecksumReport]:
    """Verify both CAL checksums, no correction. Order: CAL CRC, then ECM3."""
    crc_report, _ = verify_cal_crc(data, spec)
    ecm3_report, _ = verify_ecm3(data, spec)
    return [crc_report, ecm3_report]


def verify_discovered(data: Union[bytes, bytearray]) -> list[ChecksumReport]:
    """Verify both checksums against the structure discovered from ``data`` itself.

    For callers that must *report* on a bin they cannot identify rather than
    refuse it — patch application and preflight classification both have to
    produce a verdict for an unrecognised image. When the structure cannot be
    found this degrades to cannot-verify reports carrying the reason, which is
    the same answer those callers gave before the layout became per-car.

    This is not a back door to an ambient default: it discovers or it says it
    could not. A caller that is about to *write* a bin uses :func:`correct`,
    which raises.
    """
    try:
        spec = discover_structure(data)
    except StructureNotFound as exc:
        return [
            ChecksumReport(name, can_verify=False, is_stale=False, detail=str(exc))
            for name in ("CAL_CRC", "ECM3")
        ]
    return verify(data, spec)


def correct(
    data: Union[bytes, bytearray],
    spec: StructureSpec,
) -> tuple[bytearray, list[ChecksumReport]]:
    """Return a corrected copy of ``data`` plus the *pre-correction* reports.

    ECM3 is corrected first because its stored value lives inside the region the
    CAL CRC covers, so the CRC must be recomputed over the already-corrected ECM3
    bytes. Returns ``(corrected_bytes, reports)`` where ``reports`` are the
    verdicts *before* correction (so callers can see what was stale).

    Raises :class:`ChecksumNotLocatable` if either checksum cannot be located
    under ``spec``. Correcting is how a bin is made flash-ready, so returning
    unchanged bytes would hand the caller an uncorrected bin with no sign of it.
    """
    reports = verify(data, spec)
    unlocatable = [r for r in reports if not r.can_verify]
    if unlocatable:
        detail = "; ".join(f"{r.name}: {r.detail}" for r in unlocatable)
        raise ChecksumNotLocatable(
            f"cannot correct this bin under the {spec.name} structure — {detail}"
        )
    _, data = verify_ecm3(data, spec, correct=True)
    _, data = verify_cal_crc(data, spec, correct=True)
    return bytearray(data), reports


def correction_patches(
    data: Union[bytes, bytearray],
    spec: StructureSpec,
) -> list[tuple[int, bytes]]:
    """The minimal ``(full_bin_offset, new_bytes)`` patches that make ``data`` valid.

    Only the stored-checksum bytes of any stale checksum are returned — a few
    bytes total — so a caller can apply them in place without touching the rest
    of the buffer. ECM3 is patched before the CRC (its stored value is inside the
    CRC's coverage), and the CRC patch is computed over the ECM3-corrected buffer.
    Returns an empty list when both checksums are already valid or unverifiable.
    """
    patches: list[tuple[int, bytes]] = []
    work = bytearray(data)

    ecm3_report, corrected = verify_ecm3(work, spec, correct=True)
    if ecm3_report.can_verify and ecm3_report.is_stale:
        sloc = spec.cal_file_offset + _ecm3_stored_location(work, spec)
        patches.append((sloc, bytes(corrected[sloc : sloc + 8])))
        work = bytearray(corrected)

    crc_report, corrected = verify_cal_crc(work, spec, correct=True)
    if crc_report.can_verify and crc_report.is_stale:
        cloc = spec.cal_crc_file_offset + 4
        patches.append((cloc, bytes(corrected[cloc : cloc + 4])))

    return patches


# ---- overlap helper ----------------------------------------------------------- #
def ranges_overlap(
    ranges: list[tuple[int, int]],
    covered: tuple[tuple[int, int], ...],
) -> bool:
    """Whether any ``(offset, length)`` edit intersects any covered ``(start, end)``.

    ``ranges`` uses ``(offset, length)`` (as :attr:`CalFile.edited_ranges` does);
    ``covered`` uses half-open ``(start, end)`` (as :class:`ChecksumReport` does).
    """
    for off, length in ranges:
        e_start, e_end = off, off + length
        for c_start, c_end in covered:
            if e_start < c_end and c_start < e_end:
                return True
    return False
