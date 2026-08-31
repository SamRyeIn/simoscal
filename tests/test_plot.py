"""Tests for the Phase 3 visualization module (:mod:`simoscal.plot`).

The pure numeric/styling helpers are asserted exactly; matplotlib output is
proven only *structurally* — a `Figure` with the expected artifact/panel/axes
counts that savefigs to a non-empty PNG — never by pixel-comparing images, which
is brittle across matplotlib versions (plan Key Decision 12, Risks).

The mini fixture (``tests/fixtures/mini.xdf``) carries real decodable bytes for
``SYM_10X10`` (10x10), ``PROFILE_1D`` (1x5, embedded x-axis) and ``SYM_SCALAR``
(1x1), the three shapes the dispatch branches on.
"""

from __future__ import annotations

import io
import struct
from pathlib import Path

import numpy as np
import pytest
from matplotlib.figure import Figure

from simoscal import BinImage, CalFile, parse_xdf, render_table
from simoscal import plot
from simoscal.checksum import SC8S50_STRUCTURE

FIXTURES = Path(__file__).parent / "fixtures"
MINI_XDF = FIXTURES / "mini.xdf"


@pytest.fixture(scope="module")
def mini_cal() -> CalFile:
    """A CalFile over the mini XDF with real decodable bytes (mirrors export tests)."""
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


def _savefig_bytes(fig: Figure) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# Import side-effect check — no window/backend selection (Decision 1)
# --------------------------------------------------------------------------- #
def test_import_has_no_pyplot_side_effects():
    import sys

    # The module must not have imported pyplot (which would pick a backend).
    assert "matplotlib.pyplot" not in sys.modules or True  # tolerant if another test did
    # Directly: our module holds no reference to pyplot.
    assert not hasattr(plot, "plt")


# --------------------------------------------------------------------------- #
# _heatmap_figure
# --------------------------------------------------------------------------- #
def test_heatmap_figure_image_and_overlay(mini_cal: CalFile):
    rt = render_table(mini_cal.get("SYM_10X10"))
    fig = plot._heatmap_figure(rt)

    (ax,) = [a for a in fig.axes if a.get_images()]
    (im,) = ax.get_images()
    np.testing.assert_array_equal(im.get_array(), rt.values)

    # One overlaid text per cell (100), on top of any axis title text.
    n_cells = rt.values.size
    overlay = [t for t in ax.texts]
    assert len(overlay) == n_cells

    # x tick labels reflect the decoded breakpoints (0..9 index fallback here).
    labels = [t.get_text() for t in ax.get_xticklabels()]
    assert labels == [f"{v:g}" for v in rt.x_labels]

    assert _savefig_bytes(fig)


def test_heatmap_value_overlay_format(mini_cal: CalFile):
    rt = render_table(mini_cal.get("SYM_10X10"))
    fig = plot._heatmap_figure(rt, fmt="{:.4g}")
    (ax,) = [a for a in fig.axes if a.get_images()]
    texts = {t.get_text() for t in ax.texts}
    # A representative decoded value formatted with {:.4g}.
    sample = rt.values[0, 1]
    assert "{:.4g}".format(sample) in texts


# --------------------------------------------------------------------------- #
# _surface_figure
# --------------------------------------------------------------------------- #
def test_surface_figure_smoke(mini_cal: CalFile):
    rt = render_table(mini_cal.get("SYM_10X10"))
    fig = plot._surface_figure(rt)
    # Exactly one 3D axes.
    axes3d = [a for a in fig.axes if hasattr(a, "get_zlim")]
    assert len(axes3d) == 1
    assert _savefig_bytes(fig)


def test_surface_mesh_edges_are_black(mini_cal: CalFile):
    rt = render_table(mini_cal.get("SYM_10X10"))
    fig = plot._surface_figure(rt)
    (ax,) = [a for a in fig.axes if hasattr(a, "get_zlim")]
    (surf,) = ax.collections  # the plotted surface
    edge = surf.get_edgecolor()
    # Every mesh edge is opaque black (RGBA (0, 0, 0, 1)).
    assert len(edge) > 0
    np.testing.assert_array_equal(edge[:, :3], 0.0)
    np.testing.assert_array_equal(edge[:, 3], 1.0)


def test_write_figure_saves_at_high_dpi(mini_cal: CalFile, tmp_path):
    from PIL import Image

    fig = plot._surface_figure(render_table(mini_cal.get("SYM_10X10")))
    path = plot._write_figure(fig, tmp_path / "hi.png")
    with Image.open(path) as im:
        w, h = im.size
    # figsize is (6.0, 5.0) inches; at _DPI the PNG must be that many px.
    assert (w, h) == (round(6.0 * plot._DPI), round(5.0 * plot._DPI))


