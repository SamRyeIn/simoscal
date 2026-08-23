"""Tests for the fueling and ignition domains and the basics-SOP bridge (U4).

The headline test is :func:`test_new_api_reproduces_the_whole_r03_calibration`:
the R00–R03 calibration, re-declared in the new API, must produce the same
values in every table the frozen R03 revision wrote. Everything else here is
either a guard that protects that result or a fail-loud path.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from simoscal import CalFile
from simoscal.sop_recipe import OUTCOME_APPLIED, RecipeReport, TableOutcome
from simoscal.tune import SC8S50, BuildFailed, Tune, build
from simoscal.tune.journal import KIND_AXIS, KIND_SOP, VERDICT_BLOCKED
from simoscal.tune.sop_bridge import SopBridgeError
from simoscal.checksum import SC8S50_STRUCTURE

# The R00/R03 lambda declaration, verbatim from TUNE_Basics_Guide_R03.py.
LAMBDA_X = (1504, 2016, 2496, 3008, 3488, 4000, 4512, 4992, 5504, 5984, 6496, 7008)
LAMBDA_Y = (150.00, 299.99, 500.01, 700.00, 899.99, 1100.01, 1200.01, 1389.00)
LAMBDA_CELLS = (
    (1.00, 1.00, 1.00, 1.00, 1.00, 1.00, 1.00, 1.00, 1.00, 1.00, 1.00, 1.00),
    (1.00, 1.00, 1.00, 1.00, 1.00, 1.00, 1.00, 1.00, 1.00, 1.00, 1.00, 1.00),
    (1.00, 1.00, 1.00, 1.00, 1.00, 1.00, 1.00, 1.00, 0.92, 0.89, 0.87, 0.87),
    (1.00, 1.00, 1.00, 1.00, 1.00, 1.00, 0.95, 0.92, 0.89, 0.87, 0.85, 0.85),
    (1.00, 1.00, 1.00, 1.00, 0.97, 0.95, 0.92, 0.88, 0.86, 0.84, 0.82, 0.82),
    (1.00, 1.00, 1.00, 1.00, 0.95, 0.92, 0.88, 0.84, 0.83, 0.81, 0.80, 0.80),
    (1.00, 1.00, 1.00, 0.98, 0.93, 0.89, 0.87, 0.82, 0.80, 0.80, 0.80, 0.80),
    (1.00, 1.00, 0.98, 0.95, 0.90, 0.86, 0.84, 0.82, 0.80, 0.80, 0.80, 0.80),
)
# R04's timing overlay, verbatim, keyed (rpm, load mg/stk) -> absolute °CRK.
R04_TIMING = {
    (3500.0, 1400.0): -9.00, (4000.0, 1400.0): -6.75, (5000.0, 1400.0): -2.25,
    (5000.0, 1200.0): -2.25, (5500.0, 1200.0): -0.75, (6000.0, 1049.97): 1.875,
    (6500.0, 1049.97): 3.375,
}


@pytest.fixture
def tune(real_xdf: Path, real_bin: Path) -> Tune:
    return Tune.open(SC8S50, xdf=real_xdf, bin=real_bin)


def _declare_r03(tune: Tune) -> None:
    """The complete R00–R03 calibration, in the new API."""
    tune.fueling.rebreakpoint_lambda_axes(rpm=LAMBDA_X, load=LAMBDA_Y)
    tune.fueling.lambda_grid(LAMBDA_CELLS, rpm_keys=LAMBDA_X, load_keys=LAMBDA_Y)
    tune.apply_basics_sop()
    tune.fueling.pedal_threshold(72.0)
    tune.boost.manifold_pressure_max(350000.0)
    tune.limits.intake_air_max(2000)
    tune.limits.torque_reference_max(1000)
    tune.limits.airmass_cap_mg(2000)
    tune.fueling.lambda_floors(0.80)


# --------------------------------------------------------------------------- #
# fueling
# --------------------------------------------------------------------------- #
def test_rebreakpoint_writes_both_shared_axes(tune: Tune) -> None:
    entries = tune.fueling.rebreakpoint_lambda_axes(rpm=LAMBDA_X, load=LAMBDA_Y)

    assert len(entries) == 2
    assert all(e.kind == KIND_AXIS for e in entries)
    assert np.allclose(tune.values("lambda_rpm_axis").ravel(), LAMBDA_X, atol=1.0)
    assert np.allclose(tune.values("lambda_load_axis").ravel(), LAMBDA_Y, atol=1.0)


def test_rebreakpoint_requires_increasing_breakpoints(tune: Tune) -> None:
    bad = list(LAMBDA_X)
    bad[5] = bad[4]
    with pytest.raises(ValueError, match="strictly increase"):
        tune.fueling.rebreakpoint_lambda_axes(rpm=bad, load=LAMBDA_Y)


def test_lambda_grid_refuses_axes_it_was_not_authored_for(tune: Tune) -> None:
    """The lean-risk guard: the guide's cells against stock breakpoints.

    Without the re-breakpoint, the guide's grid would put full-load enrichment
    at the wrong loads — invisible in the resulting table, and lean where it
    matters most.
    """
    with pytest.raises(ValueError, match="do not match"):
        tune.fueling.lambda_grid(
            LAMBDA_CELLS, rpm_keys=LAMBDA_X, load_keys=LAMBDA_Y
        )


def test_lambda_grid_writes_once_the_axes_match(tune: Tune) -> None:
    tune.fueling.rebreakpoint_lambda_axes(rpm=LAMBDA_X, load=LAMBDA_Y)
    (entry,) = tune.fueling.lambda_grid(
        LAMBDA_CELLS, rpm_keys=LAMBDA_X, load_keys=LAMBDA_Y
    )

    assert np.allclose(tune.values("lambda_basic"), LAMBDA_CELLS, atol=5e-3)
    assert "richest cell 0.80" in entry.detail


def test_lambda_grid_can_write_the_whole_family(tune: Tune) -> None:
    tune.fueling.rebreakpoint_lambda_axes(rpm=LAMBDA_X, load=LAMBDA_Y)
    entries = tune.fueling.lambda_grid(
        LAMBDA_CELLS, tables=tune.fueling.FAMILY
    )

    assert len(entries) == 3
    for name in tune.fueling.FAMILY:
        assert np.allclose(tune.values(name), LAMBDA_CELLS, atol=5e-3)


def test_lambda_grid_rejects_a_wrong_shaped_grid(tune: Tune) -> None:
    with pytest.raises(ValueError, match="expected shape"):
        tune.fueling.lambda_grid([[1.0, 1.0], [1.0, 1.0]])


def test_lambda_floors_and_pedal_threshold(tune: Tune) -> None:
    floors = tune.fueling.lambda_floors(0.80)
    pedal = tune.fueling.pedal_threshold(72.0)

    assert len(floors) == 3
    for name in ("lambda_setpoint_min", "lambda_catalyst_min", "lambda_turbo_min"):
        assert np.allclose(tune.values(name), 0.80, atol=5e-3)
    assert np.allclose(tune.values("pedal_threshold_full_load"), 72.0, atol=5e-2)
    assert pedal.units == "%"


# --------------------------------------------------------------------------- #
# ignition
# --------------------------------------------------------------------------- #
def test_retard_cells_writes_all_nine_cam_grids(tune: Tune) -> None:
    """Timing pulled from only some cam positions leaves the cell reachable."""
    entries = tune.ignition.retard_cells(R04_TIMING)

    assert len(entries) == 9
    for entry in entries:
        assert entry.verdict == "applied"


def test_retard_cells_lands_on_the_intended_cells(tune: Tune) -> None:
    tune.ignition.retard_cells({(3500.0, 1400.0): -9.00})
    name = "ignition_base_vvl0_i0_e0"
    values = tune.values(name)
    x_axis, y_axis = tune.axis(name, "x"), tune.axis(name, "y")

    col = int(np.argmin(np.abs(x_axis - 3500.0)))
    row = int(np.argmin(np.abs(y_axis - 1400.0)))
    assert values[row, col] == pytest.approx(-9.00, abs=5e-2)


def test_retard_cells_records_the_resolved_breakpoints(tune: Tune) -> None:
    """A point that snapped to a neighbouring breakpoint must be visible."""
    (entry, *_rest) = tune.ignition.retard_cells({(3510.0, 1399.0): -9.00})

    assert "rpm" in entry.detail and "mg/stk" in entry.detail
    assert "→ -9.00" in entry.detail


def test_offset_cells_is_relative_to_what_is_there(tune: Tune) -> None:
    name = "ignition_base_vvl0_i0_e0"
    x_axis, y_axis = tune.axis(name, "x"), tune.axis(name, "y")
    col = int(np.argmin(np.abs(x_axis - 3500.0)))
    row = int(np.argmin(np.abs(y_axis - 1400.0)))
    before = tune.values(name)[row, col]

    tune.ignition.offset_cells({(3500.0, 1400.0): -3.0})

    assert tune.values(name)[row, col] == pytest.approx(before - 3.0, abs=5e-2)


def test_ignition_rejects_an_empty_target_map(tune: Tune) -> None:
    with pytest.raises(ValueError, match="no targets given"):
        tune.ignition.retard_cells({})
    with pytest.raises(ValueError, match="no deltas given"):
        tune.ignition.offset_cells({})


# --------------------------------------------------------------------------- #
# the SOP bridge
# --------------------------------------------------------------------------- #
def test_sop_bridge_attributes_every_byte_it_changed(tune: Tune) -> None:
    """The bridge's contract: no recipe byte goes unaccounted for."""
    before = tune.space("base").cal.binimage.to_bytes()
    entries = tune.apply_basics_sop()
    after = tune.space("base").cal.binimage.to_bytes()

    changed = {i for i, (a, b) in enumerate(zip(before, after)) if a != b}
    journaled = set().union(*(e.offsets for e in entries)) if entries else set()
    assert changed == journaled
    assert changed  # the recipe really did write something


