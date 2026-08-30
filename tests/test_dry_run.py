"""Dry-run equivalence: the preview path and the real path agree on everything.

The courier's whole safety claim rests on one property — that asking "would
this edit be accepted, and what would it encode to?" gets the same answer the
real edit would give. A preview computed by a second, parallel implementation
would be a second thing to keep in step with the guards, and the day it drifted
it would tell a person an edit is safe that the real path refuses.

So the tests here are written as *equivalence* tests rather than behaviour
tests: for every entry point that takes ``dry_run=``, run it dry, prove the
session did not move, then run it for real and prove the two outcomes are equal
field by field — including the refusal message on the calls that are supposed
to be refused.

Real SC8S50 files are gitignored → tests skip (never fail) when absent.
"""

from __future__ import annotations

import hashlib
from dataclasses import fields
from pathlib import Path

import numpy as np
import pytest

from simoscal.tune import SC8S50, Tune
from simoscal.tune.domains.switchpatch import PATCH_SPACE
from simoscal.tune.editing import EditOp, EditRejected, Selection, apply_op
from simoscal.tune.journal import VERDICT_APPLIED
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

# A boost table that is plainly editable through the generic path — grid cells,
# reversible, not domain-owned — used for the apply_op scenarios.
GRID = "pressure_quotient_max"


@pytest.fixture
def base_tune() -> Tune:
    if not _have_base:
        pytest.skip("real SC8S50 bin/XDF absent")
    return Tune.open(SC8S50, xdf=XDF, bin=STOCK_BIN)


@pytest.fixture
def patched_tune() -> Tune:
    if not _have_patch:
        pytest.skip("patched bin / switch XDF absent")
    return Tune.open(
        SC8S50, xdf=XDF, bin=PATCHED_BIN,
        extra_spaces={PATCH_SPACE: (SWITCH_PATCH_2933, SWITCH_XDF)},
    )


# --------------------------------------------------------------------------- #
# state fingerprint — what "the session did not move" means, concretely
# --------------------------------------------------------------------------- #
def _fingerprint(tune: Tune) -> tuple:
    """Everything an edit mutates, as one comparable value.

    Bytes, the journal, and each space's staged-edit ledger. The ledger is in
    here deliberately: a rollback that put the bytes back but left the ledger
    grown would leave ``CalFile.edited`` — and with it the stale-checksum
    warning on save — reporting an edit that never happened.
    """
    seen: set[int] = set()
    digests = []
    ledgers = []
    for name in sorted(tune.spaces):
        cal = tune.spaces[name].cal
        ledgers.append((name, tuple(cal.edited_ranges)))
        image = cal.binimage
        if id(image) in seen:
            continue
        seen.add(id(image))
        digests.append(hashlib.sha256(image.to_bytes()).hexdigest())
    return (tuple(digests), tuple(ledgers), tune.journal.entries)


def _entry_key(entry) -> tuple:
    """An EditEntry as plain comparable values, arrays included."""
    out = []
    for field in fields(entry):
        value = getattr(entry, field.name)
        if isinstance(value, np.ndarray):
            value = ("array", value.shape, value.tobytes())
        out.append((field.name, value))
    return tuple(out)


def _outcome_key(outcome) -> tuple:
    """Normalize a domain call's return — one entry or a tuple of them."""
    if isinstance(outcome, tuple):
        return tuple(_entry_key(e) for e in outcome)
    return (_entry_key(outcome),)


# --------------------------------------------------------------------------- #
# the entry-point table: one valid call per domain method that takes dry_run=
# --------------------------------------------------------------------------- #
def _axis_nudged(tune: Tune, name: str, *, space: str = "base") -> np.ndarray:
    """The table's own breakpoints with the last one moved out — still rising."""
    current = tune.values(name, space=space)
    nudged = current.copy()
    nudged.ravel()[-1] = float(nudged.ravel()[-1]) + 100.0
    return nudged


