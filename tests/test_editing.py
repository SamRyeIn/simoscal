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
def test_catalog_excludes_the_new_domain_owned_tables() -> None:
    """The quartet + lambda FL main map are owned → out of the generic catalog;
    the pedal maps and the FL context tables stay in (U1 verification)."""
    from simoscal.tune.profiles.sc8s50 import (
        LAMBDA_FULL_LOAD, PEDAL_MAPS, SPEED_LIMITER,
    )

    tune = _open_base()
    default_names = {t.name for t in catalog(tune)}
    all_names = {t.name for t in catalog(tune, include_domain_owned=True)}

    owned = {*SPEED_LIMITER, "lambda_full_load"}
    unowned = {*PEDAL_MAPS, *(set(LAMBDA_FULL_LOAD) - {"lambda_full_load"})}
    assert owned.isdisjoint(default_names)
    assert owned <= all_names
    assert unowned <= default_names
    # The catalog grew by exactly the added non-owned specs relative to the
    # owned set: every owned table is exactly the difference between the views.
    assert all_names - default_names == {
        t.name for t in catalog(tune, include_domain_owned=True) if t.owner
    }


@requires_base
def test_every_catalog_entry_carries_a_domain_group() -> None:
    """The heading the app's table browser files each row under.

    Every generically editable table must have one: a row with no group is a row
    a grouped browser either drops or files under a heading meaning "nobody
    decided", and neither is something to discover on a tablet.
    """
    from simoscal.tune.profile import GROUPS

    tune = _open_base()
    ungrouped = [t.name for t in catalog(tune) if not t.group]
    assert ungrouped == []
    assert {t.group for t in catalog(tune)} <= set(GROUPS)


@requires_base
def test_the_group_is_the_profile_s_not_the_xdf_s_category() -> None:
    """Why the group exists at all, stated as a test.

    The XDF files `ldp_n_ip_put_sp` — Pressure up throttle setpoint : x axis
    (engine speed) under a category called "Axis", away from the setpoint it
    breakpoints. Both facts are carried, and they disagree on purpose: the app
    groups by ``group`` and searches either.
    """
    tune = _open_base()
    by_name = {t.name: t for t in catalog(tune)}
    axis = by_name["put_setpoint_rpm_axis"]
    setpoint = by_name["put_setpoint"]

    assert "Axis" in axis.categories
    assert "Axis" not in setpoint.categories
    assert axis.group == setpoint.group == "Boost"


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
def test_generic_axis_write_is_journaled_as_axis() -> None:
    tune = _open_base()
    before = tune.values("put_setpoint_rpm_axis")
    target = before.copy()
    target[0, 1] = (before[0, 0] + before[0, 2]) / 2
    result = apply_op(
        tune, "put_setpoint_rpm_axis", EditOp.SET, array=target,
        intent="move one boost rpm breakpoint",
    )
    assert result.entry.kind == "axis"
    assert np.all(np.diff(result.encoded.ravel()) > 0)


@requires_base
def test_nonmonotonic_generic_axis_write_is_rejected_atomically() -> None:
    tune = _open_base()
    before = tune.values("put_setpoint_rpm_axis")
    n = len(tune.journal)
    target = before.copy()
    target[0, 1] = target[0, 0]
    with pytest.raises(EditRejected, match="strictly increasing"):
        apply_op(tune, "put_setpoint_rpm_axis", EditOp.SET, array=target)
    assert np.array_equal(tune.values("put_setpoint_rpm_axis"), before)
    assert len(tune.journal) == n


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


