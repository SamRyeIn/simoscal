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
from dataclasses import dataclass
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
    "CAL_FILE_OFFSET",
    "CAL_BASE_ADDRESS",
    "CAL_BLOCK_LENGTH",
]

# ---- Simos18 CAL layout constants (from VW_Flash s18_flash_info) -------------- #
# The CAL block within a full 4 MB bin.
CAL_FILE_OFFSET = 0x200000      # CAL block start, offset into the full bin
CAL_BASE_ADDRESS = 0xA0800000   # CAL absolute ECU address (subtract to get CAL-rel)
CAL_BLOCK_LENGTH = 0x7FC00      # CAL block length
FULL_BIN_SIZE = 0x400000        # expected full-bin size (4 MB)

# CAL CRC: header at CAL-relative 0x300.
CAL_CRC_HEADER = 0x300          # +0 initial, +4 stored crc, +8 area count, +12 addrs

# ECM3 monitor.
ECM3_HEADER = 0x400             # CAL-relative; +8/+12 initial, +16 area count
ASW1_FILE_OFFSET = 0x40000      # ASW1 block start, offset into the full bin
ECM3_ADDR_LOC = 0x520           # ECM3 area addresses, offset into ASW1 (late cars)
ECM3_ADDR_LOC_EARLY = 0x540     # ...offset into ASW1 (early cars)
ECM3_OFFSET_CACHED = 0x20000000  # cached-alias offset applied when uncached < base
_MAX_SANE_AREAS = 16            # more than this ⇒ a mis-located header; bail out


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


def _require_full_bin(data: Union[bytes, bytearray]) -> Optional[str]:
    """Return an explanatory string if ``data`` is not a usable full bin, else None."""
    if len(data) < CAL_FILE_OFFSET + CAL_BLOCK_LENGTH:
        return (
            f"bin is {len(data):#x} bytes; need at least "
            f"{CAL_FILE_OFFSET + CAL_BLOCK_LENGTH:#x} to reach the CAL block"
        )
    return None


def _u32(buf, off: int) -> int:
    return struct.unpack_from("<I", buf, off)[0]


# ---- CAL CRC ------------------------------------------------------------------ #
def _cal_crc_areas(data) -> Optional[list[tuple[int, int]]]:
    """CAL-relative inclusive ``[start, end]`` areas the CRC covers, or None."""
    base = CAL_FILE_OFFSET + CAL_CRC_HEADER
    area_count = data[base + 8]
    if area_count == 0 or area_count > _MAX_SANE_AREAS:
        return None
    areas: list[tuple[int, int]] = []
    for i in range(area_count):
        start = _u32(data, base + 12 + (2 * i) * 4) - CAL_BASE_ADDRESS
        end = _u32(data, base + 12 + (2 * i + 1) * 4) - CAL_BASE_ADDRESS
        if not (0 <= start <= end < CAL_BLOCK_LENGTH):
            return None
        areas.append((start, end))
    return areas


def verify_cal_crc(
    data: Union[bytes, bytearray],
    *,
    correct: bool = False,
) -> tuple[ChecksumReport, Union[bytes, bytearray]]:
    """Verify (optionally correct) the CAL block CRC over a full bin.

    Returns ``(report, data)``. With ``correct=True`` and a stale CRC, ``data``
    is a new ``bytearray`` with the corrected CRC written; otherwise ``data`` is
    returned unchanged.
    """
    why = _require_full_bin(data)
    if why is not None:
        return ChecksumReport("CAL_CRC", can_verify=False, is_stale=False, detail=why), data
    hdr = CAL_FILE_OFFSET + CAL_CRC_HEADER
    areas = _cal_crc_areas(data)
    if areas is None:
        return (
            ChecksumReport(
                "CAL_CRC", can_verify=False, is_stale=False,
                detail="CAL CRC header at 0x300 is not a sane area table",
            ),
            data,
        )
    stored = _u32(data, hdr + 4)
    buf = bytearray()
    covered: list[tuple[int, int]] = []
    for start, end in areas:
        f_start = CAL_FILE_OFFSET + start
        f_end = CAL_FILE_OFFSET + end + 1  # half-open
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
def _ecm3_areas(data) -> tuple[Optional[list[tuple[int, int]]], str]:
    """CAL-relative inclusive ``[start, end]`` areas ECM3 sums, plus a detail note.

    The area *addresses* are read from CAL if present there, otherwise from ASW1
    (late offset, falling back to the early offset). Returns ``(areas, note)`` —
    ``areas`` is None when the addresses cannot be resolved.
    """
    ecm3 = CAL_FILE_OFFSET + ECM3_HEADER
    area_count = _u32(data, ecm3 + 16)
    if area_count == 0 or area_count > _MAX_SANE_AREAS:
        return None, f"ECM3 area count {area_count} is out of range"

    # Newer CALs hold the addresses in ASW1; older ones inline them in CAL.
    cal_inline = _u32(data, ecm3 + 24)

    def resolve(src, addr_loc: int) -> Optional[list[tuple[int, int]]]:
        out: list[tuple[int, int]] = []
        for i in range(area_count):
            pair = []
            for k in range(2):
                a = _u32(src, addr_loc + (2 * i + k) * 4)
                off = a - CAL_BASE_ADDRESS
                if off < 0:
                    off = a + ECM3_OFFSET_CACHED - CAL_BASE_ADDRESS
                if not (0 <= off <= CAL_BLOCK_LENGTH):
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

    if len(data) < ASW1_FILE_OFFSET + ECM3_ADDR_LOC_EARLY + area_count * 8:
        return None, "bin has no ASW1 block — ECM3 addresses unavailable (CAL-only image?)"
    areas = resolve(data, ASW1_FILE_OFFSET + ECM3_ADDR_LOC)
    if areas is not None:
        return areas, "addresses from ASW1 (late)"
    areas = resolve(data, ASW1_FILE_OFFSET + ECM3_ADDR_LOC_EARLY)
    if areas is not None:
        return areas, "addresses from ASW1 (early)"
    return None, "ECM3 addresses in ASW1 did not resolve"


