"""U6 — acceptance regression replay against the human-reviewed R01/R04 logs.

Encodes the ground truth from ``Logs/BasicsGuide_R01/log_review.md`` (AE1) and
``Logs/BasicsGuide_R04/log_review.md`` (AE2): the tool must reproduce the
headline findings and, crucially, emit **no false High** — every High it reports
is one the human review also called High. Assertions are on finding *content*
with tolerances (severity, peak values within a few kPa, recurrence counts),
never on message strings (plan Test Strategy item 6).

These read the real folders read-only (no analyze_folder, so nothing is written
into the human's folders) and skip cleanly when the folders are absent.
"""

from __future__ import annotations

import pytest

from simoscal.analysis import (
    CheckContext,
    Severity,
    default_battery,
    detect_pulls,
    load_logset,
    run_battery,
)

# The Highs the human review called on each folder. The knock High on R04 was a
# "resolved" note (good news), not a problem finding, so the tool emitting no
# High knock there is correct — the gate is: tool Highs ⊆ human Highs.
HUMAN_HIGH = {"R01": {"boost", "knock"}, "R04": {"boost"}}


def _analyze(folder):
    ls = load_logset(folder)
    ctx = CheckContext(ls, detect_pulls(ls), cal=None)   # read-only; cal checks SKIP
    return run_battery(default_battery(), ctx)


def _finding(result, check_id):
    fs = [f for f in result.findings if f.check_id == check_id]
    return fs[0] if fs else None


# --------------------------------------------------------------------------- #
# AE1 — R01 headline reproduction
# --------------------------------------------------------------------------- #
def test_r01_high_knock_recurring(r01_log_dir):
    result = _analyze(r01_log_dir)
    knock = _finding(result, "knock")
    assert knock is not None and knock.severity == Severity.HIGH
    assert knock.evidence["worst_retard_deg"] <= -3.0 + 0.1     # repeated -3.0 deg regions
    assert len(knock.evidence["recurrence_pulls"]) >= 2         # recurs across pulls


def test_r01_high_boost_overshoot(r01_log_dir):
    result = _analyze(r01_log_dir)
    boost = _finding(result, "boost")
    assert boost is not None and boost.severity == Severity.HIGH
    # Human review: +18 to +26 kPa overshoot pockets.
    assert 16.0 <= boost.evidence["peak_overshoot_kpa"] <= 30.0


def test_r01_no_false_high(r01_log_dir):
    result = _analyze(r01_log_dir)
    high_ids = {f.check_id for f in result.high_findings}
    assert high_ids <= HUMAN_HIGH["R01"], f"unexpected High(s): {high_ids - HUMAN_HIGH['R01']}"


# --------------------------------------------------------------------------- #
# AE2 — R04 headline reproduction
# --------------------------------------------------------------------------- #
def test_r04_knock_resolved(r04_log_dir):
    result = _analyze(r04_log_dir)
    knock = _finding(result, "knock")
    # Knock is clean (0.0 on all four cylinders) → a Low informational finding,
    # never a High problem finding.
    assert knock is not None and knock.severity == Severity.LOW
    assert knock.evidence["worst_retard_deg"] >= -0.1


def test_r04_high_boost_overshoot_peaks(r04_log_dir):
    result = _analyze(r04_log_dir)
    boost = _finding(result, "boost")
    assert boost is not None and boost.severity == Severity.HIGH
    assert boost.evidence["peak_overshoot_kpa"] == pytest.approx(22.2, abs=2.0)
    assert boost.evidence["peak_put_kpa"] == pytest.approx(286.4, abs=2.0)


def test_r04_lambda_not_high(r04_log_dir):
    result = _analyze(r04_log_dir)
    lam = _finding(result, "lambda")
    assert lam is not None and lam.severity != Severity.HIGH
    # Max settled lean ~ +0.023, below the +0.05 High line.
    assert lam.evidence["max_settled_lean_error"] < 0.05


def test_r04_two_third_gear_pulls(r04_log_dir):
    result = _analyze(r04_log_dir)
    assert len(result.pulls) == 2
    assert all(p.gear == 3 for p in result.pulls)


def test_r04_no_false_high(r04_log_dir):
    result = _analyze(r04_log_dir)
    high_ids = {f.check_id for f in result.high_findings}
    assert high_ids <= HUMAN_HIGH["R04"], f"unexpected High(s): {high_ids - HUMAN_HIGH['R04']}"


# --------------------------------------------------------------------------- #
# Determinism on real data (AE5 at folder scale)
# --------------------------------------------------------------------------- #
def test_real_replay_deterministic(r04_log_dir):
    from simoscal.analysis import findings_to_dict

    a = findings_to_dict(_analyze(r04_log_dir))
    b = findings_to_dict(_analyze(r04_log_dir))
    import json

    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
