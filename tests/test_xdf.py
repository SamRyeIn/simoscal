"""Tests for the U2 XDF parser (simoscal.xdf)."""

from __future__ import annotations

import io
import time
from pathlib import Path

import pytest

from simoscal import (
    AmbiguousTableError,
    ScalingEquation,
    Table,
    XdfModel,
    XdfParseError,
    parse_xdf,
)

FIXTURES = Path(__file__).parent / "fixtures"
MINI_XDF = FIXTURES / "mini.xdf"
REAL_XDF = Path(__file__).parents[1] / "xdf" / "SC8S50.V1.0.xdf"

# Survey counts for SC8S50.V1.0.xdf (established by direct inspection).
REAL_TABLE_COUNT = 3912
REAL_HEADER_CATEGORY_COUNT = 660
REAL_EMBEDDED_AXIS_COUNT = 5305
REAL_MATH_COUNT = 11736


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def mini() -> XdfModel:
    return parse_xdf(str(MINI_XDF))


# --------------------------------------------------------------------------- #
# Header parsing
# --------------------------------------------------------------------------- #
def test_header_base_offset_and_region(mini: XdfModel):
    assert mini.base_offset == 0x200000
    assert mini.base_subtract is False
    assert mini.region_start == 0x0
    assert mini.region_size == 0x400000


def test_header_defaults(mini: XdfModel):
    assert mini.defaults.datasizeinbits == 8
    assert mini.defaults.lsbfirst is True
    assert mini.defaults.signed is False
    assert mini.defaults.is_float is False


def test_header_categories(mini: XdfModel):
    assert mini.category_by_index[0x0].name == "Axis"
    assert mini.category_by_index[0x1].name == "Boost Control"


# --------------------------------------------------------------------------- #
# Happy path — a known 10x10 table
# --------------------------------------------------------------------------- #
def test_10x10_table_fields(mini: XdfModel):
    t = mini.get("SYM_10X10")
    assert isinstance(t, Table)
    assert t.uniqueid == 0x100
    assert t.uniqueid_hex == "0x100"
    assert t.title == "Ten by Ten"
    assert t.symbol == "SYM_10X10"
    assert t.shape == (10, 10)
    assert t.embedded.address == 0x1000
    assert t.z.units == "%"


def test_10x10_typeflags_decoded_unsigned_le_colmajor(mini: XdfModel):
    emb = mini.get("SYM_10X10").embedded
    assert emb.elem_bits == 16
    # typeflags 0x6 = 0x02 (LE) | 0x04 (column-major). The sign bit is 0x01,
    # which is unset here -> unsigned. (See CR-20260706-22: 0x04 is NOT signed.)
    assert emb.signed is False
    assert emb.little_endian is True  # 0x6 has 0x02 set
    assert emb.column_major is True  # 0x6 has 0x04 set
    assert emb.is_float is False


def test_10x10_scaling_is_linear(mini: XdfModel):
    sc = mini.get("SYM_10X10").scaling
    assert isinstance(sc, ScalingEquation)
    assert sc.is_linear
    # ((1*X) - 0) / (327.68 - 0) => m = 1/327.68, b = 0
    assert sc.m == pytest.approx(1.0 / 327.68)
    assert sc.b == pytest.approx(0.0)


def test_10x10_category_membership_uses_1based(mini: XdfModel):
    # CATEGORYMEM category="2" -> header index 0x1 -> "Boost Control"
    t = mini.get("SYM_10X10")
    assert [c.name for c in t.categories] == ["Boost Control"]
    assert t in mini.by_category["Boost Control"]


# --------------------------------------------------------------------------- #
# Edge — 1x1 scalar table, and a float typeflag
# --------------------------------------------------------------------------- #
def test_scalar_1x1_parses_as_table(mini: XdfModel):
    t = mini.get("SYM_SCALAR")
    assert t.shape == (1, 1)
    assert t.embedded.elem_bits == 8
    assert t.embedded.signed is False  # 0x2 -> unsigned
    assert t.embedded.little_endian is True


