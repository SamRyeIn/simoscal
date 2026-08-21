"""Tests for the boost, wastegate, and limits domain modules (U3).

Two layers:

* **behaviour** — each method writes the cells it claims, in the units it
  claims, and refuses the misuse it exists to prevent;
* **equivalence** — a domain call applied to a frozen historical bin
  reproduces the table the corresponding hand-written revision actually
  produced. That is the check that matters: these modules are distilled from
  R03–R11's private helpers, and a distillation that changed the numbers would
  be a silent recalibration.

The equivalence tests skip cleanly when the historical output bins are absent.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from simoscal import CalFile
from simoscal.tune import SC8S50, Tune
from simoscal.tune.domains.limits import MG_PER_KG
from simoscal.tune.journal import (
    KIND_AXIS,
    KIND_GUARDED_CEILING,
    KIND_RAW,
    VERDICT_APPLIED,
    VERDICT_BLOCKED,
    VERDICT_GUARDED_SKIP,
)
from simoscal.tune.units import HPA_PER_PSI, hpa_from_psi, psi_from_hpa

# The delta maps the frozen revisions applied, copied verbatim from
# TUNE_Basics_Guide_R08.py — the point of the equivalence tests is that the new
# API reproduces these exact numbers.
R08_WG_DELTAS = {
    (6, 14): -0.02, (6, 15): -0.02,
    (7, 14): -0.06, (7, 15): -0.04,
    (8, 14): -0.06, (8, 15): -0.04,
}
R10_PQ_LOW_RPM, R10_PQ_PLATEAU = 1.70, 3.1
R11_CEILING_PSI = 30.0


@pytest.fixture
def tune(real_xdf: Path, real_bin: Path) -> Tune:
    return Tune.open(SC8S50, xdf=real_xdf, bin=real_bin)


def _open(bin_path: Path, real_xdf: Path) -> Tune:
    return Tune.open(SC8S50, xdf=real_xdf, bin=bin_path)


def _table(bin_path: Path, xdf: Path, symbol: str) -> np.ndarray:
    return np.asarray(
        CalFile.open(str(xdf), str(bin_path)).get(symbol).values, dtype=np.float64
    )


# --------------------------------------------------------------------------- #
# Units (AE2)
# --------------------------------------------------------------------------- #
def test_psi_to_hpa_floors_by_default() -> None:
    """A cap must never land above the number a human asked for."""
    exact = 10.0 * HPA_PER_PSI + 1016.0     # 1705.5
    assert hpa_from_psi(10.0) == 1705.0                      # floor
    assert hpa_from_psi(10.0, rounding="nearest") == 1706.0
    assert hpa_from_psi(10.0, rounding="exact") == pytest.approx(exact)
    assert psi_from_hpa(hpa_from_psi(10.0)) < 10.0           # the whole point


def test_psi_conversion_rejects_an_unknown_rounding_mode() -> None:
    with pytest.raises(ValueError, match="rounding must be"):
        hpa_from_psi(10.0, rounding="up")


def test_r12_valet_cap_is_reproduced_exactly() -> None:
    """AE2: the R12 slot-5 valet cap was floor(10 psi) = 1705 hPa."""
    assert hpa_from_psi(10.0, rounding="floor") == 1705.0


# --------------------------------------------------------------------------- #
# boost
# --------------------------------------------------------------------------- #
def test_put_ceiling_touches_only_the_full_load_row(tune: Tune) -> None:
    before = tune.values("put_setpoint")
    entry = tune.boost.put_ceiling_hpa(3085.0)

    after = tune.values("put_setpoint")
    assert entry.rows_changed == (3,)
    assert np.allclose(after[:3], before[:3], rtol=0, atol=0)  # byte-identical
    assert np.allclose(after[3], 3085.0, atol=1.0)


def test_put_ceiling_psi_matches_the_r11_shared_ceiling(tune: Tune) -> None:
    """R11 derived 3085 hPa from 30 psi by half-up rounding; so must this."""
    tune.boost.put_ceiling_psi(R11_CEILING_PSI, rounding="nearest")
    assert np.allclose(tune.values("put_setpoint")[3], 3085.0, atol=1.0)


def test_put_curve_rejects_a_wrong_length_curve(tune: Tune) -> None:
    with pytest.raises(ValueError, match="expected shape"):
        tune.boost.put_curve_hpa([2699.0, 2699.0, 2500.0])  # 3, not 6


def test_put_curve_writes_the_full_load_row(tune: Tune) -> None:
    curve = [2699.0, 2809.0, 2809.0, 2712.0, 2519.0, 2243.0]
    entry = tune.boost.put_curve_hpa(curve)

    assert np.allclose(tune.values("put_setpoint")[3], curve, atol=1.0)
    assert entry.rows_changed == (3,)
    assert "psi gauge" in entry.detail  # both units, for review


def test_put_rpm_axis_requires_increasing_breakpoints(tune: Tune) -> None:
    with pytest.raises(ValueError, match="strictly increase"):
        tune.boost.put_rpm_axis([3000, 3400, 3400, 5000, 5750, 6500])


def test_put_rpm_axis_is_journaled_as_an_axis_write(tune: Tune) -> None:
    entry = tune.boost.put_rpm_axis([3000, 3400, 4400, 5000, 5750, 6500])

    assert entry.kind == KIND_AXIS
    assert np.allclose(
        tune.values("put_setpoint_rpm_axis").ravel(),
        [3000, 3400, 4400, 5000, 5750, 6500], atol=1.0,
    )


def test_pressure_quotient_shapes_the_low_rpm_column(tune: Tune) -> None:
    tune.boost.pressure_quotient_max(R10_PQ_PLATEAU, low_rpm=R10_PQ_LOW_RPM)
    values = tune.values("pressure_quotient_max")

    assert np.allclose(values[:, 0], R10_PQ_LOW_RPM, atol=5e-3)
    assert np.allclose(values[:, 1:], R10_PQ_PLATEAU, atol=5e-3)


def test_manifold_pressure_max_goes_through_the_raw_path(tune: Tune) -> None:
    """A plain write would trip the FloatBugGuard; the raw path is deliberate."""
    entry = tune.boost.manifold_pressure_max(350000.0)

    assert entry.verdict == VERDICT_APPLIED
    assert entry.kind == KIND_RAW
    assert tune.values("manifold_pressure_max").ravel()[0] == pytest.approx(350000.0)
    assert "identity" in entry.detail


def test_float_bug_write_refuses_a_table_that_is_not_tagged(tune: Tune) -> None:
    with pytest.raises(ValueError, match="not marked as a float-bug table"):
        tune.limits.float_bug_value("torque_reference_max", 1000.0)


# --------------------------------------------------------------------------- #
# guarded ceiling
# --------------------------------------------------------------------------- #
def test_guarded_ceiling_raises_cells_below_the_target(tune: Tune) -> None:
    entry = tune.boost.overboost_threshold(2700.0)

    assert entry.verdict == VERDICT_APPLIED
    assert entry.kind == KIND_GUARDED_CEILING
    assert np.allclose(tune.values("overboost_threshold"), 2700.0, atol=1.0)


def test_guarded_ceiling_never_lowers_a_higher_cell(tune: Tune) -> None:
    tune.boost.overboost_threshold(2700.0)
    before = tune.values("overboost_threshold")

    entry = tune.boost.overboost_threshold(1800.0)  # below what is now there

    assert entry.verdict == VERDICT_GUARDED_SKIP
    assert "never lowered" in entry.detail
    assert np.allclose(tune.values("overboost_threshold"), before, rtol=0, atol=0)


def test_guarded_ceiling_refuses_to_exceed_a_real_declared_limit(tune: Tune) -> None:
    """Not a float-bug table, so its XDF maximum is taken as an ECU limit."""
    entry = tune.limits.raise_ceiling("torque_reference_max", 99999.0)

    assert entry.verdict == VERDICT_BLOCKED
    assert "declared upper limit" in entry.detail
    assert not entry.offsets  # byte-identical


def test_guarded_ceiling_at_target_is_unchanged_not_applied(tune: Tune) -> None:
    tune.boost.overboost_threshold(2700.0)
    entry = tune.boost.overboost_threshold(2700.0)

    assert entry.verdict == "unchanged"


# --------------------------------------------------------------------------- #
# limits — the kg/stk trap (AE5)
# --------------------------------------------------------------------------- #
def test_airmass_cap_takes_mg_and_writes_kg(tune: Tune) -> None:
    """AE5: the API takes mg/stk; 2000 mg/stk must store 0.002 kg/stk."""
    entry = tune.limits.airmass_cap_mg(2000)

    stored = tune.values("airmass_setpoint_max").ravel()[0]
    assert stored == pytest.approx(0.002)
    assert stored == pytest.approx(2000 / MG_PER_KG)
    assert "kg/stk" in entry.detail


def test_airmass_cap_rejects_a_raw_looking_value(tune: Tune) -> None:
    """Passing the raw 0.002 instead of 2000 mg/stk must fail loud, not quietly work."""
    with pytest.raises(ValueError, match="looks like a raw kg/stk value"):
        tune.limits.airmass_cap_mg(0.002)


def test_airmass_cap_rejects_a_non_positive_value(tune: Tune) -> None:
    with pytest.raises(ValueError, match="not a positive airmass"):
        tune.limits.airmass_cap_mg(0)


def test_intake_air_max_is_genuine_mg_and_sets_both_lifts(tune: Tune) -> None:
    """The contrast case: these ARE mg/stk and take 2000 as written."""
    entries = tune.limits.intake_air_max(2000)

    assert len(entries) == 2
    for name in ("intake_air_max_vvl0", "intake_air_max_vvl1"):
        assert np.allclose(tune.values(name), 2000.0, atol=1.0)


# --------------------------------------------------------------------------- #
# wastegate
# --------------------------------------------------------------------------- #
def test_overlay_applies_identical_deltas_to_both_vvl_tables(tune: Tune) -> None:
    before0 = tune.values("wastegate_feedforward_vvl0")
    before1 = tune.values("wastegate_feedforward_vvl1")

    entries = tune.wastegate.overlay(R08_WG_DELTAS)

    after0 = tune.values("wastegate_feedforward_vvl0")
    after1 = tune.values("wastegate_feedforward_vvl1")
    assert len(entries) == 2
    assert np.allclose(after0 - before0, after1 - before1, atol=1e-9)
    for (row, col), delta in R08_WG_DELTAS.items():
        assert after0[row, col] == pytest.approx(before0[row, col] + delta, abs=5e-3)


def test_overlay_refuses_to_clamp_at_the_physical_bound(tune: Tune) -> None:
    """A delta past [0, 1] means the delta map is wrong — never a clamped write."""
    with pytest.raises(ValueError, match="outside the physical"):
        tune.wastegate.overlay({(6, 14): -5.0})


def test_overlay_refuses_a_delta_that_changes_nothing(tune: Tune) -> None:
    with pytest.raises(ValueError, match="but 0 moved"):
        tune.wastegate.overlay({(6, 14): 0.0})


def test_overlay_refuses_an_out_of_range_cell(tune: Tune) -> None:
    with pytest.raises(ValueError, match="outside"):
        tune.wastegate.overlay({(99, 14): -0.02})


def test_overlay_rejects_an_empty_delta_map(tune: Tune) -> None:
    with pytest.raises(ValueError, match="no deltas given"):
        tune.wastegate.overlay({})


def test_exh_flow_axis_last_rebreakpoints_both_maps(tune: Tune) -> None:
    entry = tune.wastegate.exh_flow_axis_last(1.40)

    assert entry.kind == KIND_AXIS
    assert tune.values("wastegate_exh_flow_axis").ravel()[-1] == pytest.approx(
        1.40, abs=1e-3
    )
    # One axis table, shared: both maps now read the new top breakpoint.
    for name in ("wastegate_feedforward_vvl0", "wastegate_feedforward_vvl1"):
        axis = tune.axis(name, "x")
        assert axis[-1] == pytest.approx(1.40, abs=1e-3)


def test_exh_flow_axis_last_must_stay_increasing(tune: Tune) -> None:
    with pytest.raises(ValueError, match="strictly increase"):
        tune.wastegate.exh_flow_axis_last(0.5)


# --------------------------------------------------------------------------- #
# Equivalence with the frozen revisions
# --------------------------------------------------------------------------- #
def test_wastegate_overlay_reproduces_r08(
    historical_bins: dict, real_xdf: Path
) -> None:
    """R07's bin + the R08 delta map must equal R08's wastegate tables."""
    tune = _open(historical_bins["R07"], real_xdf)
    tune.wastegate.overlay(R08_WG_DELTAS)

    for name, symbol in (
        ("wastegate_feedforward_vvl0", "IP_FAC_BPA_SP[0]"),
        ("wastegate_feedforward_vvl1", "IP_FAC_BPA_SP[1]"),
    ):
        expected = _table(historical_bins["R08"], real_xdf, symbol)
        assert np.allclose(tune.values(name), expected, rtol=0, atol=1e-9), name


def test_pressure_quotient_max_reproduces_r10(
    historical_bins: dict, real_xdf: Path
) -> None:
    """R09's bin + the R10 compressor-cap call must equal R10's table."""
    tune = _open(historical_bins["R09"], real_xdf)
    tune.boost.pressure_quotient_max(R10_PQ_PLATEAU, low_rpm=R10_PQ_LOW_RPM)

    expected = _table(historical_bins["R10"], real_xdf, "IP_PQ_CHA_MAX")
    assert np.allclose(tune.values("pressure_quotient_max"), expected,
                       rtol=0, atol=1e-9)


def test_put_ceiling_reproduces_r11(historical_bins: dict, real_xdf: Path) -> None:
    """R10's bin + a 30 psi parked ceiling must equal R11's PUT setpoint."""
    tune = _open(historical_bins["R10"], real_xdf)
    tune.boost.put_ceiling_psi(R11_CEILING_PSI, rounding="nearest")

    expected = _table(historical_bins["R11"], real_xdf, "IP_PUT_SP")
    assert np.allclose(tune.values("put_setpoint"), expected, rtol=0, atol=1e-9)


def test_limits_reproduce_the_r03_limiter_writes(
    historical_bins: dict, real_xdf: Path, real_bin: Path
) -> None:
    """The R03 limiter/fuelling values, as written by the new domain calls."""
    tune = Tune.open(SC8S50, xdf=real_xdf, bin=real_bin)
    tune.limits.intake_air_max(2000)
    tune.limits.airmass_cap_mg(2000)
    tune.limits.torque_reference_max(1000)
    tune.boost.manifold_pressure_max(350000)

    reference = historical_bins["R07"]  # carries the full R03-R06 calibration
    for name, symbol in (
        ("intake_air_max_vvl0", "IP_M_AIR_CYL_MAX_STND_VVL[STND]"),
        ("intake_air_max_vvl1", "IP_M_AIR_CYL_MAX_STND_VVL[LFT_1]"),
        ("airmass_setpoint_max", "C_M_AIR_CYL_SP_MAX"),
        ("torque_reference_max", "IP_TQI_REF_MAX_MON"),
        ("manifold_pressure_max", "C_PRS_IM_SP_MAX"),
    ):
        expected = _table(reference, real_xdf, symbol)
        assert np.allclose(tune.values(name), expected, rtol=0, atol=1e-9), name


# --------------------------------------------------------------------------- #
# U2 — the coherent multi-table limiters and the lambda lean bound
#
# These are the ops the Limiters and Lambda screens drive. What is tested is not
# "the write lands" but the refusals: an incoherent trio, a partial quartet, and
# a lean full-load setpoint are the three states the screens must not be able to
# produce, and each must leave the tables and the journal untouched.
# --------------------------------------------------------------------------- #
REPO_ROOT = Path(__file__).resolve().parents[2]
SWITCH_XDF = REPO_ROOT / "BinToolz-main" / "definitions" / "S50 Switch Patch.29.33.V2.xdf"
PATCHED_BIN = (
    REPO_ROOT / "Tunes" / "TuningBasicsGuide" / "BinToolz-patched"
    / "CB_HSL_SP2933_5G0906259L_0002_BasicsGuide_R04.bin"
)

requires_patch = pytest.mark.skipif(
    not (PATCHED_BIN.is_file() and SWITCH_XDF.is_file()),
    reason="patched bin / switch-patch XDF absent",
)


@pytest.fixture
def patched_tune(real_xdf: Path) -> Tune:
    from simoscal.tune.domains.switchpatch import PATCH_SPACE
    from simoscal.tune.profiles import SWITCH_PATCH_2933

    return Tune.open(
        SC8S50, xdf=real_xdf, bin=PATCHED_BIN,
        extra_spaces={PATCH_SPACE: (SWITCH_PATCH_2933, SWITCH_XDF)},
    )


def _trio(tune: Tune) -> list[float]:
    from simoscal.tune.profiles.switchpatch_2933 import REV_LIMIT_TRIO

    return [
        float(tune.values(name, space="patch").ravel()[0])
        for name in REV_LIMIT_TRIO
    ]


# ---- rev limits: the escalation invariant ---------------------------------- #
@requires_patch
def test_rev_limits_writes_the_trio_as_three_journaled_entries(
    patched_tune: Tune,
) -> None:
    entries = patched_tune.limits.rev_limits(soft=100, medium=200, hard=300)

    assert len(entries) == 3
    assert _trio(patched_tune) == [100.0, 200.0, 300.0]
    # Every entry names the whole resulting trio, so a reviewer reading one row
    # of the journal can see the state it left behind.
    for entry in entries:
        assert "soft=100" in entry.detail and "hard=300" in entry.detail


@requires_patch
def test_rev_limits_refuses_a_backwards_trio_atomically(patched_tune: Tune) -> None:
    before = _trio(patched_tune)
    journal_len = len(patched_tune.journal)

    with pytest.raises(ValueError, match="escalate"):
        patched_tune.limits.rev_limits(soft=500, medium=200, hard=300)

    assert _trio(patched_tune) == before, "a refused trio must move no table"
    assert len(patched_tune.journal) == journal_len, "and journal nothing"


@requires_patch
def test_a_single_rev_limit_is_revalidated_against_the_live_others(
    patched_tune: Tune,
) -> None:
    """Passing one value still checks the trio the ECU would end up holding."""
    patched_tune.limits.rev_limits(soft=100, medium=200, hard=300)

    # 250 as the soft limit would sit above the live medium (200).
    with pytest.raises(ValueError, match="escalate"):
        patched_tune.limits.rev_limits(soft=250)
    assert _trio(patched_tune) == [100.0, 200.0, 300.0]

    # 150 fits under it, and writes only the one table it was given.
    (entry,) = patched_tune.limits.rev_limits(soft=150)
    assert entry.name == "rev_limit_soft"
    assert _trio(patched_tune) == [150.0, 200.0, 300.0]


@requires_patch
def test_rev_limits_refuses_a_value_past_the_encodable_range(
    patched_tune: Tune,
) -> None:
    before = _trio(patched_tune)
    with pytest.raises(ValueError, match="declared range"):
        patched_tune.limits.rev_limits(hard=9000)   # field maxes at 8160 rpm
    assert _trio(patched_tune) == before


@requires_patch
def test_rev_limits_requires_at_least_one_value(patched_tune: Tune) -> None:
    with pytest.raises(ValueError, match="at least one"):
        patched_tune.limits.rev_limits()


# ---- speed limiter: the quartet ------------------------------------------- #
def test_speed_limiter_writes_every_quartet_scalar(tune: Tune) -> None:
    from simoscal.tune.profiles.sc8s50 import SPEED_LIMITER

    entries = tune.limits.speed_limiter(250)

    assert len(entries) == len(SPEED_LIMITER) == 4
    for name in SPEED_LIMITER:
        assert float(tune.values(name).ravel()[0]) == pytest.approx(250.0)
    assert {e.name for e in entries} == set(SPEED_LIMITER)


def test_speed_limiter_refuses_an_unencodable_speed(tune: Tune) -> None:
    from simoscal.tune.profiles.sc8s50 import SPEED_LIMITER

    journal_len = len(tune.journal)
    with pytest.raises(ValueError, match="declared range"):
        tune.limits.speed_limiter(600)   # stored /128, so 511.99 km/h is the top

    for name in SPEED_LIMITER:
        assert float(tune.values(name).ravel()[0]) == pytest.approx(200.0)
    assert len(tune.journal) == journal_len


def test_speed_limiter_refuses_a_nonsense_speed(tune: Tune) -> None:
    with pytest.raises(ValueError, match="positive road speed"):
        tune.limits.speed_limiter(0)


# ---- lambda full-load enrichment: the lean bound --------------------------- #
def test_full_load_enrichment_writes_one_time_row(tune: Tune) -> None:
    entry = tune.fueling.full_load_enrichment(0.85, row=4)
    grid = tune.values("lambda_full_load")

    assert np.allclose(grid[4], 0.85, atol=1e-3)
    # Every other time-row is untouched — stock is a flat 1.00 map.
    assert np.allclose(np.delete(grid, 4, axis=0), 1.0)
    assert entry.rows_changed == (4,)


def test_full_load_enrichment_selects_a_row_by_its_time_breakpoint(tune: Tune) -> None:
    """`seconds=` picks the row by the axis's own units, not by index."""
    tune.fueling.full_load_enrichment(0.82, seconds=30)
    grid = tune.values("lambda_full_load")

    y = tune.axis("lambda_full_load", "y")
    expected_row = int(np.argmin(np.abs(y - 30.0)))
    assert np.allclose(grid[expected_row], 0.82, atol=1e-3)