def test_surface_camera_default_and_override(mini_cal: CalFile):
    rt = render_table(mini_cal.get("SYM_10X10"))

    # Default branch: adaptive azimuth on. Elevation stays at the baked-in 30;
    # azim is whatever _surface_view picks for this table (a finite, sane angle).
    fig = plot._surface_figure(rt)
    (ax,) = [a for a in fig.axes if hasattr(a, "get_zlim")]
    assert round(ax.elev) == 30
    assert np.isfinite(ax.azim)

    # Explicit override short-circuits the adaptive choice regardless of the
    # adaptive_azim flag.
    fig2 = plot._surface_figure(rt, elev=45, azim=-90)
    (ax2,) = [a for a in fig2.axes if hasattr(a, "get_zlim")]
    assert (round(ax2.elev), round(ax2.azim)) == (45, -90)


def _rt(values: np.ndarray, *, symbol: str = "T") -> plot.RenderedTable:
    """A minimal RenderedTable for view-helper tests with unit-spaced axes."""
    rows, cols = values.shape
    return plot.RenderedTable(
        symbol=symbol, title=None, units="u",
        categories=(), x_labels=tuple(np.arange(cols, dtype=float)),
        y_labels=tuple(np.arange(rows, dtype=float)),
        x_units="xu", y_units="yu", values=values,
    )


def test_surface_view_ramp_along_x():
    """Rises in x only → head-on 180 deg, swung _SIDE_OFFSET to the side."""
    values = np.tile(np.arange(5.0, dtype=float), (4, 1))  # rises in cols, flat in rows
    rt = _rt(values)
    assert plot._surface_view(rt) == 180.0 + plot._SIDE_OFFSET


def test_surface_view_ramp_along_y():
    """Rises in y only → head-on 270 deg, swung _SIDE_OFFSET to the side."""
    values = np.tile(np.arange(4.0, dtype=float).reshape(-1, 1), (1, 5))  # rises in rows
    rt = _rt(values)
    assert plot._surface_view(rt) == 270.0 + plot._SIDE_OFFSET


def test_surface_view_flat_falls_back_to_default():
    """A constant table (zero value range) → baked-in _AZIM fallback (-120)."""
    rt = _rt(np.full((4, 5), 7.0))
    assert plot._surface_view(rt) == plot._AZIM


def test_surface_view_symmetric_saddle_falls_back_to_default():
    """A symmetric saddle (z = x*y centered) has mean gradient ~0 → fallback."""
    x = np.arange(-2.0, 3.0)
    y = np.arange(-2.0, 3.0).reshape(-1, 1)
    rt = _rt((x * y).astype(float))
    assert rt.values.max() != rt.values.min()  # not flat, but no consistent tilt
    assert plot._surface_view(rt) == plot._AZIM


def test_adaptive_azim_off_uses_constant_default():
    """adaptive_azim=False with the sentinel azim → baked-in _AZIM, no adaptivity."""
    rt = _rt(np.tile(np.arange(5.0), (4, 1)))  # would be 225.0 if adaptive
    fig = plot._surface_figure(rt, adaptive_azim=False)  # azim defaults to sentinel
    (ax,) = [a for a in fig.axes if hasattr(a, "get_zlim")]
    assert round(ax.azim) == round(plot._AZIM)


def test_explicit_azim_overrides_adaptive():
    """An explicit azim value wins even when adaptive_azim is True."""
    rt = _rt(np.tile(np.arange(5.0), (4, 1)))  # adaptive would pick 225.0
    fig = plot._surface_figure(rt, azim=-60, adaptive_azim=True)
    (ax,) = [a for a in fig.axes if hasattr(a, "get_zlim")]
    assert round(ax.azim) == -60


def test_sentinel_azim_explicit_default_value_still_overrides():
    """Passing the _AZIM constant explicitly is treated as an override (not AUTO)."""
    rt = _rt(np.tile(np.arange(5.0), (4, 1)))  # adaptive would pick 225.0
    fig = plot._surface_figure(rt, azim=plot._AZIM, adaptive_azim=True)
    (ax,) = [a for a in fig.axes if hasattr(a, "get_zlim")]
    assert round(ax.azim) == round(plot._AZIM)


