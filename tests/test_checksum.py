"""Checksum verify/report tests (U5).

Two layers:

* **Synthetic** — a hand-built minimal bin exercising the CAL CRC verify/stale/
  correct path and the "cannot verify" degradation, with no real files needed.
* **Real bin** (skip-if-absent) — the strongest oracle: the stock bin's stored
  CAL CRC and ECM3 sums must verify clean, an edit must flag stale, and
  ``correct_checksums=True`` must produce a bin that re-verifies clean. This is
  the U5 success criterion (stands in for VW_Flash's own verdict on the file).
"""

from __future__ import annotations

import struct
import warnings
from pathlib import Path

import numpy as np
import pytest

import simoscal as s
from simoscal import checksum as ck

REAL_XDF = Path(__file__).parents[1] / "xdf" / "SC8S50.V1.0.xdf"
REAL_BIN = Path(__file__).parents[1] / "bin" / "5G0906259L__0002.bin"

requires_real = pytest.mark.skipif(
    not (REAL_XDF.exists() and REAL_BIN.exists()),
    reason=f"real XDF/BIN not present: {REAL_XDF}, {REAL_BIN}",
)


# --- CRC primitive ---------------------------------------------------------- #
def test_crc32_regression_vector():
    """Pin the CRC to a fixed vector (poly 0x04C11DB7, init 0, no reflection)."""
    assert ck.crc32_simos(b"123456789") == 0x89A1897F
    assert ck.crc32_simos(b"") == 0x00000000


def test_crc_table_matches_generated_poly():
    """The generated table's entry 1 is the raw polynomial (sanity on generation)."""
    table = ck._make_crc_table()
    assert len(table) == 256
    assert table[0] == 0x00000000
    assert table[1] == 0x04C11DB7


# --- overlap helper --------------------------------------------------------- #
def test_ranges_overlap():
    covered = ((0x1000, 0x2000), (0x3000, 0x4000))
    assert ck.ranges_overlap([(0x1500, 4)], covered) is True   # inside first
    assert ck.ranges_overlap([(0x2000, 4)], covered) is False  # abuts, half-open
    assert ck.ranges_overlap([(0x2FFE, 4)], covered) is True   # straddles into second
    assert ck.ranges_overlap([(0x5000, 8)], covered) is False  # clear
    assert ck.ranges_overlap([], covered) is False


# --- degradation ------------------------------------------------------------ #
def test_short_bin_cannot_verify():
    reports = ck.verify(b"\x00" * 100)
    assert [r.name for r in reports] == ["CAL_CRC", "ECM3"]
    assert all(not r.can_verify and not r.is_stale for r in reports)
    assert all("bytes" in r.detail for r in reports)


def test_cal_only_image_cannot_verify_ecm3():
    """A CAL-block-sized image (no ASW1) can't resolve ECM3 addresses."""
    # Just under the ECM3-address reach: a bare CAL block.
    data = bytearray(ck.CAL_BLOCK_LENGTH)
    reports = ck.verify(data)
    assert all(not r.can_verify for r in reports)


# --- synthetic CAL CRC path ------------------------------------------------- #
def _synthetic_bin_with_cal_crc():
    """A minimal full-length bin with one valid CAL CRC area at [0x1000, 0x10ff].

    ECM3 is left with a zero area-count so it degrades to cannot-verify — this
    fixture targets the CRC path only.
    """
    size = ck.CAL_FILE_OFFSET + ck.CAL_BLOCK_LENGTH
    data = bytearray(size)
    # Deterministic filler in the covered area so the CRC is non-trivial.
    for i in range(0x1000, 0x1100):
        data[ck.CAL_FILE_OFFSET + i] = (i * 7) & 0xFF

    hdr = ck.CAL_FILE_OFFSET + ck.CAL_CRC_HEADER
    data[hdr + 8] = 1  # one area
    struct.pack_into("<I", data, hdr + 12, ck.CAL_BASE_ADDRESS + 0x1000)  # start
    struct.pack_into("<I", data, hdr + 16, ck.CAL_BASE_ADDRESS + 0x10FF)  # end (incl)

    covered = data[ck.CAL_FILE_OFFSET + 0x1000 : ck.CAL_FILE_OFFSET + 0x1100]
    struct.pack_into("<I", data, hdr + 4, ck.crc32_simos(covered))
    return data


def test_synthetic_cal_crc_valid():
    data = _synthetic_bin_with_cal_crc()
    crc = ck.verify(data)[0]
    assert crc.name == "CAL_CRC"
    assert crc.can_verify and not crc.is_stale
    assert crc.covered == ((ck.CAL_FILE_OFFSET + 0x1000, ck.CAL_FILE_OFFSET + 0x1100),)


def test_synthetic_cal_crc_stale_then_corrected():
    data = _synthetic_bin_with_cal_crc()
    data[ck.CAL_FILE_OFFSET + 0x1050] ^= 0xFF  # perturb a covered byte
    assert ck.verify(data)[0].is_stale

    patches = ck.correction_patches(data)
    assert len(patches) == 1  # only the CRC stored bytes
    off, patch = patches[0]
    assert off == ck.CAL_FILE_OFFSET + ck.CAL_CRC_HEADER + 4 and len(patch) == 4

    fixed, pre = ck.correct(data)
    assert pre[0].is_stale  # reports are pre-correction
    assert not ck.verify(fixed)[0].is_stale


