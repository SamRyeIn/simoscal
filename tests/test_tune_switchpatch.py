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
from simoscal.tune.profiles.switchpatch_2933 import SLOT_DEFAULT_HPA, SLOTS
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