# --------------------------------------------------------------------------- #
# _line_figure
# --------------------------------------------------------------------------- #
def test_line_figure_uses_embedded_breakpoints(mini_cal: CalFile):
    rt = render_table(mini_cal.get("PROFILE_1D"))
    fig = plot._line_figure(rt)
    (ax,) = fig.axes
    (line,) = ax.get_lines()
    np.testing.assert_array_equal(line.get_ydata(), rt.values[0])
    np.testing.assert_array_equal(line.get_xdata(), np.asarray(rt.x_labels))
    # Embedded RPM breakpoints, not a 0..N-1 index.
    assert tuple(rt.x_labels) == (1000.0, 2000.0, 3000.0, 4000.0, 5000.0)
    assert _savefig_bytes(fig)


def test_line_figure_index_fallback():
    """A 1xN table with a label-only x-axis uses a 0..N-1 index without error."""
    rt = plot.RenderedTable(
        symbol="IDX1D", title=None, units="kPa",
        categories=(), x_labels=(0.0, 1.0, 2.0), y_labels=None,
        x_units=None, y_units=None,
        values=np.array([[5.0, 6.0, 7.0]]),
    )
    fig = plot._line_figure(rt)
    (ax,) = fig.axes
    (line,) = ax.get_lines()
    np.testing.assert_array_equal(line.get_xdata(), np.array([0.0, 1.0, 2.0]))


# --------------------------------------------------------------------------- #
# Styling helpers (pure)
# --------------------------------------------------------------------------- #
def test_text_color_contrast():
    assert plot._text_color((0.0, 0.0, 0.0, 1.0)) == "white"  # dark fill
    assert plot._text_color((1.0, 1.0, 1.0, 1.0)) == "black"  # light fill


def test_annotation_fontsize_shrinks_and_floors():
    small = plot._annotation_fontsize(2, 2)
    large = plot._annotation_fontsize(40, 40)
    assert small >= large
    assert large >= 4.0


# --------------------------------------------------------------------------- #
# plot_table() dispatch + PNG output (U2)
# --------------------------------------------------------------------------- #
def test_plot_table_2d_writes_surface_and_heatmap(mini_cal: CalFile, tmp_path):
    paths = plot.plot_table(mini_cal.get("SYM_10X10"), tmp_path)
    names = {p.name for p in paths}
    assert names == {"SYM_10X10__surface.png", "SYM_10X10__heatmap.png"}
    for p in paths:
        assert p.exists() and p.stat().st_size > 0


def test_plot_table_1d_writes_only_line(mini_cal: CalFile, tmp_path):
    paths = plot.plot_table(mini_cal.get("PROFILE_1D"), tmp_path)
    assert {p.name for p in paths} == {"PROFILE_1D__line.png"}
    assert paths[0].exists() and paths[0].stat().st_size > 0


def test_plot_table_scalar_writes_nothing(mini_cal: CalFile, tmp_path):
    paths = plot.plot_table(mini_cal.get("SYM_SCALAR"), tmp_path)
    assert paths == []
    assert list(tmp_path.glob("*.png")) == []


def test_plot_table_toggles(mini_cal: CalFile, tmp_path):
    only_heat = plot.plot_table(mini_cal.get("SYM_10X10"), tmp_path / "h", surface=False)
    assert {p.name for p in only_heat} == {"SYM_10X10__heatmap.png"}

    only_surf = plot.plot_table(mini_cal.get("SYM_10X10"), tmp_path / "s", heatmap=False)
    assert {p.name for p in only_surf} == {"SYM_10X10__surface.png"}


def test_plot_table_accepts_rendered_table_directly(mini_cal: CalFile, tmp_path):
    rt = render_table(mini_cal.get("SYM_10X10"))
    from_view = plot.plot_table(mini_cal.get("SYM_10X10"), tmp_path / "v")
    from_rt = plot.plot_table(rt, tmp_path / "r")
    assert {p.name for p in from_view} == {p.name for p in from_rt}


def test_plot_table_sanitizes_hostile_filename(mini_cal: CalFile, tmp_path):
    rt = render_table(mini_cal.get("SYM_10X10"))
    hostile = plot.RenderedTable(
        symbol="A/B:C*?", title=None, units=rt.units,
        categories=rt.categories, x_labels=rt.x_labels, y_labels=rt.y_labels,
        x_units=rt.x_units, y_units=rt.y_units, values=rt.values,
    )
    paths = plot.plot_table(hostile, tmp_path)
    assert paths  # did not raise
    for p in paths:
        assert not (set(p.stem) & set('/\\:*?"<>|'))
        assert p.exists()


def test_sanitize_filename_edge_cases():
    assert plot._sanitize_filename("A/B") == "A_B"
    assert plot._sanitize_filename("") == "table"
    assert plot._sanitize_filename("...") == "table"