BASE_CALLS = {
    "boost.put_ceiling_hpa":
        lambda t, d: t.boost.put_ceiling_hpa(2200.0, dry_run=d),
    "boost.put_ceiling_psi":
        lambda t, d: t.boost.put_ceiling_psi(17.0, dry_run=d),
    "boost.put_curve_hpa":
        lambda t, d: t.boost.put_curve_hpa(
            np.full(t.values("put_setpoint").shape[1], 2100.0), dry_run=d),
    "boost.put_rpm_axis":
        lambda t, d: t.boost.put_rpm_axis(
            _axis_nudged(t, "put_setpoint_rpm_axis"), dry_run=d),
    "boost.pressure_quotient_max":
        lambda t, d: t.boost.pressure_quotient_max(3.1, low_rpm=1.7, dry_run=d),
    "boost.manifold_pressure_max":
        lambda t, d: t.boost.manifold_pressure_max(3000.0, dry_run=d),
    "boost.overboost_threshold":
        lambda t, d: t.boost.overboost_threshold(2700.0, dry_run=d),
    "wastegate.move_intake_flow_breakpoint":
        lambda t, d: t.wastegate.move_intake_flow_breakpoint(
            8, 1.15, preserve_to=1.21, exhaust_range=(0.65, 1.45),
            dry_run=d),
    "limits.airmass_cap_mg":
        lambda t, d: t.limits.airmass_cap_mg(2000.0, dry_run=d),
    "limits.intake_air_max":
        lambda t, d: t.limits.intake_air_max(2000.0, dry_run=d),
    "limits.torque_reference_max":
        lambda t, d: t.limits.torque_reference_max(600.0, dry_run=d),
    "limits.static_rev_limit":
        lambda t, d: t.limits.static_rev_limit(4500.0, dry_run=d),
    "limits.speed_limiter":
        lambda t, d: t.limits.speed_limiter(250.0, dry_run=d),
    "limits.raise_ceiling":
        lambda t, d: t.limits.raise_ceiling(
            "intake_air_max_vvl0", 2000.0, dry_run=d),
    "limits.float_bug_value":
        lambda t, d: t.limits.float_bug_value(
            "manifold_pressure_max", 3000.0, dry_run=d),
    "fueling.rebreakpoint_lambda_axes":
        lambda t, d: t.fueling.rebreakpoint_lambda_axes(
            rpm=_axis_nudged(t, "lambda_rpm_axis"),
            load=_axis_nudged(t, "lambda_load_axis"), dry_run=d),
    "fueling.lambda_grid":
        lambda t, d: t.fueling.lambda_grid(
            np.full(t.values("lambda_basic").shape, 0.85), dry_run=d),
    "fueling.lambda_floors":
        lambda t, d: t.fueling.lambda_floors(0.80, dry_run=d),
    "fueling.full_load_enrichment":
        lambda t, d: t.fueling.full_load_enrichment(0.85, row=0, dry_run=d),
    "fueling.pedal_threshold":
        lambda t, d: t.fueling.pedal_threshold(95.0, dry_run=d),
    "ignition.retard_cells":
        lambda t, d: t.ignition.retard_cells({(4000.0, 1500.0): 5.0}, dry_run=d),
    "ignition.offset_cells":
        lambda t, d: t.ignition.offset_cells({(4000.0, 1500.0): -1.0}, dry_run=d),
    "wastegate.overlay":
        lambda t, d: t.wastegate.overlay({(6, 14): -0.02}, dry_run=d),
    "wastegate.exh_flow_axis_last":
        lambda t, d: t.wastegate.exh_flow_axis_last(
            float(t.values("wastegate_exh_flow_axis").ravel()[-1]) + 0.05,
            dry_run=d),
}

PATCH_CALLS = {
    "switchpatch.slot_curve":
        lambda t, d: t.switchpatch.slot_curve(1, psi=10.0, dry_run=d),
    "switchpatch.slot_spark_map":
        lambda t, d: t.switchpatch.slot_spark_map(
            5, rpm=[5000, 5500], rows={1200: [1.5, 1.5], 1400: [1.5, 1.5]},
            max_delivered_degrees=12.0, dry_run=d),
    "switchpatch.slot_rpm_axis":
        lambda t, d: t.switchpatch.slot_rpm_axis(
            _axis_nudged(t, "slot_put_rpm_axis", space=PATCH_SPACE), dry_run=d),
    "switchpatch.traction_control":
        lambda t, d: t.switchpatch.traction_control(slots=(1,), enable=True, dry_run=d),
    "switchpatch.set_slot_flag":
        lambda t, d: t.switchpatch.set_slot_flag(
            "enable_sl_tc", slots=(1,),
            on=not bool(t.values("slot1_enable_sl_tc", space=PATCH_SPACE).ravel()[0]),
            dry_run=d),
    "limits.rev_limits":
        lambda t, d: t.limits.rev_limits(soft=200.0, medium=400.0, hard=600.0, dry_run=d),
}


