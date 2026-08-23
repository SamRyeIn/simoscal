"""Tests for the U1 rendering layer (simoscal.render)."""

from __future__ import annotations

import struct
from pathlib import Path

import numpy as np
import pytest

from simoscal import BinImage, CalFile, RenderedTable, parse_xdf, render_table
from simoscal.checksum import SC8S50_STRUCTURE

FIXTURES = Path(__file__).parent / "fixtures"
MINI_XDF = FIXTURES / "mini.xdf"


@pytest.fixture(scope="module")
def mini_cal() -> CalFile:
    model = parse_xdf(str(MINI_XDF))
    size = model.base_offset + 0x6000
    buf = bytearray(size)
    # 10x10 int16 LE table @ 0x1000 -> file 0x201000: values 0..99.
    off = model.base_offset + 0x1000
    buf[off : off + 200] = struct.pack("<100h", *range(100))
    # 1x1 uint8 scalar @ 0x2000 -> value 200.
    buf[model.base_offset + 0x2000] = 200
    # 1x1 float32 @ 0x4000 -> 12.5.
    foff = model.base_offset + 0x4000
    buf[foff : foff + 4] = struct.pack("<f", 12.5)
    # PROFILE_1D: 5 uint16 RPM breakpoints @ 0x5000, 5 uint16 z values @ 0x5010.
    xoff = model.base_offset + 0x5000
    buf[xoff : xoff + 10] = struct.pack("<5H", 1000, 2000, 3000, 4000, 5000)
    zoff = model.base_offset + 0x5010
    buf[zoff : zoff + 10] = struct.pack("<5H", 10, 20, 30, 40, 50)
    img = BinImage(buf, region_start=model.region_start, region_size=len(buf))
    return CalFile(model, img, structure=SC8S50_STRUCTURE)


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #
def test_render_10x10_index_fallback(mini_cal: CalFile):
    view = mini_cal.get("SYM_10X10")
    rt = render_table(view)
    assert isinstance(rt, RenderedTable)
    assert rt.symbol == "SYM_10X10"
    assert rt.title == "Ten by Ten"
    assert rt.units == "%"
    assert rt.x_labels == tuple(float(i) for i in range(10))
    assert rt.y_labels == tuple(float(i) for i in range(10))
    # Mini's x/y axes are label-only with no <units> element.
    assert rt.x_units is None
    assert rt.y_units is None
    np.testing.assert_array_equal(rt.values, view.values)


def test_render_1d_real_x_axis(mini_cal: CalFile):
    view = mini_cal.get("PROFILE_1D")
    rt = render_table(view)
    assert rt.x_labels == (1000.0, 2000.0, 3000.0, 4000.0, 5000.0)
    assert rt.y_labels is None
    assert rt.x_units == "RPM"
    assert rt.y_units is None  # y axis has no <units> element
    assert rt.values.shape == (1, 5)
    np.testing.assert_array_equal(rt.values, view.values)


# --------------------------------------------------------------------------- #
# Edge cases
# --------------------------------------------------------------------------- #
def test_render_scalar_no_axis_headers(mini_cal: CalFile):
    view = mini_cal.get("SYM_SCALAR")
    rt = render_table(view)
    assert rt.y_labels is None
    assert rt.values.shape == (1, 1)
    assert float(rt.values[0, 0]) == 200


def test_render_real_2d_table(real_cal):
    view = real_cal.get("IP_PRS_UP_THR_DIF_WIDE_OPEN_THR")
    assert view.shape == (6, 6)
    rt = render_table(view)
    assert rt.x_units == "rpm"
    assert rt.y_units == "hPa"
    np.testing.assert_allclose(rt.x_labels, view.axis_values("x").ravel())
    np.testing.assert_allclose(rt.y_labels, view.axis_values("y").ravel())
    np.testing.assert_array_equal(rt.values, view.values)


# --------------------------------------------------------------------------- #
# Integration — full real dataset smoke test
# --------------------------------------------------------------------------- #
def test_render_every_real_table_survives(real_cal):
    for view in real_cal.unique_tables():
        rt = render_table(view)
        assert rt.values.shape == view.shape
