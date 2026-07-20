"""U4 — tests for the v1 check battery, built on fault injection.

Ground truth is exact by construction (plan Test Strategy item 1): a clean
multi-pull base with known defects injected, asserting each check fires at the
right severity/pull **and only there**. Assertions are on finding *content*
(severity, check id, pull refs, evidence within tolerance) — never on message
strings (item 6).
"""

from __future__ import annotations

import numpy as np
import pytest

from simoscal.analysis import (
    CheckContext,
    Severity,
    default_battery,
    detect_pulls,
    load_logset,
    run_battery,
)
from simoscal.analysis.registry import Check

from tests.faultinject import PullSpec, build_folder


def _run(tmp_path, specs, *, cal=None, **kw):
    build_folder(tmp_path, specs, **kw)
    ls = load_logset(tmp_path)
    ctx = CheckContext(ls, detect_pulls(ls), cal=cal)
    return run_battery(default_battery(), ctx)


def _by_id(result, check_id):
    return [f for f in result.findings if f.check_id == check_id]


def _sev(result, check_id):
    fs = _by_id(result, check_id)
    return fs[0].severity if fs else None


# --------------------------------------------------------------------------- #
# Clean base — false-alarm coverage
# --------------------------------------------------------------------------- #
def test_clean_base_no_high_findings(tmp_path):
    result = _run(tmp_path, [PullSpec(), PullSpec()])
    assert result.high_findings == []
    assert _sev(result, "knock") == Severity.LOW
    assert _sev(result, "boost") == Severity.LOW
    assert _sev(result, "lambda") == Severity.LOW


# --------------------------------------------------------------------------- #
# Knock
# --------------------------------------------------------------------------- #
def test_knock_high_recurs_across_two_pulls(tmp_path):
    result = _run(tmp_path, [PullSpec(knock={3: -3.0}), PullSpec(knock={3: -3.0})])
    knock = _by_id(result, "knock")
    assert len(knock) == 1
    assert knock[0].severity == Severity.HIGH
    assert knock[0].evidence["worst_retard_deg"] == pytest.approx(-3.0)
    assert sorted(knock[0].evidence["recurrence_pulls"]) == [1, 2]
    assert knock[0].evidence["channel_moved"] is True
    # False-alarm: boost stays clean on the same log.
    assert _sev(result, "boost") == Severity.LOW


def test_knock_watch_band_is_medium(tmp_path):
    result = _run(tmp_path, [PullSpec(knock={2: -2.0})])
    assert _sev(result, "knock") == Severity.MEDIUM


def test_knock_flat_zero_carries_liveness_caveat(tmp_path):
    """All-zero knock across the log is Low but flagged for a PID-liveness check."""
    result = _run(tmp_path, [PullSpec(), PullSpec()])
    knock = _by_id(result, "knock")[0]
    assert knock.severity == Severity.LOW
    assert knock.evidence["channel_moved"] is False


def test_knock_only_on_injected_cylinder(tmp_path):
    # A single -3 on cyl 4 in pull 1 only → High, referencing pull 1.
    result = _run(tmp_path, [PullSpec(knock={4: -3.0}), PullSpec()])
    knock = _by_id(result, "knock")[0]
    assert knock.severity == Severity.HIGH
    assert knock.pull_refs == (1,)
    assert knock.evidence["recurrence_pulls"] == [1]


# --------------------------------------------------------------------------- #
# Boost
# --------------------------------------------------------------------------- #
def test_boost_overshoot_high(tmp_path):
    result = _run(tmp_path, [PullSpec(put_overshoot=25.0)])
    boost = _by_id(result, "boost")[0]
    assert boost.severity == Severity.HIGH
    assert boost.evidence["peak_overshoot_kpa"] == pytest.approx(25.0, abs=0.5)
    # False-alarm: knock clean.
    assert _sev(result, "knock") == Severity.LOW


def test_boost_watch_band_is_medium(tmp_path):
    result = _run(tmp_path, [PullSpec(put_overshoot=13.0)])
    assert _sev(result, "boost") == Severity.MEDIUM


