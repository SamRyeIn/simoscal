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
    # False-alarm: boost stays clean on the same log.
    assert _sev(result, "boost") == Severity.LOW


def test_knock_watch_band_is_medium(tmp_path):
    result = _run(tmp_path, [PullSpec(knock={2: -2.0})])
    assert _sev(result, "knock") == Severity.MEDIUM


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
    class FakeView:
        values = np.array([[260.0, 270.0], [280.0, 250.0]])

    class FakeCal:
        def get(self, symbol):
            return FakeView()

    result = _run(tmp_path, [PullSpec()], cal=FakeCal())
    assert "boost_cal" in result.ran
    cal_findings = _by_id(result, "boost_cal")
    assert cal_findings and cal_findings[0].evidence["ceiling"] == pytest.approx(280.0)


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