def test_sop_bridge_journals_one_entry_per_outcome(tune: Tune) -> None:
    entries = tune.apply_basics_sop()

    assert tune.recipe_report is not None
    assert len(entries) == len(tune.recipe_report.outcomes)
    assert all(e.kind == KIND_SOP for e in entries)
    # Skips are journaled too — a deliberate non-change is part of the story.
    assert any(e.verdict == "skipped" for e in entries)


def test_sop_entries_carry_real_table_values_not_report_scalars(tune: Tune) -> None:
    """The recipe reports a min and a target for a multi-cell ceiling.

    Taken as a table those would be the wrong shape, and the readback gate
    would reject them, so the bridge reads the real before/after instead.
    """
    cal = tune.space("base").cal
    entries = tune.apply_basics_sop()
    multi = [
        e for e in entries
        if e.after is not None and e.after.size > 1 and e.offsets
    ]
    assert multi, "expected the recipe to write at least one multi-cell table"
    for entry in multi:
        assert entry.after.shape == cal.get(entry.key).shape


def test_the_recipe_alone_on_a_stock_bin_is_do_not_flash(
    tune: Tune, tmp_path: Path
) -> None:
    """Why R00 re-breakpoints the lambda axes before running the recipe.

    On stock breakpoints the guide's lambda grid is an axis mismatch, so the
    recipe applies the boost curve and skips the matching enrichment. The
    coherence rule catches exactly that, and the build must refuse.
    """
    tune.apply_basics_sop()

    with pytest.raises(BuildFailed, match="coherence") as excinfo:
        build(tune, "R00", out_root=tmp_path, plots=False)

    assert any("LEAN RISK" in p for p in excinfo.value.problems)