def test_full_load_enrichment_takes_a_per_rpm_curve(tune: Tune) -> None:
    cols = tune.values("lambda_full_load").shape[1]
    curve = np.linspace(0.95, 0.80, cols)
    tune.fueling.full_load_enrichment(curve, row=7)

    assert np.allclose(tune.values("lambda_full_load")[7], curve, atol=1e-3)


@pytest.mark.parametrize("lean", [1.0, 1.05, 1.5])
def test_full_load_enrichment_refuses_a_lean_setpoint(tune: Tune, lean: float) -> None:
    """The one refusal this op exists for: no enrichment at wide-open throttle."""
    journal_len = len(tune.journal)
    with pytest.raises(ValueError, match="at or above lambda"):
        tune.fueling.full_load_enrichment(lean, row=4)

    assert np.allclose(tune.values("lambda_full_load"), 1.0), "table untouched"
    assert len(tune.journal) == journal_len, "and nothing journaled"


def test_full_load_enrichment_refuses_a_lean_value_anywhere_in_the_curve(
    tune: Tune,
) -> None:
    """One lean cell in an otherwise rich curve still refuses the whole write."""
    cols = tune.values("lambda_full_load").shape[1]
    curve = np.full(cols, 0.85)
    curve[-1] = 1.0

    with pytest.raises(ValueError, match="at or above lambda"):
        tune.fueling.full_load_enrichment(curve, row=4)
    assert np.allclose(tune.values("lambda_full_load"), 1.0)


