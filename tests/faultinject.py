"""Fault-injection fixture generator for the analysis check tests (U4).

The primary U4 test pattern (plan Test Strategy item 1): start from a clean
multi-pull synthetic log and inject *known* defects — a knock event on one
cylinder across pulls, a settled lambda lean, a boost-overshoot pocket, a time
gap, a frozen channel — so ground truth is exact by construction. Each check
must fire at the right severity, pull, and location **and only there**
(false-alarm coverage, not just miss coverage).

Built on :mod:`tests.synthlog`; extend with each newly discovered failure mode.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from tests.synthlog import clean_pull_columns, const, write_log


@dataclass
class PullSpec:
    """One WOT pull with optional injected faults (all default to clean)."""

    n: int = 60
    gear: float = 3.0
    put_overshoot: float = 0.0            # kPa added to actual PUT
    knock: dict[int, float] = field(default_factory=dict)   # cylinder (1-4) -> retard deg
    lambda_error: float = 0.0             # added to actual lambda over the pull
    freeze: dict[str, float] = field(default_factory=dict)  # exact CSV header -> frozen value


def _idle_cols(n: int, t0: float, gear_header: str, dt: float) -> dict[str, list[float]]:
    cols = clean_pull_columns(n=n, t0=t0, gear_header=gear_header, dt=dt)
    for k in list(cols):
        if k == "Time":
            continue
        if k == "Engine Speed (rpm)":
            cols[k] = const(850.0, n)
        elif k in ("Pedal Pos (%)", "TPS (%)"):
            cols[k] = const(0.0, n)
    return cols


def _pull_cols(spec: PullSpec, t0: float, *, gear_header: str, airmass_header: str,
               dt: float) -> dict[str, list[float]]:
    cols = clean_pull_columns(
        n=spec.n, t0=t0, gear_header=gear_header, gear_value=spec.gear,
        airmass_header=airmass_header, dt=dt,
        put_overshoot=spec.put_overshoot, lambda_error=spec.lambda_error,
    )
    for cyl, deg in spec.knock.items():
        cols[f"Knock Cyl {cyl} (°)"] = const(deg, spec.n)
    for header, value in spec.freeze.items():
        if header in cols:
            cols[header] = const(value, spec.n)
    return cols


def _concat(colsets: list[dict[str, list[float]]]) -> dict[str, list[float]]:
    keys = colsets[0].keys()
    out: dict[str, list[float]] = {k: [] for k in keys}
    for cs in colsets:
        for k in keys:
            out[k].extend(cs[k])
    return out


@dataclass
class InjectedLog:
    """A written fault-injected log folder plus the row ranges of its pulls."""

    folder: Path
    pull_rows: list[tuple[int, int]]      # (start, end) inclusive per pull, in file order


def build_folder(
    tmp_path: Path,
    specs: list[PullSpec],
    *,
    gear_header: str = "Gear (gear)",
    airmass_header: str = "Airmass (mg/stk)",
    dt: float = 0.05,
    idle_between: int = 30,
    gap_before_pull: int | None = None,
    gap_seconds: float = 2.0,
    filename: str = "simostools-fault.csv",
) -> InjectedLog:
    """Write a single-CSV folder of ``specs`` pulls separated by idle stretches.

    ``gap_before_pull`` (1-based) injects a ``gap_seconds`` time discontinuity
    just before that pull, so a data-quality check can be tested for a gap that
    overlaps (or precedes) a pull.
    """
    segments: list[dict[str, list[float]]] = []
    pull_rows: list[tuple[int, int]] = []
    row = 0
    t = 0.0
    for i, spec in enumerate(specs, start=1):
        if i > 1:
            idle = _idle_cols(idle_between, t, gear_header, dt)
            segments.append(idle)
            row += idle_between
            t += idle_between * dt
        if gap_before_pull == i:
            t += gap_seconds        # discontinuity in the Time column
        pull = _pull_cols(spec, t, gear_header=gear_header, airmass_header=airmass_header, dt=dt)
        segments.append(pull)
        pull_rows.append((row, row + spec.n - 1))
        row += spec.n
        t += spec.n * dt

    cols = _concat(segments)
    write_log(tmp_path / filename, cols)
    return InjectedLog(folder=tmp_path, pull_rows=pull_rows)
