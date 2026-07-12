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
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Optional

import numpy as np
from matplotlib.figure import Figure

from .checks import (
    BOOST_HIGH_KPA,
    BOOST_WATCH_KPA,
    HPFP_WATCH_PCT,
    KNOCK_HIGH_DEG,
    KNOCK_WATCH_DEG,
    LAMBDA_WATCH,
    LPFP_WATCH_PCT,
    TURBO_SPEED_LIMIT_K,
    TURBO_SPEED_WATCH_K,
    _KNOCK_CHANNELS,
    _col,
    _loaded_mask,
    _settled_mask,
    default_battery,
)
from .coverage import CoverageResult, compute_coverage
from .log import load_logset
from .pulls import detect_pulls
from .registry import BatteryResult, CheckContext, run_battery
from .report import md_table, write_findings

__all__ = ["AnalyzeResult", "analyze_folder", "resolve_bin", "resolve_xdf"]

_DPI = 160
_PLOTS_SUBDIR = "plots"


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
        from ..calfile import CalFile

        return CalFile.open(str(xdf), str(binp)), []
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


_CYCLE = ["tab:blue", "tab:orange", "tab:green", "tab:red", "tab:purple", "tab:brown"]


def _scatter_pulls(ax, ctx, y_channel_or_fn, *, mask="loaded", size=12, label_prefix="") -> bool:
    """Scatter rpm vs a per-sample quantity, colored per pull. True if anything drawn."""
    drew = False
    for i, pull in enumerate(ctx.pulls):
        rpm = _col(ctx, pull, "rpm")
        if rpm is None:
            continue
        if callable(y_channel_or_fn):
            y = y_channel_or_fn(ctx, pull)
        else:
            y = _col(ctx, pull, y_channel_or_fn)
        if y is None:
            continue
        m = _loaded_mask(ctx, pull) if mask == "loaded" else _settled_mask(ctx, pull)
        sel = m & np.isfinite(rpm) & np.isfinite(y)
        if not np.any(sel):
            continue
        ax.scatter(rpm[sel], y[sel], s=size, alpha=0.65, color=_CYCLE[i % len(_CYCLE)],
                   label=f"{label_prefix}Pull {pull.index}")
        drew = True
    if drew:
        ax.legend(loc="best", fontsize=8)
    return drew


def _min_knock_fn(ctx, pull):
    stacked = [_col(ctx, pull, c) for c in _KNOCK_CHANNELS]
    stacked = [a for a in stacked if a is not None]
    if not stacked:
        return None
    arr = np.vstack(stacked)
    with np.errstate(all="ignore"):
        return np.nanmin(np.where(np.isfinite(arr), arr, np.nan), axis=0)


def _put_error_fn(ctx, pull):
    put = _col(ctx, pull, "put"); sp = _col(ctx, pull, "put_sp")
    return None if put is None or sp is None else put - sp


def _lambda_error_fn(ctx, pull):
    lam = _col(ctx, pull, "lambda"); sp = _col(ctx, pull, "lambda_sp")
    return None if lam is None or sp is None else lam - sp


def _di_error_fn(ctx, pull):
    di = _col(ctx, pull, "fp_di"); sp = _col(ctx, pull, "fp_di_sp")
    return None if di is None or sp is None else di - sp


def _plot_boost(ctx, path) -> bool:
    fig = Figure(figsize=(10, 8))
    ax0 = fig.add_subplot(2, 1, 1)
    drew = _scatter_pulls(ax0, ctx, "put")
    _scatter_pulls(ax0, ctx, "put_sp", size=6, label_prefix="SP ")
    _style(ax0, "PUT actual vs setpoint (loaded WOT)", "Engine speed (rpm)", "PUT / PUT SP (kPa)")
    ax1 = fig.add_subplot(2, 1, 2)
    d2 = _scatter_pulls(ax1, ctx, _put_error_fn)
    ax1.axhline(0.0, color="0.3", lw=0.9)
    ax1.axhline(BOOST_WATCH_KPA, color="tab:orange", ls="--", lw=0.9, label=f"+{BOOST_WATCH_KPA:.0f} watch")
    ax1.axhline(BOOST_HIGH_KPA, color="tab:red", ls="--", lw=1.0, label=f"+{BOOST_HIGH_KPA:.0f} high")
    ax1.legend(loc="best", fontsize=8)
    _style(ax1, "PUT overshoot", "Engine speed (rpm)", "PUT - PUT SP (kPa)")
    if not (drew or d2):
        return False
    fig.tight_layout(); fig.savefig(path, format="png", dpi=_DPI)
    return True