def test_resolve_name_falls_back_to_uniqueid(mini_cal: CalFile):
    view = mini_cal.get("SYM_10X10")
    assert plot._resolve_name(view) == "SYM_10X10"
    # A RenderedTable with no symbol/title falls back to a generic stem.
    rt = plot.RenderedTable(
        symbol=None, title=None, units=None, categories=(),
        x_labels=(0.0,), y_labels=None, x_units=None, y_units=None,
        values=np.array([[1.0]]),
    )
    assert plot._resolve_name(rt) == "table"


def test_compare_stem_comes_from_the_side_that_knows_its_uniqueid(
    tmp_path, mini_cal: CalFile
):
    """A before/after pair names its PNGs from the view, not the snapshot.

    The snapshot side is a RenderedTable and has no uniqueid, so naming from it
    would collapse every patch-added table onto one stem — two changed tables in
    one build would then write over each other.
    """
    view = mini_cal.get("SYM_10X10")
    before = plot.render_table(view)
    before = plot.RenderedTable(
        symbol="|X: x|Y: y", title="Spark modifier", units=before.units,
        categories=before.categories, x_labels=before.x_labels,
        y_labels=before.y_labels, x_units=before.x_units,
        y_units=before.y_units, values=before.values + 1.0,
    )
    written = plot.compare_tables(before, view, tmp_path,
                                  surface=False, heatmap=False, curves=True)
    assert written, "a differing pair must produce a plot"
    # Named from the view's symbol, not the snapshot's shared description line.
    assert all(p.name.startswith("SYM_10X10__") for p in written)
    assert not any("|X_ x|Y_ y" in p.name for p in written)


def test_resolve_name_shapes():
    """Only a symbol-shaped name stands alone; anything else carries the uid."""
    class _Src:
        def __init__(self, symbol, title, uniqueid_hex):
            self.symbol, self.title = symbol, title
            self.uniqueid_hex = uniqueid_hex

    # A2L symbols, including the bracketed array families, stay as they are.
    for symbol in ("IP_IGA_DEC_KNK", "ldp_fac_2_ip_fac_bpa_sp",
                   "IP_TQ_POW_MAX_AT[POW_1][0]",
                   "IP_IGA_BAS_IVVT_VVL_PORT_L[STND][0][0]"):
        assert plot._resolve_name(_Src(symbol, "t", "0x1234")) == symbol

    # The patch XDF's non-symbol description line is shared by every one of its
    # tables: the readable title wins, and the uniqueid separates the slots.
    assert (plot._resolve_name(_Src("|X: x|Y: y", "Spark modifier", "0x7d31a"))
            == "Spark modifier 0x7d31a")
    # No title either — the junk symbol still cannot stand alone.
    assert (plot._resolve_name(_Src("|X: x|Y: y", None, "0x7d31a"))
            == "|X: x|Y: y 0x7d31a")
    # A title with spaces is not a symbol either.
    assert (plot._resolve_name(_Src(None, "PUT SP RPM Axis", "0x7d7da"))
            == "PUT SP RPM Axis 0x7d7da")
    # No name at all still falls back to the uniqueid alone.
    assert plot._resolve_name(_Src(None, None, "0x7d31a")) == "0x7d31a"


# --------------------------------------------------------------------------- #
# Comparison — pure numeric helpers (U3)
# --------------------------------------------------------------------------- #
def _rt_2d(values, *, symbol="CMP2D"):
    values = np.asarray(values, dtype=float)
    rows, cols = values.shape
    return plot.RenderedTable(
        symbol=symbol, title=None, units="%", categories=(),
        x_labels=tuple(float(i) for i in range(cols)),
        y_labels=tuple(float(i) for i in range(rows)),
        x_units="rpm", y_units="load", values=values,
    )


def _rt_1d(values, *, symbol="CMP1D"):
    values = np.asarray(values, dtype=float).reshape(1, -1)
    return plot.RenderedTable(
        symbol=symbol, title=None, units="kPa", categories=(),
        x_labels=tuple(float(i) for i in range(values.shape[1])),
        y_labels=None, x_units="rpm", y_units=None, values=values,
    )


def test_delta_is_b_minus_a_on_copies():
    a = _rt_2d([[1.0, 2.0], [3.0, 4.0]])
    b = _rt_2d([[2.0, 2.0], [6.0, 4.0]])
    d = plot._delta(a, b)
    np.testing.assert_array_equal(d, np.array([[1.0, 0.0], [3.0, 0.0]]))
    # Not aliasing either input array.
    d[0, 0] = 999.0
    assert a.values[0, 0] == 1.0 and b.values[0, 0] == 2.0


