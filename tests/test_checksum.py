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

#: These tests are written against the SC8S50 layout, which is now one declared
#: structure rather than the module's ambient constants. Naming it here keeps
#: each assertion explicit about which car's layout it is asserting.
SPEC = ck.SC8S50_STRUCTURE

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
    reports = ck.verify(b"\x00" * 100, SPEC)
    assert [r.name for r in reports] == ["CAL_CRC", "ECM3"]
    assert all(not r.can_verify and not r.is_stale for r in reports)
    assert all("bytes" in r.detail for r in reports)


def test_cal_only_image_cannot_verify_ecm3():
    """A CAL-block-sized image (no ASW1) can't resolve ECM3 addresses."""
    # Just under the ECM3-address reach: a bare CAL block.
    data = bytearray(SPEC.cal_block_length)
    reports = ck.verify(data, SPEC)
    assert all(not r.can_verify for r in reports)


# --- synthetic CAL CRC path ------------------------------------------------- #
def _synthetic_bin_with_cal_crc():
    """A minimal full-length bin with one valid CAL CRC area at [0x1000, 0x10ff].

    ECM3 is left with a zero area-count so it degrades to cannot-verify — this
    fixture targets the CRC path only.
    """
    size = SPEC.cal_file_offset + SPEC.cal_block_length
    data = bytearray(size)
    # Deterministic filler in the covered area so the CRC is non-trivial.
    for i in range(0x1000, 0x1100):
        data[SPEC.cal_file_offset + i] = (i * 7) & 0xFF

    hdr = SPEC.cal_file_offset + SPEC.cal_crc_header
    data[hdr + 8] = 1  # one area
    struct.pack_into("<I", data, hdr + 12, SPEC.cal_base_address + 0x1000)  # start
    struct.pack_into("<I", data, hdr + 16, SPEC.cal_base_address + 0x10FF)  # end (incl)

    covered = data[SPEC.cal_file_offset + 0x1000 : SPEC.cal_file_offset + 0x1100]
    struct.pack_into("<I", data, hdr + 4, ck.crc32_simos(covered))
    return data


def _synthetic_bin_with_both_checksums():
    """The CRC fixture, plus an ECM3 header whose single area resolves.

    Built so :func:`correct` has both checksums to work with — the CRC-only
    fixture deliberately does not, which is what the refusal test uses.
    """
    data = _synthetic_bin_with_cal_crc()
    ecm3 = SPEC.ecm3_file_offset
    struct.pack_into("<II", data, ecm3 + 8, ck.ECM3_SEED_HI, ck.ECM3_SEED_LO)
    struct.pack_into("<I", data, ecm3 + 16, 1)  # one area
    # Inline CAL addresses (header+24 non-zero), covering [0x1000, 0x1100).
    struct.pack_into("<I", data, ecm3 + 24, SPEC.cal_base_address + 0x1000)
    struct.pack_into("<I", data, ecm3 + 28, SPEC.cal_base_address + 0x1100)
    report, _ = ck.verify_ecm3(data, SPEC)
    assert report.can_verify, report.detail
    struct.pack_into("<I", data, ecm3, report.computed >> 32)
    struct.pack_into("<I", data, ecm3 + 4, report.computed & 0xFFFFFFFF)
    # Both stored values sit inside the CRC's coverage on a real bin; here they do
    # not, so the CRC stays valid and each checksum is exercised independently.
    return data


def test_synthetic_cal_crc_valid():
    data = _synthetic_bin_with_cal_crc()
    crc = ck.verify(data, SPEC)[0]
    assert crc.name == "CAL_CRC"
    assert crc.can_verify and not crc.is_stale
    assert crc.covered == ((SPEC.cal_file_offset + 0x1000, SPEC.cal_file_offset + 0x1100),)


def test_synthetic_cal_crc_stale_yields_a_minimal_patch():
    data = _synthetic_bin_with_cal_crc()
    data[SPEC.cal_file_offset + 0x1050] ^= 0xFF  # perturb a covered byte
    assert ck.verify(data, SPEC)[0].is_stale

    patches = ck.correction_patches(data, SPEC)
    assert len(patches) == 1  # only the CRC stored bytes
    off, patch = patches[0]
    assert off == SPEC.cal_file_offset + SPEC.cal_crc_header + 4 and len(patch) == 4


