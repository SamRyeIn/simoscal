"""Tests for the U2-U5 export module (simoscal.export)."""

from __future__ import annotations

from pathlib import Path

import pytest

from simoscal import (
    AmbiguousTableError,
    BinImage,
    CalFile,
    parse_xdf,
    select_tables,
)

FIXTURES = Path(__file__).parent / "fixtures"
MINI_XDF = FIXTURES / "mini.xdf"


@pytest.fixture(scope="module")
def mini_cal() -> CalFile:
    model = parse_xdf(str(MINI_XDF))
    # Selection doesn't decode values, so a zero-filled buffer is sufficient —
    # sized just enough to cover the fixture's highest declared address.
    size = model.base_offset + 0x6000
    buf = bytearray(size)
    img = BinImage(buf, region_start=model.region_start, region_size=len(buf))
    return CalFile(model, img)


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #
def test_select_by_explicit_symbols(mini_cal: CalFile):
    views = select_tables(mini_cal, symbols=["SYM_10X10", "SYM_SCALAR"])
    assert {v.uniqueid for v in views} == {0x100, 0x200}


def test_select_by_category(mini_cal: CalFile):
    views = select_tables(mini_cal, category="Boost Control")
    assert {v.uniqueid for v in views} == {0x100, 0x200}


def test_select_all_tables(mini_cal: CalFile):
    views = select_tables(mini_cal, all_tables=True)
    assert {v.uniqueid for v in views} == {v.uniqueid for v in mini_cal.unique_tables()}
    assert len(views) == 5


# --------------------------------------------------------------------------- #
# Edge — overlap dedup
# --------------------------------------------------------------------------- #
def test_select_symbol_and_category_overlap_dedups(mini_cal: CalFile):
    views = select_tables(mini_cal, symbols=["SYM_10X10"], category="Boost Control")
    assert {v.uniqueid for v in views} == {0x100, 0x200}
    assert len(views) == 2


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
def test_select_unknown_symbol_raises_keyerror(mini_cal: CalFile):
    with pytest.raises(KeyError):
        select_tables(mini_cal, symbols=["DOES_NOT_EXIST"])


def test_select_ambiguous_symbol_raises(mini_cal: CalFile):
    with pytest.raises(AmbiguousTableError):
        select_tables(mini_cal, symbols=["SYM_DUP"])


def test_select_no_input_raises_valueerror(mini_cal: CalFile):
    with pytest.raises(ValueError):
        select_tables(mini_cal)


# --------------------------------------------------------------------------- #
# Integration — real data category selection
# --------------------------------------------------------------------------- #
def test_select_real_axis_category_count(real_cal):
    views = select_tables(real_cal, category="Axis")
    assert len(views) == 444
