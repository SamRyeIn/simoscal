"""U5 — tests for analyze_folder(), bin autolocation, and evidence plots."""

from __future__ import annotations

import json

import numpy as np
import pytest

from simoscal.analysis import analyze_folder, detect_pulls, load_logset
from simoscal.analysis.evidence import _contiguous_runs, _pull_time_spans
from simoscal.analysis.log import AnalysisError

from tests.faultinject import PullSpec, build_folder
from tests.synthlog import clean_pull_columns, write_log


def test_happy_path_writes_all_outputs(tmp_path):
    build_folder(tmp_path, [PullSpec(put_overshoot=25.0), PullSpec()])
    out = analyze_folder(tmp_path)
    assert out.json_path.exists() and out.md_path.exists()
    assert out.plot_paths                                   # at least some plots drawn
    for p in out.plot_paths.values():
        assert p.exists() and p.stat().st_size > 0
    # A fired finding carries its plot reference.
    boost = [f for f in out.result.findings if f.check_id == "boost"][0]
    assert boost.plot_refs and boost.plot_refs[0].startswith("plots/")


def test_no_bin_resolved_skips_needs_cal(tmp_path):
    # A synthetic folder outside any Tunes/ tree: no bin to autolocate.
    build_folder(tmp_path, [PullSpec()])
    out = analyze_folder(tmp_path)
    assert not out.result.cal_resolved
    assert "boost_cal" in [s.check_id for s in out.result.skipped]
    assert "boost_cal" not in out.result.ran


def test_empty_folder_fails_loud(tmp_path):
    (tmp_path / "not_a_log.txt").write_text("nope")
    with pytest.raises(AnalysisError, match="no simostools"):
        analyze_folder(tmp_path)


def test_rerun_json_byte_identical(tmp_path):
    build_folder(tmp_path, [PullSpec(put_overshoot=25.0, knock={3: -3.0})])
    out1 = analyze_folder(tmp_path)
    bytes1 = out1.json_path.read_bytes()
    out2 = analyze_folder(tmp_path)               # overwrites in place
    bytes2 = out2.json_path.read_bytes()
    assert bytes1 == bytes2                        # deterministic (AE5)


def test_no_plots_flag(tmp_path):
    build_folder(tmp_path, [PullSpec()])
    out = analyze_folder(tmp_path, make_plots=False)
    assert out.plot_paths == {}
    assert not (tmp_path / "plots").exists()


# --------------------------------------------------------------------------- #
# U2 — line-encoding restyle of the per-check plots
# --------------------------------------------------------------------------- #
def test_all_per_check_plots_render(tmp_path):
    """With every channel present, all six per-check PNGs (+ ignition) render (AE1/AE6)."""
    build_folder(tmp_path, [PullSpec(put_overshoot=25.0), PullSpec()],
                 wastegate=True, ign_table=True)
    out = analyze_folder(tmp_path)
    for key in ("boost", "knock", "lambda", "rail_pressure", "turbo_heat",
                "wastegate", "ignition"):
        assert key in out.plot_paths, f"missing plot: {key}"
        assert out.plot_paths[key].exists() and out.plot_paths[key].stat().st_size > 0


def test_contiguous_runs_splits_mask_holes():
    """A masked line must never bridge a hole: runs are the contiguous True spans."""
    m = np.array([False, True, True, False, True, False, False, True])
    assert _contiguous_runs(m) == [(1, 2), (4, 4), (7, 7)]
    assert _contiguous_runs(np.array([True])) == [(0, 0)]         # single sample: no crash
    assert _contiguous_runs(np.array([False, False])) == []       # nothing selected


# --------------------------------------------------------------------------- #
# U3 — ignition timing plot
# --------------------------------------------------------------------------- #
def test_ignition_reference_absent_still_draws(tmp_path):
    """`Ign Avg` present, `Ign Table` absent → the plot draws the primary alone."""
    build_folder(tmp_path, [PullSpec()])                 # clean base carries Ign Avg only
    out = analyze_folder(tmp_path)
    assert "ignition" in out.plot_paths and out.plot_paths["ignition"].exists()