def test_a_recipe_guard_block_does_not_fail_the_build(
    tune: Tune, tmp_path: Path
) -> None:
    """The SOP is a bulk pass; its guard hits are expected and superseded.

    A guard rejecting a call the *author* wrote by hand is a different thing,
    and still fails the build — that is covered in test_tune_build.py.
    """
    # Re-breakpoint first, so the recipe's lambda write lands and coherence
    # passes; otherwise the build fails for that reason instead.
    tune.fueling.rebreakpoint_lambda_axes(rpm=LAMBDA_X, load=LAMBDA_Y)
    tune.fueling.lambda_grid(LAMBDA_CELLS, rpm_keys=LAMBDA_X, load_keys=LAMBDA_Y)
    entries = tune.apply_basics_sop()

    assert any(e.verdict == VERDICT_BLOCKED for e in entries)
    assert tune.journal.blocked() == ()  # recipe blocks are excluded from the gate

    result = build(tune, "R00", out_root=tmp_path, plots=False)
    assert result.ok


def test_an_authored_block_still_fails_even_alongside_the_recipe(
    tune: Tune, tmp_path: Path
) -> None:
    tune.apply_basics_sop()
    tune.limits.raise_ceiling("torque_reference_max", 99999.0)  # over the XDF max

    with pytest.raises(BuildFailed, match="guard blocked"):
        build(tune, "R00", out_root=tmp_path, plots=False)