def test_float_typeflag_decoded(mini: XdfModel):
    # SYM_DUP duplicate B uses mmedtypeflags 0x10006 (float, signed, LE), 32-bit
    t = mini.by_id[0x400]
    emb = t.embedded
    assert emb.is_float is True
    assert emb.elem_bits == 32
    assert emb.little_endian is True


# --------------------------------------------------------------------------- #
# Indexes / lookup
# --------------------------------------------------------------------------- #
def test_get_by_uniqueid_int_and_hex(mini: XdfModel):
    assert mini.get(0x100) is mini.by_id[0x100]
    assert mini.get("0x100") is mini.by_id[0x100]


def test_get_ambiguous_symbol_raises(mini: XdfModel):
    with pytest.raises(AmbiguousTableError) as excinfo:
        mini.get("SYM_DUP")
    err = excinfo.value
    assert set(err.candidates) == {"0x300", "0x400"}


def test_get_missing_raises_keyerror(mini: XdfModel):
    with pytest.raises(KeyError):
        mini.get("DOES_NOT_EXIST")


def test_search_matches_symbol_and_title(mini: XdfModel):
    assert {t.uniqueid for t in mini.search("SYM_")} == {0x100, 0x200, 0x300, 0x400}
    assert {t.uniqueid for t in mini.search("ten by ten")} == {0x100}


def test_categories_lists_populated_names(mini: XdfModel):
    assert "Boost Control" in mini.categories()
    # "Axis" has no member table in the fixture, so it is not listed.
    assert "Axis" not in mini.categories()


def test_len(mini: XdfModel):
    assert len(mini) == 4


# --------------------------------------------------------------------------- #
# Error — malformed / absent EMBEDDEDDATA names the uniqueid
# --------------------------------------------------------------------------- #
MALFORMED_XDF = """<XDFFORMAT version="1.60">
  <XDFHEADER>
    <BASEOFFSET offset="0x200000" subtract="0" />
    <REGION size="0x400000" startaddress="0x0" />
    <DEFAULTS datasizeinbits="8" signed="0" lsbfirst="1" float="0" />
  </XDFHEADER>
  <XDFTABLE uniqueid="0x999" flags="0x30">
    <title>Broken</title>
    <description>SYM_BROKEN</description>
    <XDFAXIS uniqueid="0x0" id="z">
      <MATH equation="X"><VAR id="X" /></MATH>
    </XDFAXIS>
  </XDFTABLE>
</XDFFORMAT>
"""


def test_absent_embedded_raises_naming_uniqueid():
    with pytest.raises(XdfParseError) as excinfo:
        parse_xdf(io.StringIO(MALFORMED_XDF))
    assert "0x999" in str(excinfo.value)


CONFLICTING_DUP_XDF = """<XDFFORMAT version="1.60">
  <XDFHEADER>
    <BASEOFFSET offset="0x200000" subtract="0" />
    <REGION size="0x400000" startaddress="0x0" />
    <DEFAULTS datasizeinbits="8" signed="0" lsbfirst="1" float="0" />
  </XDFHEADER>
  <XDFTABLE uniqueid="0x55" flags="0x30">
    <title>First</title><description>SYM_A</description>
    <XDFAXIS uniqueid="0x0" id="z">
      <EMBEDDEDDATA mmedtypeflags="0x2" mmedaddress="0x1000" mmedelementsizebits="8" mmedcolcount="1" mmedrowcount="1" />
      <MATH equation="X"><VAR id="X" /></MATH>
    </XDFAXIS>
  </XDFTABLE>
  <XDFTABLE uniqueid="0x55" flags="0x30">
    <title>Second</title><description>SYM_B</description>
    <XDFAXIS uniqueid="0x0" id="z">
      <EMBEDDEDDATA mmedtypeflags="0x2" mmedaddress="0x9999" mmedelementsizebits="8" mmedcolcount="1" mmedrowcount="1" />
      <MATH equation="X"><VAR id="X" /></MATH>
    </XDFAXIS>
  </XDFTABLE>
</XDFFORMAT>
"""