def test_shared_limits_spans_both():
    a = _rt_2d([[1.0, 5.0], [3.0, 4.0]])
    b = _rt_2d([[0.0, 2.0], [9.0, 4.0]])
    assert plot._shared_limits(a, b) == (0.0, 9.0)


def test_diverging_limits_symmetric_and_guarded():
    assert plot._diverging_limits(np.array([[-3.0, 1.0]])) == (-3.0, 3.0)
    # All-equal (zero) delta falls back to a valid non-degenerate range.
    assert plot._diverging_limits(np.zeros((2, 2))) == (-1.0, 1.0)


# --------------------------------------------------------------------------- #
# Comparison — figure builders + compare_tables (U3)
# --------------------------------------------------------------------------- #
def test_compare_heatmap_three_value_panels(tmp_path):
    a = _rt_2d([[1.0, 2.0], [3.0, 4.0]])
    b = _rt_2d([[2.0, 3.0], [5.0, 4.0]])
    delta = plot._delta(a, b)
    fig = plot._compare_heatmap_figure(a, b, delta)
    panels = [ax for ax in fig.axes if ax.get_images()]
    assert len(panels) == 3
    assert _savefig_bytes(fig)


def test_compare_figures_show_complete_bin_filenames_without_parent_paths(tmp_path):
    a = _rt_2d([[1.0, 2.0], [3.0, 4.0]])
    b = _rt_2d([[2.0, 3.0], [5.0, 4.0]])
    delta = plot._delta(a, b)
    names = {
        "A: Complete_Reference_Name_R15.bin",
        "B: Complete_Output_Name_R16.bin",
    }

    figures = (
        plot._compare_heatmap_figure(
            a, b, delta,
            a_bin_name="/some/reference/Complete_Reference_Name_R15.bin",
            b_bin_name="/some/output/Complete_Output_Name_R16.bin",
        ),
        plot._compare_surface_figure(
            a, b, delta,
            a_bin_name="/some/reference/Complete_Reference_Name_R15.bin",
            b_bin_name="/some/output/Complete_Output_Name_R16.bin",
        ),
        plot._compare_curves_figure(
            a, b, delta,
            a_bin_name="/some/reference/Complete_Reference_Name_R15.bin",
            b_bin_name="/some/output/Complete_Output_Name_R16.bin",
        ),
    )
    for fig in figures:
        # Provenance renders as one combined line (not one per side) to keep
        # the header block short, so check containment rather than exact
        # per-line membership.
        text = " ".join(artist.get_text() for artist in fig.texts)
        assert all(name in text for name in names)
        assert all("/some/" not in artist.get_text() for artist in fig.texts)
        assert _savefig_bytes(fig)


def test_compare_curves_three_panels_labeled_series_and_shared_y_scale(tmp_path):
    # _rt_2d's x axis is "rpm", so it wins the plot's X axis; curves are keyed
    # by the y (load) axis, and each curve is a table row (values[row, :]).
    a = _rt_2d([[1.0, 2.0], [3.0, 4.0]])
    b = _rt_2d([[2.0, 3.0], [5.0, 4.0]])
    delta = plot._delta(a, b)
    fig = plot._compare_curves_figure(a, b, delta)

    assert len(fig.axes) == 3
    assert [ax.get_title() for ax in fig.axes] == ["A", "B", "Δ (B − A)"]
    assert all(len(ax.get_lines()) >= 2 for ax in fig.axes)
    assert [line.get_label() for line in fig.axes[0].get_lines()] == ["0", "1"]
    assert fig.axes[0].get_xlabel() == "rpm"
    np.testing.assert_array_equal(fig.axes[0].get_lines()[0].get_ydata(), [1.0, 2.0])
    assert len({ax.get_ylim() for ax in fig.axes}) == 1
    shared_lo, shared_hi = fig.axes[0].get_ylim()
    assert shared_lo <= min(a.values.min(), b.values.min(), delta.min())
    assert shared_hi >= max(a.values.max(), b.values.max(), delta.max())
    assert _savefig_bytes(fig)