def test_coherence_do_not_flash_fails_the_build(
    tune: Tune, tmp_path: Path
) -> None:
    """A boost change shipped without matching fuelling stops the build."""
    tune.recipe_report = RecipeReport((
        TableOutcome("IP_PUT_SP", "Boost — Option 2", OUTCOME_APPLIED),
    ))

    with pytest.raises(BuildFailed, match="coherence") as excinfo:
        build(tune, "R00", out_root=tmp_path, plots=False)

    assert any("LEAN RISK" in p for p in excinfo.value.problems)


def test_sop_bridge_raises_on_an_unreported_write(
    tune: Tune, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fault injection: a recipe that writes more than it reports."""
    import simoscal.sop_recipe as recipe_mod

    real = recipe_mod.apply_basics_sop

    def write_extra(cal, *args, **kwargs):
        report = real(cal, *args, **kwargs)
        # IP_LAMB_BAS[1] is deliberately outside the recipe's symbol map — R03
        # writes it separately precisely because the recipe does not.
        smuggled = cal.get("IP_LAMB_BAS[1]")
        smuggled.set(np.full(smuggled.shape, 0.95))
        return report

    monkeypatch.setattr(recipe_mod, "apply_basics_sop", write_extra)

    with pytest.raises(SopBridgeError, match="none of its"):
        tune.apply_basics_sop()


# --------------------------------------------------------------------------- #
# Equivalence with the frozen R03 revision
# --------------------------------------------------------------------------- #
R03_TABLES = (
    "IP_LAMB_BAS[1]", "IP_LAMB_BAS_HPDI[1]", "IP_LAMB_BAS_MPI[1]",
    "ldpm_n_32_1_lasp", "ldpm_maf_1_lasp",
    "C_LAMB_BAS_COR_MIN", "IP_LAMB_COP_MIN", "IP_LAMB_TUR_OHP_MIN",
    "ID_PV_AV_FL", "C_PRS_IM_SP_MAX", "C_M_AIR_CYL_SP_MAX",
    "IP_M_AIR_CYL_MAX_STND_VVL[STND]", "IP_M_AIR_CYL_MAX_STND_VVL[LFT_1]",
    "IP_TQI_REF_MAX_MON", "IP_PQ_CHA_MAX",
)


def test_new_api_reproduces_the_whole_r03_calibration(
    tune: Tune, tmp_path: Path, real_xdf: Path
) -> None:
    """The R00–R03 calibration re-declared in the new API, table for table."""
    reference = (
        Path(real_xdf).parents[2] / "Tunes" / "TuningBasicsGuide"
        / "TUNE_Basics_Guide_out" / "R03_20260708-132742"
        / "5G0906259L_0002_BasicsGuide_R03.bin"
    )
    if not reference.is_file():
        pytest.skip(f"frozen R03 bin absent: {reference}")

    _declare_r03(tune)
    result = build(tune, "R03", out_root=tmp_path, plots=False)

    assert result.ok
    built = CalFile.open(str(real_xdf), str(result.bin_path), structure=SC8S50_STRUCTURE)
    frozen = CalFile.open(str(real_xdf), str(reference), structure=SC8S50_STRUCTURE)
    for symbol in R03_TABLES:
        assert np.allclose(
            built.get(symbol).values, frozen.get(symbol).values, rtol=0, atol=1e-9
        ), symbol
