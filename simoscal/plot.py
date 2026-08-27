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

from pathlib import Path
from typing import Optional, Union

import numpy as np
from matplotlib import colormaps, colors
from matplotlib.figure import Figure

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

# Default value/delta colormaps (plan Key Decision 11) and cell-text format
# (Decision 7). All exposed as kwargs on the public functions.
_VALUE_CMAP = "turbo"
_DELTA_CMAP = "RdBu_r"
_CELL_FMT = "{:.4g}"

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
        fig.text(
            0.5,
            0.905,
            "\n".join(provenance),
            ha="center",
            va="top",
            fontsize=8,
            family="monospace",
            linespacing=1.2,
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


def _annotation_fontsize(rows: int, cols: int) -> float:
    """A grid-size-adaptive font size for the cell-value overlay (Decision 7).

    Full size for small grids, shrinking as the grid grows so a dense table's
    numbers do not collide. Clamped to a legible floor.
    """
    largest = max(rows, cols)
    return float(max(4.0, min(9.0, 90.0 / largest)))


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
    fmt: str = _CELL_FMT,
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

    fontsize = _annotation_fontsize(rows, cols)
    for r in range(rows):
        for c in range(cols):
            v = values[r, c]
            ax.text(
                c, r, fmt.format(v),
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


def _resolve_name(source: Union[TableView, RenderedTable]) -> str:
    """The base name for a table's files: symbol, else title, else ``uniqueid``.

    Resolved from the *source* so a :class:`~simoscal.calfile.TableView` can fall
    back to its ``uniqueid_hex`` — a :class:`RenderedTable` carries no uniqueid,
    so it falls back to a generic stem (in practice every real table has a
    symbol).
    """
    name = source.symbol or source.title
    if name:
        return name
    hexid = getattr(source, "uniqueid_hex", None)
    return hexid or "table"


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
    fmt: str = _CELL_FMT,
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
def _draw_heatmap_panel(ax, rt, values, *, cmap, norm, fmt, title) -> None:
    """Draw one imshow panel with value overlay onto an existing axes."""
    im = ax.imshow(values, cmap=cmap, norm=norm, aspect="auto")
    _axis_ticks(ax, rt.x_labels, rt.y_labels)
    ax.set_title(title, fontsize=9)
    rows, cols = values.shape
    fontsize = _annotation_fontsize(rows, cols)
    cmap_obj = colormaps[cmap] if isinstance(cmap, str) else cmap
    for r in range(rows):
        for c in range(cols):
            v = values[r, c]
            ax.text(
                c, r, fmt.format(v), ha="center", va="center",
                color=_text_color(cmap_obj(norm(v))), fontsize=fontsize,
            )
    return im


def _compare_heatmap_figure(
    rt_a: RenderedTable,
    rt_b: RenderedTable,
    delta: np.ndarray,
    *,
    value_cmap: str = _VALUE_CMAP,
    delta_cmap: str = _DELTA_CMAP,
    fmt: str = _CELL_FMT,
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

    im_a = _draw_heatmap_panel(ax_a, rt_a, rt_a.values, cmap=value_cmap,
                               norm=value_norm, fmt=fmt, title="A")
    _draw_heatmap_panel(ax_b, rt_b, rt_b.values, cmap=value_cmap,
                        norm=value_norm, fmt=fmt, title="B")
    im_d = _draw_heatmap_panel(ax_d, rt_a, delta, cmap=delta_cmap,
                               norm=delta_norm, fmt=fmt, title="Δ (B − A)")

    fig.colorbar(im_a, ax=[ax_a, ax_b], label=rt_a.units or "", shrink=0.7)
    fig.colorbar(im_d, ax=ax_d, label=rt_a.units or "", shrink=0.7)
    _apply_compare_header(
        fig, rt_a, a_bin_name=a_bin_name, b_bin_name=b_bin_name
    )
    fig.subplots_adjust(top=0.80)
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
    fig.subplots_adjust(top=0.68, bottom=0.04)
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


def _compare_columns_figure(
    rt_a: RenderedTable,
    rt_b: RenderedTable,
    delta: np.ndarray,
    *,
    value_cmap: str = _VALUE_CMAP,
    a_bin_name: Optional[Union[str, Path]] = None,
    b_bin_name: Optional[Union[str, Path]] = None,
) -> Figure:
    """A 3-panel line composite with one labeled curve per table column.

    Curves run over the row-axis breakpoints and are keyed by their column-axis
    breakpoint. Colors and labels are identical across A, B, and ``B - A``;
    all three panels also receive the exact same Y limits so their vertical
    shapes and magnitudes can be compared directly.
    """
    row_axis = np.asarray(rt_a.y_labels, dtype=float)
    column_axis = np.asarray(rt_a.x_labels, dtype=float)
    line_colors = colormaps[value_cmap](np.linspace(0.05, 0.95, len(column_axis)))
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
        for col, (column_value, color) in enumerate(zip(column_axis, line_colors)):
            ax.plot(
                row_axis,
                values[:, col],
                marker="o",
                markersize=3,
                linewidth=1.2,
                color=color,
                label=f"{column_value:g}",
            )
        if pos == 3:
            ax.axhline(0.0, color="0.6", linewidth=0.8)
        ax.set_xlabel(rt_a.y_units or "", fontweight="bold")
        ax.set_ylabel(rt_a.units or "", fontweight="bold")
        ax.set_title(title, fontsize=9)
        ax.set_ylim(ylim)
        ax.grid(True, which="both", alpha=0.3)
        axes.append(ax)

    legend_title = "Column value"
    if rt_a.x_units:
        legend_title += f" ({rt_a.x_units})"
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
    fig.tight_layout(rect=(0.0, 0.0, 0.88, 0.80))
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
    fig.tight_layout(rect=(0.0, 0.0, 1.0, 0.80))
    return fig


def compare_tables(
    a: Union[TableView, RenderedTable],
    b: Union[TableView, RenderedTable],
    out_dir: Union[str, Path],
    *,
    surface: bool = True,
    heatmap: bool = True,
    columns: bool = True,
    value_cmap: str = _VALUE_CMAP,
    delta_cmap: str = _DELTA_CMAP,
    fmt: str = _CELL_FMT,
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
      ``<name>__compare_heatmap.png``, and/or ``<name>__compare_columns.png``
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
    stem = _sanitize_filename(_resolve_name(a))

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
    if columns:
        fig = _compare_columns_figure(
            rt_a, rt_b, delta, value_cmap=value_cmap,
            a_bin_name=a_bin_name, b_bin_name=b_bin_name,
        )
        written.append(_write_figure(fig, out / f"{stem}__compare_columns.png"))
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
    fmt: str = _CELL_FMT,
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
    columns: bool = True,
    value_cmap: str = _VALUE_CMAP,
    delta_cmap: str = _DELTA_CMAP,
    fmt: str = _CELL_FMT,
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
                columns=columns,
                value_cmap=value_cmap, delta_cmap=delta_cmap, fmt=fmt,
                elev=elev, azim=azim, adaptive_azim=adaptive_azim,
                a_bin_name=a_bin_name, b_bin_name=b_bin_name,
            )
    return written
