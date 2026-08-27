"""Acceptance suite — the AE1-AE9 examples from the Phase 3 visualization plan.

Each test maps to one acceptance example (see
``Docs/brainstorms/2026-07-06-xdf-visualization-module-requirements.md``) and
exercises the public :mod:`simoscal.plot` surface end-to-end. AE1-AE9 need no
real files — the mini fixture (with real decodable bytes) proves every shape,
toggle, before/after, and mismatch rule, so this suite runs fast and always-on.
Output is asserted only by its *observable* contract (files produced/skipped,
panel/axes counts, delta values, mismatch error) — never by pixel-comparing
PNGs (plan Key Decision 12). One real-data pass at the end uses the ``real_cal``
conftest fixture and skips cleanly when the bundled files are absent, consistent
with ``test_acceptance_export.py``.

    AE1  2D plot        surface + heatmap PNGs; every cell value overlaid
    AE2  1D plot        one line PNG; x = breakpoints, y = Z units
    AE3  scalar plot    no file produced
    AE4  2D compare     3-panel heatmap, surface, and column-curves composites
    AE5  1D compare     2-panel composite (overlay + delta)
    AE6  compare toggles can select surface, heatmap, or column curves alone
    AE7  category batch  file set per non-scalar table under the category folder
    AE8  before/after   render_table snapshot compared through compare_tables
    AE9  mismatch        TableMismatchError naming both tables
"""

from __future__ import annotations

import struct
from pathlib import Path

import numpy as np
import pytest

from simoscal import (
    BinImage,
    CalFile,
    TableMismatchError,
    compare_tables,
    parse_xdf,
    plot_table,
    plot_tables,
    render_table,
)
from simoscal.checksum import SC8S50_STRUCTURE

FIXTURES = Path(__file__).parent / "fixtures"
MINI_XDF = FIXTURES / "mini.xdf"


@pytest.fixture(scope="module")
def mini_cal() -> CalFile:
    model = parse_xdf(str(MINI_XDF))
    size = model.base_offset + 0x6000
    buf = bytearray(size)
    off = model.base_offset + 0x1000
    buf[off : off + 200] = struct.pack("<100h", *range(100))
    buf[model.base_offset + 0x2000] = 200
    foff = model.base_offset + 0x4000
    buf[foff : foff + 4] = struct.pack("<f", 12.5)
    xoff = model.base_offset + 0x5000
    buf[xoff : xoff + 10] = struct.pack("<5H", 1000, 2000, 3000, 4000, 5000)
    zoff = model.base_offset + 0x5010
    buf[zoff : zoff + 10] = struct.pack("<5H", 10, 20, 30, 40, 50)
    img = BinImage(buf, region_start=model.region_start, region_size=len(buf))
    return CalFile(model, img, structure=SC8S50_STRUCTURE, float_bug_symbols=frozenset())


def _images(fig):
    return [ax for ax in fig.axes if ax.get_images()]


# --------------------------------------------------------------------------- #
# AE1 — 2D plot: surface + heatmap with value overlay
# --------------------------------------------------------------------------- #
def test_ae1_2d_surface_and_heatmap_with_overlay(mini_cal: CalFile, tmp_path):
    view = mini_cal.get("SYM_10X10")
    paths = plot_table(view, tmp_path)
    assert {p.name for p in paths} == {"SYM_10X10__surface.png", "SYM_10X10__heatmap.png"}
    assert all(p.stat().st_size > 0 for p in paths)

    # The heatmap overlays every cell's value (100 text artifacts).
    from simoscal import plot

    fig = plot._heatmap_figure(render_table(view))
    (ax,) = _images(fig)
    assert len(ax.texts) == view.values.size


# --------------------------------------------------------------------------- #
# AE2 — 1D plot: one line file, breakpoints on x
# --------------------------------------------------------------------------- #
def test_ae2_1d_line(mini_cal: CalFile, tmp_path):
    paths = plot_table(mini_cal.get("PROFILE_1D"), tmp_path)
    assert {p.name for p in paths} == {"PROFILE_1D__line.png"}


# --------------------------------------------------------------------------- #
# AE3 — scalar: nothing produced
# --------------------------------------------------------------------------- #
def test_ae3_scalar_no_file(mini_cal: CalFile, tmp_path):
    assert plot_table(mini_cal.get("SYM_SCALAR"), tmp_path) == []
    assert list(tmp_path.glob("*.png")) == []


# --------------------------------------------------------------------------- #
# AE4 — 2D comparison: 3-panel heatmap composite + surface composite
# --------------------------------------------------------------------------- #
def test_ae4_2d_compare_composites(mini_cal: CalFile, tmp_path):
    from simoscal import plot

    a = render_table(mini_cal.get("SYM_10X10"))
    b = plot.RenderedTable(
        symbol=a.symbol, title=a.title, units=a.units, categories=a.categories,
        x_labels=a.x_labels, y_labels=a.y_labels, x_units=a.x_units,
        y_units=a.y_units, values=a.values + 3.0,
    )
    paths = compare_tables(a, b, tmp_path)
    assert {p.name for p in paths} == {
        "SYM_10X10__compare_surface.png", "SYM_10X10__compare_heatmap.png",
        "SYM_10X10__compare_columns.png",
    }

    delta = plot._delta(a, b)
    fig = plot._compare_heatmap_figure(a, b, delta)
    assert len(_images(fig)) == 3                       # A / B / delta
    assert plot._shared_limits(a, b) == (
        min(float(a.values.min()), float(b.values.min())),
        max(float(a.values.max()), float(b.values.max())),
    )
    lo, hi = plot._diverging_limits(delta)              # zero-centered, symmetric
    assert lo == -hi