def test_ignition_omitted_without_channels(tmp_path):
    """A log without any ignition channel → no ignition PNG, no error."""
    cols = clean_pull_columns()
    del cols["Ign Avg (°)"]
    write_log(tmp_path / "simostools-noign.csv", cols)
    out = analyze_folder(tmp_path)
    assert "ignition" not in out.plot_paths


# --------------------------------------------------------------------------- #
# U4 — log overview plot with pull windows
# --------------------------------------------------------------------------- #
def test_overview_spans_match_detected_pulls(tmp_path):
    """The overview's shaded spans equal detect_pulls' rows mapped through time (AE2)."""
    inj = build_folder(tmp_path, [PullSpec(put_overshoot=25.0), PullSpec()])
    ls = load_logset(tmp_path)
    pulls = detect_pulls(ls)
    lf = ls.files[0]

    class _Ctx:
        pass
    ctx = _Ctx(); ctx.pulls = pulls
    spans = _pull_time_spans(ctx, lf)
    assert len(spans) == len(pulls) == 2
    t = lf.time
    for (idx, ts, te), p in zip(spans, pulls):
        assert idx == p.index
        assert ts == pytest.approx(float(t[p.start_row]))
        assert te == pytest.approx(float(t[p.end_row]))

    out = analyze_folder(tmp_path)
    assert f"overview:{lf.name}" in out.plot_paths
    assert out.plot_paths[f"overview:{lf.name}"].exists()


def test_overview_one_per_csv(tmp_path):
    """Two distinct captures → two overview PNGs (time axes never concatenate)."""
    write_log(tmp_path / "simostools-a.csv", clean_pull_columns(n=60, t0=0.0))
    write_log(tmp_path / "simostools-b.csv", clean_pull_columns(n=60, t0=900.0))
    out = analyze_folder(tmp_path)
    overview_keys = [k for k in out.plot_paths if k.startswith("overview:")]
    assert len(overview_keys) == 2


def test_overview_zero_pulls_still_renders(tmp_path):
    """A log with no detected pulls still renders traces, just without shading."""
    cols = clean_pull_columns()
    cols["Pedal Pos (%)"] = [0.0] * len(cols["Time"])     # never WOT → no pulls
    cols["TPS (%)"] = [0.0] * len(cols["Time"])
    write_log(tmp_path / "simostools-nopull.csv", cols)
    out = analyze_folder(tmp_path)
    assert any(k.startswith("overview:") for k in out.plot_paths)


# --------------------------------------------------------------------------- #
# U5 — TC activity plot
# --------------------------------------------------------------------------- #
def test_tc_plot_renders_with_wheel_speeds(tmp_path):
    """A log carrying wheel speeds → a TC-activity PNG with the slip panel (AE5)."""
    build_folder(tmp_path, [PullSpec()], wheel_speeds=True, ign_table=True)
    out = analyze_folder(tmp_path)
    tc_keys = [k for k in out.plot_paths if k.startswith("tc_activity:")]
    assert len(tc_keys) == 1
    assert out.plot_paths[tc_keys[0]].exists()


def test_tc_plot_absent_without_wheel_speeds(tmp_path):
    """R04-style log (no wheel speeds) → the TC plot is skipped, no error (AE5)."""
    build_folder(tmp_path, [PullSpec()])
    out = analyze_folder(tmp_path)
    assert not any(k.startswith("tc_activity:") for k in out.plot_paths)


def test_bin_override_runs_cal_check(tmp_path, real_xdf, real_bin):
    """With an explicit bin+XDF, the calibration-aware check runs (AE4)."""
    build_folder(tmp_path, [PullSpec(put_overshoot=25.0)])
    out = analyze_folder(tmp_path, xdf_path=str(real_xdf), bin_path=str(real_bin))
    assert out.result.cal_resolved
    assert "boost_cal" in out.result.ran
    doc = json.loads(out.json_path.read_text())
    assert doc["cal_resolved"] is True