def test_correct_refuses_a_bin_whose_ecm3_cannot_be_located():
    """A partially-correctable bin is not correctable (AE7).

    This fixture's ECM3 area count is zero, so ECM3 cannot be located. Correcting
    only the CRC and returning the buffer would hand back a bin that *looks*
    corrected and is not flash-ready — the exact silent no-op this raise exists
    to prevent. The message must name which checksum and why.
    """
    data = _synthetic_bin_with_cal_crc()
    data[SPEC.cal_file_offset + 0x1050] ^= 0xFF
    with pytest.raises(ck.ChecksumNotLocatable) as excinfo:
        ck.correct(data, SPEC)
    assert "ECM3" in str(excinfo.value)
    assert "out of range" in str(excinfo.value)


def test_correct_fixes_a_bin_whose_checksums_are_both_locatable():
    """The happy path, on a fixture where both checksums can be read."""
    data = _synthetic_bin_with_both_checksums()
    data[SPEC.cal_file_offset + 0x1050] ^= 0xFF
    assert ck.verify(data, SPEC)[0].is_stale

    fixed, pre = ck.correct(data, SPEC)
    assert pre[0].is_stale  # reports are pre-correction
    assert all(not r.is_stale for r in ck.verify(fixed, SPEC))


def test_synthetic_bad_area_table_cannot_verify():
    """A wild area count is treated as a mis-located header, not a crash."""
    data = _synthetic_bin_with_cal_crc()
    data[SPEC.cal_file_offset + SPEC.cal_crc_header + 8] = 0xFF  # 255 areas
    crc = ck.verify(data, SPEC)[0]
    assert not crc.can_verify


def test_verify_discovered_degrades_rather_than_raising():
    """A caller that must report on an unidentifiable bin gets reports, not an exception.

    Patch application and preflight both have to return a verdict for an image
    they do not recognise. ``discover_structure`` raises by design; this wrapper
    is the reporting path, and the reason must survive into the report.
    """
    reports = ck.verify_discovered(bytes(0x400000))
    assert [r.name for r in reports] == ["CAL_CRC", "ECM3"]
    assert all(not r.can_verify and not r.is_stale for r in reports)
    assert all("ECM3 seed" in r.detail for r in reports)


@requires_real
def test_verify_discovered_agrees_with_the_declared_spec_on_a_known_bin():
    """Discovery and declaration must not disagree about the same file."""
    data = REAL_BIN.read_bytes()
    discovered = {(r.name, r.stored, r.computed) for r in ck.verify_discovered(data)}
    declared = {(r.name, r.stored, r.computed) for r in ck.verify(data, SPEC)}
    assert discovered == declared


# --- discovery on an edited bin (CR-20260828-05) ---------------------------- #
@requires_real
def test_discovery_recognises_a_bin_whose_ecm3_is_stale():
    """A stale bin is the library's own output, and must stay reopenable.

    Discovery used to accept a CAL block only when the ECM3 sum stored in it
    already recomputed exactly — which refuses every edited-but-uncorrected
    binary, the exact class of file this library produces and then reopens
    (CR-20260828-05). Layout and correctness are separate questions: this asserts
    the layout comes back *identical* to the clean bin's, and that the stale sum
    is reported as stale rather than as an unrecognised file.
    """
    clean = REAL_BIN.read_bytes()
    baseline = ck.discover_structure(clean)

    data = bytearray(clean)
    ecm3_area = ck.verify(clean, SPEC)[1].covered[0]
    data[ecm3_area[0]] ^= 0xFF

    assert ck.discover_structure(bytes(data)) == baseline

    reports = {r.name: r for r in ck.verify_discovered(bytes(data))}
    assert reports["ECM3"].can_verify and reports["ECM3"].is_stale
    assert reports["CAL_CRC"].can_verify and reports["CAL_CRC"].is_stale


@requires_real
def test_discovery_still_refuses_what_it_cannot_locate():
    """Relaxing the checksum precondition must not relax recognition itself.

    The control on the test above: a file with no CAL block is still refused, and
    so is a CAL-only slice of a real bin — whose calibration bytes are entirely
    genuine, and which fails on the one thing a slice cannot supply.
    """
    for label, data in (
        ("zeros", bytes(0x400000)),
        ("cal-only slice", REAL_BIN.read_bytes()[SPEC.cal_file_offset:][:0x80000]),
    ):
        with pytest.raises(ck.StructureNotFound):
            ck.discover_structure(data)