def _ecm3_stored_location(data) -> int:
    """CAL-relative offset of the ECM3 stored value (moved +56 on old-school CALs)."""
    ecm3 = ECM3_HEADER
    if data[CAL_FILE_OFFSET + ecm3 + 56] > 0:
        return ecm3 + 56
    return ecm3


def stored_checksum_ranges(
    data: Union[bytes, bytearray],
) -> list[tuple[str, int, int]]:
    """Where each checksum's *stored value* lives: ``(name, offset, length)``.

    Full-bin offsets. A byte-level diff of two revisions always finds these
    changed — they are computed over the calibration, so any real edit moves
    them — and a consumer needs to say so explicitly rather than treat them as
    a mystery. Their *correctness* is a separate question, answered by
    :func:`verify`.
    """
    return [
        ("CAL_CRC", CAL_FILE_OFFSET + CAL_CRC_HEADER + 4, 4),
        ("ECM3", CAL_FILE_OFFSET + _ecm3_stored_location(data), 8),
    ]


def verify_ecm3(
    data: Union[bytes, bytearray],
    *,
    correct: bool = False,
) -> tuple[ChecksumReport, Union[bytes, bytearray]]:
    """Verify (optionally correct) the 64-bit ECM3 monitor summation."""
    why = _require_full_bin(data)
    if why is not None:
        return ChecksumReport("ECM3", can_verify=False, is_stale=False, detail=why), data
    ecm3 = CAL_FILE_OFFSET + ECM3_HEADER
    areas, note = _ecm3_areas(data)
    if areas is None:
        return ChecksumReport("ECM3", can_verify=False, is_stale=False, detail=note), data

    # 64-bit accumulator seeded from the header's initial hi/lo words.
    acc = (_u32(data, ecm3 + 8) << 32) + _u32(data, ecm3 + 12)
    covered: list[tuple[int, int]] = []
    for start, end in areas:
        f_start = CAL_FILE_OFFSET + start
        f_end = CAL_FILE_OFFSET + end  # ECM3 sums words in [start, end), step 4
        for j in range(f_start, f_end, 4):
            acc += _u32(data, j)
        covered.append((f_start, f_end))
    acc &= 0xFFFFFFFFFFFFFFFF

    stored_rel = _ecm3_stored_location(data)
    sloc = CAL_FILE_OFFSET + stored_rel
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


# ---- combined ----------------------------------------------------------------- #
def verify(data: Union[bytes, bytearray]) -> list[ChecksumReport]:
    """Verify both CAL checksums, no correction. Order: CAL CRC, then ECM3."""
    crc_report, _ = verify_cal_crc(data)
    ecm3_report, _ = verify_ecm3(data)
    return [crc_report, ecm3_report]


def correct(data: Union[bytes, bytearray]) -> tuple[bytearray, list[ChecksumReport]]:
    """Return a corrected copy of ``data`` plus the *pre-correction* reports.

    ECM3 is corrected first because its stored value lives inside the region the
    CAL CRC covers, so the CRC must be recomputed over the already-corrected ECM3
    bytes. Returns ``(corrected_bytes, reports)`` where ``reports`` are the
    verdicts *before* correction (so callers can see what was stale).
    """
    reports = verify(data)
    _, data = verify_ecm3(data, correct=True)
    _, data = verify_cal_crc(data, correct=True)
    return bytearray(data), reports


def correction_patches(
    data: Union[bytes, bytearray],
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

    ecm3_report, corrected = verify_ecm3(work, correct=True)
    if ecm3_report.can_verify and ecm3_report.is_stale:
        sloc = CAL_FILE_OFFSET + _ecm3_stored_location(work)
        patches.append((sloc, bytes(corrected[sloc : sloc + 8])))
        work = bytearray(corrected)

    crc_report, corrected = verify_cal_crc(work, correct=True)
    if crc_report.can_verify and crc_report.is_stale:
        cloc = CAL_FILE_OFFSET + CAL_CRC_HEADER + 4
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