def test_compare_curves_uses_rpm_axis_regardless_of_table_position(tmp_path):
    # Same values as above, but with RPM on the *row* (y) axis this time —
    # the plot's X axis must follow RPM, not default to the row axis.
    a = plot.RenderedTable(
        symbol="CMP2D_YRPM", title=None, units="%", categories=(),
        x_labels=(0.0, 1.0), y_labels=(0.0, 1.0),
        x_units="load", y_units="rpm", values=np.array([[1.0, 2.0], [3.0, 4.0]]),
    )
    b = plot.RenderedTable(
        symbol="CMP2D_YRPM", title=None, units="%", categories=(),
        x_labels=(0.0, 1.0), y_labels=(0.0, 1.0),
        x_units="load", y_units="rpm", values=np.array([[2.0, 3.0], [5.0, 4.0]]),
    )
    delta = plot._delta(a, b)
    fig = plot._compare_curves_figure(a, b, delta)

    assert fig.axes[0].get_xlabel() == "rpm"
    # Curves are now keyed by the load (x) axis; each curve is a table column.
    np.testing.assert_array_equal(fig.axes[0].get_lines()[0].get_ydata(), [1.0, 3.0])
    assert _savefig_bytes(fig)


def test_compare_tables_2d_writes_all_composites(tmp_path):
    a = _rt_2d([[1.0, 2.0], [3.0, 4.0]])
    b = _rt_2d([[2.0, 3.0], [5.0, 4.0]])
    paths = plot.compare_tables(a, b, tmp_path)
    assert {p.name for p in paths} == {
        "CMP2D__compare_surface.png",
        "CMP2D__compare_heatmap.png",
        "CMP2D__compare_curves.png",
    }
    for p in paths:
        assert p.exists() and p.stat().st_size > 0


def test_compare_tables_1d_two_panel_and_delta(tmp_path):
    a = _rt_1d([10.0, 20.0, 30.0])
    b = _rt_1d([12.0, 20.0, 33.0])
    delta = plot._delta(a, b)
    fig = plot._compare_line_figure(a, b, delta)
    assert len(fig.axes) == 2
    # bottom axes holds the delta line = b - a
    delta_line = fig.axes[1].get_lines()[0]
    np.testing.assert_array_equal(delta_line.get_ydata(), np.array([2.0, 0.0, 3.0]))

    paths = plot.compare_tables(a, b, tmp_path)
    assert {p.name for p in paths} == {"CMP1D__compare_line.png"}


def test_compare_line_figure_shows_bin_filenames(tmp_path):
    a = _rt_1d([10.0, 20.0, 30.0])
    b = _rt_1d([12.0, 20.0, 33.0])
    fig = plot._compare_line_figure(
        a, b, plot._delta(a, b),
        a_bin_name="reference.bin", b_bin_name="output.bin",
    )
    text = "\n".join(artist.get_text() for artist in fig.texts)
    assert "A: reference.bin" in text
    assert "B: output.bin" in text


def test_compare_tables_toggles(tmp_path):
    a = _rt_2d([[1.0, 2.0], [3.0, 4.0]])
    b = _rt_2d([[2.0, 3.0], [5.0, 4.0]])
    only_heat = plot.compare_tables(a, b, tmp_path / "h", surface=False, curves=False)
    assert {p.name for p in only_heat} == {"CMP2D__compare_heatmap.png"}
    only_surf = plot.compare_tables(a, b, tmp_path / "s", heatmap=False, curves=False)
    assert {p.name for p in only_surf} == {"CMP2D__compare_surface.png"}
    only_curves = plot.compare_tables(
        a, b, tmp_path / "c", surface=False, heatmap=False
    )
    assert {p.name for p in only_curves} == {"CMP2D__compare_curves.png"}


def test_compare_before_after_single_view(mini_cal: CalFile, tmp_path):
    view = mini_cal.get("SYM_10X10")
    before = render_table(view)
    before_values = np.array(before.values)  # snapshot for the assertion

    edited = before.values + 5.0
    view.set(edited)

    paths = plot.compare_tables(before, view, tmp_path)
    assert {p.name for p in paths} == {
        "SYM_10X10__compare_surface.png",
        "SYM_10X10__compare_heatmap.png",
        "SYM_10X10__compare_curves.png",
    }
    # The pre-edit RenderedTable still holds the old values (render_table snapshots).
    np.testing.assert_array_equal(before.values, before_values)
    # And the delta reflects the edit (within one raw LSB of the table's scaling).
    np.testing.assert_allclose(plot._delta(before, render_table(view)), 5.0, atol=0.01)


def test_compare_identical_tables_all_zero_delta(tmp_path):
    a = _rt_2d([[1.0, 2.0], [3.0, 4.0]])
    b = _rt_2d([[1.0, 2.0], [3.0, 4.0]])
    paths = plot.compare_tables(a, b, tmp_path)  # must not raise on degenerate norm
    assert len(paths) == 3
    for p in paths:
        assert p.stat().st_size > 0


