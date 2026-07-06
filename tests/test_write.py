"""Tests for the U4 BIN write path (writer + safety + TableView edits).

Uses an inline XDF with ``BASEOFFSET 0x0`` so a tiny in-memory buffer suffices;
no real files needed. Covers inverse-scale, minimal-diff staging, the warn+allow
edit policy (AE4), non-linear fallback (AE5), the float-bug hard guard, the
raw-range guard, and float-table (no-round) writes.
"""

from __future__ import annotations

import io

import numpy as np
import pytest

from simoscal import (
    CalFile,
    BinImage,
    EditRangeWarning,
    FloatBugGuardError,
    NonLinearEquationError,
    RawRangeError,
    parse_xdf,
)

# Inline XDF: base offset 0 so mmedaddress == file offset (tiny buffer).
WRITE_XDF = """<XDFFORMAT version="1.60">
  <XDFHEADER>
    <BASEOFFSET offset="0x0" subtract="0" />
    <REGION size="0x1000" startaddress="0x0" />
    <DEFAULTS datasizeinbits="8" signed="0" lsbfirst="1" float="0" />
    <CATEGORY index="0x0" name="Test" />
  </XDFHEADER>
  <XDFTABLE uniqueid="0x10" flags="0x30">
    <title>Tight U8</title><description>TIGHT_U8</description>
    <XDFAXIS id="z">
      <EMBEDDEDDATA mmedtypeflags="0x2" mmedaddress="0x10" mmedelementsizebits="8" mmedcolcount="1" mmedrowcount="1" mmedmajorstridebits="8" mmedminorstridebits="0" />
      <min>0.0</min><max>50.0</max><units>-</units>
      <MATH equation="X"><VAR id="X" /></MATH>
    </XDFAXIS>
  </XDFTABLE>
  <XDFTABLE uniqueid="0x20" flags="0x30">
    <title>Quant</title><description>QUANT</description>
    <XDFAXIS id="z">
      <EMBEDDEDDATA mmedtypeflags="0x2" mmedaddress="0x20" mmedelementsizebits="8" mmedcolcount="1" mmedrowcount="1" mmedmajorstridebits="8" mmedminorstridebits="0" />
      <min>0.0</min><max>100.0</max><units>x</units>
      <MATH equation="X / 2.0"><VAR id="X" /></MATH>
    </XDFAXIS>
  </XDFTABLE>
  <XDFTABLE uniqueid="0x30" flags="0x30">
    <title>Nonlinear</title><description>NONLIN</description>
    <XDFAXIS id="z">
      <EMBEDDEDDATA mmedtypeflags="0x2" mmedaddress="0x30" mmedelementsizebits="8" mmedcolcount="1" mmedrowcount="1" mmedmajorstridebits="8" mmedminorstridebits="0" />
      <min>0.0</min><max>100.0</max><units>-</units>
      <MATH equation="X * X"><VAR id="X" /></MATH>
    </XDFAXIS>
  </XDFTABLE>
  <XDFTABLE uniqueid="0x40" flags="0x30">
    <title>Multi</title><description>MULTI</description>
    <XDFAXIS id="z">
      <EMBEDDEDDATA mmedtypeflags="0x2" mmedaddress="0x40" mmedelementsizebits="8" mmedcolcount="2" mmedrowcount="2" mmedmajorstridebits="8" mmedminorstridebits="0" />
      <min>0.0</min><max>200.0</max><units>-</units>
      <MATH equation="X"><VAR id="X" /></MATH>
    </XDFAXIS>
  </XDFTABLE>
  <XDFTABLE uniqueid="0x50" flags="0x30">
    <title>Float Bug</title><description>C_PRS_IM_SP_MAX</description>
    <XDFAXIS id="z">
      <EMBEDDEDDATA mmedtypeflags="0x10006" mmedaddress="0x50" mmedelementsizebits="32" mmedcolcount="1" mmedrowcount="1" mmedmajorstridebits="32" mmedminorstridebits="0" />
      <min>0.0</min><max>10000.0</max><units>hPa</units>
      <MATH equation="X"><VAR id="X" /></MATH>
    </XDFAXIS>
  </XDFTABLE>
  <XDFTABLE uniqueid="0x60" flags="0x30">
    <title>Signed Tight</title><description>SIGNED_TIGHT</description>
    <XDFAXIS id="z">
      <EMBEDDEDDATA mmedtypeflags="0x3" mmedaddress="0x60" mmedelementsizebits="8" mmedcolcount="1" mmedrowcount="1" mmedmajorstridebits="8" mmedminorstridebits="0" />
      <min>-10.0</min><max>10.0</max><units>-</units>
      <MATH equation="X"><VAR id="X" /></MATH>
    </XDFAXIS>
  </XDFTABLE>
</XDFFORMAT>
"""


@pytest.fixture
def cal() -> CalFile:
    model = parse_xdf(io.StringIO(WRITE_XDF))
    img = BinImage(bytearray(0x1000), region_start=0, region_size=0x1000)
    return CalFile(model, img)


# --------------------------------------------------------------------------- #
# Minimal-diff writes
# --------------------------------------------------------------------------- #
def test_set_cell_touches_only_that_cell(cal: CalFile):
    v = cal.get("MULTI")  # 2x2 uint8 @ 0x40, all zero initially
    v.set_cell(0, 1, 5)
    buf = cal.binimage.to_bytes()
    # cell (0,1) is index 1 -> offset 0x41.
    assert buf[0x40] == 0 and buf[0x41] == 5 and buf[0x42] == 0 and buf[0x43] == 0
    assert v.values[0, 1] == 5


