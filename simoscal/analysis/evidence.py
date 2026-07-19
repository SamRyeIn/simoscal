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
    WG_I_CLAMP_WATCH_PCT,
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


def _legend(ax) -> None:
    """Attach a compact legend only if any artist on the axes carries a label."""
    handles, labels = ax.get_legend_handles_labels()
    if labels:
        ax.legend(loc="best", fontsize=8)


_CYCLE = ["tab:blue", "tab:orange", "tab:green", "tab:red", "tab:purple", "tab:brown"]
_REF_COLOR = "0.35"        # dashed dark-gray reference (setpoint / base / table)
_SECONDARY_COLOR = "0.55"  # dash-dot mid-gray secondary-actual (e.g. HPFP alongside LPFP)
PSI_PER_KPA = 6.894757     # 1 psi in kPa — for the gauge-boost (psi) reframe of PUT


def _contiguous_runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """Inclusive index runs where ``mask`` is True — so a line never bridges a hole."""
    runs: list[tuple[int, int]] = []
    n = mask.size
    i = 0
    while i < n:
        if not mask[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and mask[j + 1]:
            j += 1
        runs.append((i, j))
        i = j + 1
    return runs


def _pull_lines(ax, ctx, y_src, *, mask="loaded", role="primary", label=None,
                x_src="rpm", lw=1.4) -> bool:
    """Draw each pull as an x-sorted line. Returns True if anything was drawn.

    The core encoding rule (D1): **quantity = line style, pull = color.**

    - ``role="primary"`` — solid line, per-pull :data:`_CYCLE` color, one legend
      entry *per pull* (``"<label> (Pull N)"``, or ``"Pull N"`` if no label).
    - ``role="reference"`` — dashed :data:`_REF_COLOR` gray, **one** legend entry
      total (the label, deduplicated), drawn per pull so each sweep keeps its
      own curve (setpoint / base / table).
    - ``role="secondary"`` — dash-dot :data:`_SECONDARY_COLOR` gray, one legend
      entry total (a second actual sharing the panel, e.g. HPFP vs LPFP).

    ``y_src`` / ``x_src`` are either a canonical channel id or a
    ``fn(ctx, pull) -> array | None``. Samples are masked (``"loaded"`` /
    ``"settled"`` / ``"none"``), split into contiguous runs to avoid bridging
    mask holes, then sorted by x so each sweep reads as a single-valued curve.
    """
    drew = False
    ref_label_used = False
    for i, pull in enumerate(ctx.pulls):
        x = x_src(ctx, pull) if callable(x_src) else _col(ctx, pull, x_src)
        y = y_src(ctx, pull) if callable(y_src) else _col(ctx, pull, y_src)
        if x is None or y is None:
            continue
        if mask == "loaded":
            m = _loaded_mask(ctx, pull)
        elif mask == "settled":
            m = _settled_mask(ctx, pull)
        else:
            m = np.ones(pull.n_samples, dtype=bool)
        sel = m & np.isfinite(x) & np.isfinite(y)
        if not np.any(sel):
            continue
        if role == "primary":
            color, ls = _CYCLE[i % len(_CYCLE)], "-"
            entry = f"{label} (Pull {pull.index})" if label else f"Pull {pull.index}"
        elif role == "reference":
            color, ls = _REF_COLOR, "--"
            entry = None if ref_label_used else label
        else:  # secondary
            color, ls = _SECONDARY_COLOR, "-."
            entry = None if ref_label_used else label
        first_seg = True
        for lo, hi in _contiguous_runs(sel):
            xs, ys = x[lo : hi + 1], y[lo : hi + 1]
            order = np.argsort(xs, kind="stable")
            ax.plot(xs[order], ys[order], ls=ls, color=color, lw=lw, alpha=0.9,
                    label=entry if first_seg else None)
            drew = True
            first_seg = False
        if entry and role != "primary":
            ref_label_used = True
    return drew


def _min_knock_fn(ctx, pull):
    return _min_knock_arrays([_col(ctx, pull, c) for c in _KNOCK_CHANNELS])


def _min_knock_arrays(stacked) -> Optional[np.ndarray]:
    """Per-sample most-retarded value across the present knock-cylinder arrays."""
    stacked = [a for a in stacked if a is not None]
    if not stacked:
        return None
    arr = np.vstack(stacked)
    with np.errstate(all="ignore"):
        return np.nanmin(np.where(np.isfinite(arr), arr, np.nan), axis=0)


def _put_error_fn(ctx, pull):
    put = _col(ctx, pull, "put"); sp = _col(ctx, pull, "put_sp")
    return None if put is None or sp is None else put - sp


def _boost_fn(ctx, pull):
    """Gauge boost (psi): PUT above ambient. The wastegate loop's controlled var."""
    put = _col(ctx, pull, "put"); amb = _col(ctx, pull, "ambient_press")
    return None if put is None or amb is None else (put - amb) / PSI_PER_KPA


def _boost_sp_fn(ctx, pull):
    """Gauge boost setpoint (psi): PUT setpoint above ambient (same basis as `_boost_fn`)."""
    sp = _col(ctx, pull, "put_sp"); amb = _col(ctx, pull, "ambient_press")
    return None if sp is None or amb is None else (sp - amb) / PSI_PER_KPA


def _lambda_error_fn(ctx, pull):
    lam = _col(ctx, pull, "lambda"); sp = _col(ctx, pull, "lambda_sp")
    return None if lam is None or sp is None else lam - sp


def _di_error_fn(ctx, pull):
    di = _col(ctx, pull, "fp_di"); sp = _col(ctx, pull, "fp_di_sp")
    return None if di is None or sp is None else di - sp


def _transient_scatter(ctx, ax, y_fn, *, label="loaded transient") -> bool:
    """Faint low-alpha scatter of loaded-but-not-settled samples, for context.

    Transients (post-shift recovery, torque cuts) are genuinely non-curve-like,
    so scatter is the honest mark there — drawn behind the settled lines (D4/D9).
    """
    drew = False
    for pull in ctx.pulls:
        rpm = _col(ctx, pull, "rpm")
        y = y_fn(ctx, pull)
        if rpm is None or y is None:
            continue
        m = _loaded_mask(ctx, pull) & ~_settled_mask(ctx, pull) & np.isfinite(rpm) & np.isfinite(y)
        if not np.any(m):
            continue
        ax.scatter(rpm[m], y[m], s=8, alpha=0.2, color="0.5",
                   label=label if not drew else None)
        drew = True
    return drew


def _plot_boost(ctx, path) -> bool:
    # Top panel reframes boost control as gauge boost (psi) = (PUT - ambient),
    # easier to read than absolute PUT; it's PUT-based, so it's the same tracking
    # story as the PUT panel below, just zeroed at ambient and scaled to psi. Only
    # shown when ambient pressure was logged (else we'd have to guess a baseline).
    show_boost = ctx.logset.has("ambient_press") and ctx.logset.has("put")
    n = 3 if show_boost else 2
    fig = Figure(figsize=(10, 4 * n))
    row = 1
    drew = []
    if show_boost:
        axb = fig.add_subplot(n, 1, row); row += 1
        db = _pull_lines(axb, ctx, _boost_fn, role="primary", label="Boost")
        _pull_lines(axb, ctx, _boost_sp_fn, role="reference", label="Boost SP")
        _style(axb, "Gauge boost actual vs setpoint (loaded WOT)", "Engine speed (rpm)",
               "Boost / Boost SP (psi)")
        _legend(axb)
        drew.append(db)
    ax0 = fig.add_subplot(n, 1, row); row += 1
    drew.append(_pull_lines(ax0, ctx, "put", role="primary", label="PUT"))
    _pull_lines(ax0, ctx, "put_sp", role="reference", label="PUT SP")
    _style(ax0, "PUT actual vs setpoint (loaded WOT)", "Engine speed (rpm)", "PUT / PUT SP (kPa)")
    _legend(ax0)
    ax1 = fig.add_subplot(n, 1, row)
    drew.append(_pull_lines(ax1, ctx, _put_error_fn, role="primary"))
    ax1.axhline(0.0, color="0.3", lw=0.9)
    ax1.axhline(BOOST_WATCH_KPA, color="tab:orange", ls="--", lw=0.9, label=f"+{BOOST_WATCH_KPA:.0f} watch")
    ax1.axhline(BOOST_HIGH_KPA, color="tab:red", ls="--", lw=1.0, label=f"+{BOOST_HIGH_KPA:.0f} high")
    _style(ax1, "PUT overshoot", "Engine speed (rpm)", "PUT - PUT SP (kPa)")
    _legend(ax1)
    if not any(drew):
        return False
    fig.tight_layout(); fig.savefig(path, format="png", dpi=_DPI)
    return True


def _plot_knock(ctx, path) -> bool:
    fig = Figure(figsize=(10, 5.5))
    ax = fig.add_subplot()
    drew = _pull_lines(ax, ctx, _min_knock_fn, role="primary")
    ax.axhline(0.0, color="0.3", lw=0.9)
    ax.axhline(KNOCK_WATCH_DEG, color="tab:orange", ls="--", lw=0.9, label=f"{KNOCK_WATCH_DEG} watch")
    ax.axhline(KNOCK_HIGH_DEG, color="tab:red", ls="--", lw=1.0, label=f"{KNOCK_HIGH_DEG} high")
    _style(ax, "Most-retarded cylinder (loaded WOT)", "Engine speed (rpm)", "Knock retard (deg)")
    _legend(ax)
    if not drew:
        return False
    fig.tight_layout(); fig.savefig(path, format="png", dpi=_DPI)
    return True


def _plot_lambda(ctx, path) -> bool:
    fig = Figure(figsize=(10, 5.5))
    ax = fig.add_subplot()
    _transient_scatter(ctx, ax, _lambda_error_fn)
    drew = _pull_lines(ax, ctx, _lambda_error_fn, mask="settled", role="primary")
    ax.axhline(0.0, color="0.3", lw=0.9)
    ax.axhline(LAMBDA_WATCH, color="tab:orange", ls="--", lw=0.9, label=f"+{LAMBDA_WATCH} lean watch")
    _style(ax, "Settled-WOT lambda error", "Engine speed (rpm)", "Lambda - Lambda SP")
    _legend(ax)
    if not drew:
        return False
    fig.tight_layout(); fig.savefig(path, format="png", dpi=_DPI)
    return True


def _plot_rail(ctx, path) -> bool:
    fig = Figure(figsize=(10, 8))
    ax0 = fig.add_subplot(2, 1, 1)
    d0 = _pull_lines(ax0, ctx, _di_error_fn, role="primary")
    ax0.axhline(0.0, color="0.3", lw=0.9)
    _style(ax0, "DI rail pressure error (loaded WOT)", "Engine speed (rpm)", "FP DI - FP DI SP (bar)")
    _legend(ax0)
    ax1 = fig.add_subplot(2, 1, 2)
    d1 = _pull_lines(ax1, ctx, "lpfp_duty", role="primary", label="LPFP")
    _pull_lines(ax1, ctx, "hpfp_eff_vol", role="secondary", label="HPFP")
    ax1.axhline(LPFP_WATCH_PCT, color="tab:orange", ls="--", lw=0.9, label=f"{LPFP_WATCH_PCT:.0f}% LPFP")
    ax1.axhline(HPFP_WATCH_PCT, color="tab:red", ls="--", lw=0.9, label=f"{HPFP_WATCH_PCT:.0f}% HPFP")
    _style(ax1, "Fuel pump headroom", "Engine speed (rpm)", "Percent")
    _legend(ax1)
    if not (d0 or d1):
        return False
    fig.tight_layout(); fig.savefig(path, format="png", dpi=_DPI)
    return True


def _plot_turbo(ctx, path) -> bool:
    fig = Figure(figsize=(10, 5.5))
    ax = fig.add_subplot()
    drew = _pull_lines(ax, ctx, "turbo_speed", role="primary")
    ax.axhline(TURBO_SPEED_WATCH_K, color="tab:orange", ls="--", lw=0.9, label=f"{TURBO_SPEED_WATCH_K:.0f}k watch")
    ax.axhline(TURBO_SPEED_LIMIT_K, color="tab:red", ls="--", lw=1.0, label=f"{TURBO_SPEED_LIMIT_K:.0f}k limit")
    _style(ax, "Turbo speed (loaded WOT)", "Engine speed (rpm)", "Turbo speed (krpm logged)")
    _legend(ax)
    if not drew:
        return False
    fig.tight_layout(); fig.savefig(path, format="png", dpi=_DPI)
    return True


def _plot_wastegate(ctx, path) -> bool:
    fig = Figure(figsize=(10, 8))
    ax0 = fig.add_subplot(2, 1, 1)
    d0 = _pull_lines(ax0, ctx, "wg_pos_final", role="primary", label="Final")
    d1 = _pull_lines(ax0, ctx, "wg_pos_base", role="reference", label="Base")
    _style(ax0, "Wastegate final vs base position", "Engine speed (rpm)", "WG position (%)")
    _legend(ax0)
    # Integral term: the closed-loop-headroom signal the finding turns on. Driven
    # to its opening clamp (below the watch line) while boost overshoots == out of
    # authority (audit 3.1).
    ax1 = fig.add_subplot(2, 1, 2)
    d2 = _pull_lines(ax1, ctx, "wg_i_value", role="primary", label="I term")
    _pull_lines(ax1, ctx, "wg_pd_value", role="secondary", label="P-D term")
    ax1.axhline(0.0, color="0.3", lw=0.9)
    ax1.axhline(WG_I_CLAMP_WATCH_PCT, color="tab:red", ls="--", lw=1.0,
                label=f"{WG_I_CLAMP_WATCH_PCT:.0f}% clamp watch")
    _style(ax1, "Wastegate closed-loop correction terms", "Engine speed (rpm)", "Correction (%)")
    _legend(ax1)
    if not (d0 or d1 or d2):
        return False
    fig.tight_layout(); fig.savefig(path, format="png", dpi=_DPI)
    return True


def _plot_ignition(ctx, path) -> bool:
    """U3 — the timing the engine actually ran (`ign_avg`) vs the table (`ign_table`)."""
    fig = Figure(figsize=(10, 5.5))
    ax = fig.add_subplot()
    d0 = _pull_lines(ax, ctx, "ign_avg", role="primary", label="Ign Avg")
    d1 = _pull_lines(ax, ctx, "ign_table", role="reference", label="Ign Table")
    _style(ax, "Delivered vs table timing (loaded WOT)", "Engine speed (rpm)",
           "Ignition advance (deg)")
    _legend(ax)
    if not (d0 or d1):
        return False
    fig.tight_layout(); fig.savefig(path, format="png", dpi=_DPI)
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
    fig = Figure(figsize=(11, 2.1 * len(panels)))
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
    fig = Figure(figsize=(11, 2.1 * len(panels)))
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


# Fixed-name plotters, keyed by their output id -> `analysis_<id>.png`. The six
# per-check plots share a key with their check id (so `_attach_plot_refs` wires
# them onto findings); `ignition` is standalone (no check, so no plot_ref). A
# plotter that finds no data returns False (no file). (D3, D9)
_PLOTTERS: dict[str, Callable] = {
    "boost": _plot_boost,
    "knock": _plot_knock,
    "lambda": _plot_lambda,
    "rail_pressure": _plot_rail,
    "turbo_heat": _plot_turbo,
    "wastegate": _plot_wastegate,
    "ignition": _plot_ignition,
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
