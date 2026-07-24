"""Generic edit operations, catalog, and the boost-curve read model (V5).

The switch-patch guards themselves (psi floor, below-base-ceiling refusal, row
tiling, strictly-increasing axis) are covered by the switchpatch domain tests;
here the focus is the new app-facing surface: the read-only catalog, the generic
atomic edit operations with requested-vs-encoded, and the boost-curve model plus
its requested-vs-encoded slot wrapper. The boost cases still assert the floor and
the min() invariant end-to-end, since those are the safety properties the hero
editor leans on.

Real SC8S50 files are gitignored → tests skip (never fail) when absent.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from simoscal.tune import SC8S50, Tune
from simoscal.tune.boostcurve import boost_curve_model, slot_curve_result
from simoscal.tune.catalog import catalog, table_detail
from simoscal.tune.domains.switchpatch import PATCH_SPACE
from simoscal.tune.editing import (
    EditOp,
    EditRejected,
    Selection,
    apply_op,
)
from simoscal.tune.profiles.switchpatch_2933 import SWITCH_PATCH_2933

CODE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = CODE_ROOT.parent

STOCK_BIN = CODE_ROOT / "bin" / "5G0906259L__0002.bin"
XDF = CODE_ROOT / "xdf" / "SC8S50.V1.0.xdf"
SWITCH_XDF = REPO_ROOT / "BinToolz-main" / "definitions" / "S50 Switch Patch.29.33.V2.xdf"
PATCHED_BIN = (
    REPO_ROOT / "Tunes" / "TuningBasicsGuide" / "BinToolz-patched"
    / "CB_HSL_SP2933_5G0906259L_0002_BasicsGuide_R04.bin"
)

_have_base = STOCK_BIN.is_file() and XDF.is_file()
_have_patch = PATCHED_BIN.is_file() and SWITCH_XDF.is_file() and XDF.is_file()
requires_base = pytest.mark.skipif(not _have_base, reason="real SC8S50 bin/XDF absent")
requires_patch = pytest.mark.skipif(not _have_patch, reason="patched bin / switch XDF absent")


def _open_base() -> Tune:
    return Tune.open(SC8S50, xdf=XDF, bin=STOCK_BIN)


def _open_patched() -> Tune:
    return Tune.open(
        SC8S50, xdf=XDF, bin=PATCHED_BIN,
        extra_spaces={PATCH_SPACE: (SWITCH_PATCH_2933, SWITCH_XDF)},
    )


# --------------------------------------------------------------------------- #
# catalog
# --------------------------------------------------------------------------- #
@requires_base
def test_catalog_lists_profile_tables_with_detail() -> None:
    tune = _open_base()
    cat = catalog(tune)
    assert cat, "catalog should not be empty"
    names = {t.name for t in cat}
    assert "put_setpoint" in names

    put = table_detail(tune, "put_setpoint")
    assert put.symbol == "IP_PUT_SP"
    assert put.ndim == 2 and put.shape == (4, 6)
    assert put.reversible is True
    assert put.x_axis is not None and put.x_axis.units == "rpm"
    assert put.id_and_description.startswith("`IP_PUT_SP` —")


@requires_base
def test_catalog_classifies_dimensionality() -> None:
    tune = _open_base()
    detail = table_detail(tune, "manifold_pressure_max")
    assert detail.ndim == 0  # (1,1) scalar
    axis = table_detail(tune, "put_setpoint_rpm_axis")
    assert axis.ndim == 1    # (1,6) vector


# --------------------------------------------------------------------------- #
# generic edit operations
# --------------------------------------------------------------------------- #
@requires_base
def test_set_reports_requested_vs_encoded() -> None:
    tune = _open_base()
    res = apply_op(tune, "put_setpoint", EditOp.SET,
                   selection=Selection.row(3), value=1500.0, intent="set full-load 1500")
    assert res.entry.verdict in ("applied", "unchanged")
    # 1500 hPa does not land on an exact step → quantization is reported, small.
    assert res.quantized is True
    assert 0 < res.max_abs_quantization() < 1.0


@requires_base
def test_arithmetic_ops_apply_over_selection() -> None:
    tune = _open_base()
    before = tune.values("put_setpoint").copy()
    apply_op(tune, "put_setpoint", EditOp.MUL, selection=Selection.row(3), value=1.10)
    after = tune.values("put_setpoint")
    # part-load rows unchanged, full-load row scaled up
    assert np.allclose(after[:3], before[:3])
    assert np.all(after[3] > before[3])


@requires_base
def test_division_by_zero_is_rejected_atomically() -> None:
    tune = _open_base()
    n = len(tune.journal)
    before = tune.values("put_setpoint").copy()
    with pytest.raises(EditRejected, match="division by zero"):
        apply_op(tune, "put_setpoint", EditOp.DIV, value=0.0)
    assert len(tune.journal) == n, "rejected edit must not journal"
    assert np.array_equal(tune.values("put_setpoint"), before), "table must be unchanged"


@requires_base
def test_empty_selection_is_rejected() -> None:
    tune = _open_base()
    with pytest.raises(EditRejected):
        apply_op(tune, "put_setpoint", EditOp.SET, selection=Selection.cells([]), value=1.0)


@requires_base
def test_out_of_range_selection_is_rejected() -> None:
    tune = _open_base()
    with pytest.raises(EditRejected, match="out of range"):
        apply_op(tune, "put_setpoint", EditOp.SET, selection=Selection.row(99), value=1.0)


@requires_base
def test_interpolate_ramps_a_contiguous_run() -> None:
    tune = _open_base()
    # Set the two endpoints of the full-load row, then interpolate between.
    apply_op(tune, "put_setpoint", EditOp.SET, selection=Selection.cells([(3, 0)]), value=1000.0)
    apply_op(tune, "put_setpoint", EditOp.SET, selection=Selection.cells([(3, 5)]), value=2000.0)
    apply_op(tune, "put_setpoint", EditOp.INTERPOLATE, selection=Selection.row(3))
    row = tune.values("put_setpoint")[3]
    # monotonic ramp between the endpoints (within encoding quantization)
    assert np.all(np.diff(row) > -1.0)
    assert abs(row[0] - 1000.0) < 1.0 and abs(row[-1] - 2000.0) < 1.0


@requires_base
def test_restore_returns_a_cell_to_source() -> None:
    tune = _open_base()
    source = tune.values("put_setpoint").copy()
    apply_op(tune, "put_setpoint", EditOp.SET, selection=Selection.row(3), value=1234.0)
    assert not np.allclose(tune.values("put_setpoint")[3], source[3])
    apply_op(tune, "put_setpoint", EditOp.RESTORE, selection=Selection.row(3))
    assert np.allclose(tune.values("put_setpoint")[3], source[3], atol=1.0)


@requires_base
def test_paste_writes_a_block() -> None:
    tune = _open_base()
    block = np.full((1, 6), 1400.0)
    apply_op(tune, "put_setpoint", EditOp.PASTE, selection=Selection.row(3), array=block)
    assert np.allclose(tune.values("put_setpoint")[3], 1400.0, atol=1.0)


@requires_base
def test_generic_edit_never_modifies_source_bin() -> None:
    import hashlib
    before = hashlib.sha256(STOCK_BIN.read_bytes()).hexdigest()
    tune = _open_base()
    apply_op(tune, "put_setpoint", EditOp.MUL, selection=Selection.row(3), value=1.2)
    assert hashlib.sha256(STOCK_BIN.read_bytes()).hexdigest() == before


# --------------------------------------------------------------------------- #
# boost-curve read model + slot wrapper
# --------------------------------------------------------------------------- #
@requires_patch
def test_boost_curve_model_shape_and_axes() -> None:
    tune = _open_patched()
    m = boost_curve_model(tune)
    assert len(m.rpm_axis) == 12
    assert len(m.slots) == 5
    assert all(len(s.psi) == 12 for s in m.slots)
    assert len(m.base_ceiling_psi) == 12


@requires_patch
def test_boost_curve_min_semantics_exposed() -> None:
    """effective = min(base, slot) — the shaded 'capped by base' region as data."""
    tune = _open_patched()
    m = boost_curve_model(tune)
    for s in m.slots:
        eff = m.effective_psi(s.slot)
        for e, slot_v, base_v in zip(eff, s.psi, m.base_ceiling_psi):
            assert abs(e - min(slot_v, base_v)) < 1e-6


@requires_patch
def test_slot_curve_result_reports_floor() -> None:
    tune = _open_patched()
    res = slot_curve_result(tune, 5, psi=10.0, intent="cap slot5 at 10 psi")
    # psi floors, so the encoded cap is never above the request.
    assert all(e <= 10.0 + 1e-9 for e in res.encoded_psi)
    # The whole grid tiled across all 8 rows.
    grid = tune.values("slot5_put_setpoint", space=PATCH_SPACE)
    assert grid.shape[0] == 8
    assert np.allclose(grid, grid[0])


@requires_patch
def test_slot_curve_above_base_ceiling_is_refused() -> None:
    """The min() invariant is enforced at write time (fingertip guard analog)."""
    tune = _open_patched()
    m = boost_curve_model(tune)
    over = max(m.base_ceiling_own_psi) + 20.0
    with pytest.raises(ValueError, match="base"):
        slot_curve_result(tune, 5, psi=over, intent="above the base ceiling")
