"""Tests for the switch-patch domain module (U5).

Everything here needs a *patched* bin, so a session-scoped fixture applies the
three real ``.btp`` patches once and the tests reopen that file. They skip
cleanly without the vendored BinToolz tree or the real bin.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from simoscal.tune import (
    SC8S50,
    SWITCH_PATCH_2933,
    BuildFailed,
    PatchSpec,
    Tune,
    build,
)
from simoscal.tune.domains.switchpatch import PATCH_SPACE
from simoscal.tune.editing import EditRejected, apply_op
from simoscal.tune.profiles.switchpatch_2933 import (
    LAMBDA_DEFAULT_OFFSET,
    SLOT_DEFAULT_HPA,
    SLOT_SETTINGS,
    SLOTS,
    SPARK_DEFAULT_DEGREES,
)
from simoscal.tune.units import hpa_from_psi
from tests.conftest import requires_bintoolz

# The R11 shared rpm axis and slot-1 curve, for realistic declarations.
R11_AXIS = [3000, 3200, 3400, 3800, 4400, 4700, 5000, 5400, 5750, 6000, 6250, 6500]
R11_SLOT1 = [2699, 2699, 2500, 2350, 2350, 2320, 2299, 2260, 2299, 2250, 2225, 2199]

pytestmark = requires_bintoolz


def _patch_specs(bintoolz_root: Path) -> list[PatchSpec]:
    patches = bintoolz_root / "patches"
    return [
        PatchSpec("SL CBRICK v1.2 - S50",
                  patches / "SL CBRICK v1.2 - S50.btp", "anti-brick patch"),
        PatchSpec("SL HSL v1.1 - S50",
                  patches / "SL HSL v1.1 - S50.btp", "high-speed logging"),
        PatchSpec("SL PATCH.29.33 - S50",
                  patches / "SL PATCH.29.33 - S50.btp", "5-slot map switch"),
    ]


@pytest.fixture(scope="session")
def patched_bin(
    bintoolz_root: Path, real_xdf: Path, real_bin: Path,
    switch_patch_xdf: Path, tmp_path_factory: pytest.TempPathFactory,
) -> Path:
    """The stock bin with the three real patches applied, built once."""
    out = tmp_path_factory.mktemp("patched")
    tune = Tune.open(
        SC8S50, xdf=real_xdf, bin=real_bin,
        patches=_patch_specs(bintoolz_root),
        extra_spaces={PATCH_SPACE: (SWITCH_PATCH_2933, switch_patch_xdf)},
    )
    path = out / "patched.bin"
    tune.save(path, correct_checksums=True)
    return path


@pytest.fixture
def patched(
    patched_bin: Path, real_xdf: Path, switch_patch_xdf: Path
) -> Tune:
    """A tune over the already-patched bin, with both table spaces."""
    return Tune.open(
        SC8S50, xdf=real_xdf, bin=patched_bin,
        extra_spaces={PATCH_SPACE: (SWITCH_PATCH_2933, switch_patch_xdf)},
    )


# --------------------------------------------------------------------------- #
# Opening a patched tune
# --------------------------------------------------------------------------- #
def test_patches_are_journaled_when_applied(
    bintoolz_root: Path, real_xdf: Path, real_bin: Path, switch_patch_xdf: Path
) -> None:
    tune = Tune.open(
        SC8S50, xdf=real_xdf, bin=real_bin,
        patches=_patch_specs(bintoolz_root),
        extra_spaces={PATCH_SPACE: (SWITCH_PATCH_2933, switch_patch_xdf)},
    )

    assert len(tune.journal) == 3
    assert len(tune.patch_results) == 3
    assert all(r.confined for r in tune.patch_results)
    assert "5-slot map switch" in tune.journal.entries[2].label


def test_both_spaces_share_one_buffer(patched: Tune) -> None:
    """Editing through either XDF must be visible in the one saved file."""
    assert (
        patched.space("base").cal.binimage
        is patched.space(PATCH_SPACE).cal.binimage
    )


def test_slots_start_at_the_patch_default(patched: Tune) -> None:
    for slot in SLOTS:
        values = patched.values(f"slot{slot}_put_setpoint", space=PATCH_SPACE)
        assert np.allclose(values, SLOT_DEFAULT_HPA, atol=1.0)


# --------------------------------------------------------------------------- #
# slot caps (AE2)
# --------------------------------------------------------------------------- #
def test_psi_cap_floors_and_tiles_across_all_rows(patched: Tune) -> None:
    """AE2: a 10 psi cap must encode below 10 psi, on all eight rows."""
    entry = patched.switchpatch.slot_curve(5, psi=10.0)

    values = patched.values("slot5_put_setpoint", space=PATCH_SPACE)
    assert values.shape == (8, 12)
    assert np.allclose(values, hpa_from_psi(10.0), atol=1.0)   # 1705, not 1706
    assert np.allclose(values, values[0])                      # every row tiled
    assert "psi gauge" in entry.detail


def test_a_per_rpm_curve_is_written_across_the_axis(patched: Tune) -> None:
    # Park the base ceiling first, as R11 does — otherwise the stock 2506 hPa
    # full-load row sits below this curve and the ceiling guard refuses it.
    patched.boost.put_ceiling_psi(30.0, rounding="nearest")
    patched.switchpatch.slot_curve(1, hpa=R11_SLOT1)
    values = patched.values("slot1_put_setpoint", space=PATCH_SPACE)

    assert np.allclose(values[0], R11_SLOT1, atol=2.0)
    for row in values:
        assert np.allclose(row, values[0])


def test_editing_one_slot_leaves_the_others_untouched(patched: Tune) -> None:
    before = {
        s: patched.values(f"slot{s}_put_setpoint", space=PATCH_SPACE)
        for s in SLOTS
    }
    patched.switchpatch.slot_curve(5, psi=10.0)

    for slot in (1, 2, 3, 4):
        assert np.array_equal(
            patched.values(f"slot{slot}_put_setpoint", space=PATCH_SPACE),
            before[slot],
        ), slot


def test_a_slot_at_or_above_the_base_ceiling_fails_loud(patched: Tune) -> None:
    """Above the base ceiling the min() semantics make the slot meaningless."""
    patched.boost.put_ceiling_hpa(2500.0)

    with pytest.raises(ValueError, match="min\\(\\) semantics"):
        patched.switchpatch.slot_curve(3, hpa=2600.0)


def test_a_slot_below_ambient_fails_loud(patched: Tune) -> None:
    with pytest.raises(ValueError, match="above ambient"):
        patched.switchpatch.slot_curve(3, hpa=500.0)


def test_slot_curve_wants_exactly_one_of_psi_or_hpa(patched: Tune) -> None:
    with pytest.raises(ValueError, match="exactly one of"):
        patched.switchpatch.slot_curve(3, psi=10.0, hpa=1705.0)
    with pytest.raises(ValueError, match="exactly one of"):
        patched.switchpatch.slot_curve(3)


def test_slot_curve_rejects_a_wrong_length_curve(patched: Tune) -> None:
    with pytest.raises(ValueError, match="expected shape"):
        patched.switchpatch.slot_curve(3, hpa=[2000.0, 2100.0])


def test_an_unknown_slot_fails_loud(patched: Tune) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        patched.switchpatch.slot_curve(6, psi=10.0)


def test_require_as_patched_catches_a_non_default_base(patched: Tune) -> None:
    patched.switchpatch.slot_curve(2, psi=15.0)

    with pytest.raises(ValueError, match="not a freshly patched base"):
        patched.switchpatch.slot_curve(2, psi=16.0, require_as_patched=True)


def test_slot_rpm_axis_is_shared_and_must_increase(patched: Tune) -> None:
    patched.switchpatch.slot_rpm_axis(R11_AXIS)
    assert np.allclose(
        patched.values("slot_put_rpm_axis", space=PATCH_SPACE).ravel(),
        R11_AXIS, atol=1.0,
    )

    with pytest.raises(ValueError, match="strictly increase"):
        patched.switchpatch.slot_rpm_axis(sorted(R11_AXIS, reverse=True))


# --------------------------------------------------------------------------- #
# traction control
# --------------------------------------------------------------------------- #
def test_traction_control_sets_both_flags_on_every_slot(patched: Tune) -> None:
    entries = patched.switchpatch.traction_control()

    assert len(entries) == 10  # 5 slots × 2 flags
    for slot in SLOTS:
        for flag in ("enable_sl_tc", "disable_oem_tc"):
            value = patched.values(f"slot{slot}_{flag}", space=PATCH_SPACE)
            assert value.ravel()[0] == pytest.approx(1.0)


def test_traction_control_can_leave_a_slot_on_factory_tc(patched: Tune) -> None:
    """A deliberate 'safe' slot is a supported choice, not an oversight."""
    patched.switchpatch.traction_control(slots=(1, 2, 3, 4))

    for flag in ("enable_sl_tc", "disable_oem_tc"):
        assert patched.values(f"slot5_{flag}", space=PATCH_SPACE).ravel()[0] == 0.0
        assert patched.values(f"slot1_{flag}", space=PATCH_SPACE).ravel()[0] == 1.0


def test_traction_control_can_be_turned_off(patched: Tune) -> None:
    patched.switchpatch.traction_control()
    patched.switchpatch.traction_control(enable=False)

    for slot in SLOTS:
        assert patched.values(
            f"slot{slot}_enable_sl_tc", space=PATCH_SPACE
        ).ravel()[0] == 0.0


# --------------------------------------------------------------------------- #
# the per-slot switchboard
# --------------------------------------------------------------------------- #
def test_slot_settings_reads_every_scalar_against_every_slot(patched: Tune) -> None:
    settings = patched.switchpatch.slot_settings()

    assert len(settings) == len(SLOT_SETTINGS)
    for row in settings:
        assert len(row["values"]) == len(SLOTS)
        assert row["slots"] == list(SLOTS)
        # Every row is describable: the app renders these verbatim, and a blank
        # here would be an unlabelled switch on a screen full of switches.
        assert row["title"] and row["description"]


def test_slot_settings_marks_the_uncharacterised_ones_read_only(patched: Tune) -> None:
    by_key = {row["key"]: row for row in patched.switchpatch.slot_settings()}

    for key in ("rpm_limiter", "speed_limiter", "manual_afu", "gauge_settings"):
        assert by_key[key]["writable"] is False
        # The reason travels with the refusal — a row that will not toggle and
        # does not say why reads as a bug.
        assert by_key[key]["readonly"]
    assert by_key["enable_sl_tc"]["writable"] is True
    assert by_key["enable_sl_tc"]["readonly"] == ""


def test_a_flag_is_set_only_on_the_slots_named(patched: Tune) -> None:
    patched.switchpatch.set_slot_flag("enable_lc", slots=(2, 4), on=True)

    by_key = {row["key"]: row for row in patched.switchpatch.slot_settings()}
    assert by_key["enable_lc"]["values"] == [0.0, 1.0, 0.0, 1.0, 0.0]


def test_a_flag_can_be_turned_back_off(patched: Tune) -> None:
    patched.switchpatch.set_slot_flag("pops_enable", on=True)
    patched.switchpatch.set_slot_flag("pops_enable", slots=(3,), on=False)

    by_key = {row["key"]: row for row in patched.switchpatch.slot_settings()}
    assert by_key["pops_enable"]["values"] == [1.0, 1.0, 0.0, 1.0, 1.0]


def test_an_unknown_setting_is_refused_and_names_the_real_ones(patched: Tune) -> None:
    # A typo that wrote nothing would be indistinguishable from a flag the patch
    # ignores, so it fails loud and lists what it does have.
    with pytest.raises(ValueError, match="no per-slot setting"):
        patched.switchpatch.set_slot_flag("enable_launch_control", on=True)


@pytest.mark.parametrize(
    "key", ["rpm_limiter", "speed_limiter", "manual_afu", "gauge_settings"]
)
def test_a_read_only_setting_cannot_be_toggled(patched: Tune, key: str) -> None:
    """The four we can read and describe but have no business writing.

    ``Manual AFU`` is the sharpest of them: it is a 0–1 fraction stored ``/128``,
    so a "toggle" would write 128× what a caller meant.
    """
    with pytest.raises(ValueError, match="read-only"):
        patched.switchpatch.set_slot_flag(key, on=True)

    assert patched.values(f"slot1_{key}", space=PATCH_SPACE).ravel()[0] == 0.0


def test_a_byte_that_is_not_a_flag_is_never_overwritten(patched: Tune) -> None:
    """The last line of defence if a binding is ever wrong.

    Half these tables sit within a few bytes of each other, so a mis-bound
    uniqueid lands on a neighbour that holds something else entirely. Writing a
    0/1 over it would destroy that value silently; reading it first does not.
    """
    patched.write("slot2_enable_nls", [[1.0]], space=PATCH_SPACE, intent="setup")
    view = patched.table("slot2_enable_nls", space=PATCH_SPACE).view
    view.set_raw([[7]])

    with pytest.raises(ValueError, match="expected the 0/1 of a flag"):
        patched.switchpatch.set_slot_flag("enable_nls", slots=(2,), on=True)


def test_every_switchboard_setting_is_mapped_and_owned(patched: Tune) -> None:
    """The registry and the profile cannot drift apart.

    They are generated from one source precisely so a setting cannot be
    toggleable in the app and unmapped in the profile — this asserts the
    generation actually happened for all five slots.
    """
    for setting in SLOT_SETTINGS:
        for slot in SLOTS:
            resolved = patched.table(f"slot{slot}_{setting.key}", space=PATCH_SPACE)
            assert resolved.spec.owner, f"slot{slot}_{setting.key} is unowned"
            assert tuple(resolved.view.shape) == (1, 1)


# --------------------------------------------------------------------------- #
# gates
# --------------------------------------------------------------------------- #
def test_sanity_gate_passes_on_a_properly_patched_bin(
    patched: Tune, tmp_path: Path, real_bin: Path
) -> None:
    patched.switchpatch.slot_curve(5, psi=10.0)
    patched.switchpatch.require_sanity(stock_bin=real_bin)

    result = build(patched, "R12ish", out_root=tmp_path, plots=False)

    assert result.ok
    report = result.report_path.read_text()
    assert "switch-patch sanity" in report
    assert "PASS" in report


def test_sanity_gate_fails_the_build_on_an_unpatched_bin(
    real_xdf: Path, real_bin: Path, switch_patch_xdf: Path, tmp_path: Path
) -> None:
    """The stock bin has no switch patch, so the gate must refuse it."""
    tune = Tune.open(
        SC8S50, xdf=real_xdf, bin=real_bin,
        extra_spaces={PATCH_SPACE: (SWITCH_PATCH_2933, switch_patch_xdf)},
    )
    tune.switchpatch.require_sanity(stock_bin=real_bin)

    with pytest.raises(BuildFailed, match="switch-patch sanity"):
        build(tune, "R00", out_root=tmp_path, plots=False)


def test_the_full_patched_build_passes_every_gate(
    patched: Tune, tmp_path: Path, real_bin: Path
) -> None:
    """An R11/R12-shaped declaration, start to finish, in one build()."""
    patched.boost.put_ceiling_psi(30.0, rounding="nearest")
    patched.switchpatch.slot_rpm_axis(R11_AXIS)
    patched.switchpatch.slot_curve(1, hpa=R11_SLOT1, require_as_patched=True)
    patched.switchpatch.slot_curve(5, psi=10.0, require_as_patched=True)
    patched.switchpatch.traction_control()
    patched.switchpatch.require_sanity(stock_bin=real_bin)

    result = build(patched, "R12ish", out_root=tmp_path, bin_name="out.bin",
                   reference_bin=patched.source_bin, plots=False)

    assert result.ok
    assert result.checksums_clean
    assert result.readback_failures == ()
    assert result.diff is not None and result.diff.clean
    # 1 base ceiling + axis + 2 slot grids + 10 TC flags = 14 tables read back.
    assert len(result.journal.tables_touched()) == 14


# --------------------------------------------------------------------------- #
# Per-slot timing — the Spark modifier grid
# --------------------------------------------------------------------------- #
#: The R20 shape: the two top airmass rows, written identically, over the eight
#: rpm breakpoints above 3000. Every other cell of the 16x16 stays neutral.
R20_RPM = [3000, 3500, 4000, 4500, 5000, 5500, 6000, 6500]
#: The brainstorm's shape on the grid's 0.375 deg lattice. The raw figures
#: (1.00, 2.00, 2.75, 3.50) are not storable and the domain refuses them rather
#: than rounding; these are the round-*up* neighbours, chosen so the deliberate
#: half-octane-credit margin is preserved rather than eroded.
R20_OFFSETS = [1.125, 1.500, 2.250, 3.000, 3.750, 2.250, 1.500, 1.125]
R20_ROWS = {1200: R20_OFFSETS, 1400: R20_OFFSETS}

#: Comfortably above the +4.38 the R20 map actually delivers, so the guard is
#: not the thing under test except where a test means it to be.
GENEROUS_CEILING = 8.0


def test_spark_modifiers_start_neutral(patched: Tune) -> None:
    """Neutral is a decoded 0.00 degrees, not a raw zero — the additive proof."""
    for slot in SLOTS:
        values = patched.values(f"slot{slot}_spark_modifier", space=PATCH_SPACE)
        assert values.shape == (16, 16)
        assert np.allclose(values, SPARK_DEFAULT_DEGREES, atol=1e-6)


def test_slot_spark_map_writes_only_the_named_cells(patched: Tune) -> None:
    patched.switchpatch.slot_spark_map(
        5, rpm=R20_RPM, rows=R20_ROWS,
        max_delivered_degrees=GENEROUS_CEILING,
        intent="slot 5 booster timing",
    )
    grid = patched.values("slot5_spark_modifier", space=PATCH_SPACE)

    written = ~np.isclose(grid, SPARK_DEFAULT_DEGREES, atol=1e-6)
    assert written.sum() == 16, "16 of 256 cells, as declared"
    assert np.allclose(grid[15][8:], R20_OFFSETS)
    assert np.allclose(grid[14][8:], R20_OFFSETS)
    # Every other slot is untouched: this is the point of a per-slot grid.
    for slot in (1, 2, 3, 4):
        other = patched.values(f"slot{slot}_spark_modifier", space=PATCH_SPACE)
        assert np.allclose(other, SPARK_DEFAULT_DEGREES, atol=1e-6)


def test_slot_spark_map_leaves_the_shared_base_timing_alone(patched: Tune) -> None:
    """The invariant the whole R20 approach rests on."""
    before = patched.values("ignition_base_vvl0_i0_e0")
    patched.switchpatch.slot_spark_map(
        5, rpm=R20_RPM, rows=R20_ROWS, max_delivered_degrees=GENEROUS_CEILING,
    )
    assert np.array_equal(before, patched.values("ignition_base_vvl0_i0_e0"))


def test_delivered_timing_ceiling_counts_the_base_not_the_offset(
    patched: Tune,
) -> None:
    """A modest offset onto an advanced cell is not a modest amount of timing."""
    base = patched.values("ignition_base_vvl0_i0_e0")
    # Both rows this writes have to clear the ceiling, so the binding cell is
    # whichever of the two carries more base advance at 6500 rpm.
    highest_base = max(float(base[14][15]), float(base[15][15]))
    offset = 1.875
    # An offset well under 2 degrees, refused against a ceiling well over it,
    # because the cell it lands on already carries base advance.
    with pytest.raises(ValueError, match="above the declared ceiling"):
        patched.switchpatch.slot_spark_map(
            5, rpm=[6500], rows={1200: [offset], 1400: [offset]},
            max_delivered_degrees=highest_base + offset - 0.5,
        )
    # The same offset passes once the ceiling admits the delivered figure.
    patched.switchpatch.slot_spark_map(
        5, rpm=[6500], rows={1200: [offset], 1400: [offset]},
        max_delivered_degrees=highest_base + offset + 0.001,
    )


def test_the_ceiling_refuses_rather_than_passes_without_a_base_map(
    patched: Tune,
) -> None:
    with pytest.raises(ValueError, match="cannot read the base ignition map"):
        patched.switchpatch.slot_spark_map(
            5, rpm=R20_RPM, rows=R20_ROWS,
            max_delivered_degrees=GENEROUS_CEILING,
            base_map="no_such_ignition_map",
        )


def test_the_top_airmass_row_must_match_the_one_below(patched: Tune) -> None:
    """WOT runs past 1400 mg/stk, and only a flat last segment is bounded."""
    with pytest.raises(ValueError, match="only a flat last segment is bounded"):
        patched.switchpatch.slot_spark_map(
            5, rpm=R20_RPM,
            rows={1200: R20_OFFSETS, 1400: [v + 0.375 for v in R20_OFFSETS]},
            max_delivered_degrees=GENEROUS_CEILING,
        )
    # Writing the top row alone trips it too — the row below is still neutral.
    with pytest.raises(ValueError, match="only a flat last segment is bounded"):
        patched.switchpatch.slot_spark_map(
            5, rpm=R20_RPM, rows={1400: R20_OFFSETS},
            max_delivered_degrees=GENEROUS_CEILING,
        )


def test_a_row_or_column_off_the_breakpoints_is_refused_not_snapped(
    patched: Tune,
) -> None:
    with pytest.raises(ValueError, match="4600 rpm is not a rpm breakpoint"):
        patched.switchpatch.slot_spark_map(
            5, rpm=[4600], rows={1200: [0.75], 1400: [0.75]},
            max_delivered_degrees=GENEROUS_CEILING,
        )
    with pytest.raises(ValueError, match="1300 mg/stk is not a airmass breakpoint"):
        patched.switchpatch.slot_spark_map(
            5, rpm=[5000], rows={1300: [0.75]},
            max_delivered_degrees=GENEROUS_CEILING,
        )


def test_the_nominal_breakpoints_are_named_despite_a_quantised_axis(
    patched: Tune,
) -> None:
    """1200 mg/stk decodes to 1200.01, and naming it must still work."""
    airmass = patched.axis("slot5_spark_modifier", "y", space=PATCH_SPACE)
    assert not np.array_equal(airmass, np.round(airmass)), "axis really is quantised"
    patched.switchpatch.slot_spark_map(
        5, rpm=[5000], rows={1200: [0.75], 1400: [0.75]},
        max_delivered_degrees=GENEROUS_CEILING,
    )


def test_an_offset_off_the_storage_lattice_is_refused_not_rounded(
    patched: Tune,
) -> None:
    """+1.00 deg would silently become +1.125 -- a round *up*, on advance."""
    with pytest.raises(ValueError, match="do not land on it"):
        patched.switchpatch.slot_spark_map(
            5, rpm=[5000], rows={1200: [1.0], 1400: [1.0]},
            max_delivered_degrees=GENEROUS_CEILING,
        )
    with pytest.raises(ValueError, match=r"nearest storable \+0.750 or \+1.125"):
        patched.switchpatch.slot_spark_map(
            5, rpm=[5000], rows={1200: [1.0], 1400: [1.0]},
            max_delivered_degrees=GENEROUS_CEILING,
        )


def test_what_the_grid_stores_is_what_was_asked_for(patched: Tune) -> None:
    """The point of refusing: no gap between the calibration and the bin."""
    patched.switchpatch.slot_spark_map(
        5, rpm=R20_RPM, rows=R20_ROWS, max_delivered_degrees=GENEROUS_CEILING,
    )
    grid = patched.values("slot5_spark_modifier", space=PATCH_SPACE)
    assert np.allclose(grid[15][8:], R20_OFFSETS, atol=1e-9)
    assert np.allclose(grid[14][8:], R20_OFFSETS, atol=1e-9)


def test_a_row_with_the_wrong_number_of_offsets_is_refused(patched: Tune) -> None:
    with pytest.raises(ValueError, match="one offset per rpm"):
        patched.switchpatch.slot_spark_map(
            5, rpm=R20_RPM, rows={1200: [0.75, 1.5], 1400: R20_OFFSETS},
            max_delivered_degrees=GENEROUS_CEILING,
        )


def test_spark_map_rejects_an_unknown_slot(patched: Tune) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        patched.switchpatch.slot_spark_map(
            6, rpm=R20_RPM, rows=R20_ROWS,
            max_delivered_degrees=GENEROUS_CEILING,
        )


def test_spark_map_require_as_patched_catches_a_written_grid(patched: Tune) -> None:
    patched.switchpatch.slot_spark_map(
        5, rpm=R20_RPM, rows=R20_ROWS, max_delivered_degrees=GENEROUS_CEILING,
    )
    with pytest.raises(ValueError, match="not the untouched base"):
        patched.switchpatch.slot_spark_map(
            5, rpm=R20_RPM, rows=R20_ROWS,
            max_delivered_degrees=GENEROUS_CEILING,
            require_as_patched=True,
        )


def test_a_generic_edit_to_a_spark_grid_is_refused(patched: Tune) -> None:
    """Domain-owned: the generic editor cannot bypass the delivered-timing guard."""
    with pytest.raises(EditRejected) as excinfo:
        apply_op(
            patched, "slot5_spark_modifier", "set",
            space=PATCH_SPACE, value=4.0,
            intent="bypass the domain",
        )
    assert "slot_spark_map" in str(excinfo.value)


# --- per-slot Lambda modifier -------------------------------------------- #
#
# The base lambda grid's own breakpoints, which these grids share. These are the
# **factory** ones: this fixture patches the stock bin, and R00's lambda-axis
# re-breakpoint is a tune edit, not part of the patch. So a test that hard-coded
# the lineage's 1389 mg/stk top row would be asserting against a bin that does
# not exist here. 600 and 1020 are the two top airmass rows, and 1020 is the top
# of the axis, so the flat-top rule applies to it.
LAMBDA_RPM = [3008, 3520]
LAMBDA_ROWS_MG = (600, 1020)
#: Wide enough that the both-sign check is not what a test trips on unless it
#: means to: the base grid runs 0.80 to 1.00 in these cells.
GENEROUS_LAMBDA_RANGE = (0.60, 1.20)


def _lambda_rows(offsets):
    return {mg: list(offsets) for mg in LAMBDA_ROWS_MG}


def test_lambda_modifiers_start_neutral(patched: Tune) -> None:
    """Neutral is a decoded 0.00 lambda — the same additive proof as spark."""
    for slot in SLOTS:
        values = patched.values(f"slot{slot}_lambda_modifier", space=PATCH_SPACE)
        assert values.shape == (8, 12)
        assert np.allclose(values, LAMBDA_DEFAULT_OFFSET, atol=1e-6)


def test_slot_lambda_map_writes_only_the_named_cells(patched: Tune) -> None:
    offsets = [0.01953125, 0.01953125]   # 20 and 20 raw steps of 1/1024
    patched.switchpatch.slot_lambda_map(
        2, rpm=LAMBDA_RPM, rows=_lambda_rows(offsets),
        delivered_lambda_range=GENEROUS_LAMBDA_RANGE,
        intent="hold slot 2 at its prior lambda",
    )
    grid = patched.values("slot2_lambda_modifier", space=PATCH_SPACE)

    written = ~np.isclose(grid, LAMBDA_DEFAULT_OFFSET, atol=1e-9)
    assert written.sum() == 4, "4 of 96 cells, as declared"
    for row in (6, 7):
        assert np.allclose(grid[row][5:7], offsets)
    # Every other slot untouched — the whole point of a per-slot grid.
    for slot in (1, 3, 4, 5):
        other = patched.values(f"slot{slot}_lambda_modifier", space=PATCH_SPACE)
        assert np.allclose(other, LAMBDA_DEFAULT_OFFSET, atol=1e-9)


def test_slot_lambda_map_leaves_the_shared_base_grid_alone(patched: Tune) -> None:
    before = patched.values("lambda_basic_hpdi")
    patched.switchpatch.slot_lambda_map(
        2, rpm=LAMBDA_RPM, rows=_lambda_rows([0.01953125] * 2),
        delivered_lambda_range=GENEROUS_LAMBDA_RANGE,
    )
    assert np.array_equal(before, patched.values("lambda_basic_hpdi"))


def test_the_delivered_lambda_range_is_checked_under_both_signs(
    patched: Tune,
) -> None:
    """The guard that makes a first use of this grid safe at all.

    The sign has never been observed on this car, so an offset is only allowed
    when ``base + offset`` and ``base - offset`` are *both* inside the declared
    range. Here the lean side of the range admits the offset and the rich side
    does not, and the write is refused even though one convention would be fine.
    """
    base = patched.values("lambda_basic_hpdi")
    cell = max(float(base[6][5]), float(base[7][5]))
    offset = 0.05078125   # 52 raw steps
    with pytest.raises(ValueError, match="outside the declared range"):
        patched.switchpatch.slot_lambda_map(
            2, rpm=[3008], rows={600: [offset], 1020: [offset]},
            delivered_lambda_range=(cell - offset / 2.0, cell + offset + 0.01),
        )
    # Widen the rich bound past the other sign and the same write is allowed.
    patched.switchpatch.slot_lambda_map(
        2, rpm=[3008], rows={600: [offset], 1020: [offset]},
        delivered_lambda_range=(cell - offset - 0.01, cell + offset + 0.01),
    )


def test_the_lambda_range_refuses_rather_than_passes_without_a_base_grid(
    patched: Tune,
) -> None:
    with pytest.raises(ValueError, match="cannot read the base lambda grid"):
        patched.switchpatch.slot_lambda_map(
            2, rpm=LAMBDA_RPM, rows=_lambda_rows([0.01953125] * 2),
            delivered_lambda_range=GENEROUS_LAMBDA_RANGE,
            base_grid="no_such_lambda_grid",
        )


def test_a_lambda_range_the_wrong_way_round_is_refused(patched: Tune) -> None:
    with pytest.raises(ValueError, match="rich bound lower"):
        patched.switchpatch.slot_lambda_map(
            2, rpm=LAMBDA_RPM, rows=_lambda_rows([0.0] * 2),
            delivered_lambda_range=(1.20, 0.60),
        )


def test_a_lambda_offset_off_the_storage_lattice_is_refused(
    patched: Tune,
) -> None:
    """The grid stores 1/1024 per raw step; 0.02 is not on it."""
    with pytest.raises(ValueError, match="do not land"):
        patched.switchpatch.slot_lambda_map(
            2, rpm=[3008], rows={600: [0.02], 1020: [0.02]},
            delivered_lambda_range=GENEROUS_LAMBDA_RANGE,
        )


def test_the_top_lambda_row_must_match_the_one_below(patched: Tune) -> None:
    """WOT airmass runs past the top breakpoint; only a flat segment is bounded."""
    with pytest.raises(ValueError, match="only a flat last segment is bounded"):
        patched.switchpatch.slot_lambda_map(
            2, rpm=[3008], rows={1020: [0.01953125]},
            delivered_lambda_range=GENEROUS_LAMBDA_RANGE,
        )


def test_a_lambda_row_or_column_off_the_breakpoints_is_refused(
    patched: Tune,
) -> None:
    with pytest.raises(ValueError, match="3200 rpm is not a rpm breakpoint"):
        patched.switchpatch.slot_lambda_map(
            2, rpm=[3200], rows={600: [0.0], 1020: [0.0]},
            delivered_lambda_range=GENEROUS_LAMBDA_RANGE,
        )
    with pytest.raises(ValueError, match="1400 mg/stk is not a airmass breakpoint"):
        patched.switchpatch.slot_lambda_map(
            2, rpm=[3008], rows={1400: [0.0]},
            delivered_lambda_range=GENEROUS_LAMBDA_RANGE,
        )


def test_require_as_patched_catches_a_lambda_grid_already_written(
    patched: Tune,
) -> None:
    patched.switchpatch.slot_lambda_map(
        2, rpm=LAMBDA_RPM, rows=_lambda_rows([0.01953125] * 2),
        delivered_lambda_range=GENEROUS_LAMBDA_RANGE,
    )
    with pytest.raises(ValueError, match="not the untouched base"):
        patched.switchpatch.slot_lambda_map(
            2, rpm=LAMBDA_RPM, rows=_lambda_rows([0.01953125] * 2),
            delivered_lambda_range=GENEROUS_LAMBDA_RANGE,
            require_as_patched=True,
        )


def test_a_generic_edit_to_a_lambda_grid_is_refused(patched: Tune) -> None:
    """Domain-owned: the generic editor cannot bypass the both-sign guard."""
    with pytest.raises(EditRejected) as excinfo:
        apply_op(
            patched, "slot3_lambda_modifier", "set",
            space=PATCH_SPACE, value=-0.1,
            intent="bypass the domain",
        )
    assert "slot_lambda_map" in str(excinfo.value)