def test_full_load_enrichment_accepts_just_under_the_bound(tune: Tune) -> None:
    """0.999 is accepted, and encodes at or below what was asked for."""
    tune.fueling.full_load_enrichment(0.999, row=4)
    encoded = tune.values("lambda_full_load")[4]

    assert np.all(encoded < 1.0), "an accepted value must not encode lean of 1.00"
    assert np.allclose(encoded, 0.999, atol=1e-3)


def test_full_load_enrichment_refuses_a_mistyped_decimal(tune: Tune) -> None:
    with pytest.raises(ValueError, match="at or below lambda"):
        tune.fueling.full_load_enrichment(0.08, row=4)   # 0.80 with a slipped key


def test_full_load_enrichment_refuses_a_row_outside_the_map(tune: Tune) -> None:
    with pytest.raises(ValueError, match="time-rows"):
        tune.fueling.full_load_enrichment(0.85, row=99)


def test_full_load_enrichment_needs_exactly_one_row_selector(tune: Tune) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        tune.fueling.full_load_enrichment(0.85)
    with pytest.raises(ValueError, match="exactly one"):
        tune.fueling.full_load_enrichment(0.85, row=1, seconds=30)


# --------------------------------------------------------------------------- #
# the standstill rev cap
#
# Stock holds this engine to 3808 rpm whenever the car is stopped. Raising that
# toward the engine's own limiter does not raise what the engine will reach — it
# lets the existing limiter be what catches it in park, as it already does in
# gear. The tests below pin that framing as much as the write: the guard exists
# so this call cannot be mistaken for one that raises the redline.
# --------------------------------------------------------------------------- #
STOCK_STANDSTILL_RPM = 3808.0
ENGINE_REV_LIMIT_RPM = 6816.0