def test_genuinely_conflicting_duplicate_uniqueid_fails_loud():
    # Same uniqueid, DIFFERENT z address -> must hard-fail, never silently
    # map one uniqueid to two locations.
    with pytest.raises(XdfParseError) as excinfo:
        parse_xdf(io.StringIO(CONFLICTING_DUP_XDF))
    assert "0x55" in str(excinfo.value)


def test_embedded_missing_address_raises_naming_uniqueid():
    bad = MALFORMED_XDF.replace(
        '<MATH equation="X"><VAR id="X" /></MATH>',
        '<EMBEDDEDDATA mmedelementsizebits="8" mmedcolcount="1" mmedrowcount="1" />'
        '<MATH equation="X"><VAR id="X" /></MATH>',
    )
    with pytest.raises(XdfParseError) as excinfo:
        parse_xdf(io.StringIO(bad))
    assert "0x999" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# Real-file oracle — skipped cleanly if the bundled XDF is absent
# --------------------------------------------------------------------------- #
requires_real = pytest.mark.skipif(
    not REAL_XDF.exists(), reason=f"real XDF not present: {REAL_XDF}"
)


@pytest.fixture(scope="module")
def real() -> XdfModel:
    if not REAL_XDF.exists():
        pytest.skip(f"real XDF not present: {REAL_XDF}")
    return parse_xdf(str(REAL_XDF))


@requires_real
def test_real_table_count(real: XdfModel):
    assert len(real.tables) == REAL_TABLE_COUNT


@requires_real
def test_real_category_count(real: XdfModel):
    assert len(real.category_by_index) == REAL_HEADER_CATEGORY_COUNT


@requires_real
def test_real_embedded_axis_count(real: XdfModel):
    n = 0
    for t in real.tables:
        for ax in (t.x, t.y, t.z):
            if ax is not None and ax.embedded is not None:
                n += 1
    assert n == REAL_EMBEDDED_AXIS_COUNT


@requires_real
def test_real_every_equation_linear(real: XdfModel):
    total = 0
    nonlinear = []
    for t in real.tables:
        for ax in (t.x, t.y, t.z):
            if ax is not None and ax.scaling is not None:
                total += 1
                if not ax.scaling.is_linear:
                    nonlinear.append((t.uniqueid_hex, ax.axis_id, ax.scaling.expression))
    assert total == REAL_MATH_COUNT
    assert nonlinear == []


@requires_real
def test_real_get_named_symbol_single(real: XdfModel):
    t = real.get("C_FAC_POW_PUT_CTL_BOL")
    assert t.uniqueid == 0x36EC
    assert t.z.units == "%"


@requires_real
def test_real_duplicate_uniqueids_are_metadata_only(real: XdfModel):
    # a2l2xdf emits 98 uniqueids twice (same calibration cross-listed under
    # DTC/MIL titles). We keep all 3912 XDFTABLE entries but map each uniqueid
    # to a single data location: 3814 distinct ids, 98 with a duplicate.
    assert len(real.tables) == REAL_TABLE_COUNT
    assert len(real.by_id) == 3814
    assert sum(real.duplicate_ids.values()) == REAL_TABLE_COUNT - 3814
    assert len(real.duplicate_ids) == 98


@requires_real
def test_real_cross_listed_symbol_not_falsely_ambiguous(real: XdfModel):
    # C_ERR_CLAS_2_AFU_SENS_ERR is one of the cross-listed (duplicate-id) tables;
    # dedup-by-uniqueid means get() still returns exactly one.
    t = real.get("C_ERR_CLAS_2_AFU_SENS_ERR")
    assert t.uniqueid == 0x2487A


@requires_real
def test_real_parse_is_fast():
    start = time.perf_counter()
    model = parse_xdf(str(REAL_XDF))
    elapsed = time.perf_counter() - start
    assert len(model.tables) == REAL_TABLE_COUNT
    # "well under a few seconds" — generous ceiling for CI variance.
    assert elapsed < 5.0, f"parse took {elapsed:.2f}s"
