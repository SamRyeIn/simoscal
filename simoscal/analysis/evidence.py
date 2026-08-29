"""U5 — evidence plots and the folder entry point.

:func:`analyze_folder` is the public API: it loads a ``Logs/<Tune>_R<NN>/``
folder, detects pulls, autolocates the flashed bin (so calibration-aware checks
can run), runs the battery, writes evidence PNGs into ``plots/`` and the
``analysis_findings.{json,md}`` files into the folder, and returns everything it
produced. ``python -m simoscal.analysis <folder>`` (``__main__``) is a thin
wrapper over it.

matplotlib is used headless via the object API only (constructing
:class:`matplotlib.figure.Figure` directly, never importing ``pyplot``),
matching :mod:`simoscal.plot`.

Evidence-plot inventory (all written into ``plots/`` as ``analysis_*.png``):

- Six per-check plots vs RPM — ``boost``, ``knock``, ``lambda``,
  ``rail_pressure``, ``turbo_heat``, ``wastegate`` — keyed by their check id so
  a fired finding carries the PNG as a ``plot_ref``.
- ``ignition`` — delivered (`ign_avg`) vs table (`ign_table`) timing vs RPM;
  standalone (no check, so no ``plot_ref``).
- ``overview_<log-stem>`` — one whole-log panel-stack per CSV vs time
  (rpm+gear, pedal, PUT, lambda, min knock, IAT) with detected pull windows
  shaded, auditing pull detection.
- ``tc_activity_<log-stem>`` — one per CSV vs time inferring the switch-patch
  slip-based TC (wheel slip, ignition, wastegate, torque); skipped entirely
  when no wheel-speed channel is present.

All plots share one encoding rule: **quantity = line style, pull = color** —
each pull is a per-color solid line (RPM-sorted), reference quantities
(setpoint/base/table) are dashed dark gray with a single legend entry. A
plotter that finds no data returns False and writes no file.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from .checks import _KNOCK_CHANNELS, default_battery
from .coverage import CoverageResult, cells_hit, compute_coverage, coverage_to_dict
from .log import load_logset
from .pulls import detect_pulls
from .registry import BatteryResult, CheckContext, run_battery
from .report import md_table, write_findings
from .series import (
    PLOT_SPECS,
    PSI_PER_KPA,
    PanelSpec,
    PlotSpec,
    Role,
    SeriesSpec,
    Tone,
)
from .series import contiguous_runs as _contiguous_runs
from .series import min_knock_arrays as _min_knock_arrays
from .series import panel_available, pull_ordinals, series_segments

__all__ = ["AnalyzeResult", "analyze_folder", "resolve_bin", "resolve_xdf"]

_DPI = 160
_PLOTS_SUBDIR = "plots"


def _figure(*args, **kwargs):
    """Construct a matplotlib ``Figure``, importing it lazily.

    matplotlib is an optional ``plot`` extra, not a core dependency, so the
    library stays importable on-device without it. The analysis battery only
    needs matplotlib when it actually renders evidence plots; touching this
    helper without matplotlib installed raises an actionable error naming the
    extra rather than failing at import time.
    """
    try:
        from matplotlib.figure import Figure
    except ImportError as exc:  # pragma: no cover - env-dependent
        raise ImportError(
            "Rendering analysis evidence plots needs the optional 'plot' "
            "dependencies (matplotlib). Install them with: "
            "pip install 'simoscal[plot]'"
        ) from exc
    return Figure(*args, **kwargs)


@dataclass(frozen=True)
class AnalyzeResult:
    """Everything :func:`analyze_folder` produced."""

    result: BatteryResult
    json_path: Path
    md_path: Path
    plot_paths: dict[str, Path]
    bin_path: Optional[Path]
    xdf_path: Optional[Path]


# --------------------------------------------------------------------------- #
# Bin / XDF autolocation
# --------------------------------------------------------------------------- #
def _find_project_root(start: Path) -> Optional[Path]:
    """Walk up from ``start`` to the dir holding both ``Tunes/`` and ``Code/``."""
    for d in [start, *start.parents]:
        if (d / "Tunes").is_dir() and (d / "Code").is_dir():
            return d
    return None


def resolve_bin(folder: Path, override: Optional[str | Path] = None) -> Optional[Path]:
    """Locate the flashed bin for a log folder.

    An explicit ``override`` wins. Otherwise the ``*.bin.txt`` record's filename
    names the bin (the ``.bin.txt`` itself is empty — the *name* is the record):
    ``<box>_<Tune>_R<NN>.bin.txt`` → search ``Tunes/`` for ``<same-stem>.bin``
    (newest wins). Returns ``None`` if nothing matches — calibration-aware
    checks then degrade to SKIPPED, matching the missing-channel policy.
    """
    if override is not None:
        p = Path(override)
        return p if p.is_file() else None
    txts = sorted(folder.glob("*.bin.txt"))
    if not txts:
        return None
    stem = txts[0].name[: -len(".bin.txt")]
    root = _find_project_root(folder.resolve())
    if root is None:
        return None
    candidates = sorted(
        root.glob(f"Tunes/**/{stem}.bin"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    return candidates[0] if candidates else None


def resolve_xdf(folder: Path, override: Optional[str | Path] = None) -> Optional[Path]:
    """The XDF definition: explicit ``override``, else the project's SC8S50 XDF."""
    if override is not None:
        p = Path(override)
        return p if p.is_file() else None
    root = _find_project_root(folder.resolve())
    if root is None:
        return None
    p = root / "Code" / "xdf" / "SC8S50.V1.0.xdf"
    return p if p.is_file() else None