def test_synthetic_bad_area_table_cannot_verify():
    """A wild area count is treated as a mis-located header, not a crash."""
    data = _synthetic_bin_with_cal_crc()
    data[ck.CAL_FILE_OFFSET + ck.CAL_CRC_HEADER + 8] = 0xFF  # 255 areas
    crc = ck.verify(data)[0]
    assert not crc.can_verify


# --- real bin: the authoritative oracle ------------------------------------- #
@requires_real
def test_real_bin_verifies_clean():
    """AE-adjacent: the stock bin's CAL CRC and ECM3 both verify valid."""
    data = REAL_BIN.read_bytes()
    reports = ck.verify(data)
    assert {r.name for r in reports} == {"CAL_CRC", "ECM3"}
    for r in reports:
        assert r.can_verify, r.detail
        assert not r.is_stale, f"{r.name} unexpectedly stale in stock bin"
        assert r.stored == r.computed


@requires_real
def test_real_cal_crc_coverage_is_whole_block_minus_header():
    data = REAL_BIN.read_bytes()
    crc = ck.verify(data)[0]
    assert crc.covered == (
        (ck.CAL_FILE_OFFSET, ck.CAL_FILE_OFFSET + 0x300),
        (ck.CAL_FILE_OFFSET + 0x400, ck.CAL_FILE_OFFSET + 0x7FA00),
    )


@requires_real
def test_real_edit_flags_cal_crc_stale():
    """Perturbing a CRC-covered byte flips the report to stale (ECM3 unaffected)."""
    data = bytearray(REAL_BIN.read_bytes())
    data[0x201000] ^= 0xFF  # in CAL CRC coverage, outside the small ECM3 area
    crc, ecm3 = ck.verify(data)
    assert crc.is_stale
    assert not ecm3.is_stale


@requires_real
def test_real_edit_in_ecm3_area_flags_both():
    data = bytearray(REAL_BIN.read_bytes())
    ecm3_area = ck.verify(data)[1].covered[0]
    data[ecm3_area[0]] ^= 0xFF
    crc, ecm3 = ck.verify(data)
    assert crc.is_stale and ecm3.is_stale


@requires_real
def test_real_correct_yields_clean_bin():
    data = bytearray(REAL_BIN.read_bytes())
    data[0x20E000] ^= 0xFF  # inside both ECM3 and CAL CRC coverage
    assert all(r.is_stale for r in ck.verify(data))
    fixed, _ = ck.correct(data)
    assert all(not r.is_stale for r in ck.verify(fixed))


@requires_real
def test_edit_in_header_gap_does_not_stale_crc():
    """A byte in the [0x300, 0x400) header gap isn't covered by the CRC."""
    data = REAL_BIN.read_bytes()
    crc_covered = ck.verify(data)[0].covered
    assert not ck.ranges_overlap([(0x200350, 1)], crc_covered)


# --- CalFile integration ---------------------------------------------------- #
@requires_real
def test_calfile_unchanged_is_clean_and_silent(tmp_path):
    cal = s.CalFile.open(str(REAL_XDF), str(REAL_BIN))
    out = tmp_path / "clean.bin"
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        reports = cal.save(out)
    assert all(not r.is_stale for r in reports)
    assert not [w for w in caught if issubclass(w.category, s.StaleChecksumWarning)]
    assert out.read_bytes() == REAL_BIN.read_bytes()


@requires_real
def test_calfile_edit_save_warns_stale(tmp_path):
    cal = s.CalFile.open(str(REAL_XDF), str(REAL_BIN))
    v = cal.get(0x11F9C)  # ID_PORT_SP, in the CAL block
    v.set_raw_cell(0, 0, int(v.raw[0, 0]) ^ 1)

    out = tmp_path / "stale.bin"
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        reports = cal.save(out)
    stale_warnings = [w for w in caught if issubclass(w.category, s.StaleChecksumWarning)]
    assert stale_warnings, "expected a StaleChecksumWarning after an edit"
    assert "touched its range" in str(stale_warnings[0].message)
    assert any(r.name == "CAL_CRC" and r.is_stale for r in reports)


@requires_real
def test_calfile_save_with_correct_is_flash_ready(tmp_path):
    cal = s.CalFile.open(str(REAL_XDF), str(REAL_BIN))
    v = cal.get(0x11F9C)
    v.set_raw_cell(0, 0, int(v.raw[0, 0]) ^ 1)

    out = tmp_path / "corrected.bin"
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # no stale warning may fire when correcting
        reports = cal.save(out, correct_checksums=True)
    assert all(not r.is_stale for r in reports)
    # The on-disk file independently verifies clean.
    assert all(not r.is_stale for r in ck.verify(out.read_bytes()))
    # And it stayed minimal-diff: only the edited cell + checksum bytes changed.
    before, after = REAL_BIN.read_bytes(), out.read_bytes()
    diff = [i for i in range(len(before)) if before[i] != after[i]]
    cell = v.table.embedded.address + cal.model.base_offset
    crc_bytes = set(range(0x200304, 0x200308))
    assert cell in diff
    assert all(i == cell or i in crc_bytes for i in diff), diff


@requires_real
def test_calfile_verify_checksums_method(tmp_path):
    cal = s.CalFile.open(str(REAL_XDF), str(REAL_BIN))
    assert all(not r.is_stale for r in cal.verify_checksums())
