"""Unit tests for the U1 core data model (simoscal.model)."""

from __future__ import annotations

import numpy as np
import pytest

import simoscal
from simoscal import (
    AmbiguousTableError,
    Axis,
    Category,
    EmbeddedData,
    FloatBugGuardError,
    NonLinearEquationError,
    RegionBoundsError,
    ScalingEquation,
    Table,
)


# --------------------------------------------------------------------------- #
# ScalingEquation — round-trip (the safety-critical property)
# --------------------------------------------------------------------------- #
def test_scaling_roundtrip_within_rounding():
    """Happy path: to_raw(to_physical(raw)) == raw for a real Simos scaling."""
    eq = ScalingEquation(m=0.0029296875, b=-48.0)
    assert eq.is_linear
    raw = np.arange(0, 256, dtype=np.int64)
    phys = eq.to_physical(raw)
    back = eq.to_raw(phys)
    assert np.array_equal(back, raw)


def test_scaling_roundtrip_single_value():
    eq = ScalingEquation(m=0.0029296875, b=-48.0)
    phys = eq.to_physical(100)
    assert phys == pytest.approx(-47.70703125)
    assert int(eq.to_raw(phys)) == 100


def test_identity_roundtrips_exactly():
    """Edge: identity equation (m=1, b=0) round-trips with no rounding."""
    eq = ScalingEquation.identity()
    assert (eq.m, eq.b, eq.is_linear) == (1.0, 0.0, True)
    raw = np.array([-128, -1, 0, 1, 127, 32767], dtype=np.int64)
    assert np.array_equal(eq.to_raw(eq.to_physical(raw)), raw)


def test_to_physical_returns_float_array():
    eq = ScalingEquation(m=0.5, b=10.0)
    out = eq.to_physical(4)
    assert isinstance(out, np.ndarray)
    assert out.dtype == np.float64
    assert out == pytest.approx(12.0)


def test_to_raw_quantizes_to_nearest():
    """A physical value between two raw steps stores the nearest raw."""
    eq = ScalingEquation(m=1.0, b=0.0)
    # 2.4 -> 2, 2.6 -> 3 (round-half-to-even at .5 is fine either way)
    assert int(eq.to_raw(2.4)) == 2
    assert int(eq.to_raw(2.6)) == 3


# --------------------------------------------------------------------------- #
# ScalingEquation — linearity detection via from_expression
# --------------------------------------------------------------------------- #
def test_from_expression_identity():
    eq = ScalingEquation.from_expression("X")
    assert eq.is_linear
    assert eq.m == pytest.approx(1.0)
    assert eq.b == pytest.approx(0.0)


def test_from_expression_linear_forms():
    eq = ScalingEquation.from_expression("X*0.0029296875-48")
    assert eq.is_linear
    assert eq.m == pytest.approx(0.0029296875)
    assert eq.b == pytest.approx(-48.0)


def test_from_expression_divide_form_with_d_zero_is_linear():
    """The ((a*X)-b)/(c-(d*X)) form with d=0 reduces to linear."""
    # ((2*X)-4)/(8-(0*X)) = (2X - 4)/8 = 0.25*X - 0.5
    eq = ScalingEquation.from_expression("((2*X)-4)/(8-(0*X))")
    assert eq.is_linear
    assert eq.m == pytest.approx(0.25)
    assert eq.b == pytest.approx(-0.5)


def test_from_expression_nonlinear_sets_flag_false():
    """Error scenario: a non-linear equation sets is_linear=False."""
    eq = ScalingEquation.from_expression("X*X")
    assert eq.is_linear is False
    assert np.isnan(eq.m)


def test_from_expression_divide_by_variable_is_nonlinear():
    eq = ScalingEquation.from_expression("1000/X")
    assert eq.is_linear is False


def test_from_expression_rejects_unsafe_identifiers():
    """A stray identifier must not be evaluated; it is treated as non-linear."""
    eq = ScalingEquation.from_expression("__import__('os')")
    assert eq.is_linear is False