def _open_cal(xdf: Optional[Path], binp: Optional[Path]) -> tuple[Optional[object], list[str]]:
    """Open a CalFile if both files resolved; on failure return a loud note."""
    if xdf is None or binp is None:
        return None, []
    try:
        from ..calfile import CalFile, structure_of

        return CalFile.open(str(xdf), str(binp), structure=structure_of(binp)), []
    except Exception as exc:  # a broken bin should not crash analysis, but must be reported
        return None, [f"could not open calibration ({binp.name} / {xdf.name}): {exc}"]


# --------------------------------------------------------------------------- #
# Plot helpers
# --------------------------------------------------------------------------- #
def _style(ax, title: str, xlabel: str, ylabel: str) -> None:
    ax.set_title(title, fontweight="bold")
    ax.set_xlabel(xlabel, fontweight="bold")
    ax.set_ylabel(ylabel, fontweight="bold")
    ax.grid(True, which="major", alpha=0.35)
    ax.grid(True, which="minor", alpha=0.15)
    ax.minorticks_on()


def _legend(ax) -> None:
    """Attach a compact legend only if any artist on the axes carries a label."""
    handles, labels = ax.get_legend_handles_labels()
    if labels:
        ax.legend(loc="best", fontsize=8)


_REF_COLOR = "0.35"        # dashed dark-gray reference (setpoint / base / table)
_SECONDARY_COLOR = "0.55"  # dash-dot mid-gray secondary-actual (e.g. HPFP alongside LPFP)
_TRANSIENT_COLOR = "0.5"   # faint scatter of loaded-but-unsettled samples

#: matplotlib's half of the encoding rule the specs declare: **quantity = line
#: style, pull = colour** (see :mod:`simoscal.analysis.series`). Only the
#: mark-making lives here; which channel belongs on which panel does not.
_ROLE_STYLE: dict[str, tuple[str, str]] = {
    Role.REFERENCE: ("--", _REF_COLOR),
    Role.SECONDARY: ("-.", _SECONDARY_COLOR),
}

#: Threshold lines by tone: ``(colour, linestyle, linewidth)``. These are the
#: lines where *this tool* starts paying attention — never a limit the ECU
#: enforces — which is why none of them is drawn in the refusal red the app
#: reserves for an engine rejection.
_THRESHOLD_STYLE: dict[str, tuple[str, str, float]] = {
    Tone.ZERO: ("0.3", "-", 0.9),
    Tone.WATCH: ("tab:orange", "--", 0.9),
    Tone.HIGH: ("tab:red", "--", 1.0),
}


#: Tier 2 — bold, saturated named colors added once the 10-color matplotlib
#: default cycle (tier 1, built from ``axes.prop_cycle`` below) runs out.
_BOLD_NAMED_COLORS = (
    "black", "cyan", "magenta", "gold", "lime", "deeppink", "saddlebrown", "navy",
)