# --------------------------------------------------------------------------- #
# AE5 — 1D comparison: 2-panel composite
# --------------------------------------------------------------------------- #
def test_ae5_1d_compare(mini_cal: CalFile, tmp_path):
    from simoscal import plot

    a = render_table(mini_cal.get("PROFILE_1D"))
    b = plot.RenderedTable(
        symbol=a.symbol, title=a.title, units=a.units, categories=a.categories,
        x_labels=a.x_labels, y_labels=None, x_units=a.x_units,
        y_units=a.y_units, values=a.values + 2.0,
    )
    paths = compare_tables(a, b, tmp_path)
    assert {p.name for p in paths} == {"PROFILE_1D__compare_line.png"}
    fig = plot._compare_line_figure(a, b, plot._delta(a, b))
    assert len(fig.axes) == 2


# --------------------------------------------------------------------------- #
# AE6 — comparison toggles
# --------------------------------------------------------------------------- #
def test_ae6_compare_toggles(mini_cal: CalFile, tmp_path):
    from simoscal import plot

    a = render_table(mini_cal.get("SYM_10X10"))
    b = plot.RenderedTable(
        symbol=a.symbol, title=a.title, units=a.units, categories=a.categories,
        x_labels=a.x_labels, y_labels=a.y_labels, x_units=a.x_units,
        y_units=a.y_units, values=a.values + 1.0,
    )
    heat_only = compare_tables(
        a, b, tmp_path / "h", surface=False, columns=False
    )
    assert {p.name for p in heat_only} == {"SYM_10X10__compare_heatmap.png"}
    surf_only = compare_tables(
        a, b, tmp_path / "s", heatmap=False, columns=False
    )
    assert {p.name for p in surf_only} == {"SYM_10X10__compare_surface.png"}
    columns_only = compare_tables(
        a, b, tmp_path / "c", surface=False, heatmap=False
    )
    assert {p.name for p in columns_only} == {"SYM_10X10__compare_columns.png"}


# --------------------------------------------------------------------------- #
# AE7 — category batch
# --------------------------------------------------------------------------- #
def test_ae7_category_batch(mini_cal: CalFile, tmp_path):
    paths = plot_tables(mini_cal, tmp_path, category="Fuel Trim")
    rel = {p.relative_to(tmp_path).as_posix() for p in paths}
    # PROFILE_1D (1D → line) is produced; SYM_SCALAR (scalar) is skipped.
    assert rel == {"Fuel Trim/PROFILE_1D__line.png"}


# --------------------------------------------------------------------------- #
# AE8 — before/after through the same compare path, no second bin
# --------------------------------------------------------------------------- #
def test_ae8_before_after_single_cal(mini_cal: CalFile, tmp_path):
    from simoscal import plot

    view = mini_cal.get("SYM_10X10")
    before = render_table(view)
    snapshot = np.array(before.values)

    view.set(before.values + 4.0)
    paths = compare_tables(before, view, tmp_path)
    assert {p.name for p in paths} == {
        "SYM_10X10__compare_surface.png", "SYM_10X10__compare_heatmap.png",
        "SYM_10X10__compare_columns.png",
    }
    # The pre-edit snapshot survived the edit (render_table does not alias).
    np.testing.assert_array_equal(before.values, snapshot)
    np.testing.assert_allclose(
        plot._delta(before, render_table(view)), 4.0, atol=0.01
    )
    # Restore so the module-scoped fixture stays pristine for other tests.
    view.set(snapshot)


# --------------------------------------------------------------------------- #
# AE9 — mismatch hard-fails naming both tables
# --------------------------------------------------------------------------- #
def test_ae9_mismatch_raises_naming_both(mini_cal: CalFile, tmp_path):
    a = render_table(mini_cal.get("SYM_10X10"))     # 10x10
    b = render_table(mini_cal.get("PROFILE_1D"))    # 1x5
    with pytest.raises(TableMismatchError) as exc:
        compare_tables(a, b, tmp_path)
    msg = str(exc.value)
    assert "SYM_10X10" in msg and "PROFILE_1D" in msg


# --------------------------------------------------------------------------- #
# Real-data pass — skips cleanly when the bundled files are absent
# --------------------------------------------------------------------------- #
def test_plot_real_selection(real_cal, tmp_path):
    symbols = [v.symbol for v in real_cal.unique_tables()[:3] if v.symbol]
    paths = plot_tables(real_cal, tmp_path, symbols=symbols)
    assert all(p.exists() and p.stat().st_size > 0 for p in paths)