def test_set_full_table(cal: CalFile):
    v = cal.get("MULTI")
    v.set([[1, 2], [3, 4]])
    buf = cal.binimage.to_bytes()
    assert list(buf[0x40:0x44]) == [1, 2, 3, 4]
    np.testing.assert_array_equal(v.values, [[1, 2], [3, 4]])


def test_set_cell_recorded_as_edit(cal: CalFile):
    assert cal.edited is False
    cal.get("MULTI").set_cell(0, 0, 7)
    assert cal.edited is True
    assert cal.edited_ranges == [(0x40, 1)]


# --------------------------------------------------------------------------- #
# Inverse scaling + quantization
# --------------------------------------------------------------------------- #
def test_quantization_stores_nearest_raw(cal: CalFile):
    v = cal.get("QUANT")  # phys = X/2  ->  raw = round(2*phys), LSB = 0.5
    v.set_cell(0, 0, 1.7)  # raw = round(3.4) = 3 -> reread 1.5
    got = float(v.values[0, 0])
    assert got == 1.5
    assert abs(got - 1.7) <= 0.5  # within one LSB


def test_float_table_is_not_rounded(cal: CalFile):
    v = cal.get("C_PRS_IM_SP_MAX")  # float32 identity scaling
    v.set_cell(0, 0, 123.45)
    got = float(v.values[0, 0])
    assert abs(got - 123.45) < 1e-2  # float32 precision, NOT integer-rounded


# --------------------------------------------------------------------------- #
# AE4 — warn + allow on out-of-declared-range
# --------------------------------------------------------------------------- #
def test_over_max_warns_and_writes(cal: CalFile):
    v = cal.get("TIGHT_U8")  # max 50, but u8 holds up to 255
    with pytest.warns(EditRangeWarning) as rec:
        v.set_cell(0, 0, 60)
    assert int(v.values[0, 0]) == 60  # written anyway
    msg = str(rec[0].message)
    assert "0x10" in msg and "50" in msg and "(0,0)" in msg


def test_below_min_warns_and_writes(cal: CalFile):
    v = cal.get("SIGNED_TIGHT")  # min -10, int8 holds down to -128
    with pytest.warns(EditRangeWarning):
        v.set_cell(0, 0, -20)
    assert int(v.values[0, 0]) == -20


def test_within_limits_does_not_warn(cal: CalFile, recwarn):
    cal.get("TIGHT_U8").set_cell(0, 0, 25)
    assert len(recwarn) == 0


# --------------------------------------------------------------------------- #
# Raw-range guard — hard fail, never a silent wrap
# --------------------------------------------------------------------------- #
def test_raw_overflow_hard_fails(cal: CalFile):
    v = cal.get("TIGHT_U8")
    with pytest.warns(EditRangeWarning):  # 300 > display max 50 also warns first
        with pytest.raises(RawRangeError):
            v.set_cell(0, 0, 300)  # 300 > u8 max 255
    # nothing written
    assert cal.binimage.to_bytes()[0x10] == 0


# --------------------------------------------------------------------------- #
# AE5 — non-linear table: reject set(physical), allow set_raw
# --------------------------------------------------------------------------- #
def test_nonlinear_rejects_physical_set(cal: CalFile):
    v = cal.get("NONLIN")
    with pytest.raises(NonLinearEquationError):
        v.set_cell(0, 0, 5)
    with pytest.raises(NonLinearEquationError):
        v.set([[5]])


def test_nonlinear_allows_set_raw(cal: CalFile):
    v = cal.get("NONLIN")
    v.set_raw_cell(0, 0, 7)
    assert int(v.raw[0, 0]) == 7  # non-linear -> values fall back to raw
    assert cal.binimage.to_bytes()[0x30] == 7


# --------------------------------------------------------------------------- #
# Float-bug hard guard (Decision 9)
# --------------------------------------------------------------------------- #
def test_float_bug_guard_rejects_over_limit(cal: CalFile):
    v = cal.get("C_PRS_IM_SP_MAX")  # flagged, max 10000
    with pytest.raises(FloatBugGuardError):
        v.set_cell(0, 0, 20000)


def test_float_bug_guard_rejects_even_with_override(cal: CalFile):
    v = cal.get("C_PRS_IM_SP_MAX")
    with pytest.raises(FloatBugGuardError):
        v.set_cell(0, 0, 20000, override=True)
    assert cal.binimage.to_bytes()[0x50:0x54] == bytes(4)  # unchanged


def test_float_bug_within_limit_writes(cal: CalFile):
    v = cal.get("C_PRS_IM_SP_MAX")
    v.set_cell(0, 0, 5000)
    assert abs(float(v.values[0, 0]) - 5000) < 1e-2


# --------------------------------------------------------------------------- #
# save()
# --------------------------------------------------------------------------- #
def test_save_writes_edits_to_disk(cal: CalFile, tmp_path):
    cal.get("MULTI").set([[9, 8], [7, 6]])
    out = tmp_path / "out.bin"
    cal.save(out)
    reload = BinImage.from_path(out)
    assert list(reload.to_bytes()[0x40:0x44]) == [9, 8, 7, 6]