def _pull_color(ordinal: int, total: int) -> str:
    """A color unique to this pull among ``total`` pulls sharing one plot.

    Tier 1 is matplotlib's own default cycle (``C0``..``C9``), tier 2 is a
    short list of bold named colors distinct from it, and only once both are
    exhausted does the rest come from an HSV colormap sweep — MATLAB's `hsv`
    equivalent — so a handful of pulls still gets matplotlib's familiar
    palette instead of an unfamiliar rainbow.
    """
    import matplotlib as mpl
    import matplotlib.colors as mcolors

    tier1 = list(mpl.rcParams["axes.prop_cycle"].by_key()["color"])
    palette = tier1 + list(_BOLD_NAMED_COLORS)
    if ordinal < len(palette):
        return mcolors.to_hex(palette[ordinal])
    extra_total = max(1, total - len(palette))
    frac = (ordinal - len(palette)) / extra_total
    return mcolors.to_hex(mpl.colormaps["hsv"](frac))


def _gear_tag(ctx, pull_index: int) -> str:
    """``", 3G"`` for a resolved gear, else ``""`` — never guessed."""
    for p in ctx.pulls:
        if p.index == pull_index:
            if p.gear_resolved and p.gear is not None:
                return f", {p.gear}G"
            return ""
    return ""


def _draw_series(ax, ctx, spec: SeriesSpec, ordinals: dict[int, int]) -> bool:
    """Draw one declared series onto ``ax``. True if any sample was drawn.

    The samples themselves come from :func:`series_segments`, which the app's
    JSON payload also calls — so a line here and the same line on the phone are
    the same masked, segmented, x-sorted data, and only the ink differs.

    A ``primary`` series takes one legend entry *per pull*; a ``reference`` or
    ``secondary`` series takes one entry in total however many pulls it spans,
    because it is one quantity drawn repeatedly rather than several.
    """
    drew = False
    shared_label_used = False
    for data in series_segments(ctx, spec):
        if spec.role == Role.PRIMARY:
            color = _pull_color(ordinals.get(data.pull_index, 0), len(ordinals))
            style = "-"
            pull_tag = f"Pull {data.pull_index}{_gear_tag(ctx, data.pull_index)}"
            entry = f"{spec.label} ({pull_tag})" if spec.label else pull_tag
        elif spec.role == Role.TRANSIENT:
            # Scatter, not a line: a transient is genuinely not curve-like, and
            # joining the points would assert a sweep that never happened.
            for segment in data.segments:
                if not segment.x.size:
                    continue
                ax.scatter(
                    segment.x, segment.y, s=8, alpha=0.2, color=_TRANSIENT_COLOR,
                    label=None if shared_label_used else (spec.label or None),
                )
                drew = True
                shared_label_used = True
            continue
        else:
            style, color = _ROLE_STYLE[spec.role]
            entry = None if shared_label_used else (spec.label or None)

        first_segment = True
        for segment in data.segments:
            if not segment.x.size:
                continue
            ax.plot(
                segment.x, segment.y, ls=style, color=color, lw=1.4, alpha=0.9,
                label=entry if first_segment else None,
            )
            drew = True
            first_segment = False
        if entry and spec.role != Role.PRIMARY:
            shared_label_used = True
    return drew


def _draw_panel(ax, ctx, panel: PanelSpec, ordinals: dict[int, int]) -> bool:
    """Draw one declared panel — its series, then its threshold lines."""
    drew = False
    # Transients first so the settled lines sit on top of their own context.
    ordered = sorted(panel.series, key=lambda s: s.role != Role.TRANSIENT)
    for spec in ordered:
        drew = _draw_series(ax, ctx, spec, ordinals) or drew
    for threshold in panel.thresholds:
        color, style, width = _THRESHOLD_STYLE[threshold.tone]
        ax.axhline(
            threshold.value, color=color, ls=style, lw=width,
            label=threshold.label or None,
        )
    _style(ax, panel.title, panel.x_label, panel.y_label)
    _legend(ax)
    return drew


def _render_plot_spec(ctx, spec: PlotSpec, path) -> bool:
    """Render one declared plot to PNG. False (and no file) if nothing drew.

    Panels whose ``requires`` channels are absent are dropped rather than drawn
    empty — the gauge-boost panel without ambient pressure is the case that
    motivates it. A plot left with no drawable panel writes nothing at all.
    """
    panels = [panel for panel in spec.panels if panel_available(ctx, panel)]
    if not panels:
        return False
    subtitle = spec.subtitle_fn(ctx) if spec.subtitle_fn else None
    if subtitle:
        panels = [replace(panels[0], title=f"{panels[0].title}\n{subtitle}"), *panels[1:]]
    count = len(panels)
    fig = _figure(figsize=(10, 5.5) if count == 1 else (10, 4 * count))
    ordinals = pull_ordinals(ctx)
    drew = False
    for position, panel in enumerate(panels, start=1):
        ax = fig.add_subplot(count, 1, position)
        drew = _draw_panel(ax, ctx, panel, ordinals) or drew
    if not drew:
        return False
    fig.tight_layout()
    fig.savefig(path, format="png", dpi=_DPI)
    return True