def _assert_equivalent(tune: Tune, call) -> None:
    """Dry then real, from the same state: nothing moved, same outcome."""
    before = _fingerprint(tune)
    dry = call(tune, True)
    assert _fingerprint(tune) == before, "a dry run moved the session"

    real = call(tune, False)
    assert _outcome_key(dry) == _outcome_key(real)
    assert _fingerprint(tune) != before, (
        "the real call moved nothing, so this case proves nothing about dry-run"
    )


@requires_base
@pytest.mark.parametrize("name", sorted(BASE_CALLS))
def test_domain_dry_run_matches_real(base_tune: Tune, name: str) -> None:
    _assert_equivalent(base_tune, BASE_CALLS[name])


@requires_patch
@pytest.mark.parametrize("name", sorted(PATCH_CALLS))
def test_patch_domain_dry_run_matches_real(patched_tune: Tune, name: str) -> None:
    _assert_equivalent(patched_tune, PATCH_CALLS[name])


@requires_base
def test_every_domain_entry_point_is_covered(base_tune: Tune) -> None:
    """The table above names every method that takes ``dry_run=``.

    The verification U1 claims is "every domain edit entry point accepts the
    flag", and a table of examples only proves that while it is complete. This
    finds the entry points by asking the objects, so a method added later
    without a case here fails rather than passing silently.
    """
    import inspect

    covered = set(BASE_CALLS) | set(PATCH_CALLS)
    missing = []
    for domain_name in ("boost", "wastegate", "limits", "fueling", "ignition",
                        "switchpatch"):
        domain = getattr(base_tune, domain_name)
        for attr, method in inspect.getmembers(type(domain), inspect.isfunction):
            if attr.startswith("_"):
                continue
            if "dry_run" not in inspect.signature(method).parameters:
                continue
            if f"{domain_name}.{attr}" not in covered:
                missing.append(f"{domain_name}.{attr}")
    assert not missing, f"entry points with dry_run= and no equivalence case: {missing}"


# --------------------------------------------------------------------------- #
# apply_op — the generic grid editor
# --------------------------------------------------------------------------- #
@requires_base
def test_apply_op_dry_and_real_agree_cell_for_cell(base_tune: Tune) -> None:
    dry = apply_op(base_tune, GRID, EditOp.FILL, value=3.1, dry_run=True)
    real = apply_op(base_tune, GRID, EditOp.FILL, value=3.1)

    assert dry.dry_run is True and real.dry_run is False
    assert np.array_equal(dry.requested, real.requested)
    assert np.array_equal(dry.encoded, real.encoded)
    assert dry.warning == real.warning
    assert dry.quantized == real.quantized
    assert dry.max_abs_quantization() == real.max_abs_quantization()
    assert _entry_key(dry.entry) == _entry_key(real.entry)


@requires_base
def test_apply_op_dry_run_leaves_session_untouched(base_tune: Tune) -> None:
    before = _fingerprint(base_tune)
    values_before = base_tune.values(GRID)

    result = apply_op(base_tune, GRID, EditOp.FILL, value=3.1, dry_run=True)

    assert result.entry.verdict == VERDICT_APPLIED   # it really did apply, then rewound
    assert _fingerprint(base_tune) == before
    assert np.array_equal(base_tune.values(GRID), values_before)
    assert len(base_tune.journal) == 0
    assert not base_tune.space().cal.edited


@requires_base
def test_apply_op_dry_run_reports_the_same_quantization(base_tune: Tune) -> None:
    """A value that cannot land exactly quantizes identically on both paths."""
    dry = apply_op(base_tune, GRID, EditOp.FILL, value=2.3456789, dry_run=True)
    real = apply_op(base_tune, GRID, EditOp.FILL, value=2.3456789)

    assert dry.quantized and real.quantized, "pick a value that does not encode exactly"
    assert dry.max_abs_quantization() == real.max_abs_quantization()
    assert np.array_equal(dry.encoded, real.encoded)