def test_static_rev_limit_writes_every_transmission_variant(tune: Tune) -> None:
    from simoscal.tune.profiles.sc8s50 import STATIC_REV_LIMIT

    entries = tune.limits.static_rev_limit(ENGINE_REV_LIMIT_RPM)

    assert len(entries) == len(STATIC_REV_LIMIT) == 4
    for name in STATIC_REV_LIMIT:
        assert float(tune.values(name).ravel()[0]) == pytest.approx(ENGINE_REV_LIMIT_RPM)


def test_static_rev_limit_names_which_variant_this_car_reads(tune: Tune) -> None:
    """The three inert ones are written, and the journal says they are inert."""
    entries = {e.name: e for e in tune.limits.static_rev_limit(6000)}

    assert "actually reads" in entries["static_rev_limit_dct"].detail
    for name in ("static_rev_limit_at", "static_rev_limit_mt", "static_rev_limit_cvt"):
        assert "inert for this transmission" in entries[name].detail


def test_static_rev_limit_refuses_a_target_above_the_engines_own_limiter(
    tune: Tune,
) -> None:
    """The guard that keeps this from looking like a redline raise.

    A standstill cap above the rev limiter could never be reached, so it would
    change nothing except what the calibration appears to say.
    """
    journal_len = len(tune.journal)
    with pytest.raises(ValueError, match="above this engine's own rev limiter"):
        tune.limits.static_rev_limit(7200)

    assert float(tune.values("static_rev_limit_dct").ravel()[0]) == pytest.approx(
        STOCK_STANDSTILL_RPM
    ), "a refusal writes nothing"
    assert len(tune.journal) == journal_len