# --------------------------------------------------------------------------- #
# Per-file time-axis plots (overview + TC activity)
# --------------------------------------------------------------------------- #
def _time_line(ax, t, y, *, role="primary", label=None, color="tab:blue", lw=1.2) -> bool:
    """Whole-log line vs time, split at NaN holes. Reference role = dashed gray."""
    if y is None:
        return False
    sel = np.isfinite(t) & np.isfinite(y)
    if not np.any(sel):
        return False
    ls = "--" if role == "reference" else "-"
    c = _REF_COLOR if role == "reference" else color
    first = True
    for lo, hi in _contiguous_runs(sel):
        ax.plot(t[lo : hi + 1], y[lo : hi + 1], ls=ls, color=c, lw=lw,
                label=label if first else None)
        first = False
    return True


def _pull_time_spans(ctx, lf) -> list[tuple[int, float, float]]:
    """``(pull_index, t_start, t_end)`` for the pulls in ``lf``, via its time array.

    Factored so a test can assert the shaded spans equal ``detect_pulls`` output.
    """
    t = lf.time
    spans: list[tuple[int, float, float]] = []
    if t is None:
        return spans
    for p in ctx.pulls:
        if p.file != lf.name:
            continue
        if not (0 <= p.start_row < t.size and 0 <= p.end_row < t.size):
            continue
        ts, te = t[p.start_row], t[p.end_row]
        if np.isfinite(ts) and np.isfinite(te):
            spans.append((p.index, float(ts), float(te)))
    return spans


def _render_stacked(fig, panels, spans) -> None:
    """Draw ``panels`` as stacked shared-x axes, shading + labeling pull spans.

    ``panels`` is a list of ``(drawer(ax), title, ylabel)``. Pull windows are
    shaded on every panel and labeled once (``Pull N``) on the top panel.
    """
    axes = fig.subplots(len(panels), 1, sharex=True, squeeze=False)[:, 0]
    for ax, (drawer, title, ylabel) in zip(axes, panels):
        drawer(ax)
        _style(ax, title, "", ylabel)
        for _idx, ts, te in spans:
            ax.axvspan(ts, te, color="tab:gray", alpha=0.12)
        _legend(ax)
    top = axes[0]
    trans = top.get_xaxis_transform()
    for idx, ts, te in spans:
        top.text((ts + te) / 2.0, 0.98, f"Pull {idx}", transform=trans,
                 ha="center", va="top", fontsize=7, color="0.25")
    axes[-1].set_xlabel("Time (s)", fontweight="bold")


def _plot_overview(ctx, lf, path) -> bool:
    """U4 — one whole-log overview per CSV, auditing pull detection against time."""
    t = lf.time
    if t is None:
        return False
    spans = _pull_time_spans(ctx, lf)

    def _rpm(ax):
        _time_line(ax, t, lf.channel("rpm"), label="RPM", color="tab:blue")
        g = lf.channel("gear")
        if g is not None and lf.gear_resolved:
            gsel = np.isfinite(t) & np.isfinite(g)
            if np.any(gsel):
                ax2 = ax.twinx()
                ax2.step(t[gsel], g[gsel], where="post", color="tab:green", lw=1.0, alpha=0.8)
                ax2.set_ylabel("Gear", fontweight="bold")

    def _put(ax):
        _time_line(ax, t, lf.channel("put"), label="PUT", color="tab:blue")
        _time_line(ax, t, lf.channel("put_sp"), role="reference", label="PUT SP")

    def _lam(ax):
        _time_line(ax, t, lf.channel("lambda"), label="Lambda", color="tab:red")
        _time_line(ax, t, lf.channel("lambda_sp"), role="reference", label="Lambda SP")

    panels = []
    if lf.has("rpm"):
        panels.append((_rpm, "Engine speed and gear", "RPM"))
    if lf.has("pedal"):
        panels.append((lambda ax: _time_line(ax, t, lf.channel("pedal"), label="Pedal",
                                              color="tab:purple"), "Pedal", "Pedal (%)"))
    if lf.has("put"):
        panels.append((_put, "PUT vs setpoint", "kPa"))
    if lf.has("lambda"):
        panels.append((_lam, "Lambda vs setpoint", "Lambda"))
    if any(lf.has(c) for c in _KNOCK_CHANNELS):
        panels.append((lambda ax: _time_line(ax, t, _min_knock_arrays(
            [lf.channel(c) for c in _KNOCK_CHANNELS]), label="Min knock", color="tab:orange"),
            "Most-retarded cylinder", "Knock retard (deg)"))
    if lf.has("iat"):
        panels.append((lambda ax: _time_line(ax, t, lf.channel("iat"), label="IAT",
                                              color="tab:brown"), "Intake air temp", "IAT (deg C)"))
    if not panels:
        return False
    fig = _figure(figsize=(11, 2.1 * len(panels)))
    _render_stacked(fig, panels, spans)
    fig.tight_layout(); fig.savefig(path, format="png", dpi=_DPI)
    return True