def _plot_knock(ctx, path) -> bool:
    fig = Figure(figsize=(10, 5.5))
    ax = fig.add_subplot()
    drew = _scatter_pulls(ax, ctx, _min_knock_fn)
    ax.axhline(0.0, color="0.3", lw=0.9)
    ax.axhline(KNOCK_WATCH_DEG, color="tab:orange", ls="--", lw=0.9, label=f"{KNOCK_WATCH_DEG} watch")
    ax.axhline(KNOCK_HIGH_DEG, color="tab:red", ls="--", lw=1.0, label=f"{KNOCK_HIGH_DEG} high")
    ax.legend(loc="best", fontsize=8)
    _style(ax, "Most-retarded cylinder (loaded WOT)", "Engine speed (rpm)", "Knock retard (deg)")
    if not drew:
        return False
    fig.tight_layout(); fig.savefig(path, format="png", dpi=_DPI)
    return True


def _plot_lambda(ctx, path) -> bool:
    fig = Figure(figsize=(10, 5.5))
    ax = fig.add_subplot()
    drew = _scatter_pulls(ax, ctx, _lambda_error_fn, mask="settled")
    ax.axhline(0.0, color="0.3", lw=0.9)
    ax.axhline(LAMBDA_WATCH, color="tab:orange", ls="--", lw=0.9, label=f"+{LAMBDA_WATCH} lean watch")
    ax.legend(loc="best", fontsize=8)
    _style(ax, "Settled-WOT lambda error", "Engine speed (rpm)", "Lambda - Lambda SP")
    if not drew:
        return False
    fig.tight_layout(); fig.savefig(path, format="png", dpi=_DPI)
    return True


def _plot_rail(ctx, path) -> bool:
    fig = Figure(figsize=(10, 8))
    ax0 = fig.add_subplot(2, 1, 1)
    d0 = _scatter_pulls(ax0, ctx, _di_error_fn)
    ax0.axhline(0.0, color="0.3", lw=0.9)
    _style(ax0, "DI rail pressure error (loaded WOT)", "Engine speed (rpm)", "FP DI - FP DI SP (bar)")
    ax1 = fig.add_subplot(2, 1, 2)
    d1 = _scatter_pulls(ax1, ctx, "lpfp_duty")
    _scatter_pulls(ax1, ctx, "hpfp_eff_vol", size=6, label_prefix="HPFP ")
    ax1.axhline(LPFP_WATCH_PCT, color="tab:orange", ls="--", lw=0.9, label=f"{LPFP_WATCH_PCT:.0f}% LPFP")
    ax1.axhline(HPFP_WATCH_PCT, color="tab:red", ls="--", lw=0.9, label=f"{HPFP_WATCH_PCT:.0f}% HPFP")
    ax1.legend(loc="best", fontsize=8)
    _style(ax1, "Fuel pump headroom", "Engine speed (rpm)", "Percent")
    if not (d0 or d1):
        return False
    fig.tight_layout(); fig.savefig(path, format="png", dpi=_DPI)
    return True


def _plot_turbo(ctx, path) -> bool:
    fig = Figure(figsize=(10, 5.5))
    ax = fig.add_subplot()
    drew = _scatter_pulls(ax, ctx, "turbo_speed")
    ax.axhline(TURBO_SPEED_WATCH_K, color="tab:orange", ls="--", lw=0.9, label=f"{TURBO_SPEED_WATCH_K:.0f}k watch")
    ax.axhline(TURBO_SPEED_LIMIT_K, color="tab:red", ls="--", lw=1.0, label=f"{TURBO_SPEED_LIMIT_K:.0f}k limit")
    ax.legend(loc="best", fontsize=8)
    _style(ax, "Turbo speed (loaded WOT)", "Engine speed (rpm)", "Turbo speed (krpm logged)")
    if not drew:
        return False
    fig.tight_layout(); fig.savefig(path, format="png", dpi=_DPI)
    return True