def test_nonlinear_transform_raises():
    eq = ScalingEquation.from_expression("X*X")
    with pytest.raises(NonLinearEquationError):
        eq.to_physical(3)
    with pytest.raises(NonLinearEquationError):
        eq.to_raw(9.0)


def test_zero_slope_not_invertible():
    eq = ScalingEquation(m=0.0, b=5.0)
    with pytest.raises(NonLinearEquationError):
        eq.to_raw(5.0)


# --------------------------------------------------------------------------- #
# EmbeddedData
# --------------------------------------------------------------------------- #
def test_embedded_data_instantiates_and_derives():
    emb = EmbeddedData(address=0x36EC, rows=10, cols=10, elem_bits=16, signed=True)
    assert emb.element_bytes == 2
    assert emb.count == 100
    assert emb.shape == (10, 10)
    assert emb.little_endian is True


@pytest.mark.parametrize("bits", [7, 12, 24, 64])
def test_embedded_data_rejects_bad_elem_bits(bits):
    with pytest.raises(ValueError):
        EmbeddedData(address=0, rows=1, cols=1, elem_bits=bits)


def test_embedded_data_float_must_be_32bit():
    with pytest.raises(ValueError):
        EmbeddedData(address=0, rows=1, cols=1, elem_bits=16, is_float=True)
    # 32-bit float is fine
    EmbeddedData(address=0, rows=1, cols=1, elem_bits=32, is_float=True)


def test_embedded_data_rejects_bad_shape():
    with pytest.raises(ValueError):
        EmbeddedData(address=0, rows=0, cols=1, elem_bits=8)


# --------------------------------------------------------------------------- #
# Axis / Category / Table
# --------------------------------------------------------------------------- #
def test_axis_instantiates():
    ax = Axis(axis_id="z", units="%", min=0.0, max=100.0,
              scaling=ScalingEquation.identity())
    assert ax.axis_id == "z"
    assert ax.units == "%"


def test_axis_rejects_bad_id():
    with pytest.raises(ValueError):
        Axis(axis_id="w")


def test_category_instantiates():
    cat = Category(name="Boost", index=3)
    assert cat.name == "Boost"
    assert cat.index == 3


def test_table_instantiates_and_derives():
    emb = EmbeddedData(address=0x36EC, rows=10, cols=10, elem_bits=16, is_float=True) \
        if False else EmbeddedData(address=0x36EC, rows=1, cols=16, elem_bits=32,
                                   is_float=True)
    scaling = ScalingEquation.identity()
    z = Axis(axis_id="z", units="deg", embedded=emb, scaling=scaling)
    table = Table(
        uniqueid=0x36EC,
        title="Ignition Timing",
        symbol="C_FAC_POW_PUT_CTL_BOL",
        categories=(Category(name="Boost"),),
        z=z,
    )
    assert table.uniqueid_hex == "0x36ec"
    assert table.embedded is emb
    assert table.scaling is scaling
    assert table.shape == (1, 16)


def test_table_without_z_axis_has_none_derivations():
    table = Table(uniqueid=1)
    assert table.embedded is None
    assert table.scaling is None
    assert table.shape is None


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #
def test_all_exceptions_defined_and_raisable():
    for exc in (NonLinearEquationError, RegionBoundsError, FloatBugGuardError):
        with pytest.raises(exc):
            raise exc("boom")


def test_ambiguous_table_error_carries_candidates():
    err = AmbiguousTableError("C_FOO", ["0x1", "0x2"])
    assert err.key == "C_FOO"
    assert err.candidates == ["0x1", "0x2"]
    assert "2 tables" in str(err)


# --------------------------------------------------------------------------- #
# Package-level
# --------------------------------------------------------------------------- #
def test_package_exports_and_version():
    assert isinstance(simoscal.__version__, str)
    for name in simoscal.__all__:
        assert hasattr(simoscal, name)