def _wheel_slip(lf) -> Optional[np.ndarray]:
    """Front-mean minus rear-mean wheel speed (FWD drive slip); None if a side is absent."""
    front = [lf.channel(c) for c in ("wheel_fl", "wheel_fr")]
    rear = [lf.channel(c) for c in ("wheel_rl", "wheel_rr")]
    front = [a for a in front if a is not None]
    rear = [a for a in rear if a is not None]
    if not front or not rear:
        return None
    with np.errstate(all="ignore"):
        fmean = np.nanmean(np.vstack(front), axis=0)
        rmean = np.nanmean(np.vstack(rear), axis=0)
    return fmean - rmean


_WHEEL_CHANNELS = ("wheel_fl", "wheel_fr", "wheel_rl", "wheel_rr")


def _plot_tc_activity(ctx, lf, path) -> bool:
    """U5 — infer the switch-patch slip-based TC's activity per CSV, vs time.

    Skipped entirely when no wheel-speed channel is present — slip is the
    defining panel and the plot has nothing to say without it (D8, AE5).
    """
    t = lf.time
    if t is None or not any(lf.has(c) for c in _WHEEL_CHANNELS):
        return False
    spans = _pull_time_spans(ctx, lf)
    slip = _wheel_slip(lf)

    def _ign(ax):
        _time_line(ax, t, lf.channel("ign_avg"), label="Ign Avg", color="tab:blue")
        _time_line(ax, t, lf.channel("ign_table"), role="reference", label="Ign Table")
        _time_line(ax, t, _min_knock_arrays([lf.channel(c) for c in _KNOCK_CHANNELS]),
                   label="Min knock", color="tab:orange")

    def _wg(ax):
        _time_line(ax, t, lf.channel("wg_pos_final"), label="Final", color="tab:blue")
        _time_line(ax, t, lf.channel("wg_pos_base"), role="reference", label="Base")

    def _torque(ax):
        _time_line(ax, t, lf.channel("torque"), label="Torque", color="tab:blue")
        _time_line(ax, t, lf.channel("torque_req"), role="reference", label="Torque Req")

    panels = []
    if slip is not None:
        panels.append((lambda ax: _time_line(ax, t, slip, label="Front - rear slip",
                                              color="tab:red"), "Wheel slip (front - rear)", "km/h"))
    if lf.has("ign_avg") and lf.has("ign_table"):
        panels.append((_ign, "Ignition: delivered vs table", "deg"))
    if lf.has("wg_pos_final"):
        panels.append((_wg, "Wastegate: final vs base", "%"))
    if lf.has("torque") and lf.has("torque_req"):
        panels.append((_torque, "Torque: delivered vs request", "Nm"))
    if not panels:
        return False
    fig = _figure(figsize=(11, 2.1 * len(panels)))
    _render_stacked(fig, panels, spans)
    fig.tight_layout(); fig.savefig(path, format="png", dpi=_DPI)
    return True


def _sanitize(name: str) -> str:
    return "".join("_" if c in "[]/\\:*?<>| " else c for c in name).strip("_") or "table"