def _plot_wastegate(ctx, path) -> bool:
    fig = Figure(figsize=(10, 5.5))
    ax = fig.add_subplot()
    d0 = _scatter_pulls(ax, ctx, "wg_pos_final")
    d1 = _scatter_pulls(ax, ctx, "wg_pos_base", size=6, label_prefix="Base ")
    _style(ax, "Wastegate final vs base position", "Engine speed (rpm)", "WG position (%)")
    if not (d0 or d1):
        return False
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
    fig = Figure(figsize=(12, 5))
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


# check id -> plotter. A plotter that finds no data returns False (no file).
_PLOTTERS: dict[str, Callable] = {
    "boost": _plot_boost,
    "knock": _plot_knock,
    "lambda": _plot_lambda,
    "rail_pressure": _plot_rail,
    "turbo_heat": _plot_turbo,
    "wastegate": _plot_wastegate,
}


def _make_plots(ctx: CheckContext, folder: Path) -> dict[str, Path]:
    plot_dir = folder / _PLOTS_SUBDIR
    plot_dir.mkdir(parents=True, exist_ok=True)
    out: dict[str, Path] = {}
    for check_id, fn in _PLOTTERS.items():
        path = plot_dir / f"analysis_{check_id}.png"
        try:
            if fn(ctx, path):
                out[check_id] = path
        except Exception:  # a plotting failure must never sink the analysis
            continue
    return out


def _cells_hit(counts) -> int:
    return int(np.count_nonzero(np.array(counts)))


def _coverage_section(ctx, cov_results, cov_skipped, folder, plot_paths, make_plots) -> dict:
    """Build the JSON ``coverage`` section, rendering a heatmap per table."""
    plot_dir = folder / _PLOTS_SUBDIR
    if make_plots and cov_results:
        plot_dir.mkdir(parents=True, exist_ok=True)
    out_results = []
    for cov in cov_results:
        entry = {
            "symbol": cov.symbol,
            "description": cov.description,
            "shape": list(cov.shape),
            "x_channel": cov.x_channel,
            "y_channel": cov.y_channel,
            "x_breakpoints": cov.x_breakpoints,
            "y_breakpoints": cov.y_breakpoints,
            "counts_whole": cov.counts_whole,
            "counts_wot": cov.counts_wot,
            "total_whole": cov.total_whole,
            "total_wot": cov.total_wot,
            "cells_hit_whole": _cells_hit(cov.counts_whole),
            "cells_hit_wot": _cells_hit(cov.counts_wot),
        }
        if make_plots:
            path = plot_dir / f"analysis_coverage_{_sanitize(cov.symbol)}.png"
            try:
                if _plot_coverage(cov, path):
                    entry["plot"] = f"{_PLOTS_SUBDIR}/{path.name}"
            except Exception:
                pass
        out_results.append(entry)
    return {
        "results": out_results,
        "skipped": [
            {"symbol": s.check_id.removeprefix("coverage:"), "reason": s.reason,
             "missing_channels": list(s.missing_channels)}
            for s in cov_skipped
        ],
    }


def _coverage_markdown(cov_results, cov_skipped) -> list[str]:
    L = ["## Table coverage", ""]
    if cov_results:
        rows = []
        for cov in cov_results:
            n_cells = int(np.prod(cov.shape))
            rows.append([
                cov.symbol,
                f"{_cells_hit(cov.counts_whole)}/{n_cells}",
                f"{_cells_hit(cov.counts_wot)}/{n_cells}",
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


def _attach_plot_refs(result: BatteryResult, plot_paths: dict[str, Path]) -> BatteryResult:
    findings = tuple(
        replace(f, plot_refs=(f"{_PLOTS_SUBDIR}/{plot_paths[f.check_id].name}",))
        if f.check_id in plot_paths and not f.plot_refs else f
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