def test_boost_clean_is_low(tmp_path):
    result = _run(tmp_path, [PullSpec(put_overshoot=4.0)])
    assert _sev(result, "boost") == Severity.LOW


def test_boost_high_only_in_injected_pull(tmp_path):
    result = _run(tmp_path, [PullSpec(), PullSpec(put_overshoot=25.0)])
    boost = _by_id(result, "boost")[0]
    assert boost.severity == Severity.HIGH
    assert boost.evidence["high_pulls"] == [2]


def test_boost_reports_overshoot_zones(tmp_path):
    """A sustained overshoot is surfaced as a zone with duration, not just a peak."""
    result = _run(tmp_path, [PullSpec(put_overshoot=25.0)])
    boost = _by_id(result, "boost")[0]
    zones = boost.evidence["zones"]
    assert zones and zones[0]["sustained"] is True
    assert zones[0]["peak_kpa"] == pytest.approx(25.0, abs=0.5)
    assert zones[0]["duration_s"] > 0.5


def test_boost_sustained_ridge_high_without_peak(tmp_path):
    """A long ridge whose mean clears +15 kPa is High even with peak < +20 (audit 3.2)."""
    result = _run(tmp_path, [PullSpec(put_overshoot=16.0)])
    boost = _by_id(result, "boost")[0]
    assert boost.severity == Severity.HIGH
    assert boost.evidence["peak_overshoot_kpa"] < 20.0


# --------------------------------------------------------------------------- #
# Wastegate authority (audit 3.1 — co-sample, integral-based)
# --------------------------------------------------------------------------- #
def test_wastegate_skipped_without_channels(tmp_path):
    # No wastegate channels logged -> the check cannot run.
    result = _run(tmp_path, [PullSpec()])
    assert "wastegate" in [s.check_id for s in result.skipped]


def test_wastegate_healthy_is_low(tmp_path):
    # Boost tracks and the integral sits near zero -> Low, not out-of-authority.
    result = _run(tmp_path, [PullSpec()], wastegate=True)
    wg = _by_id(result, "wastegate")
    assert wg and wg[0].severity == Severity.LOW


def test_wastegate_out_of_authority_medium(tmp_path):
    # Overshoot AND the integral driven to its opening clamp -> Medium.
    result = _run(tmp_path, [PullSpec(put_overshoot=25.0, freeze={"WG I Value (%)": -30.0})],
                  wastegate=True)
    wg = _by_id(result, "wastegate")[0]
    assert wg.severity == Severity.MEDIUM
    assert wg.evidence["wg_i_min_during_overshoot_pct"] == pytest.approx(-30.0)
    assert wg.evidence["worst_overshoot_kpa"] == pytest.approx(25.0, abs=0.5)


def test_wastegate_overshoot_with_integral_headroom_is_low(tmp_path):
    # Overshoot but the integral has headroom (default -2%) -> Low, not a false Medium.
    result = _run(tmp_path, [PullSpec(put_overshoot=25.0)], wastegate=True)
    wg = _by_id(result, "wastegate")[0]
    assert wg.severity == Severity.LOW


# --------------------------------------------------------------------------- #
# Timing / torque-limiter correlation (audit 3.3)
# --------------------------------------------------------------------------- #
def test_timing_correlates_torque_limiter_when_knock_clean(tmp_path):
    result = _run(tmp_path, [PullSpec(put_overshoot=12.0, freeze={"Torque Lim ()": 64.0})],
                  torque_lim=True)
    timing = _by_id(result, "timing")[0]
    assert timing.evidence["knock_active"] is False
    assert timing.evidence["torque_lim_active"] is True
    torque = _by_id(result, "torque_limiter")[0]
    # The limiter window carries its correlated timing/boost state.
    assert torque.evidence["ign_min_during_limiter_deg"] is not None
    assert torque.evidence["put_err_during_limiter_kpa"] == pytest.approx(12.0, abs=0.5)


# --------------------------------------------------------------------------- #
# Lambda
# --------------------------------------------------------------------------- #
def test_lambda_high_lean(tmp_path):
    result = _run(tmp_path, [PullSpec(lambda_error=0.06)])
    lam = _by_id(result, "lambda")[0]
    assert lam.severity == Severity.HIGH
    assert lam.evidence["max_settled_lean_error"] == pytest.approx(0.06, abs=0.005)