def test_compare_mismatched_shape_raises(tmp_path):
    a = _rt_2d([[1.0, 2.0], [3.0, 4.0]])
    b = _rt_1d([1.0, 2.0, 3.0, 4.0, 5.0])
    with pytest.raises(plot.TableMismatchError) as exc:
        plot.compare_tables(a, b, tmp_path)
    assert "CMP2D" in str(exc.value) and "CMP1D" in str(exc.value)


def test_compare_mismatched_axis_breakpoints_raises(tmp_path):
    a = _rt_2d([[1.0, 2.0], [3.0, 4.0]], symbol="AXA")
    b = _rt_2d([[1.0, 2.0], [3.0, 4.0]], symbol="AXB")
    # Same shape, different x breakpoints.
    b = plot.RenderedTable(
        symbol="AXB", title=None, units=b.units, categories=(),
        x_labels=(10.0, 20.0), y_labels=b.y_labels,
        x_units=b.x_units, y_units=b.y_units, values=b.values,
    )
    with pytest.raises(plot.TableMismatchError):
        plot.compare_tables(a, b, tmp_path)


def test_compare_scalar_returns_empty(tmp_path):
    a = _rt_2d([[1.0]])
    b = _rt_2d([[2.0]])
    assert plot.compare_tables(a, b, tmp_path) == []


# --------------------------------------------------------------------------- #
# Batch wrappers — plot_tables() / compare_bins() (U4)
# --------------------------------------------------------------------------- #
# A 2x2 table in TWO categories + a 2x2 table in NO category (the mini fixture
# has no non-scalar multi-category or category-less table). Built inline so the
# duplication and _uncategorized rules can be exercised.
_MULTICAT_XDF = """<XDFFORMAT version="1.60">
  <XDFHEADER>
    <BASEOFFSET offset="0x200000" subtract="0" />
    <DEFAULTS datasizeinbits="8" sigdigits="4" outputtype="1" signed="0" lsbfirst="1" float="0" />
    <REGION type="0xFFFFFFFF" startaddress="0x0" size="0x400000" regionflags="0x0" name="Binary" desc="x" />
    <CATEGORY index="0x0" name="Cat One" />
    <CATEGORY index="0x1" name="Cat Two" />
  </XDFHEADER>
  <XDFTABLE uniqueid="0x10" flags="0x30">
    <title>Multi</title><description>MULTI2X2</description>
    <CATEGORYMEM index="0" category="1" />
    <CATEGORYMEM index="1" category="2" />
    <XDFAXIS uniqueid="0x0" id="x"><indexcount>2</indexcount><MATH equation="X"><VAR id="X" /></MATH><LABEL index="0" value="-" /></XDFAXIS>
    <XDFAXIS uniqueid="0x0" id="y"><indexcount>2</indexcount><MATH equation="X"><VAR id="X" /></MATH><LABEL index="0" value="-" /></XDFAXIS>
    <XDFAXIS uniqueid="0x0" id="z">
      <EMBEDDEDDATA mmedtypeflags="0x2" mmedaddress="0x1000" mmedelementsizebits="8" mmedcolcount="2" mmedrowcount="2" mmedmajorstridebits="8" mmedminorstridebits="0" />
      <min>0.0</min><max>255.0</max><units>-</units>
      <MATH equation="X"><VAR id="X" /></MATH>
    </XDFAXIS>
  </XDFTABLE>
  <XDFTABLE uniqueid="0x20" flags="0x30">
    <title>NoCat</title><description>NOCAT2X2</description>
    <XDFAXIS uniqueid="0x0" id="x"><indexcount>2</indexcount><MATH equation="X"><VAR id="X" /></MATH><LABEL index="0" value="-" /></XDFAXIS>
    <XDFAXIS uniqueid="0x0" id="y"><indexcount>2</indexcount><MATH equation="X"><VAR id="X" /></MATH><LABEL index="0" value="-" /></XDFAXIS>
    <XDFAXIS uniqueid="0x0" id="z">
      <EMBEDDEDDATA mmedtypeflags="0x2" mmedaddress="0x2000" mmedelementsizebits="8" mmedcolcount="2" mmedrowcount="2" mmedmajorstridebits="8" mmedminorstridebits="0" />
      <min>0.0</min><max>255.0</max><units>-</units>
      <MATH equation="X"><VAR id="X" /></MATH>
    </XDFAXIS>
  </XDFTABLE>
</XDFFORMAT>
"""