def _plot_coverage(cov: CoverageResult, path: Path) -> bool:
    """Side-by-side whole-log vs WOT-only hit-count heatmaps for one table."""
    if not cov.y_channel:
        return False   # 1D coverage heatmaps not rendered in v1
    whole = np.array(cov.counts_whole, dtype=float)
    wot = np.array(cov.counts_wot, dtype=float)
    fig = _figure(figsize=(12, 5))
    vmax = max(whole.max(), 1.0)
    for pos, (grid, name) in enumerate(((whole, "Whole log"), (wot, "WOT pulls")), start=1):
        ax = fig.add_subplot(1, 2, pos)
        im = ax.imshow(grid, origin="lower", aspect="auto", cmap="viridis", vmin=0, vmax=vmax)
        ax.set_title(f"{name}", fontweight="bold")
        ax.set_xlabel(f"{cov.x_channel} (axis idx)", fontweight="bold")
        ax.set_ylabel(f"{cov.y_channel} (axis idx)", fontweight="bold")
        # Breakpoint tick labels (thinned if dense).
        xs = cov.x_breakpoints
        ys = cov.y_breakpoints
        xstep = max(1, len(xs) // 8)
        ystep = max(1, len(ys) // 8)
        ax.set_xticks(range(0, len(xs), xstep))
        ax.set_xticklabels([f"{xs[i]:.0f}" for i in range(0, len(xs), xstep)], fontsize=7, rotation=45)
        ax.set_yticks(range(0, len(ys), ystep))
        ax.set_yticklabels([f"{ys[i]:.0f}" for i in range(0, len(ys), ystep)], fontsize=7)
        fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04, label="hit count")
    fig.suptitle(cov.symbol, fontweight="bold")
    fig.tight_layout()
    fig.savefig(path, format="png", dpi=_DPI)
    return True


# Fixed-name plotters, keyed by their output id -> `analysis_<id>.png`. The six
# per-check plots share a key with their check id (so `_attach_plot_refs` wires
# them onto findings); `ignition` is standalone (no check, so no plot_ref). A
# plotter that finds no data returns False (no file). (D3, D9)
#
# Built from `PLOT_SPECS` rather than written out here: the inventory has one
# home (`simoscal.analysis.series`) because the Android app renders the same
# plots from the same declarations without matplotlib, and a second hand-kept
# list is exactly where the two would drift apart.
_PLOTTERS: dict[str, Callable] = {
    spec.id: (lambda ctx, path, spec=spec: _render_plot_spec(ctx, spec, path))
    for spec in PLOT_SPECS
}

# Per-file plotters, keyed by prefix -> one `analysis_<prefix>_<stem>.png` per
# LogFile, registered in `plot_paths` under `"<prefix>:<log-stem>"`. Never a
# check id, so findings content is untouched.
_PER_FILE_PLOTTERS: dict[str, Callable] = {
    "overview": _plot_overview,
    "tc_activity": _plot_tc_activity,
}


def _make_plots(ctx: CheckContext, folder: Path) -> dict[str, Path]:
    plot_dir = folder / _PLOTS_SUBDIR
    plot_dir.mkdir(parents=True, exist_ok=True)
    out: dict[str, Path] = {}
    for plot_id, fn in _PLOTTERS.items():
        path = plot_dir / f"analysis_{plot_id}.png"
        try:
            if fn(ctx, path):
                out[plot_id] = path
        except Exception:  # a plotting failure must never sink the analysis
            continue
    for prefix, fn in _PER_FILE_PLOTTERS.items():
        for lf in ctx.logset.files:
            path = plot_dir / f"analysis_{prefix}_{_sanitize(lf.name)}.png"
            try:
                if fn(ctx, lf, path):
                    out[f"{prefix}:{lf.name}"] = path
            except Exception:
                continue
    return out


def _coverage_section(ctx, cov_results, cov_skipped, folder, plot_paths, make_plots) -> dict:
    """The JSON ``coverage`` section plus a rendered heatmap per table.

    The section itself comes from :func:`~simoscal.analysis.coverage.coverage_to_dict`,
    which the advice bundle also calls — one serialisation, so a bundle's
    coverage and a folder's ``analysis_findings.json`` cannot describe the same
    logs differently. All this layer adds is the ``plot`` key.
    """
    section = coverage_to_dict(cov_results, cov_skipped)
    if not make_plots:
        return section
    plot_dir = folder / _PLOTS_SUBDIR
    if cov_results:
        plot_dir.mkdir(parents=True, exist_ok=True)
    for cov, entry in zip(cov_results, section["results"]):
        path = plot_dir / f"analysis_coverage_{_sanitize(cov.symbol)}.png"
        try:
            if _plot_coverage(cov, path):
                entry["plot"] = f"{_PLOTS_SUBDIR}/{path.name}"
        except Exception:
            pass
    return section


def _coverage_markdown(cov_results, cov_skipped) -> list[str]:
    L = ["## Table coverage", ""]
    if cov_results:
        rows = []
        for cov in cov_results:
            n_cells = int(np.prod(cov.shape))
            rows.append([
                cov.symbol,
                f"{cells_hit(cov.counts_whole)}/{n_cells}",
                f"{cells_hit(cov.counts_wot)}/{n_cells}",
                str(cov.total_whole),
                str(cov.total_wot),
            ])
        L.append(md_table(
            ["Table", "Cells hit (whole)", "Cells hit (WOT)", "Samples (whole)", "Samples (WOT)"],
            rows,
        ))
        L.append("")
    if cov_skipped:
        L.append("Skipped coverage:")
        L.append("")
        L.append(md_table(
            ["Table", "Reason"],
            [[s.check_id.removeprefix("coverage:"), s.reason] for s in cov_skipped],
        ))
        L.append("")
    if not cov_results and not cov_skipped:
        L.append("_No coverage specs configured._")
        L.append("")
    return L


# Checks that read the same picture as another check. The boost plot draws PUT
# against its setpoint and the signed error underneath, which is the shortfall
# check's evidence exactly as much as the overshoot check's — rendering it twice
# under two names would put two identical PNGs in the folder.
_PLOT_ALIASES: dict[str, str] = {"boost_shortfall": "boost"}


def _attach_plot_refs(result: BatteryResult, plot_paths: dict[str, Path]) -> BatteryResult:
    def key(check_id: str) -> str:
        return _PLOT_ALIASES.get(check_id, check_id)

    findings = tuple(
        replace(f, plot_refs=(f"{_PLOTS_SUBDIR}/{plot_paths[key(f.check_id)].name}",))
        if key(f.check_id) in plot_paths and not f.plot_refs else f
        for f in result.findings
    )
    return replace(result, findings=findings)


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def analyze_folder(
    folder: str | Path,
    *,
    xdf_path: Optional[str | Path] = None,
    bin_path: Optional[str | Path] = None,
    make_plots: bool = True,
) -> AnalyzeResult:
    """Run the full battery over a log folder and write all artifacts into it.

    Loads the CSVs, detects pulls, autolocates (or takes an override for) the
    flashed bin + XDF, runs the battery, writes evidence PNGs and the
    ``analysis_findings.{json,md}`` files, and returns an :class:`AnalyzeResult`.
    An unresolved bin is not an error — calibration-aware checks simply SKIP.
    """
    folder = Path(folder)
    logset = load_logset(folder)
    pulls = detect_pulls(logset)

    binp = resolve_bin(folder, bin_path)
    xdfp = resolve_xdf(folder, xdf_path)
    cal, cal_notes = _open_cal(xdfp, binp)

    ctx = CheckContext(logset=logset, pulls=pulls, cal=cal)
    result = run_battery(default_battery(), ctx)

    plot_paths: dict[str, Path] = {}
    if make_plots:
        plot_paths = _make_plots(ctx, folder)
        result = _attach_plot_refs(result, plot_paths)

    # Table coverage maps (U7): needs the resolved calibration.
    cov_results, cov_skipped = compute_coverage(ctx)
    coverage_json = _coverage_section(ctx, cov_results, cov_skipped, folder, plot_paths, make_plots)

    # Assemble extra JSON sections and Markdown.
    extra_json: dict = {"coverage": coverage_json}
    if cal_notes:
        extra_json["cal_notes"] = cal_notes
    extra_sections: list[str] = []
    if cal_notes:
        extra_sections += ["## Calibration notes", "", *[f"- {n}" for n in cal_notes], ""]
    extra_sections += _coverage_markdown(cov_results, cov_skipped)

    json_path, md_path = write_findings(
        result, folder, extra_json=extra_json, extra_sections=extra_sections or None
    )

    return AnalyzeResult(
        result=result,
        json_path=json_path,
        md_path=md_path,
        plot_paths=plot_paths,
        bin_path=binp,
        xdf_path=xdfp,
    )
