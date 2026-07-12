"""U2 — tests for WOT pull detection and the per-pull summary."""

from __future__ import annotations

import pytest

from simoscal.analysis import detect_pulls, load_logset
from simoscal.analysis.pulls import MIN_RPM_SPAN

from tests.synthlog import clean_pull_columns, const, idle_columns, ramp, write_log


def _concat(*colsets: dict[str, list[float]]) -> dict[str, list[float]]:
    """Stack several column dicts (same keys) end to end into one log."""
    keys = colsets[0].keys()
    out: dict[str, list[float]] = {k: [] for k in keys}
    for cs in colsets:
        assert cs.keys() == keys, "column sets must share the same headers"
        for k in keys:
            out[k].extend(cs[k])
    return out


def _two_pull_columns() -> dict[str, list[float]]:
    """Idle, pull, idle, pull, idle — two clean 3rd-gear pulls."""
    idle_keys = clean_pull_columns(n=1).keys()

    def idle(n, t0):
        # An idle stretch with the SAME columns as the pull (so we can concat).
        cols = clean_pull_columns(n=n, t0=t0)
        for k in cols:
            if k == "Time":
                continue
            if k == "Engine Speed (rpm)":
                cols[k] = const(850.0, n)
            elif k in ("Pedal Pos (%)", "TPS (%)"):
                cols[k] = const(0.0, n)
        return cols

    p1 = clean_pull_columns(n=60, t0=0.0)
    gap1 = idle(40, t0=3.0)
    p2 = clean_pull_columns(n=60, t0=5.0)
    gap2 = idle(20, t0=8.0)
    return _concat(p1, gap1, p2, gap2)


def test_two_clean_pulls_detected(tmp_path):
    cols = _two_pull_columns()
    write_log(tmp_path / "simostools-two.csv", cols)
    pulls = detect_pulls(load_logset(tmp_path))

    assert len(pulls) == 2
    assert [p.index for p in pulls] == [1, 2]
    for p in pulls:
        assert p.gear == 3
        assert p.gear_resolved
        assert p.rpm_max - p.rpm_min >= MIN_RPM_SPAN
        assert p.n_samples == 60


def test_long_lift_splits_not_merges(tmp_path):
    """A lift longer than the bridge window must not merge two pulls into one."""
    p1 = clean_pull_columns(n=60, t0=0.0)
    # A long lift (pedal 0, rpm coasting) far exceeding BRIDGE_SAMPLES.
    n_lift = 30
    lift = clean_pull_columns(n=n_lift, t0=3.0)
    for k in lift:
        if k == "Time":
            continue
        if k == "Engine Speed (rpm)":
            lift[k] = const(2000.0, n_lift)
        elif k in ("Pedal Pos (%)", "TPS (%)"):
            lift[k] = const(0.0, n_lift)
    p2 = clean_pull_columns(n=60, t0=5.0)
    write_log(tmp_path / "simostools-lift.csv", _concat(p1, lift, p2))
    pulls = detect_pulls(load_logset(tmp_path))
    assert len(pulls) == 2   # split, not merged into one


def test_short_blip_rejected_by_min_duration(tmp_path):
    """A brief WOT stab that does not sweep rpm is not a pull."""
    n = 8
    cols = {
        "Time": [i * 0.05 for i in range(n)],
        "Engine Speed (rpm)": const(3000.0, n),   # no rpm sweep
        "Gear (gear)": const(3.0, n),
        "Pedal Pos (%)": const(100.0, n),
        "TPS (%)": const(85.0, n),
    }
    write_log(tmp_path / "simostools-blip.csv", cols)
    assert detect_pulls(load_logset(tmp_path)) == []


def test_unresolved_gear_pull_detected_but_gear_none(tmp_path):
    cols = clean_pull_columns(n=60, gear_header="Gear (idx)")
    write_log(tmp_path / "simostools-nogear.csv", cols)
    pulls = detect_pulls(load_logset(tmp_path))
    assert len(pulls) == 1
    assert pulls[0].gear is None
    assert not pulls[0].gear_resolved


def test_environment_absent_reported_unavailable(tmp_path):
    """Missing environment channels are None, never 0 or blank."""
    cols = clean_pull_columns(n=60)
    del cols["Ambient Temp (°C)"]
    del cols["Eth Content (%)"]
    write_log(tmp_path / "simostools-noenv.csv", cols)
    p = detect_pulls(load_logset(tmp_path))[0]
    assert p.environment.ambient_temp_c is None
    assert p.environment.eth_content_pct is None
    # Present ones are populated (taken at pull start).
    assert p.environment.coolant_temp_c == pytest.approx(95.0)
    assert p.environment.iat_start_c == pytest.approx(25.0)


def test_pull_metrics_populated(tmp_path):
    cols = clean_pull_columns(n=60, put_overshoot=15.0, knock_cyl3=-3.0)
    write_log(tmp_path / "simostools-metrics.csv", cols)
    p = detect_pulls(load_logset(tmp_path))[0]
    assert p.min_knock == pytest.approx(-3.0)
    assert p.max_put_error == pytest.approx(15.0, abs=1e-6)
    assert p.max_put is not None and p.max_put > 240.0
    assert p.airmass_max == pytest.approx(1490.0)