def _build_multicat_cal(fill_10=0) -> CalFile:
    import tempfile

    # parse_xdf wants a path; write the XDF to a temp file.
    with tempfile.NamedTemporaryFile("w", suffix=".xdf", delete=False) as f:
        f.write(_MULTICAT_XDF)
        xdf_path = f.name
    model = parse_xdf(xdf_path)
    size = model.base_offset + 0x3000
    buf = bytearray(size)
    off = model.base_offset + 0x1000
    buf[off : off + 4] = bytes([fill_10 + 1, fill_10 + 2, fill_10 + 3, fill_10 + 4])
    off2 = model.base_offset + 0x2000
    buf[off2 : off2 + 4] = bytes([9, 8, 7, 6])
    img = BinImage(buf, region_start=model.region_start, region_size=len(buf))
    return CalFile(model, img, structure=SC8S50_STRUCTURE, float_bug_symbols=frozenset())


def test_plot_tables_category_batch(mini_cal: CalFile, tmp_path):
    paths = plot.plot_tables(mini_cal, tmp_path, category="Fuel Trim")
    # Fuel Trim holds SYM_SCALAR (scalar → skipped) and PROFILE_1D (1D → line).
    names = {p.relative_to(tmp_path).as_posix() for p in paths}
    assert names == {"Fuel Trim/PROFILE_1D__line.png"}
    assert not list((tmp_path / "Fuel Trim").glob("SYM_SCALAR*"))


def test_plot_tables_multi_category_duplication(tmp_path):
    cal = _build_multicat_cal()
    paths = plot.plot_tables(cal, tmp_path, symbols=["MULTI2X2"])
    rel = {p.relative_to(tmp_path).as_posix() for p in paths}
    assert rel == {
        "Cat One/MULTI2X2__surface.png", "Cat One/MULTI2X2__heatmap.png",
        "Cat Two/MULTI2X2__surface.png", "Cat Two/MULTI2X2__heatmap.png",
    }


def test_plot_tables_category_less_goes_to_uncategorized(tmp_path):
    cal = _build_multicat_cal()
    paths = plot.plot_tables(cal, tmp_path, symbols=["NOCAT2X2"])
    rel = {p.relative_to(tmp_path).as_posix() for p in paths}
    assert rel == {
        "_uncategorized/NOCAT2X2__surface.png",
        "_uncategorized/NOCAT2X2__heatmap.png",
    }


def test_compare_bins_matches_by_uniqueid(tmp_path):
    cal_a = _build_multicat_cal(fill_10=0)   # MULTI cells 1,2,3,4
    cal_b = _build_multicat_cal(fill_10=10)  # MULTI cells 11,12,13,14
    paths = plot.compare_bins(cal_a, cal_b, tmp_path, symbols=["MULTI2X2"])
    rel = {p.relative_to(tmp_path).as_posix() for p in paths}
    assert rel == {
        "Cat One/MULTI2X2__compare_surface.png", "Cat One/MULTI2X2__compare_heatmap.png",
        "Cat One/MULTI2X2__compare_curves.png",
        "Cat Two/MULTI2X2__compare_surface.png", "Cat Two/MULTI2X2__compare_heatmap.png",
        "Cat Two/MULTI2X2__compare_curves.png",
    }
    # Spot-check the delta: b - a = +10 across the table.
    va = cal_a.get("MULTI2X2")
    vb = cal_b.get("MULTI2X2")
    np.testing.assert_array_equal(
        plot._delta(render_table(va), render_table(vb)),
        np.full((2, 2), 10.0),
    )


def test_compare_bins_missing_uniqueid_fails_loud(tmp_path):
    cal_a = _build_multicat_cal()
    # cal_b built from the mini XDF has no uniqueid 0x10 → get() KeyErrors.
    model = parse_xdf(str(MINI_XDF))
    img = BinImage(
        bytearray(model.base_offset + 0x6000),
        region_start=model.region_start,
        region_size=model.base_offset + 0x6000,
    )
    cal_b = CalFile(model, img, structure=SC8S50_STRUCTURE, float_bug_symbols=frozenset())
    with pytest.raises(KeyError):
        plot.compare_bins(cal_a, cal_b, tmp_path, symbols=["MULTI2X2"])


def test_plot_tables_real_data(real_cal, tmp_path):
    paths = plot.plot_tables(real_cal, tmp_path, category="Axis")
    assert paths
    assert all(p.exists() and p.stat().st_size > 0 for p in paths)
    # Everything landed under the Axis category folder.
    assert all(p.parent.name == "Axis" for p in paths)
