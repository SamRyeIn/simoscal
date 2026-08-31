"""Visualization module (Phase 3): render calibration tables to static PNGs.

Read-only and entirely additive — like :mod:`simoscal.export`, it consumes the
Phase 2 grid layer (:class:`~simoscal.render.RenderedTable` /
:func:`~simoscal.render.render_table`) and touches no bin-mutation path. It turns
any selection of tables into static images:

* **2D** tables → a 3D **surface** plot *and* a value-overlaid **heatmap**.
* **1D** tables → a **line** plot.
* **scalar** (``1x1``) tables → nothing (there is nothing to plot).

plus a provenance-agnostic :func:`compare_tables` that produces fixed composite
comparison images (3-panel for 2D, 2-panel for 1D) from two views of the same
calibration item — two ``.bin``\\ s *or* before/after one in-session edit.
The 2D comparison set also includes a 3-panel **column-curves** view: every
matrix column is a labeled line over the row-axis breakpoints.

matplotlib is used **headless via the object API only** — the module constructs
:class:`matplotlib.figure.Figure` objects directly and never imports
``matplotlib.pyplot`` (which would pull in a global figure registry and an
interactive backend). This keeps the module thread-safe and side-effect-free on
import (plan Key Decision 1).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional, Union

import numpy as np
from matplotlib import colormaps, colors
from matplotlib.figure import Figure
from matplotlib.font_manager import FontProperties
from matplotlib.textpath import TextPath

# Registers the "3d" projection used by add_subplot(projection="3d"). Imported
# for its side effect; the name is referenced so linters keep the import.
import mpl_toolkits.mplot3d  # noqa: F401

from .calfile import CalFile, TableView
from .export import select_tables
from .render import RenderedTable, render_table

_ = mpl_toolkits.mplot3d  # keep the projection registration import alive

__all__ = [
    "plot_table",
    "compare_tables",
    "plot_tables",
    "compare_bins",
    "TableMismatchError",
]

# Default value/delta colormaps (plan Key Decision 11). Exposed as kwargs on
# the public functions.
_VALUE_CMAP = "turbo"
_DELTA_CMAP = "RdBu_r"

# Sentinel default for the cell-text ``fmt`` kwarg: "pick a shared fixed-point
# precision from this figure's own values" (see ``_resolve_fmt``), rather than
# a fixed format string. A caller that passes an explicit ``fmt=`` (e.g. the
# old "{:.4g}") is honored verbatim and never auto-picked.
_CELL_FMT: object = object()

# Baked-in surface camera (plan Key Decision 13): a conventional three-quarter
# view that shows both axes and the surface's relief in a non-interactive PNG.
_ELEV = 30
_AZIM = -120

# Output resolution (dots-per-inch) for saved PNGs. Higher than matplotlib's
# default 100 so the value overlays and axis text stay crisp when zoomed in.
_DPI = 200

# Sentinel for "caller did not specify azim" — distinguishes an explicit pass of
# the default value from "use the adaptive / fallback path". Used by every public
# surface-bearing function and the two figure builders.
_AZIM_AUTO: object = object()

# Relative tolerance for the gradient-based azimuth fallback: when the surface's
# mean tilt magnitude (per cell) is below this fraction of its value range, the
# table is treated as flat / symmetric and the baked-in ``_AZIM`` is used.
_SLOPE_TOL = 1e-6

# Lateral swing (degrees) added to the head-on "looking straight up the slope"
# azimuth so the camera sits uphill *and to the side* — a three-quarter view
# reads the relief far better than a dead-on one, where the rise hides behind
# itself. ~45 deg is the conventional three-quarter offset.
_SIDE_OFFSET = 45.0


# --------------------------------------------------------------------------- #
# Styling helpers (pure, no I/O)
# --------------------------------------------------------------------------- #
def _title_for(rt: RenderedTable) -> str:
    """A single-line identifier for a table: symbol, else title, else ``"(table)"``.

    Used where one line is required — error messages, file stems. Plot titles use
    :func:`_title_lines` / :func:`_apply_fig_title` to show the description too.
    """
    return rt.symbol or rt.title or "(table)"


def _title_lines(rt: RenderedTable) -> tuple[str, str]:
    """``(id_line, desc_line)`` — the parameter ID and its plain-English description.

    When a symbol is present it is the ID line and the XDF title is the
    description beneath it; with no symbol the title becomes the ID line and the
    description is empty. The description is dropped when it would merely repeat
    the ID line.
    """
    ident = rt.symbol or rt.title or "(table)"
    desc = rt.title if rt.symbol else ""
    if desc == ident:
        desc = ""
    return ident, desc or ""


def _axes_title(rt: RenderedTable) -> str:
    """Two-line axes title: parameter ID with its description on the line below."""
    ident, desc = _title_lines(rt)
    return f"{ident}\n{desc}" if desc else ident


def _apply_fig_title(fig, rt: RenderedTable) -> None:
    """Set a two-tier figure title: parameter ID (bold) over its description.

    Falls back to a plain bold ID when there is no distinct description.
    """
    ident, desc = _title_lines(rt)
    fig.suptitle(ident, fontweight="bold")
    if desc:
        fig.text(0.5, 0.945, desc, ha="center", va="top",
                 fontsize=9, style="italic", color="0.35")


def _apply_compare_header(
    fig: Figure,
    rt: RenderedTable,
    *,
    a_bin_name: Optional[Union[str, Path]] = None,
    b_bin_name: Optional[Union[str, Path]] = None,
) -> None:
    """Add the table title and centered A/B BIN filename provenance.

    Paths are reduced to their complete basenames: the filename is the useful
    review identity, while embedding machine-specific parent directories would
    make otherwise identical artifacts differ across workspaces.
    """
    _apply_fig_title(fig, rt)
    provenance = []
    if a_bin_name is not None:
        provenance.append(f"A: {Path(a_bin_name).name}")
    if b_bin_name is not None:
        provenance.append(f"B: {Path(b_bin_name).name}")
    if provenance:
        # One line, not one per side: two stacked lines were the biggest single
        # contributor to the gap between the title block and the axes below.
        fig.text(
            0.5,
            0.92,
            "   ".join(provenance),
            ha="center",
            va="top",
            fontsize=8,
            family="monospace",
            zorder=100,
            bbox={"facecolor": "white", "edgecolor": "none", "pad": 1.5},
        )


def _text_color(rgba) -> str:
    """Black or white, whichever contrasts better with an RGBA fill.

    Uses Rec. 601 relative luminance so overlaid cell values stay legible over
    both the dark and light ends of a sequential colormap (plan Decision 7).
    """
    r, g, b = rgba[0], rgba[1], rgba[2]
    luminance = 0.299 * r + 0.587 * g + 0.114 * b
    return "black" if luminance > 0.5 else "white"


def _auto_decimals(*value_arrays: np.ndarray, max_decimals: int = 4, tol: float = 1e-6) -> int:
    """Decimal places every value across ``value_arrays`` needs to round-trip.

    The largest per-value requirement wins (capped at ``max_decimals``), so a
    figure's whole cell-text set shares one precision — ``6`` renders as
    ``6.000`` alongside ``9.375`` rather than at mismatched precision.
    """
    decimals = 0
    for arr in value_arrays:
        for v in np.ravel(np.asarray(arr, dtype=np.float64)):
            if not np.isfinite(v):
                continue
            for d in range(decimals, max_decimals + 1):
                if abs(round(float(v), d) - float(v)) < tol:
                    break
            else:
                d = max_decimals
            decimals = max(decimals, d)
    return decimals


def _resolve_fmt(fmt, *value_arrays: np.ndarray) -> str:
    """A concrete ``str.format`` spec for cell text.

    ``fmt`` verbatim when the caller supplied one; otherwise (the
    :data:`_CELL_FMT` sentinel) a fixed-point spec auto-picked from
    ``value_arrays`` via :func:`_auto_decimals` — never scientific notation,
    and the same precision for every cell on the figure.
    """
    if fmt is not _CELL_FMT:
        return fmt
    return f"{{:.{_auto_decimals(*value_arrays)}f}}"


def _annotation_fontsize(rows: int, cols: int) -> float:
    """A grid-size-adaptive font size for the cell-value overlay (Decision 7).

    Full size for small grids, shrinking as the grid grows so a dense table's
    numbers do not collide. Clamped to a legible floor.
    """
    largest = max(rows, cols)
    return float(max(4.0, min(9.0, 90.0 / largest)))


_TEXT_FONT_PROPS = FontProperties()  # matches ax.text's default family/weight


def _text_extent_per_point(text: str) -> tuple[float, float]:
    """``(width, height)`` of ``text`` at 1pt, in points — real glyph metrics.

    Built from :class:`~matplotlib.textpath.TextPath`, which lays out the
    actual font outlines rather than guessing an average character width; it
    needs no renderer or canvas, so it works on a bare, undrawn ``Figure``.
    Scale-invariant in font size, so this is measured once at size 1 and the
    caller scales linearly to whatever size it is solving for.
    """
    if not text:
        return (0.0, 0.0)
    ext = TextPath((0, 0), text, size=1.0, prop=_TEXT_FONT_PROPS).get_extents()
    return (float(ext.width), float(ext.height))


def _fit_cell_fontsize(ax, rows: int, cols: int, values: np.ndarray, fmt: str) -> float:
    """Font size that keeps every cell's formatted value inside its own cell.

    Derived from the axes' actual on-figure size (its position, read after
    layout is settled) and the widest formatted value this table will draw,
    measured with real glyph metrics (:func:`_text_extent_per_point`) rather
    than an average-character-width guess — not just the grid's row/column
    count, which :func:`_annotation_fontsize` uses and which the comparison
    heatmap's shared 2x2 layout can put more or less real estate behind: the
    A/B tiles are half the figure's width, the delta tile below is the full
    width, so the same row/col count needs a different font size in each.

    No floor: a table dense enough that even a tiny font would overflow its
    cell gets that tiny font rather than a bigger one guaranteed to overlap —
    overflow is worse than small.
    """
    fig = ax.get_figure()
    bbox = ax.get_position()
    fig_w_in, fig_h_in = fig.get_size_inches()
    cell_w_pt = bbox.width * fig_w_in * 72.0 / max(cols, 1)
    cell_h_pt = bbox.height * fig_h_in * 72.0 / max(rows, 1)
    widest = max((fmt.format(v) for v in np.ravel(values)), key=len, default="")
    width_per_pt, height_per_pt = _text_extent_per_point(widest)
    if width_per_pt <= 0 or height_per_pt <= 0:
        return 9.0
    # A small margin so text clears the cell border rather than touching it.
    width_fit = (cell_w_pt * 0.90) / width_per_pt
    height_fit = (cell_h_pt * 0.70) / height_per_pt
    return float(min(9.0, width_fit, height_fit))


def _axis_ticks(ax, x_labels, y_labels) -> None:
    """Place tick labels from decoded breakpoints on a 2D (imshow) axes."""
    ax.set_xticks(range(len(x_labels)))
    ax.set_xticklabels([f"{v:g}" for v in x_labels], rotation=90, fontsize=6)
    if y_labels is not None:
        ax.set_yticks(range(len(y_labels)))
        ax.set_yticklabels([f"{v:g}" for v in y_labels], fontsize=6)


# Mesh-edge styling for `plot_surface`: black cell boundaries on the surface
# itself (each grid cell outlined) so the table's discretization reads clearly.
_SURFACE_EDGE = {"edgecolor": "black", "linewidth": 0.3}


def _surface_view(rt: RenderedTable) -> float:
    """Gradient-based surface azimuth: place the camera on the rising side.

    Computes the mean gradient of ``rt.values`` along each axis. If the surface
    has a consistent tilt (mean gradient magnitude above :data:`_SLOPE_TOL`
    relative to its value range), the camera is aimed up the slope but swung
    :data:`_SIDE_OFFSET` degrees to the side — a three-quarter view that shows
    the rise *and* the relief along it, rather than the dead-on view where the
    slope hides behind itself. Flat, saddle-shaped, or symmetric tables (mean
    gradient cancels to ~0) fall back to the baked-in :data:`_AZIM`.

    Returns the azimuth in degrees. Elevation is not adapted — it stays at
    :data:`_ELEV` (still overridable per-call via ``elev``).
    """
    values = rt.values
    z_range = float(np.max(values) - np.min(values))
    if z_range == 0.0:
        return float(_AZIM)

    grads = np.gradient(values)
    gy_mean = float(np.mean(grads[0]))  # axis 0 == y direction (rows)
    gx_mean = float(np.mean(grads[1]))  # axis 1 == x direction (cols)
    slope_mag = float(np.hypot(gx_mean, gy_mean))
    if slope_mag / z_range < _SLOPE_TOL:
        return float(_AZIM)

    head_on = np.degrees(np.arctan2(gy_mean, gx_mean)) + 180.0
    return float(head_on + _SIDE_OFFSET)


def _resolve_azim(rt: RenderedTable, azim, adaptive_azim: bool) -> float:
    """Resolve the ``azim`` kwarg + ``adaptive_azim`` flag to a concrete azimuth.

    Precedence (see the plan's dispatch table):

    * explicit ``azim`` value (anything that is not :data:`_AZIM_AUTO`) wins
      regardless of the adaptive flag.
    * ``azim is _AZIM_AUTO`` and ``adaptive_azim`` is true  → :func:`_surface_view`.
    * ``azim is _AZIM_AUTO`` and ``adaptive_azim`` is false → :data:`_AZIM`.
    """
    if azim is not _AZIM_AUTO:
        return float(azim)
    return _surface_view(rt) if adaptive_azim else float(_AZIM)


# --------------------------------------------------------------------------- #
# Single-table figure builders (U1): RenderedTable -> in-memory Figure
# --------------------------------------------------------------------------- #
def _heatmap_figure(
    rt: RenderedTable,
    *,
    value_cmap: str = _VALUE_CMAP,
    fmt: Union[str, object] = _CELL_FMT,
    norm: Optional[colors.Normalize] = None,
) -> Figure:
    """A TunerPro-style heatmap of ``rt.values`` with every cell value overlaid.

    ``imshow`` fills the grid; ticks come from ``x_labels``/``y_labels``, axis
    titles from ``x_units``/``y_units``, plus a colorbar and a figure title.
    Every cell's value is drawn as text with luminance-based contrast and a
    grid-size-adaptive font (Decision 7). ``norm`` overrides the color scaling
    (used by the comparison composite to share a scale across two panels).
    """
    values = rt.values
    rows, cols = values.shape

    fig = Figure(figsize=(max(4.0, cols * 0.5), max(3.0, rows * 0.5)))
    ax = fig.add_subplot()
    cmap = colormaps[value_cmap]
    if norm is None:
        norm = colors.Normalize(vmin=float(np.min(values)), vmax=float(np.max(values)))
    im = ax.imshow(values, cmap=cmap, norm=norm, aspect="auto")
    fig.colorbar(im, ax=ax, label=rt.units or "")

    _axis_ticks(ax, rt.x_labels, rt.y_labels)
    ax.set_xlabel(rt.x_units or "", fontweight="bold")
    ax.set_ylabel(rt.y_units or "", fontweight="bold")
    ax.set_title(_axes_title(rt))

    resolved_fmt = _resolve_fmt(fmt, values)
    fontsize = _annotation_fontsize(rows, cols)
    for r in range(rows):
        for c in range(cols):
            v = values[r, c]
            ax.text(
                c, r, resolved_fmt.format(v),
                ha="center", va="center",
                color=_text_color(cmap(norm(v))),
                fontsize=fontsize,
            )
    fig.tight_layout()
    return fig


def _surface_figure(
    rt: RenderedTable,
    *,
    value_cmap: str = _VALUE_CMAP,
    elev: float = _ELEV,
    azim=_AZIM_AUTO,
    adaptive_azim: bool = True,
    norm: Optional[colors.Normalize] = None,
) -> Figure:
    """A 3D surface of ``rt.values`` over the ``x``/``y`` breakpoints.

    The camera elevation is fixed (``ax.view_init(elev, ...)``) because the PNG
    is not interactive — the angle is the only chance to read the relief
    (Decision 13). The azimuth, by default, is *adaptive*: :func:`_surface_view`
    picks an angle that places the surface's rising side toward the viewer,
    falling back to :data:`_AZIM` on flat / symmetric tables. Pass an explicit
    ``azim`` to override the adaptive choice, or ``adaptive_azim=False`` to force
    the baked-in default. ``set_box_aspect`` keeps proportions undistorted; a
    value colorbar is retained so height stays legible where the projection is
    ambiguous.
    """
    values = rt.values
    x = np.asarray(rt.x_labels, dtype=float)
    y = np.asarray(rt.y_labels, dtype=float)
    xx, yy = np.meshgrid(x, y)

    az = _resolve_azim(rt, azim, adaptive_azim)
    fig = Figure(figsize=(6.0, 5.0))
    ax = fig.add_subplot(projection="3d")
    cmap = colormaps[value_cmap]
    if norm is None:
        norm = colors.Normalize(vmin=float(np.min(values)), vmax=float(np.max(values)))
    surf = ax.plot_surface(xx, yy, values, cmap=cmap, norm=norm, **_SURFACE_EDGE)
    fig.colorbar(surf, ax=ax, shrink=0.6, label=rt.units or "")

    ax.set_xlabel(rt.x_units or "", fontweight="bold")
    ax.set_ylabel(rt.y_units or "", fontweight="bold")
    ax.set_zlabel(rt.units or "", fontweight="bold")
    ax.set_title(_axes_title(rt))
    ax.view_init(elev=elev, azim=az)
    ax.set_box_aspect((1, 1, 0.6))
    return fig


def _line_figure(
    rt: RenderedTable,
    *,
    color: Optional[str] = None,
) -> Figure:
    """A line plot of a 1D table: ``rt.values[0]`` vs the ``x`` breakpoints."""
    y = rt.values[0]
    x = np.asarray(rt.x_labels, dtype=float)

    fig = Figure(figsize=(6.0, 4.0))
    ax = fig.add_subplot()
    ax.plot(x, y, marker="o", color=color)
    ax.set_xlabel(rt.x_units or "", fontweight="bold")
    ax.set_ylabel(rt.units or "", fontweight="bold")
    ax.set_title(_axes_title(rt))
    ax.grid(True, which="both", alpha=0.3)
    fig.tight_layout()
    return fig


# --------------------------------------------------------------------------- #
# Normalization, naming, and disk I/O (U2)
# --------------------------------------------------------------------------- #
_INVALID_FILENAME_CHARS = set('/\\:*?"<>|')


def _normalize(source: Union[TableView, RenderedTable]) -> RenderedTable:
    """Return a :class:`RenderedTable` for a view or an already-rendered table.

    A :class:`~simoscal.calfile.TableView` is rendered via
    :func:`~simoscal.render.render_table`; a :class:`RenderedTable` passes
    through unchanged (plan Key Decision 4). The pass-through is what lets the
    before/after flow hand in a pre-edit snapshot.
    """
    if isinstance(source, RenderedTable):
        return source
    return render_table(source)


#: An A2L symbol as this ECU's definitions spell one: a C identifier, optionally
#: with the bracketed array indices the XDF uses for table families
#: (``IP_TQ_POW_MAX_AT[POW_1][0]``). A name that does not match is not a symbol,
#: whatever field it came out of — see :func:`_resolve_name`.
_SYMBOL_SHAPE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\[[^\[\]]+\])*$")


def _resolve_name(source: Union[TableView, RenderedTable]) -> str:
    """The base name for a table's files, unique per table.

    Symbol, else title, else ``uniqueid`` — but a name that is not shaped like
    an A2L symbol gets its ``uniqueid`` appended, because only a symbol is
    guaranteed to name one table.

    That distinction is not pedantry. A table's ``symbol`` is the first line of
    the XDF ``<description>``, and patch-added tables have no A2L symbol at all:
    in ``S50 Switch Patch.29.33.V2.xdf`` every one of them reads ``|X: x|Y: y``,
    and the five ``PUT setpoint`` grids share a title as well. Without the
    suffix all 185 of those tables resolve to the same stem, and a build that
    changed two of them would write one plot over the other and leave a review
    gate looking at the wrong table.

    A :class:`RenderedTable` carries no uniqueid, so it falls back to a generic
    stem.
    """
    symbol = source.symbol
    hexid = getattr(source, "uniqueid_hex", None)
    if symbol and _SYMBOL_SHAPE.match(symbol):
        return symbol
    # Not a symbol. Prefer the title, which for a patch table is the only
    # human-readable thing it has ("Spark modifier" beats "|X: x|Y: y"), and
    # disambiguate with the uniqueid because titles repeat across slots.
    name = source.title or symbol
    if not name:
        return hexid or "table"
    return f"{name} {hexid}" if hexid else name


def _sanitize_filename(name: str) -> str:
    """A path-safe file stem: path-hostile characters replaced with ``_``.

    Analogous to the export module's ``_sanitize_sheet_name`` but for a POSIX/
    Windows filename rather than an Excel sheet title.
    """
    cleaned = "".join("_" if c in _INVALID_FILENAME_CHARS else c for c in name)
    cleaned = cleaned.strip().rstrip(".")
    return cleaned or "table"


def _write_figure(fig: Figure, path: Path) -> Path:
    """Save ``fig`` to ``path`` as a high-resolution PNG and return the path."""
    fig.savefig(path, format="png", dpi=_DPI)
    return path


def plot_table(
    source: Union[TableView, RenderedTable],
    out_dir: Union[str, Path],
    *,
    surface: bool = True,
    heatmap: bool = True,
    value_cmap: str = _VALUE_CMAP,
    fmt: Union[str, object] = _CELL_FMT,
    elev: float = _ELEV,
    azim=_AZIM_AUTO,
    adaptive_azim: bool = True,
) -> list[Path]:
    """Render one table to PNG(s) in ``out_dir``; return the written paths.

    Dispatch is shape-driven (plan Key Decision 5), mirroring
    :func:`~simoscal.render.render_table`'s own rule:

    * ``(1, 1)`` scalar → nothing produced (returns ``[]``).
    * single row (1D) → one ``<name>__line.png``.
    * otherwise (2D) → ``<name>__surface.png`` and/or ``<name>__heatmap.png``,
      each gated by its ``surface``/``heatmap`` toggle.

    ``out_dir`` is written flat (per-category subfoldering is the batch wrappers'
    job) and created if absent. The file stem is symbol → title → ``uniqueid``
    (:func:`_resolve_name`), path-sanitized.

    ``azim`` defaults to the :data:`_AZIM_AUTO` sentinel: when ``adaptive_azim``
    is true (the default) the surface azimuth is chosen per-table from the
    surface's gradient (see :func:`_surface_view`); when false the baked-in
    :data:`_AZIM` is used. Passing an explicit ``azim`` value overrides the
    adaptive choice regardless of ``adaptive_azim``.
    """
    rt = _normalize(source)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stem = _sanitize_filename(_resolve_name(source))

    rows, cols = rt.values.shape
    if rows == 1 and cols == 1:
        return []

    written: list[Path] = []
    if rows == 1:
        fig = _line_figure(rt)
        written.append(_write_figure(fig, out / f"{stem}__line.png"))
        return written

    if surface:
        fig = _surface_figure(rt, value_cmap=value_cmap, elev=elev,
                              azim=azim, adaptive_azim=adaptive_azim)
        written.append(_write_figure(fig, out / f"{stem}__surface.png"))
    if heatmap:
        fig = _heatmap_figure(rt, value_cmap=value_cmap, fmt=fmt)
        written.append(_write_figure(fig, out / f"{stem}__heatmap.png"))
    return written


# --------------------------------------------------------------------------- #
# Comparison — pure numeric helpers (U3)
# --------------------------------------------------------------------------- #
class TableMismatchError(ValueError):
    """Raised when two tables handed to :func:`compare_tables` are not comparable.

    Fires on differing ``values.shape`` or differing axis breakpoints
    (``x_labels``/``y_labels``). A comparison of two genuinely different tables
    would be a *misleading* review artifact, so it hard-fails with both tables
    named rather than silently producing a plot (plan Key Decision 9 — the one
    safety-adjacent rule of this phase).
    """


def _check_comparable(rt_a: RenderedTable, rt_b: RenderedTable) -> None:
    """Raise :class:`TableMismatchError` if ``rt_a`` and ``rt_b`` can't be compared."""
    name_a, name_b = _title_for(rt_a), _title_for(rt_b)
    if rt_a.values.shape != rt_b.values.shape:
        raise TableMismatchError(
            f"cannot compare {name_a!r} {rt_a.values.shape} with "
            f"{name_b!r} {rt_b.values.shape}: shapes differ"
        )
    if rt_a.x_labels != rt_b.x_labels or rt_a.y_labels != rt_b.y_labels:
        raise TableMismatchError(
            f"cannot compare {name_a!r} with {name_b!r}: axis breakpoints differ"
        )


def _delta(rt_a: RenderedTable, rt_b: RenderedTable) -> np.ndarray:
    """The signed difference ``b - a`` (second minus first) on copies.

    Copies so the returned delta never aliases a caller's cached ``.values``
    array (plan Key Decision 8, Research Findings defensive-copy note).
    """
    return np.array(rt_b.values, dtype=float) - np.array(rt_a.values, dtype=float)


def _shared_limits(rt_a: RenderedTable, rt_b: RenderedTable) -> tuple[float, float]:
    """A common ``(vmin, vmax)`` spanning both value arrays (shared value scale)."""
    lo = min(float(np.min(rt_a.values)), float(np.min(rt_b.values)))
    hi = max(float(np.max(rt_a.values)), float(np.max(rt_b.values)))
    return lo, hi


def _diverging_limits(delta: np.ndarray) -> tuple[float, float]:
    """A symmetric, zero-centered ``(-M, +M)`` for the delta panel.

    ``M = max|delta|``. When the two tables are identical (all-zero delta) the
    guard falls back to ``(-1, 1)`` so the diverging norm stays valid and the
    delta panel renders flat instead of raising on a zero-width range
    (plan Key Decision 10).
    """
    m = float(np.max(np.abs(delta))) if delta.size else 0.0
    if m == 0.0:
        return -1.0, 1.0
    return -m, m


# --------------------------------------------------------------------------- #
# Comparison — figure builders (U3)
# --------------------------------------------------------------------------- #
def _draw_heatmap_panel(ax, rt, values, *, cmap, norm, title):
    """Draw one imshow panel (no cell text yet) onto an existing axes.

    Cell-value text is added later, by :func:`_annotate_heatmap_cells`, once
    every axes on the figure has its *final* position — for the comparison
    heatmap's A/B panels that means after the shared colorbar has carved its
    space out of them, since sizing text from a pre-colorbar width would
    overflow once the colorbar shrinks the panel.
    """
    im = ax.imshow(values, cmap=cmap, norm=norm, aspect="auto")
    _axis_ticks(ax, rt.x_labels, rt.y_labels)
    # Cell borders: a minor-tick grid one half-cell off the major (labeled)
    # ticks, so it falls on cell edges rather than cell centers. Kept separate
    # from the major ticks so it never grows tick labels of its own.
    rows, cols = values.shape
    ax.set_xticks(np.arange(-0.5, cols, 1.0), minor=True)
    ax.set_yticks(np.arange(-0.5, rows, 1.0), minor=True)
    ax.grid(which="minor", color="black", linewidth=0.6)
    ax.tick_params(which="minor", bottom=False, left=False)
    ax.set_xlabel(rt.x_units or "", fontweight="bold")
    if rt.y_labels is not None:
        ax.set_ylabel(rt.y_units or "", fontweight="bold")
    ax.set_title(title, fontsize=9)
    return im


def _annotate_heatmap_cells(ax, values, *, cmap, norm, fmt) -> None:
    """Overlay every cell's formatted value, sized to that axes' final bbox."""
    rows, cols = values.shape
    fontsize = _fit_cell_fontsize(ax, rows, cols, values, fmt)
    cmap_obj = colormaps[cmap] if isinstance(cmap, str) else cmap
    for r in range(rows):
        for c in range(cols):
            v = values[r, c]
            ax.text(
                c, r, fmt.format(v), ha="center", va="center",
                color=_text_color(cmap_obj(norm(v))), fontsize=fontsize,
            )


def _compare_heatmap_figure(
    rt_a: RenderedTable,
    rt_b: RenderedTable,
    delta: np.ndarray,
    *,
    value_cmap: str = _VALUE_CMAP,
    delta_cmap: str = _DELTA_CMAP,
    fmt: Union[str, object] = _CELL_FMT,
    a_bin_name: Optional[Union[str, Path]] = None,
    b_bin_name: Optional[Union[str, Path]] = None,
) -> Figure:
    """A 3-panel heatmap composite: A and B on a shared scale, delta diverging."""
    vmin, vmax = _shared_limits(rt_a, rt_b)
    value_norm = colors.Normalize(vmin=vmin, vmax=vmax)
    dmin, dmax = _diverging_limits(delta)
    delta_norm = colors.TwoSlopeNorm(vmin=dmin, vcenter=0.0, vmax=dmax)

    fig = Figure(figsize=(11.0, 8.0))
    ax_a = fig.add_subplot(2, 2, 1)
    ax_b = fig.add_subplot(2, 2, 2)
    ax_d = fig.add_subplot(2, 1, 2)
    # Fix the subplot positions *before* drawing the colorbars: each colorbar
    # carves its space out of the axes' current position, so creating one
    # ahead of a later subplots_adjust leaves it stranded over the A/B panel
    # once that adjust shifts everything up to clear the header.
    fig.subplots_adjust(top=0.87)

    im_a = _draw_heatmap_panel(ax_a, rt_a, rt_a.values, cmap=value_cmap,
                               norm=value_norm, title="A")
    _draw_heatmap_panel(ax_b, rt_b, rt_b.values, cmap=value_cmap,
                        norm=value_norm, title="B")
    im_d = _draw_heatmap_panel(ax_d, rt_a, delta, cmap=delta_cmap,
                               norm=delta_norm, title="Δ (B − A)")

    # Colorbars before cell text: a colorbar carves its space out of the axes
    # it's attached to, so ax_a/ax_b are only at their final (narrower) width
    # once this runs — sizing text before it would overflow the panel.
    fig.colorbar(im_a, ax=[ax_a, ax_b], label=rt_a.units or "", shrink=0.7)
    fig.colorbar(im_d, ax=ax_d, label=rt_a.units or "", shrink=0.7)

    # One shared precision across all three panels (A, B, and the delta), not
    # picked per-panel — so the same quantity reads at the same precision
    # wherever it appears on this figure.
    resolved_fmt = _resolve_fmt(fmt, rt_a.values, rt_b.values, delta)
    _annotate_heatmap_cells(ax_a, rt_a.values, cmap=value_cmap, norm=value_norm, fmt=resolved_fmt)
    _annotate_heatmap_cells(ax_b, rt_b.values, cmap=value_cmap, norm=value_norm, fmt=resolved_fmt)
    _annotate_heatmap_cells(ax_d, delta, cmap=delta_cmap, norm=delta_norm, fmt=resolved_fmt)

    _apply_compare_header(
        fig, rt_a, a_bin_name=a_bin_name, b_bin_name=b_bin_name
    )
    return fig


def _compare_surface_figure(
    rt_a: RenderedTable,
    rt_b: RenderedTable,
    delta: np.ndarray,
    *,
    value_cmap: str = _VALUE_CMAP,
    delta_cmap: str = _DELTA_CMAP,
    elev: float = _ELEV,
    azim=_AZIM_AUTO,
    adaptive_azim: bool = True,
    a_bin_name: Optional[Union[str, Path]] = None,
    b_bin_name: Optional[Union[str, Path]] = None,
) -> Figure:
    """A 3-panel surface composite: A and B on a shared scale, delta diverging.

    The adaptive / override azimuth (see :func:`_surface_figure`) is resolved
    once from ``rt_a`` and applied to all three panels so the comparison stays
    visually aligned.
    """
    x = np.asarray(rt_a.x_labels, dtype=float)
    y = np.asarray(rt_a.y_labels, dtype=float)
    xx, yy = np.meshgrid(x, y)

    az = _resolve_azim(rt_a, azim, adaptive_azim)
    vmin, vmax = _shared_limits(rt_a, rt_b)
    value_norm = colors.Normalize(vmin=vmin, vmax=vmax)
    dmin, dmax = _diverging_limits(delta)
    delta_norm = colors.TwoSlopeNorm(vmin=dmin, vcenter=0.0, vmax=dmax)

    fig = Figure(figsize=(15.0, 6.0))
    specs = [
        (1, rt_a.values, value_cmap, value_norm, "A"),
        (2, rt_b.values, value_cmap, value_norm, "B"),
        (3, delta, delta_cmap, delta_norm, "Δ (B − A)"),
    ]
    for pos, z, cmap, norm, title in specs:
        ax = fig.add_subplot(1, 3, pos, projection="3d")
        surf = ax.plot_surface(xx, yy, z, cmap=cmap, norm=norm, **_SURFACE_EDGE)
        ax.set_xlabel(rt_a.x_units or "", fontweight="bold")
        ax.set_ylabel(rt_a.y_units or "", fontweight="bold")
        ax.set_zlabel(rt_a.units or "", fontweight="bold")
        ax.set_title(title, fontsize=9)
        ax.view_init(elev=elev, azim=az)
        ax.set_box_aspect((1, 1, 0.6))
        fig.colorbar(surf, ax=ax, shrink=0.5)
    _apply_compare_header(
        fig, rt_a, a_bin_name=a_bin_name, b_bin_name=b_bin_name
    )
    fig.subplots_adjust(top=0.78, bottom=0.04)
    return fig


def _comparison_line_limits(*arrays: np.ndarray) -> tuple[float, float]:
    """A padded Y range spanning every array in a line comparison.

    The column-curves comparison intentionally uses this one range for A, B,
    and ``B - A``. A small pad keeps extrema off the axes frame; constant data
    gets a deterministic non-degenerate range rather than matplotlib's
    per-axes automatic expansion.
    """
    lo = min(float(np.min(values)) for values in arrays)
    hi = max(float(np.max(values)) for values in arrays)
    span = hi - lo
    pad = span * 0.05 if span else max(abs(lo) * 0.05, 1.0)
    return lo - pad, hi + pad


def _is_rpm(units: Optional[str]) -> bool:
    return (units or "").strip().lower() == "rpm"


def _curve_axes(rt: RenderedTable) -> tuple[np.ndarray, Optional[str], np.ndarray, Optional[str], bool]:
    """Pick which table axis is the plot's X axis vs. the per-curve key.

    RPM, when present on exactly one axis, is always the plot's X axis — an
    RPM sweep is what a reader wants to see spread along the bottom, whatever
    row/column position it happens to occupy in the table. Otherwise falls
    back to the row axis (``y_labels``) as X, keyed by the column axis
    (``x_labels``), matching this composite's original orientation.

    Returns ``(x_axis, x_axis_units, key_axis, key_axis_units, x_is_table_x)``.
    """
    x_is_rpm = _is_rpm(rt.x_units)
    y_is_rpm = _is_rpm(rt.y_units)
    if x_is_rpm and not y_is_rpm:
        return (
            np.asarray(rt.x_labels, dtype=float), rt.x_units,
            np.asarray(rt.y_labels, dtype=float), rt.y_units,
            True,
        )
    return (
        np.asarray(rt.y_labels, dtype=float), rt.y_units,
        np.asarray(rt.x_labels, dtype=float), rt.x_units,
        False,
    )


def _compare_curves_figure(
    rt_a: RenderedTable,
    rt_b: RenderedTable,
    delta: np.ndarray,
    *,
    value_cmap: str = _VALUE_CMAP,
    a_bin_name: Optional[Union[str, Path]] = None,
    b_bin_name: Optional[Union[str, Path]] = None,
) -> Figure:
    """A 3-panel line composite with one labeled curve per table row/column.

    Curves run over the plot's X axis and are keyed by the other axis's
    breakpoints — see :func:`_curve_axes` for which axis becomes which (an
    RPM axis always wins the X slot). Colors and labels are identical across
    A, B, and ``B - A``; all three panels also receive the exact same Y
    limits so their vertical shapes and magnitudes can be compared directly.
    """
    x_axis, x_units, key_axis, key_units, x_is_table_x = _curve_axes(rt_a)
    line_colors = colormaps[value_cmap](np.linspace(0.05, 0.95, len(key_axis)))
    ylim = _comparison_line_limits(rt_a.values, rt_b.values, delta)

    fig = Figure(figsize=(15.0, 5.0))
    panels = (
        (rt_a.values, "A"),
        (rt_b.values, "B"),
        (delta, "Δ (B − A)"),
    )
    axes = []
    for pos, (values, title) in enumerate(panels, start=1):
        ax = fig.add_subplot(1, 3, pos)
        for key, (key_value, color) in enumerate(zip(key_axis, line_colors)):
            curve = values[key, :] if x_is_table_x else values[:, key]
            ax.plot(
                x_axis,
                curve,
                marker="o",
                markersize=3,
                linewidth=1.2,
                color=color,
                label=f"{key_value:g}",
            )
        if pos == 3:
            ax.axhline(0.0, color="0.6", linewidth=0.8)
        ax.set_xlabel(x_units or "", fontweight="bold")
        ax.set_ylabel(rt_a.units or "", fontweight="bold")
        ax.set_title(title, fontsize=9)
        ax.set_ylim(ylim)
        ax.minorticks_on()
        ax.grid(True, which="major", alpha=0.3)
        ax.grid(True, which="minor", alpha=0.12)
        axes.append(ax)

    legend_title = "Curve key"
    if key_units:
        legend_title += f" ({key_units})"
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(
        handles,
        labels,
        title=legend_title,
        loc="center right",
        bbox_to_anchor=(0.995, 0.5),
        fontsize=7,
        title_fontsize=8,
    )
    _apply_compare_header(
        fig, rt_a, a_bin_name=a_bin_name, b_bin_name=b_bin_name
    )
    # tight_layout alone pads well beyond the header block to keep clear of the
    # suptitle, even with an explicit rect; fixing the top margin afterward
    # with subplots_adjust overrides that and closes the gap. The right-side
    # rect stays, since that margin is for the legend, not the header.
    fig.tight_layout(rect=(0.0, 0.0, 0.88, 1.0))
    fig.subplots_adjust(top=0.80)
    return fig


def _compare_line_figure(
    rt_a: RenderedTable,
    rt_b: RenderedTable,
    delta: np.ndarray,
    *,
    value_cmap: str = _VALUE_CMAP,
    delta_cmap: str = _DELTA_CMAP,
    a_bin_name: Optional[Union[str, Path]] = None,
    b_bin_name: Optional[Union[str, Path]] = None,
) -> Figure:
    """A 2-panel line composite: A/B overlaid on a shared scale, delta below."""
    x = np.asarray(rt_a.x_labels, dtype=float)

    fig = Figure(figsize=(7.0, 7.0))
    ax_top = fig.add_subplot(2, 1, 1)
    ax_top.plot(x, rt_a.values[0], marker="o", label="A")
    ax_top.plot(x, rt_b.values[0], marker="s", label="B")
    ax_top.set_ylabel(rt_a.units or "", fontweight="bold")
    ax_top.set_title("A and B")
    ax_top.legend()
    ax_top.grid(True, which="both", alpha=0.3)

    ax_bot = fig.add_subplot(2, 1, 2)
    ax_bot.plot(x, delta[0], marker="o", color="tab:red")
    ax_bot.axhline(0.0, color="0.6", lw=0.8)
    ax_bot.set_xlabel(rt_a.x_units or "", fontweight="bold")
    ax_bot.set_ylabel("Δ (B − A)", fontweight="bold")
    ax_bot.grid(True, which="both", alpha=0.3)

    _apply_compare_header(
        fig, rt_a, a_bin_name=a_bin_name, b_bin_name=b_bin_name
    )
    # tight_layout alone pads well beyond the header block to keep clear of the
    # suptitle, even with an explicit rect; fixing the top margin afterward
    # with subplots_adjust overrides that and closes the gap.
    fig.tight_layout()
    fig.subplots_adjust(top=0.84)
    return fig


def compare_tables(
    a: Union[TableView, RenderedTable],
    b: Union[TableView, RenderedTable],
    out_dir: Union[str, Path],
    *,
    surface: bool = True,
    heatmap: bool = True,
    curves: bool = True,
    value_cmap: str = _VALUE_CMAP,
    delta_cmap: str = _DELTA_CMAP,
    fmt: Union[str, object] = _CELL_FMT,
    elev: float = _ELEV,
    azim=_AZIM_AUTO,
    adaptive_azim: bool = True,
    a_bin_name: Optional[Union[str, Path]] = None,
    b_bin_name: Optional[Union[str, Path]] = None,
) -> list[Path]:
    """Compare two views of the same table; write composite PNG(s) to ``out_dir``.

    Provenance-agnostic (plan Key Decision 8): ``a``/``b`` may be two ``.bin``\\ s
    or a before/after pair (a pre-edit :class:`RenderedTable` snapshot and its
    edited :class:`~simoscal.calfile.TableView`). The delta is ``b − a``
    (second minus first — read as tuned−stock or after−before).

    Validates first (:func:`_check_comparable`, raising
    :class:`TableMismatchError` on any shape/axis mismatch), then dispatches on
    shape:

    * ``(1, 1)`` scalar → nothing produced (returns ``[]``).
    * single row (1D) → one ``<name>__compare_line.png`` (2-panel: overlay + delta).
    * otherwise (2D) → ``<name>__compare_surface.png``,
      ``<name>__compare_heatmap.png``, and/or ``<name>__compare_curves.png``
      (each a 3-panel composite), gated by its corresponding toggle.

    ``a_bin_name`` and ``b_bin_name`` add centered provenance lines containing
    each complete BIN filename. Parent directories are intentionally omitted.

    ``azim`` defaults to the :data:`_AZIM_AUTO` sentinel; see :func:`plot_table`
    for the adaptive / override semantics.
    """
    rt_a = _normalize(a)
    rt_b = _normalize(b)
    _check_comparable(rt_a, rt_b)

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    # Name from whichever side still knows its uniqueid. A before/after pair is
    # normally a RenderedTable snapshot against a live TableView, and only the
    # view carries one — so naming from ``a`` alone would throw away the very
    # thing that makes a patch table's stem unique (see :func:`_resolve_name`).
    named = a if getattr(a, "uniqueid_hex", None) else b
    stem = _sanitize_filename(_resolve_name(named))

    rows, cols = rt_a.values.shape
    if rows == 1 and cols == 1:
        return []

    delta = _delta(rt_a, rt_b)
    written: list[Path] = []

    if rows == 1:
        fig = _compare_line_figure(rt_a, rt_b, delta, value_cmap=value_cmap,
                                   delta_cmap=delta_cmap,
                                   a_bin_name=a_bin_name,
                                   b_bin_name=b_bin_name)
        written.append(_write_figure(fig, out / f"{stem}__compare_line.png"))
        return written

    if surface:
        fig = _compare_surface_figure(rt_a, rt_b, delta, value_cmap=value_cmap,
                                      delta_cmap=delta_cmap, elev=elev,
                                      azim=azim, adaptive_azim=adaptive_azim,
                                      a_bin_name=a_bin_name,
                                      b_bin_name=b_bin_name)
        written.append(_write_figure(fig, out / f"{stem}__compare_surface.png"))
    if heatmap:
        fig = _compare_heatmap_figure(rt_a, rt_b, delta, value_cmap=value_cmap,
                                      delta_cmap=delta_cmap, fmt=fmt,
                                      a_bin_name=a_bin_name,
                                      b_bin_name=b_bin_name)
        written.append(_write_figure(fig, out / f"{stem}__compare_heatmap.png"))
    if curves:
        fig = _compare_curves_figure(
            rt_a, rt_b, delta, value_cmap=value_cmap,
            a_bin_name=a_bin_name, b_bin_name=b_bin_name,
        )
        written.append(_write_figure(fig, out / f"{stem}__compare_curves.png"))
    return written


# --------------------------------------------------------------------------- #
# Batch wrappers (U4): selection-driven, per-category subfolder output
# --------------------------------------------------------------------------- #
def _category_dirs(rt: RenderedTable) -> list[str]:
    """The sanitized subfolder name(s) for a table.

    One per category (a multi-category table is written under each, mirroring
    Phase 2's xlsx duplication). A category-less table goes to
    ``_uncategorized`` rather than being silently dropped — an explicitly-named
    table must never vanish (plan Key Decision 6, the Phase 2 divergence).
    """
    if rt.categories:
        return [_sanitize_filename(c) for c in rt.categories]
    return ["_uncategorized"]


def plot_tables(
    cal: CalFile,
    out_dir: Union[str, Path],
    *,
    symbols=None,
    category: Optional[str] = None,
    all_tables: bool = False,
    surface: bool = True,
    heatmap: bool = True,
    value_cmap: str = _VALUE_CMAP,
    fmt: Union[str, object] = _CELL_FMT,
    elev: float = _ELEV,
    azim=_AZIM_AUTO,
    adaptive_azim: bool = True,
) -> list[Path]:
    """Batch-plot a selection of tables, grouped into per-category subfolders.

    Reuses Phase 2's :func:`~simoscal.export.select_tables` for the
    symbol/category/``all_tables`` selection model (deduped by uniqueid). Each
    non-scalar table's PNG set is written under ``out_dir/<category>/`` for every
    category it belongs to (``_uncategorized`` if none — Decision 6). Scalars
    produce nothing. Returns every written path.

    ``azim`` defaults to the :data:`_AZIM_AUTO` sentinel; see :func:`plot_table`
    for the adaptive / override semantics.
    """
    out = Path(out_dir)
    views = select_tables(cal, symbols=symbols, category=category, all_tables=all_tables)
    written: list[Path] = []
    for view in views:
        rt = render_table(view)
        for cat in _category_dirs(rt):
            written += plot_table(
                rt, out / cat, surface=surface, heatmap=heatmap,
                value_cmap=value_cmap, fmt=fmt, elev=elev,
                azim=azim, adaptive_azim=adaptive_azim,
            )
    return written


def compare_bins(
    cal_a: CalFile,
    cal_b: CalFile,
    out_dir: Union[str, Path],
    *,
    symbols=None,
    category: Optional[str] = None,
    all_tables: bool = False,
    surface: bool = True,
    heatmap: bool = True,
    curves: bool = True,
    value_cmap: str = _VALUE_CMAP,
    delta_cmap: str = _DELTA_CMAP,
    fmt: Union[str, object] = _CELL_FMT,
    elev: float = _ELEV,
    azim=_AZIM_AUTO,
    adaptive_azim: bool = True,
    a_bin_name: Optional[Union[str, Path]] = None,
    b_bin_name: Optional[Union[str, Path]] = None,
) -> list[Path]:
    """Batch-compare the same tables across two bins, into per-category subfolders.

    Selects from ``cal_a`` (:func:`~simoscal.export.select_tables`), then matches
    each table in ``cal_b`` by ``uniqueid`` (``cal_b.get(view_a.uniqueid)``) —
    which **fails loud** (``KeyError``) if the two bins were opened against
    different XDFs, never a silent skip. Each match produces the comparison
    composite(s) under ``out_dir/<category>/`` per category (``_uncategorized``
    if none). Scalars produce nothing. Returns every written path.

    ``azim`` defaults to the :data:`_AZIM_AUTO` sentinel; see :func:`plot_table`
    for the adaptive / override semantics.
    """
    out = Path(out_dir)
    views_a = select_tables(cal_a, symbols=symbols, category=category, all_tables=all_tables)
    written: list[Path] = []
    for view_a in views_a:
        view_b = cal_b.get(view_a.uniqueid)
        rt_a = render_table(view_a)
        rt_b = render_table(view_b)
        for cat in _category_dirs(rt_a):
            written += compare_tables(
                rt_a, rt_b, out / cat, surface=surface, heatmap=heatmap,
                curves=curves,
                value_cmap=value_cmap, delta_cmap=delta_cmap, fmt=fmt,
                elev=elev, azim=azim, adaptive_azim=adaptive_azim,
                a_bin_name=a_bin_name, b_bin_name=b_bin_name,
            )
    return written