# --------------------------------------------------------------------------- #
# the source-value ghost (U6): what the imported bin held, before this session
# --------------------------------------------------------------------------- #
@requires_base
def test_table_detail_carries_the_imported_bin_s_values() -> None:
    """The ghost a curve editor draws behind a working draft.

    It must track the *source*, not the last-applied values — otherwise the
    reference moves every time an edit lands and stops being a reference.
    """
    tune = _open_base()
    before = table_detail(tune, "pedal_threshold_full_load")
    assert before.source_values == before.values, "nothing edited yet"

    tune.fueling.pedal_threshold(90.0)
    after = table_detail(tune, "pedal_threshold_full_load")

    assert after.values[0][0] != after.source_values[0][0]
    assert after.source_values == before.values, "the ghost stayed at stock"


@requires_base
def test_a_second_edit_does_not_move_the_ghost() -> None:
    tune = _open_base()
    stock = table_detail(tune, "pedal_threshold_full_load").source_values

    tune.fueling.pedal_threshold(90.0)
    tune.fueling.pedal_threshold(80.0)
    assert table_detail(tune, "pedal_threshold_full_load").source_values == stock


@requires_base
def test_the_catalog_does_not_pay_for_a_ghost_it_never_draws() -> None:
    """Listing tables decodes each once; only the detail view decodes twice."""
    tune = _open_base()
    assert all(info.source_values is None for info in catalog(tune))
    assert table_detail(tune, "put_setpoint").source_values is not None


@requires_base
def test_the_ghost_is_absent_rather_than_wrong_when_unavailable() -> None:
    """No pre-edit buffer means no ghost — never a guess at one."""
    tune = _open_base()
    tune._source_snapshot = b""
    assert table_detail(tune, "put_setpoint").source_values is None


@requires_base
def test_every_ghost_reads_through_one_decoder_per_space() -> None:
    """The whole catalog's ghosts cost one copy of the source buffer, not 70.

    ``BinImage`` owns its bytes, so wrapping the snapshot copies the whole bin.
    Building one wrapper per *table* — as this path used to — cost a 4 MB copy
    each, and ``CalFile._views``/``TableView._cal`` form a reference cycle, so
    refcounting never freed them: they accumulated until a cyclic-GC pass
    happened to run. On a tablet that is the difference between 4 MB and ~300.
    """
    tune = _open_base()
    decoder = tune.source_space()
    assert decoder is not None
    for name in tune.space().tables.names():
        assert table_detail(tune, name).source_values is not None
    assert tune.source_space() is decoder, "a second decoder was built"


@requires_base
def test_the_ghost_decodes_to_what_an_independent_read_of_the_source_finds() -> None:
    """The shared decoder must not change the answer, only the cost of it."""
    from simoscal.binimage import BinImage
    from simoscal.calfile import CalFile

    tune = _open_base()
    space = tune.space()
    model = space.cal.model
    for name in ("put_setpoint", "pedal_threshold_full_load"):
        independent = CalFile(
            model,
            BinImage(tune.source_snapshot,
                     region_start=model.region_start, region_size=model.region_size),
            structure=space.cal.structure,
        ).get(space.tables[name].spec.key).values
        ghost = np.asarray(table_detail(tune, name).source_values, dtype=float)
        assert np.allclose(ghost.ravel(), np.asarray(independent, dtype=float).ravel())


@requires_base
def test_replacing_the_snapshot_replaces_the_ghost_rather_than_outliving_it() -> None:
    """A cached decoder must never answer for a snapshot that is no longer there."""
    tune = _open_base()
    assert table_detail(tune, "put_setpoint").source_values is not None
    tune._source_snapshot = b""
    assert tune.source_space() is None
    assert table_detail(tune, "put_setpoint").source_values is None


@requires_patch
def test_each_table_space_gets_its_own_ghost_decoder_over_the_one_image() -> None:
    """Spaces share the live buffer, so they share the ghost image — not the model."""
    tune = _open_patched()
    base, patch = tune.source_space(), tune.source_space(PATCH_SPACE)
    assert base is not None and patch is not None and base is not patch
    assert base.binimage is patch.binimage
    patch_table = next(iter(tune.space(PATCH_SPACE).tables.names()))
    assert table_detail(tune, patch_table, space=PATCH_SPACE).source_values is not None
