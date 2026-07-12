"""U7 — per-table cell-coverage maps.

For a curated list of primary tuning tables, simulate the ECU axis lookup —
map each logged sample onto the table's axis breakpoints (read from the flashed
bin via ``CalFile``) and accumulate per-cell hit counts, in a whole-log and a
WOT-pull-only variant. The output answers "which cells did this log exercise?":
evidence for findings, the basis of the next-log request, and the future
hit-count gate for a Layer-2 proposer.

The design artifact is the **axis-to-channel mapping** (:data:`DEFAULT_COVERAGE_SPECS`):
each covered table declares, as data, which log channel drives each axis and the
unit conversion. Mappings are only included where the axis variable is confidently
known from the XDF axis labels or the tuning guide — never guessed. A table whose
axis channel is absent from the log, or with no bin resolved, goes to SKIPPED
with the reason named (same policy as the ``needs_cal`` checks).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np

from .registry import CheckContext, Skipped

__all__ = [
    "CoverageAxis",
    "CoverageSpec",
    "CoverageResult",
    "DEFAULT_COVERAGE_SPECS",
    "compute_coverage",
]


@dataclass(frozen=True)
class CoverageAxis:
    """One table axis: the log channel that drives it and its unit conversion.

    ``scale`` multiplies the (already unit-normalized) log channel to reach the
    table axis's physical unit — e.g. MAP setpoint logged in kPa onto an hPa
    axis uses ``scale=10.0``; an airmass mg/stk channel onto an mg/stk axis uses
    ``scale=1.0``.
    """

    channel: str
    scale: float = 1.0


@dataclass(frozen=True)
class CoverageSpec:
    """A covered table and its axis-to-channel mapping."""

    symbol: str
    x: CoverageAxis
    y: Optional[CoverageAxis]      # None for a 1D table
    description: str = ""


@dataclass(frozen=True)
class CoverageResult:
    """Per-cell hit counts for one table (whole-log and WOT-pull-only)."""

    symbol: str
    description: str
    shape: tuple[int, ...]
    x_channel: str
    y_channel: Optional[str]
    x_breakpoints: list[float]
    y_breakpoints: list[float]
    counts_whole: list          # nested lists shaped like the table
    counts_wot: list
    total_whole: int
    total_wot: int


# The v1 covered-table set. Axis variables confirmed against the XDF axis labels
# (ldp_n = engine speed; ldp_map_sp = MAP setpoint hPa) and the tuning guide
# (wastegate X = exhaust flow factor, Y = intake flow factor —
# knowledge/ecu-tuning-basics.md). Airmass axes are mg/stk, matching the U1
# canonical unit. Extend here as more axis variables are confirmed.
DEFAULT_COVERAGE_SPECS: tuple[CoverageSpec, ...] = (
    CoverageSpec(
        "IP_IGA_BAS_IVVT_VVL_PORT_L[STND][0][0]",
        CoverageAxis("rpm"), CoverageAxis("airmass"),
        "Basic Ignition Angle, VVL 0 Port Flap Low (rpm x airmass)",
    ),
    CoverageSpec(
        "IP_LAMB_BAS_HPDI[1]",
        CoverageAxis("rpm"), CoverageAxis("airmass"),
        "Basic lambda setpoint grid, HPDI (rpm x airmass)",
    ),
    CoverageSpec(
        "IP_PUT_SP",
        CoverageAxis("rpm"), CoverageAxis("map_sp", scale=10.0),
        "PUT setpoint curve (rpm x MAP setpoint hPa)",
    ),
    CoverageSpec(
        "IP_FAC_BPA_SP[0]",
        CoverageAxis("exh_flow_factor"), CoverageAxis("intake_flow_fact"),
        "Map for boost pressure actuator setpoint [0] (exhaust x intake flow factor)",
    ),
    CoverageSpec(
        "IP_FAC_BPA_SP[1]",
        CoverageAxis("exh_flow_factor"), CoverageAxis("intake_flow_fact"),
        "Map for boost pressure actuator setpoint [1] (exhaust x intake flow factor)",
    ),
)


def _nearest_index(values: np.ndarray, breakpoints: np.ndarray) -> np.ndarray:
    """Nearest-breakpoint index for each value (clamps at the axis edges).

    Attributes a sample to the cell whose breakpoint it is closest to; a value
    beyond the axis range lands in the edge cell, matching the ECU's clamp
    behavior rather than being dropped.
    """
    return np.argmin(np.abs(values[:, None] - breakpoints[None, :]), axis=1)


def _wot_mask_for_file(ctx: CheckContext, file_name: str, n: int) -> np.ndarray:
    mask = np.zeros(n, dtype=bool)
    for pull in ctx.pulls:
        if pull.file == file_name:
            mask[pull.start_row : pull.end_row + 1] = True
    return mask


def _axis_breakpoints(cal, symbol: str) -> tuple[Optional[np.ndarray], Optional[np.ndarray], Optional[tuple[int, ...]]]:
    view = cal.get(symbol)
    xa = view.axis_values("x")
    ya = view.axis_values("y")
    x = None if xa is None else np.asarray(xa, dtype=float).ravel()
    y = None if ya is None else np.asarray(ya, dtype=float).ravel()
    return x, y, tuple(view.shape)


def compute_coverage(
    ctx: CheckContext,
    specs: tuple[CoverageSpec, ...] = DEFAULT_COVERAGE_SPECS,
) -> tuple[list[CoverageResult], list[Skipped]]:
    """Compute cell-coverage maps for every spec that can be resolved.

    Returns ``(results, skipped)``. With no calibration resolved, every table is
    skipped (the ``needs_cal`` policy). A table whose axis channel is absent
    from the log is skipped with the missing channel named.
    """
    results: list[CoverageResult] = []
    skipped: list[Skipped] = []

    if ctx.cal is None:
        for spec in specs:
            skipped.append(Skipped("coverage:" + spec.symbol, spec.description,
                                   "no bin/XDF resolved — coverage needs the flashed calibration"))
        return results, skipped

    available = ctx.logset.channels()
    for spec in specs:
        needed = [spec.x.channel] + ([spec.y.channel] if spec.y else [])
        missing = [c for c in needed if c not in available]
        if missing:
            skipped.append(Skipped("coverage:" + spec.symbol, spec.description,
                                   f"missing axis channel(s): {', '.join(missing)}",
                                   tuple(missing)))
            continue
        try:
            xbp, ybp, shape = _axis_breakpoints(ctx.cal, spec.symbol)
        except Exception as exc:
            skipped.append(Skipped("coverage:" + spec.symbol, spec.description,
                                   f"table not resolvable in the bin/XDF: {exc}"))
            continue
        if xbp is None or (spec.y is not None and ybp is None):
            skipped.append(Skipped("coverage:" + spec.symbol, spec.description,
                                   "table has no embedded axis breakpoints"))
            continue

        grid_shape = (ybp.size, xbp.size) if spec.y else (xbp.size,)
        counts_whole = np.zeros(grid_shape, dtype=np.int64)
        counts_wot = np.zeros(grid_shape, dtype=np.int64)

        for lf in ctx.logset.files:
            xv = lf.channel(spec.x.channel)
            yv = lf.channel(spec.y.channel) if spec.y else None
            if xv is None or (spec.y and yv is None):
                continue
            xs = xv * spec.x.scale
            valid = np.isfinite(xs)
            if spec.y:
                ys = yv * spec.y.scale
                valid &= np.isfinite(ys)
            wot = _wot_mask_for_file(ctx, lf.name, xv.size)
            if not np.any(valid):
                continue
            xi = _nearest_index(xs[valid], xbp)
            if spec.y:
                yi = _nearest_index(ys[valid], ybp)
                np.add.at(counts_whole, (yi, xi), 1)
                wot_sel = wot[valid]
                np.add.at(counts_wot, (yi[wot_sel], xi[wot_sel]), 1)
            else:
                np.add.at(counts_whole, (xi,), 1)
                wot_sel = wot[valid]
                np.add.at(counts_wot, (xi[wot_sel],), 1)

        results.append(CoverageResult(
            symbol=spec.symbol,
            description=spec.description,
            shape=shape,
            x_channel=spec.x.channel,
            y_channel=spec.y.channel if spec.y else None,
            x_breakpoints=[float(v) for v in xbp],
            y_breakpoints=[float(v) for v in ybp] if spec.y else [],
            counts_whole=counts_whole.tolist(),
            counts_wot=counts_wot.tolist(),
            total_whole=int(counts_whole.sum()),
            total_wot=int(counts_wot.sum()),
        ))

    return results, skipped