def test_lambda_watch_band_is_medium(tmp_path):
    result = _run(tmp_path, [PullSpec(lambda_error=0.04)])
    assert _sev(result, "lambda") == Severity.MEDIUM


def test_lambda_below_watch_is_low(tmp_path):
    result = _run(tmp_path, [PullSpec(lambda_error=0.02)])
    assert _sev(result, "lambda") == Severity.LOW


def test_lambda_lean_attributed_to_correct_pull(tmp_path):
    result = _run(tmp_path, [PullSpec(), PullSpec(lambda_error=0.06)])
    lam = _by_id(result, "lambda")[0]
    assert lam.severity == Severity.HIGH
    assert lam.pull_refs == (2,)


# --------------------------------------------------------------------------- #
# Data quality
# --------------------------------------------------------------------------- #
def test_gap_before_pull_is_flagged(tmp_path):
    result = _run(tmp_path, [PullSpec(), PullSpec()], gap_before_pull=2, gap_seconds=2.0)
    dq = _by_id(result, "data_quality")
    assert any("gap" in f.message for f in dq)


def test_frozen_dynamic_channel_flagged_stuck(tmp_path):
    # Freeze PUT (a dynamic channel) while rpm sweeps → stuck-channel finding.
    result = _run(tmp_path, [PullSpec(freeze={"PUT (kpa)": 240.0})])
    dq = _by_id(result, "data_quality")
    assert any(f.evidence.get("channel") == "put" for f in dq)
    assert all(f.severity in (Severity.LOW, Severity.MEDIUM) for f in dq)


# --------------------------------------------------------------------------- #
# Calibration-aware check gating
# --------------------------------------------------------------------------- #
def test_boost_cal_skipped_without_bin(tmp_path):
    result = _run(tmp_path, [PullSpec()], cal=None)
    assert "boost_cal" in [s.check_id for s in result.skipped]
    assert "boost_cal" not in result.ran


def test_boost_cal_runs_with_fake_cal(tmp_path):
    # Symbol stores hPa; the check converts to kPa (/10). 350000 hPa -> 35000 kPa,
    # well above the synth setpoint peak (~250 kPa) -> Low with a large margin.
    class FakeView:
        values = np.array([[349000.0, 350000.0], [348000.0, 347000.0]])

    class FakeCal:
        def get(self, symbol):
            return FakeView()

    result = _run(tmp_path, [PullSpec()], cal=FakeCal())
    assert "boost_cal" in result.ran
    cal_findings = _by_id(result, "boost_cal")
    assert cal_findings
    ev = cal_findings[0].evidence
    assert ev["ceiling_kpa"] == pytest.approx(35000.0)
    assert cal_findings[0].severity == Severity.LOW           # setpoint well under ceiling
    assert ev["setpoint_channel"] == "put_sp"                 # map_sp not logged -> falls back


def test_boost_cal_flags_setpoint_over_ceiling(tmp_path):
    # A ceiling of 2000 hPa == 200 kPa; the synth setpoint peaks ~250 kPa -> Medium.
    class FakeView:
        values = np.array([2000.0])

    class FakeCal:
        def get(self, symbol):
            return FakeView()

    result = _run(tmp_path, [PullSpec()], cal=FakeCal())
    cal = _by_id(result, "boost_cal")[0]
    assert cal.severity == Severity.MEDIUM
    assert cal.evidence["ceiling_kpa"] == pytest.approx(200.0)


def test_p0234_runs_with_cal_and_ambient(tmp_path):
    # Threshold 3000 hPa; synth PUT peaks ~250 kPa, ambient 101 kPa -> diff ~149 kPa
    # == ~1490 hPa, under 3000 -> Low with a positive margin.
    class FakeView:
        values = np.array([3000.0])

    class FakeCal:
        def get(self, symbol):
            return FakeView()

    result = _run(tmp_path, [PullSpec()], cal=FakeCal())
    assert "boost_p0234" in result.ran
    p = _by_id(result, "boost_p0234")[0]
    assert p.severity == Severity.LOW
    assert p.evidence["threshold_hpa"] == pytest.approx(3000.0)
    assert p.evidence["logged_put_minus_ambient_hpa"] > 0