@requires_base
def test_apply_op_dry_run_refuses_a_falling_axis_with_the_same_words(
    base_tune: Tune,
) -> None:
    axis = "put_setpoint_rpm_axis"
    falling = base_tune.values(axis).copy()
    falling.ravel()[-1] = float(falling.ravel()[0]) - 100.0

    with pytest.raises(EditRejected) as dry:
        apply_op(base_tune, axis, EditOp.PASTE, array=falling, dry_run=True)
    with pytest.raises(EditRejected) as real:
        apply_op(base_tune, axis, EditOp.PASTE, array=falling)

    assert str(dry.value) == str(real.value)
    assert "strictly increasing" in str(dry.value)
    assert len(base_tune.journal) == 0


@requires_base
def test_apply_op_dry_run_refuses_a_domain_owned_table_identically(
    base_tune: Tune,
) -> None:
    """An owner-locked table is refused in dry-run exactly as it is for real."""
    owned = "speed_limiter_level1"
    with pytest.raises(EditRejected) as dry:
        apply_op(base_tune, owned, EditOp.FILL, value=250.0, dry_run=True)
    with pytest.raises(EditRejected) as real:
        apply_op(base_tune, owned, EditOp.FILL, value=250.0)

    assert str(dry.value) == str(real.value)
    assert len(base_tune.journal) == 0


@requires_base
def test_apply_op_dry_run_refuses_an_out_of_range_value_identically(
    base_tune: Tune,
) -> None:
    with pytest.raises(EditRejected) as dry:
        apply_op(base_tune, GRID, EditOp.FILL, value=1e9, dry_run=True)
    with pytest.raises(EditRejected) as real:
        apply_op(base_tune, GRID, EditOp.FILL, value=1e9)

    assert str(dry.value) == str(real.value)
    assert len(base_tune.journal) == 0
    assert not base_tune.space().cal.edited


@requires_base
def test_dry_run_between_two_real_edits_leaves_no_trace(base_tune: Tune) -> None:
    """The journal reads as though the dry run were not there."""
    first = apply_op(base_tune, GRID, EditOp.FILL, value=2.9)
    apply_op(base_tune, GRID, EditOp.FILL, value=3.4, dry_run=True)
    second = apply_op(
        base_tune, GRID, EditOp.SET, value=2.95, selection=Selection.row(0),
    )

    entries = base_tune.journal.entries
    assert len(entries) == 2
    assert entries[0] is first.entry and entries[1] is second.entry
    # The second edit started from the first's result, not the dry run's.
    assert np.allclose(second.entry.before, first.encoded)


@requires_base
def test_nested_dry_runs_rewind_to_where_each_started(base_tune: Tune) -> None:
    before = _fingerprint(base_tune)
    with base_tune.dry_run():
        apply_op(base_tune, GRID, EditOp.FILL, value=2.9)
        inner = _fingerprint(base_tune)
        with base_tune.dry_run():
            apply_op(base_tune, GRID, EditOp.FILL, value=3.4)
        assert _fingerprint(base_tune) == inner
    assert _fingerprint(base_tune) == before


@requires_patch
def test_dry_run_rewinds_every_space_of_a_two_space_tune(patched_tune: Tune) -> None:
    """A patch-space edit rewinds as completely as a base-space one."""
    before = _fingerprint(patched_tune)
    patched_tune.switchpatch.slot_curve(1, psi=10.0, dry_run=True)
    assert _fingerprint(patched_tune) == before


@requires_base
def test_dry_run_does_not_leave_a_stale_decode_behind(base_tune: Tune) -> None:
    """Reads after a rewind come off the restored buffer, not a cached decode."""
    before = base_tune.values(GRID)
    apply_op(base_tune, GRID, EditOp.FILL, value=3.4, dry_run=True)
    assert np.array_equal(base_tune.values(GRID), before)
    assert np.array_equal(base_tune.table(GRID).view.values, before)
