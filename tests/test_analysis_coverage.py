"""U7 — tests for table coverage maps."""

from __future__ import annotations

import numpy as np
import pytest

from simoscal.analysis import (
    CheckContext,
    CoverageAxis,
    CoverageSpec,
    DEFAULT_COVERAGE_SPECS,
    compute_coverage,
    detect_pulls,
    load_logset,
)

from tests.faultinject import PullSpec, build_folder
from tests.synthlog import const, ramp, write_log


class _FakeView:
    def __init__(self, x, y, shape):
        self._x = np.asarray(x, dtype=float)
        self._y = np.asarray(y, dtype=float)
        self.shape = shape

    def axis_values(self, which):
        return self._x if which == "x" else self._y


class _FakeCal:
    def __init__(self, tables):
        self._tables = tables

    def get(self, symbol):
        if symbol not in self._tables:
            raise KeyError(symbol)
        return self._tables[symbol]


# A 3x4 table: x = rpm breakpoints, y = airmass breakpoints.
_RPM_BP = [2000.0, 3000.0, 4000.0, 5000.0]
_AM_BP = [500.0, 1000.0, 1500.0]
_SPEC = CoverageSpec("T", CoverageAxis("rpm"), CoverageAxis("airmass"), "test table")


def _cal():
    return _FakeCal({"T": _FakeView(_RPM_BP, _AM_BP, (3, 4))})


def _ctx(tmp_path, columns, *, cal=None):
    write_log(tmp_path / "simostools-cov.csv", columns)
    ls = load_logset(tmp_path)
    return CheckContext(ls, detect_pulls(ls), cal=cal)


def test_hits_land_in_expected_cells(tmp_path):
    n = 20
    cols = {
        "Time": [i * 0.05 for i in range(n)],
        "Engine Speed (rpm)": const(2000.0, 10) + const(4000.0, 10),
        "Airmass (mg/stk)": const(500.0, n),
    }
    res, skip = compute_coverage(_ctx(tmp_path, cols, cal=_cal()), specs=(_SPEC,))
    assert skip == []
    counts = np.array(res[0].counts_whole)     # shape (y=3, x=4)
    assert counts[0][0] == 10                   # rpm 2000 (x0), airmass 500 (y0)
    assert counts[0][2] == 10                   # rpm 4000 (x2), airmass 500 (y0)
    assert counts.sum() == 20                   # every sample attributed


def test_beyond_range_clamps_to_edge(tmp_path):
    n = 4
    cols = {
        "Time": [i * 0.05 for i in range(n)],
        "Engine Speed (rpm)": [100.0, 9000.0, 100.0, 9000.0],   # below / above axis
        "Airmass (mg/stk)": const(500.0, n),
    }
    res, _ = compute_coverage(_ctx(tmp_path, cols, cal=_cal()), specs=(_SPEC,))
    counts = np.array(res[0].counts_whole)
    assert counts[0][0] == 2                     # 100 rpm clamps to first cell
    assert counts[0][3] == 2                     # 9000 rpm clamps to last cell
    assert counts.sum() == 4                     # nothing dropped


def test_missing_axis_channel_skipped(tmp_path):
    cols = {
        "Time": [0.0, 0.05],
        "Engine Speed (rpm)": [3000.0, 3000.0],
        # no airmass column
    }
    spec = CoverageSpec("T", CoverageAxis("rpm"), CoverageAxis("airmass"), "t")
    res, skip = compute_coverage(_ctx(tmp_path, cols, cal=_cal()), specs=(spec,))
    assert res == []
    assert len(skip) == 1
    assert "airmass" in skip[0].missing_channels
    assert "airmass" in skip[0].reason


def test_no_cal_all_skipped(tmp_path):
    cols = {"Time": [0.0, 0.05], "Engine Speed (rpm)": [3000.0, 3000.0],
            "Airmass (mg/stk)": [800.0, 800.0]}
    res, skip = compute_coverage(_ctx(tmp_path, cols, cal=None), specs=(_SPEC,))
    assert res == []
    assert len(skip) == 1
    assert "no bin" in skip[0].reason.lower() or "calibration" in skip[0].reason.lower()


def test_totals_equal_sample_count_when_axes_present(tmp_path):
    n = 30
    cols = {
        "Time": [i * 0.05 for i in range(n)],
        "Engine Speed (rpm)": ramp(2000.0, 5000.0, n),
        "Airmass (mg/stk)": ramp(400.0, 1600.0, n),
    }
    res, _ = compute_coverage(_ctx(tmp_path, cols, cal=_cal()), specs=(_SPEC,))
    assert res[0].total_whole == n               # metamorphic invariant


def test_wot_counts_subset_of_whole(tmp_path):
    build_folder(tmp_path, [PullSpec(), PullSpec()])
    ls = load_logset(tmp_path)
    ctx = CheckContext(ls, detect_pulls(ls), cal=_cal())
    res, _ = compute_coverage(ctx, specs=(_SPEC,))
    whole = np.array(res[0].counts_whole)
    wot = np.array(res[0].counts_wot)
    assert np.all(wot <= whole)                  # per-cell subset
    assert 0 < res[0].total_wot < res[0].total_whole


def test_determinism(tmp_path):
    cols = {
        "Time": [i * 0.05 for i in range(20)],
        "Engine Speed (rpm)": ramp(2000.0, 5000.0, 20),
        "Airmass (mg/stk)": ramp(400.0, 1600.0, 20),
    }
    ctx = _ctx(tmp_path, cols, cal=_cal())
    r1, _ = compute_coverage(ctx, specs=(_SPEC,))
    r2, _ = compute_coverage(ctx, specs=(_SPEC,))
    assert r1[0].counts_whole == r2[0].counts_whole


def test_default_specs_resolve_against_real_bin(tmp_path, real_xdf, real_bin):
    """All DEFAULT_COVERAGE_SPECS resolve against the real XDF/bin (mappings valid)."""
    from simoscal import CalFile

    n = 30
    cols = {
        "Time": [i * 0.05 for i in range(n)],
        "Engine Speed (rpm)": ramp(2000.0, 6000.0, n),
        "Airmass (mg/stk)": ramp(400.0, 1500.0, n),
        "MAP SP (kpa)": ramp(100.0, 250.0, n),
        "Intake Flow Fact ()": ramp(0.1, 1.1, n),
        "Exh Flow Factor ()": ramp(0.7, 1.3, n),
    }
    cal = CalFile.open(str(real_xdf), str(real_bin))
    ctx = _ctx(tmp_path, cols, cal=cal)
    res, skip = compute_coverage(ctx)
    resolved = {r.symbol for r in res}
    # Every default spec resolves (no table-missing skips) — the axis mappings
    # are valid against the real bin.
    for spec in DEFAULT_COVERAGE_SPECS:
        assert spec.symbol in resolved, f"{spec.symbol} unexpectedly skipped: {[(s.check_id, s.reason) for s in skip]}"
    assert skip == []
