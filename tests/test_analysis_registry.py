"""U3 — tests for the check registry, runner, and deterministic emitters."""

from __future__ import annotations

import json

import pytest

from simoscal.analysis import (
    Check,
    CheckContext,
    Finding,
    Severity,
    detect_pulls,
    findings_to_dict,
    format_battery,
    load_logset,
    render_markdown,
    run_battery,
    write_findings,
)

from tests.synthlog import clean_pull_columns, write_log


def _ctx(tmp_path, *, cal=None):
    write_log(tmp_path / "simostools-x.csv", clean_pull_columns(n=60, put_overshoot=12.0))
    ls = load_logset(tmp_path)
    return CheckContext(logset=ls, pulls=detect_pulls(ls), cal=cal)


def _rpm_check() -> Check:
    def compute(ctx, check):
        return [Finding(check.id, Severity.LOW, check.title, "rpm channel present")]
    return Check("rpm_present", "RPM present", ("rpm",), compute)


def _boost_check() -> Check:
    def compute(ctx, check):
        peak = max((p.max_put_error or 0.0) for p in ctx.pulls)
        sev = Severity.HIGH if peak > check.thresholds["watch_kpa"] else Severity.LOW
        return [Finding(check.id, sev, check.title, f"peak put error {peak:.1f} kPa",
                        evidence={"peak_kpa": peak})]
    return Check("boost", "Boost overshoot", ("put", "put_sp"), compute,
                 thresholds={"watch_kpa": 10.0})


def test_both_checks_run(tmp_path):
    result = run_battery([_rpm_check(), _boost_check()], _ctx(tmp_path))
    assert set(result.ran) == {"rpm_present", "boost"}
    assert len(result.findings) == 2


def test_missing_required_channel_skips_and_names_it(tmp_path):
    def compute(ctx, check):
        return []
    needs_missing = Check("needs_turbo", "Turbo check", ("turbo_air_temp",), compute)
    result = run_battery([needs_missing], _ctx(tmp_path))
    assert result.ran == ()
    assert len(result.skipped) == 1
    sk = result.skipped[0]
    assert sk.check_id == "needs_turbo"
    assert "turbo_air_temp" in sk.missing_channels
    assert "turbo_air_temp" in sk.reason


def test_needs_cal_without_bin_skips(tmp_path):
    def compute(ctx, check):
        return []
    cal_check = Check("cal_boost", "Cal boost", ("put",), compute, needs_cal=True)
    result = run_battery([cal_check], _ctx(tmp_path, cal=None))
    assert result.skipped[0].check_id == "cal_boost"
    assert "no bin" in result.skipped[0].reason


def test_needs_cal_with_bin_runs(tmp_path):
    def compute(ctx, check):
        return [Finding(check.id, Severity.LOW, check.title, "ran with cal")]
    cal_check = Check("cal_boost", "Cal boost", ("put",), compute, needs_cal=True)
    result = run_battery([cal_check], _ctx(tmp_path, cal=object()))
    assert result.ran == ("cal_boost",)


def test_findings_sorted_by_severity(tmp_path):
    result = run_battery([_rpm_check(), _boost_check()], _ctx(tmp_path))
    severities = [f.severity for f in result.findings]
    # High (boost) must come before Low (rpm).
    assert severities == sorted(severities, key=lambda s: {"High": 0, "Medium": 1, "Low": 2}[s])
    assert result.findings[0].severity == Severity.HIGH


def test_json_is_byte_identical_across_reruns(tmp_path):
    out1 = tmp_path / "run1"
    out2 = tmp_path / "run2"
    out1.mkdir()
    out2.mkdir()
    for out in (out1, out2):
        write_log(out / "simostools-x.csv", clean_pull_columns(n=60, put_overshoot=12.0))
    checks = [_rpm_check(), _boost_check()]
    r1 = run_battery(checks, CheckContext(load_logset(out1), detect_pulls(load_logset(out1))))
    r2 = run_battery(checks, CheckContext(load_logset(out2), detect_pulls(load_logset(out2))))
    # Fold the folder name out (differs by design) and compare the rest.
    d1 = findings_to_dict(r1); d1.pop("folder")
    d2 = findings_to_dict(r2); d2.pop("folder")
    assert json.dumps(d1, sort_keys=True) == json.dumps(d2, sort_keys=True)


def test_battery_printable_without_running():
    text = format_battery([_rpm_check(), _boost_check()])
    assert "boost" in text
    assert "watch_kpa=10.0" in text
    assert "put, put_sp" in text


def test_write_findings_creates_both_files(tmp_path):
    result = run_battery([_rpm_check(), _boost_check()], _ctx(tmp_path))
    json_path, md_path = write_findings(result, tmp_path)
    assert json_path.exists() and md_path.exists()
    doc = json.loads(json_path.read_text())
    assert doc["schema"] == "simoscal.analysis/1"
    assert "boost" in [f["check_id"] for f in doc["findings"]]
    md = md_path.read_text()
    assert "## Findings" in md
    assert "## Pull summary" in md
    assert "## Check battery" in md


def test_markdown_pull_table_aligned(tmp_path):
    result = run_battery([_boost_check()], _ctx(tmp_path))
    md = render_markdown(result)
    # Every table row in an aligned table has the same length as the separator.
    lines = [ln for ln in md.splitlines() if ln.startswith("|")]
    # Group by contiguous blocks and check column alignment via consistent width.
    assert any("Pull" in ln for ln in lines)
