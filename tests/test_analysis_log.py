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
    load_logset_files,
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
    # Second file lacks the knock channels. Distinct time base (a separate
    # capture) so the duplicate-capture dedup does not fold it into file 1.
    n = 10
    write_log(tmp_path / "simostools-2.csv", {
        "Time": ramp(100.0, 100.5, n),
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


def test_wheel_speed_channels_load(tmp_path):
    """The four `Wheel Speed FL/FR/RL/RR (km/h)` columns load unscaled."""
    cols = clean_pull_columns(wheel_speeds=True)
    lf = load_logfile(write_log(tmp_path / "simostools-wheels.csv", cols))
    for cid in ("wheel_fl", "wheel_fr", "wheel_rl", "wheel_rr"):
        assert lf.has(cid), cid
    # Unscaled: first-row value matches the km/h ramp start (60.0).
    assert lf.channel("wheel_fl")[0] == pytest.approx(60.0)
    assert lf.channel("wheel_rr")[0] == pytest.approx(60.0)


def test_wheel_speed_km_per_hr_spelling(tmp_path):
    """`km/hr` maps with factor 1.0, mirroring vehicle_speed."""
    n = 5
    cols = {
        "Time": ramp(0.0, 0.2, n),
        "Engine Speed (rpm)": const(3000.0, n),
        "Wheel Speed FL (km/hr)": const(80.0, n),
    }
    lf = load_logfile(write_log(tmp_path / "simostools-kmhr.csv", cols))
    assert lf.has("wheel_fl")
    np.testing.assert_allclose(lf.channel("wheel_fl"), 80.0)


def test_wheel_speed_unrecognized_unit_left_unmapped(tmp_path):
    """A wheel-speed column with an unrecognized unit is reported, not guessed."""
    n = 5
    cols = {
        "Time": ramp(0.0, 0.2, n),
        "Engine Speed (rpm)": const(3000.0, n),
        "Wheel Speed FL (mph)": const(50.0, n),     # mph not a recognized unit
    }
    lf = load_logfile(write_log(tmp_path / "simostools-mph.csv", cols))
    assert not lf.has("wheel_fl")
    assert "Wheel Speed FL (mph)" in lf.unmapped_headers
    assert ("Wheel Speed FL (mph)", "wheel_fl") in lf.quality.unit_unrecognized


def test_duplicate_trim_pair_deduped_with_note(tmp_path):
    """A capture and its trimmed re-export (overlapping time) count once."""
    full = clean_pull_columns(n=60, t0=500.0)           # t = 500 .. 502.95
    trim = clean_pull_columns(n=30, t0=500.0)           # a subset re-export
    write_log(tmp_path / "simostools-2026_07_07-22_50_43.csv", full)
    write_log(tmp_path / "simostools-2026_07_07-22_50_43_trim.csv", trim)
    ls = load_logset(tmp_path)
    assert len(ls) == 1                                 # the trim was dropped
    assert ls.files[0].n_rows == 60                     # the larger file survived
    assert ls.notes and "trim" in ls.notes[0]


def test_dedup_keeps_distinct_captures(tmp_path):
    """Two genuinely separate captures (non-overlapping time) are both kept."""
    write_log(tmp_path / "simostools-a.csv", clean_pull_columns(n=60, t0=100.0))
    write_log(tmp_path / "simostools-b.csv", clean_pull_columns(n=60, t0=900.0))
    ls = load_logset(tmp_path)
    assert len(ls) == 2
    assert ls.notes == ()


# --------------------------------------------------------------------------- #
# load_logset_files — the explicit-path form the embedded client uses
# --------------------------------------------------------------------------- #
def test_load_logset_files_takes_explicit_paths(tmp_path):
    """No glob and no folder convention: the app has neither."""
    a = write_log(tmp_path / "aaa.csv", clean_pull_columns(n=40, t0=0.0))
    b = write_log(tmp_path / "bbb.csv", clean_pull_columns(n=40, t0=500.0))
    logset = load_logset_files([a, b])
    assert [f.name for f in logset.files] == ["aaa", "bbb"]
    assert logset.has("rpm")


def test_load_logset_files_honours_display_names(tmp_path):
    """The app's copy is content-addressed, so the recognisable name is passed in."""
    hashed = write_log(tmp_path / "9f86d081884c.csv", clean_pull_columns(n=40, t0=0.0))
    logset = load_logset_files([hashed], names={str(hashed): "sunday morning pull.csv"})
    assert [f.name for f in logset.files] == ["sunday morning pull.csv"]


def test_load_logset_files_still_dedups_overlapping_captures(tmp_path):
    """A trimmed re-export must not double-count its pull, however the file arrived."""
    full = write_log(tmp_path / "full.csv", clean_pull_columns(n=80, t0=0.0))
    trim = write_log(tmp_path / "trim.csv", clean_pull_columns(n=40, t0=0.0))
    logset = load_logset_files([full, trim])
    assert len(logset.files) == 1
    assert logset.files[0].name == "full"          # the superset survives
    assert logset.notes, "the dedup decision is reported, never silent"


def test_load_logset_files_fails_loud_on_an_empty_list(tmp_path):
    with pytest.raises(AnalysisError, match="no log files"):
        load_logset_files([])


def test_load_logset_files_fails_loud_on_a_missing_path(tmp_path):
    real = write_log(tmp_path / "real.csv", clean_pull_columns(n=40, t0=0.0))
    with pytest.raises(AnalysisError, match="not found"):
        load_logset_files([real, tmp_path / "gone.csv"])


def test_load_logset_and_load_logset_files_agree(tmp_path):
    """The folder form delegates to the explicit form; they must not diverge."""
    write_log(tmp_path / "simostools-a.csv", clean_pull_columns(n=40, t0=0.0))
    write_log(tmp_path / "simostools-b.csv", clean_pull_columns(n=40, t0=500.0))
    by_folder = load_logset(tmp_path)
    by_paths = load_logset_files(sorted(tmp_path.glob("simostools-*.csv")), folder=tmp_path)
    assert [f.name for f in by_folder.files] == [f.name for f in by_paths.files]
    assert by_folder.channels() == by_paths.channels()
