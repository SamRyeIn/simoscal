"""U1 — tests for log loading, channel resolution, and the quality preflight."""

from __future__ import annotations

import math

import numpy as np
import pytest

from simoscal.analysis import (
    DuplicateChannelError,
    GearResolution,
    load_logfile,
    load_logset,
)
from simoscal.analysis.log import AnalysisError

from tests.synthlog import clean_pull_columns, const, ramp, write_log


def test_actual_gear_and_mgstk_airmass(tmp_path):
    """R04-style header: `Gear (gear)` = actual, `Airmass (g/stk)` -> mg/stk."""
    cols = clean_pull_columns(gear_header="Gear (gear)", gear_value=3.0,
                              airmass_header="Airmass (g/stk)")
    lf = load_logfile(write_log(tmp_path / "simostools-a.csv", cols))

    assert lf.gear_resolution == GearResolution.ACTUAL
    assert lf.gear_resolved
    np.testing.assert_allclose(lf.channel("gear"), 3.0)
    # g/stk normalized to mg/stk: first row 0.95 g -> 950 mg.
    assert lf.channel("airmass")[0] == pytest.approx(950.0)
    assert lf.channel("airmass")[-1] == pytest.approx(1490.0)


def test_logged_gear_gets_plus_one(tmp_path):
    """R01-style `Gear ()` is zero-indexed: actual gear = logged + 1."""
    cols = clean_pull_columns(gear_header="Gear ()", gear_value=2.0,
                              airmass_header="Airmass (mg/stk)")
    lf = load_logfile(write_log(tmp_path / "simostools-b.csv", cols))

    assert lf.gear_resolution == GearResolution.LOGGED_PLUS_ONE
    assert lf.gear_resolved
    # logged 2 -> actual 3rd gear.
    np.testing.assert_allclose(lf.channel("gear"), 3.0)


def test_unknown_gear_form_is_unresolved(tmp_path):
    """`Gear (idx)` is neither header form -> unresolved, no load-time error."""
    cols = clean_pull_columns(gear_header="Gear (idx)", gear_value=3.0)
    lf = load_logfile(write_log(tmp_path / "simostools-c.csv", cols))

    assert lf.gear_resolution == GearResolution.UNRESOLVED
    assert not lf.gear_resolved
    assert not lf.has("gear")           # not mapped; never guessed
    assert "Gear (idx)" in lf.unmapped_headers


def test_duplicate_canonical_channel_fails_loud(tmp_path):
    """Two columns mapping to the same canonical channel is corruption."""
    n = 5
    cols = {
        "Time": ramp(0.0, 0.2, n),
        "Engine Speed (rpm)": const(3000.0, n),
        "Airmass (mg/stk)": const(900.0, n),
        "Airmass (g/stk)": const(0.9, n),   # both -> canonical `airmass`
    }
    path = write_log(tmp_path / "simostools-dup.csv", cols)
    with pytest.raises(DuplicateChannelError):
        load_logfile(path)


def test_unrecognized_unit_left_unmapped_not_guessed(tmp_path):
    """A known channel with an unrecognized unit is reported, not mis-scaled."""
    n = 5
    cols = {
        "Time": ramp(0.0, 0.2, n),
        "Engine Speed (rpm)": const(3000.0, n),
        "PUT (mbar)": const(2400.0, n),     # PUT known; mbar not a recognized unit
    }
    lf = load_logfile(write_log(tmp_path / "simostools-unit.csv", cols))
    assert not lf.has("put")
    assert "PUT (mbar)" in lf.unmapped_headers
    assert ("PUT (mbar)", "put") in lf.quality.unit_unrecognized


def test_unmapped_columns_retained_and_reported(tmp_path):
    n = 5
    cols = {
        "Time": ramp(0.0, 0.2, n),
        "Engine Speed (rpm)": const(3000.0, n),
        "Some Novel Channel (widgets)": const(1.0, n),
    }
    lf = load_logfile(write_log(tmp_path / "simostools-novel.csv", cols))
    assert "Some Novel Channel (widgets)" in lf.unmapped_headers
    # The trailing SimosTools tag column is also unmapped but harmless.
    assert any(h.startswith("SimosTools") for h in lf.unmapped_headers)


def test_time_gap_recorded(tmp_path):
    """A 2 s pause mid-file is recorded in quality metadata with timestamps."""
    n = 20
    time = [i * 0.05 for i in range(10)] + [1.0 + 2.0 + i * 0.05 for i in range(10)]
    cols = {
        "Time": time,
        "Engine Speed (rpm)": ramp(3000.0, 6000.0, n),
    }
    lf = load_logfile(write_log(tmp_path / "simostools-gap.csv", cols))
    assert len(lf.quality.gaps) == 1
    gap = lf.quality.gaps[0]
    assert gap.index == 10
    assert gap.gap_s == pytest.approx(2.0 + 0.05, abs=1e-6) or gap.gap_s > 2.0


def test_stuck_channel_flagged_over_rpm_sweep(tmp_path):
    """A dynamic channel frozen while rpm sweeps is annotated as stuck."""
    n = 40
    cols = {
        "Time": [i * 0.05 for i in range(n)],
        "Engine Speed (rpm)": ramp(3000.0, 6500.0, n),
        "PUT (kpa)": const(240.0, n),       # frozen while rpm sweeps 3500 rpm
        "Boost (psi)": ramp(20.0, 26.0, n),  # legitimately moving -> not stuck
    }
    lf = load_logfile(write_log(tmp_path / "simostools-stuck.csv", cols))
    assert "put" in lf.quality.stuck_channels
    assert "boost" not in lf.quality.stuck_channels


def test_constant_channel_without_sweep_not_stuck(tmp_path):
    """No rpm sweep -> no stuck-channel false positives (engine effectively idle)."""
    n = 40
    cols = {
        "Time": [i * 0.05 for i in range(n)],
        "Engine Speed (rpm)": const(850.0, n),
        "PUT (kpa)": const(100.0, n),
    }
    lf = load_logfile(write_log(tmp_path / "simostools-idle.csv", cols))
    assert lf.quality.stuck_channels == ()


def test_interval_stats(tmp_path):
    n = 11
    cols = {"Time": [i * 0.05 for i in range(n)], "Engine Speed (rpm)": const(3000.0, n)}
    lf = load_logfile(write_log(tmp_path / "simostools-int.csv", cols))
    assert lf.quality.interval_median_s == pytest.approx(0.05)
    assert lf.quality.n_rows == n


def test_empty_folder_fails_loud(tmp_path):
    with pytest.raises(AnalysisError, match="no simostools"):
        load_logset(tmp_path)


def test_logset_channels_union_and_has(tmp_path):
    write_log(tmp_path / "simostools-1.csv",
              clean_pull_columns(airmass_header="Airmass (mg/stk)"))
    # Second file lacks the knock channels.
    n = 10
    write_log(tmp_path / "simostools-2.csv", {
        "Time": ramp(0.0, 0.5, n),
        "Engine Speed (rpm)": ramp(3000.0, 5000.0, n),
        "Gear (gear)": const(3.0, n),
        "PUT (kpa)": const(240.0, n),
        "PUT SP (kpa)": const(230.0, n),
    })
    ls = load_logset(tmp_path)
    assert len(ls) == 2
    assert "put" in ls.channels()
    assert "knock_3" in ls.channels()      # union: present in file 1
    assert ls.has("put")                   # in every file
    assert not ls.has("knock_3")           # not in file 2