def test_static_rev_limit_accepts_exactly_the_limiter(tune: Tune) -> None:
    """Matching the limiter is the intended outcome, not an edge case."""
    tune.limits.static_rev_limit(ENGINE_REV_LIMIT_RPM)
    assert float(tune.values("static_rev_limit_dct").ravel()[0]) == pytest.approx(
        ENGINE_REV_LIMIT_RPM
    )


def test_static_rev_limit_does_not_touch_the_rev_limiter_itself(tune: Tune) -> None:
    """The whole safety argument: the engine's ceiling is unchanged."""
    before = [tune.values(n).copy() for n in ("engine_speed_limit_vvl0", "engine_speed_limit_vvl1")]
    tune.limits.static_rev_limit(ENGINE_REV_LIMIT_RPM)
    after = [tune.values(n) for n in ("engine_speed_limit_vvl0", "engine_speed_limit_vvl1")]

    for b, a in zip(before, after):
        assert np.array_equal(b, a)
    # And it is not writable by any other route either.
    assert SC8S50["engine_speed_limit_vvl0"].domain_owned
    assert "no write path" in SC8S50["engine_speed_limit_vvl0"].owner


def test_static_rev_limit_quantizes_to_the_stores_32_rpm_step(tune: Tune) -> None:
    """8-bit scaled x32, so a target lands on a 32 rpm step and says where."""
    (entry, *_rest) = tune.limits.static_rev_limit(6500)
    encoded = float(tune.values("static_rev_limit_dct").ravel()[0])

    assert encoded % 32 == pytest.approx(0.0)
    assert abs(encoded - 6500) <= 32
    assert float(entry.after.ravel()[0]) == pytest.approx(encoded)


def test_static_rev_limit_refuses_a_nonsense_target(tune: Tune) -> None:
    with pytest.raises(ValueError, match="positive engine speed"):
        tune.limits.static_rev_limit(0)
    with pytest.raises(ValueError, match="positive engine speed"):
        tune.limits.static_rev_limit(-1000)


def test_the_standstill_caps_are_domain_owned(tune: Tune) -> None:
    """A grid write to one alone could leave the car capped by a sibling."""
    from simoscal.tune.profiles.sc8s50 import STATIC_REV_LIMIT

    for name in STATIC_REV_LIMIT:
        spec = SC8S50[name]
        assert spec.domain_owned
        assert "static_rev_limit" in spec.owner


def test_the_fuel_cut_offset_stays_ordinary(tune: Tune) -> None:
    """It is the soft-to-hard distance, not the cap — no owner, no guard."""
    spec = SC8S50["static_rev_fuel_cut_offset"]
    assert not spec.domain_owned
    assert float(tune.values("static_rev_fuel_cut_offset").ravel()[0]) == pytest.approx(100.0)