# --- real bin: the authoritative oracle ------------------------------------- #
@requires_real
def test_real_bin_verifies_clean():
    """AE-adjacent: the stock bin's CAL CRC and ECM3 both verify valid."""
    data = REAL_BIN.read_bytes()
    reports = ck.verify(data, SPEC)
    assert {r.name for r in reports} == {"CAL_CRC", "ECM3"}
    for r in reports:
        assert r.can_verify, r.detail
        assert not r.is_stale, f"{r.name} unexpectedly stale in stock bin"
        assert r.stored == r.computed


@requires_real
def test_real_cal_crc_coverage_is_whole_block_minus_header():
    data = REAL_BIN.read_bytes()
    crc = ck.verify(data, SPEC)[0]
    assert crc.covered == (
        (SPEC.cal_file_offset, SPEC.cal_file_offset + 0x300),
        (SPEC.cal_file_offset + 0x400, SPEC.cal_file_offset + 0x7FA00),
    )


@requires_real
def test_real_edit_flags_cal_crc_stale():
    """Perturbing a CRC-covered byte flips the report to stale (ECM3 unaffected)."""
    data = bytearray(REAL_BIN.read_bytes())
    data[0x201000] ^= 0xFF  # in CAL CRC coverage, outside the small ECM3 area
    crc, ecm3 = ck.verify(data, SPEC)
    assert crc.is_stale
    assert not ecm3.is_stale


@requires_real
def test_real_edit_in_ecm3_area_flags_both():
    data = bytearray(REAL_BIN.read_bytes())
    ecm3_area = ck.verify(data, SPEC)[1].covered[0]
    data[ecm3_area[0]] ^= 0xFF
    crc, ecm3 = ck.verify(data, SPEC)
    assert crc.is_stale and ecm3.is_stale


@requires_real
def test_real_correct_yields_clean_bin():
    data = bytearray(REAL_BIN.read_bytes())
    data[0x20E000] ^= 0xFF  # inside both ECM3 and CAL CRC coverage
    assert all(r.is_stale for r in ck.verify(data, SPEC))
    fixed, _ = ck.correct(data, SPEC)
    assert all(not r.is_stale for r in ck.verify(fixed, SPEC))


@requires_real
def test_edit_in_header_gap_does_not_stale_crc():
    """A byte in the [0x300, 0x400) header gap isn't covered by the CRC."""
    data = REAL_BIN.read_bytes()
    crc_covered = ck.verify(data, SPEC)[0].covered
    assert not ck.ranges_overlap([(0x200350, 1)], crc_covered)


# --- CalFile integration ---------------------------------------------------- #
@requires_real
def test_calfile_unchanged_is_clean_and_silent(tmp_path):
    cal = s.CalFile.open(str(REAL_XDF), str(REAL_BIN), structure=SPEC)
    out = tmp_path / "clean.bin"
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        reports = cal.save(out)
    assert all(not r.is_stale for r in reports)
    assert not [w for w in caught if issubclass(w.category, s.StaleChecksumWarning)]
    assert out.read_bytes() == REAL_BIN.read_bytes()


@requires_real
def test_calfile_edit_save_warns_stale(tmp_path):
    cal = s.CalFile.open(str(REAL_XDF), str(REAL_BIN), structure=SPEC)
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
    cal = s.CalFile.open(str(REAL_XDF), str(REAL_BIN), structure=SPEC)
    v = cal.get(0x11F9C)
    v.set_raw_cell(0, 0, int(v.raw[0, 0]) ^ 1)

    out = tmp_path / "corrected.bin"
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # no stale warning may fire when correcting
        reports = cal.save(out, correct_checksums=True)
    assert all(not r.is_stale for r in reports)
    # The on-disk file independently verifies clean.
    assert all(not r.is_stale for r in ck.verify(out.read_bytes(), SPEC))
    # And it stayed minimal-diff: only the edited cell + checksum bytes changed.
    before, after = REAL_BIN.read_bytes(), out.read_bytes()
    diff = [i for i in range(len(before)) if before[i] != after[i]]
    cell = v.table.embedded.address + cal.model.base_offset
    crc_bytes = set(range(0x200304, 0x200308))
    assert cell in diff
    assert all(i == cell or i in crc_bytes for i in diff), diff


@requires_real
def test_calfile_verify_checksums_method(tmp_path):
    cal = s.CalFile.open(str(REAL_XDF), str(REAL_BIN), structure=SPEC)
    assert all(not r.is_stale for r in cal.verify_checksums())
