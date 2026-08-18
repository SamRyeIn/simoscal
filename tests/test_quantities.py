"""Axis labels and table signatures — what makes a grid of numbers readable.

Two things are being protected here. The first is that every axis an editor can
put on screen is *named*, with its unit, because breakpoints alone do not say
what they measure and "4000" is engine speed on one table and kg/h on the next.
The second is that a name is never invented: an axis outside the curated map
falls back to its symbol, which is unhelpful but true, rather than to a guess
made from the unit, which would be helpful and sometimes wrong.

Real SC8S50 files are gitignored → the catalog cases skip (never fail) when
absent; the pure-function cases always run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from simoscal.tune import SC8S50, Tune
from simoscal.tune.catalog import catalog, table_detail
from simoscal.tune.quantities import (
    AXIS_QUANTITIES,
    DIMENSIONLESS,
    axis_label,
    table_signature,
    units_label,
)

CODE_ROOT = Path(__file__).resolve().parents[1]
STOCK_BIN = CODE_ROOT / "bin" / "5G0906259L__0002.bin"
XDF = CODE_ROOT / "xdf" / "SC8S50.V1.0.xdf"

requires_base = pytest.mark.skipif(
    not (STOCK_BIN.is_file() and XDF.is_file()),
    reason="real SC8S50 bin/XDF absent",
)


def _open_base() -> Tune:
    return Tune.open(SC8S50, xdf=XDF, bin=STOCK_BIN)


# --------------------------------------------------------------------------- #
# unit and label spelling
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raw", ["", "-", "–", "—", "  "])
def test_a_missing_or_dash_unit_is_spelled_out(raw: str) -> None:
    # The XDF writes dimensionless as "-", which on screen is indistinguishable
    # from "nobody recorded a unit for this".
    assert units_label(raw) == DIMENSIONLESS


def test_a_real_unit_is_left_alone() -> None:
    assert units_label("hPa") == "hPa"
    assert units_label("mg/stk") == "mg/stk"


def test_a_curated_axis_is_labelled_with_its_quantity_and_unit() -> None:
    assert axis_label("ldp_n_ip_cha_max", "rpm") == "Engine speed [rpm]"
    assert axis_label("ldp_fac_1_ip_fac_bpa_sp", "-") == (
        f"Exhaust flow factor [{DIMENSIONLESS}]"
    )


def test_an_uncurated_axis_falls_back_to_its_symbol_rather_than_guessing() -> None:
    # rpm is engine speed on nine catalog axes and turbocharger speed elsewhere
    # in this XDF, so inferring the quantity from the unit would eventually put
    # the wrong name over the wrong column. Say the symbol instead.
    assert axis_label("ldp_pq_cmpr_ip_n_tcha_stnd", "rpm") == (
        "ldp_pq_cmpr_ip_n_tcha_stnd [rpm]"
    )
    assert axis_label(None, "rpm") == "Axis [rpm]"


def test_signature_names_the_cell_unit_against_the_axes() -> None:
    assert table_signature(
        "hPa", "Engine speed [rpm]", "Manifold pressure setpoint [hPa]"
    ) == "hPa vs. Engine speed [rpm] and Manifold pressure setpoint [hPa]"
    assert table_signature("Nm", "Engine speed [rpm]") == "Nm vs. Engine speed [rpm]"


def test_a_table_with_no_axes_is_counted_rather_than_left_dangling() -> None:
    assert table_signature("hPa") == "A single value in hPa"
    assert table_signature("-") == f"A single {DIMENSIONLESS} value"
    assert table_signature("rpm", count=12, is_axis=True) == "12 breakpoints in rpm"
    assert table_signature("mg/stk", count=8) == "8 values in mg/stk"


# --------------------------------------------------------------------------- #
# against the real XDF
# --------------------------------------------------------------------------- #
@requires_base
def test_every_catalog_axis_resolves_a_symbol_and_a_label() -> None:
    # The whole point of the linkobjid parse: an axis with no symbol cannot be
    # named at all, and one silently losing its link would show a raw "Axis"
    # heading with no way to tell that anything was missing.
    tune = _open_base()
    for info in catalog(tune):
        for which, axis in (("x", info.x_axis), ("y", info.y_axis)):
            if axis is None:
                continue
            assert axis.symbol, f"{info.symbol}.{which} has no axis symbol"
            assert axis.label.endswith("]"), f"{info.symbol}.{which}: {axis.label}"


@requires_base
def test_every_catalog_axis_is_curated() -> None:
    # A new profile entry that brings an uncurated axis with it should show up
    # here rather than on a tablet as a raw A2L symbol over a column of numbers.
    tune = _open_base()
    uncurated = {
        axis.symbol
        for info in catalog(tune)
        for axis in (info.x_axis, info.y_axis)
        if axis is not None and axis.symbol not in AXIS_QUANTITIES
    }
    assert not uncurated, f"add these to AXIS_QUANTITIES: {sorted(uncurated)}"


@requires_base
def test_a_real_table_reads_as_a_sentence() -> None:
    tune = _open_base()
    pq = table_detail(tune, "pressure_quotient_max")
    assert pq.signature == (
        "dimensionless vs. Engine speed [rpm] and "
        "Compressor-inlet air temperature [°C]"
    )
    assert pq.units_description == DIMENSIONLESS
    assert pq.x_axis.symbol == "ldp_n_ip_cha_max"
    assert pq.y_axis.label == "Compressor-inlet air temperature [°C]"

    put = table_detail(tune, "put_setpoint")
    assert put.signature == (
        "hPa vs. Engine speed [rpm] and Manifold pressure setpoint [hPa]"
    )