def test_p0234_flags_overboost_exposure(tmp_path):
    # Threshold 1000 hPa == 100 kPa differential; synth diff ~149 kPa exceeds -> Medium.
    class FakeView:
        values = np.array([1000.0])

    class FakeCal:
        def get(self, symbol):
            return FakeView()

    result = _run(tmp_path, [PullSpec()], cal=FakeCal())
    p = _by_id(result, "boost_p0234")[0]
    assert p.severity == Severity.MEDIUM


def test_p0234_skipped_without_bin(tmp_path):
    result = _run(tmp_path, [PullSpec()], cal=None)
    assert "boost_p0234" in [s.check_id for s in result.skipped]


# --------------------------------------------------------------------------- #
# Metamorphic invariance (plan Test Strategy item 2)
# --------------------------------------------------------------------------- #
def _finding_signature(result):
    return sorted((f.check_id, f.severity, tuple(f.pull_refs)) for f in result.findings)


def test_gear_form_invariance(tmp_path):
    """`Gear (gear)`=3 and `Gear ()`=2 describe the same pull → same findings."""
    a = tmp_path / "actual"
    b = tmp_path / "logged"
    a.mkdir(); b.mkdir()
    specs = [PullSpec(knock={3: -3.0}, put_overshoot=25.0)]
    build_folder(a, specs, gear_header="Gear (gear)")
    build_folder(b, [PullSpec(gear=2.0, knock={3: -3.0}, put_overshoot=25.0)],
                 gear_header="Gear ()")
    ra = run_battery(default_battery(), CheckContext(load_logset(a), detect_pulls(load_logset(a))))
    rb = run_battery(default_battery(), CheckContext(load_logset(b), detect_pulls(load_logset(b))))
    assert _finding_signature(ra) == _finding_signature(rb)


def test_airmass_unit_invariance(tmp_path):
    """mg/stk and g/stk headers describe the same airmass → same findings."""
    a = tmp_path / "mg"
    b = tmp_path / "g"
    a.mkdir(); b.mkdir()
    specs = [PullSpec(put_overshoot=25.0)]
    build_folder(a, specs, airmass_header="Airmass (mg/stk)")
    build_folder(b, specs, airmass_header="Airmass (g/stk)")
    ra = run_battery(default_battery(), CheckContext(load_logset(a), detect_pulls(load_logset(a))))
    rb = run_battery(default_battery(), CheckContext(load_logset(b), detect_pulls(load_logset(b))))
    assert _finding_signature(ra) == _finding_signature(rb)


def test_dropping_channel_grows_skipped_not_findings(tmp_path):
    """Dropping PUT SP → boost SKIPS; no new findings appear."""
    full = tmp_path / "full"
    dropped = tmp_path / "dropped"
    full.mkdir(); dropped.mkdir()
    build_folder(full, [PullSpec(put_overshoot=25.0)])
    inj = build_folder(dropped, [PullSpec(put_overshoot=25.0)])
    # Rewrite the dropped folder's CSV without the PUT SP column.
    import csv
    src = next(dropped.glob("simostools-*.csv"))
    rows = list(csv.reader(src.open()))
    header = rows[0]
    drop_i = header.index("PUT SP (kpa)")
    with src.open("w", newline="") as fh:
        w = csv.writer(fh)
        for r in rows:
            w.writerow([c for i, c in enumerate(r) if i != drop_i])

    rf = run_battery(default_battery(), CheckContext(load_logset(full), detect_pulls(load_logset(full))))
    rd = run_battery(default_battery(), CheckContext(load_logset(dropped), detect_pulls(load_logset(dropped))))
    assert "boost" not in rd.ran and "boost" in [s.check_id for s in rd.skipped]
    assert not _by_id(rd, "boost")
    # Dropping a channel only removes findings; no new check id appears.
    assert {f.check_id for f in rd.findings} <= {f.check_id for f in rf.findings}
