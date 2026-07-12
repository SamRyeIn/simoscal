"""U5 — tests for analyze_folder(), bin autolocation, and evidence plots."""

from __future__ import annotations

import json

import pytest

from simoscal.analysis import analyze_folder
from simoscal.analysis.log import AnalysisError

from tests.faultinject import PullSpec, build_folder


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


def test_bin_override_runs_cal_check(tmp_path, real_xdf, real_bin):
    """With an explicit bin+XDF, the calibration-aware check runs (AE4)."""
    build_folder(tmp_path, [PullSpec(put_overshoot=25.0)])
    out = analyze_folder(tmp_path, xdf_path=str(real_xdf), bin_path=str(real_bin))
    assert out.result.cal_resolved
    assert "boost_cal" in out.result.ran
    doc = json.loads(out.json_path.read_text())
    assert doc["cal_resolved"] is True
