"""Synthetic SimosTools-log builders for the analysis test suite.

The real fixtures (``Logs/BasicsGuide_R01/`` etc.) live in the *root* repo and
may be absent from a lean ``Code/`` checkout, so unit tests build small,
deterministic CSVs here instead. Callers pass **exact CSV header strings**
(e.g. ``"Engine Speed (rpm)"``) mapped to per-row value lists, so a test can
exercise any unit spelling or gear form it needs.
"""

from __future__ import annotations

import csv
from pathlib import Path

import numpy as np

# The trailing metadata column SimosTools appends to every real log header.
SIMOSTOOLS_TAG = "SimosTools [R2.11:test:synthetic.bin]"


def ramp(start: float, stop: float, n: int) -> list[float]:
    """Linear ramp of ``n`` values from ``start`` to ``stop`` inclusive."""
    return list(np.linspace(start, stop, n))


def const(value: float, n: int) -> list[float]:
    return [float(value)] * n


def write_log(path: Path, columns: dict[str, list[float]], *, tag: bool = True) -> Path:
    """Write ``columns`` (header string -> per-row values) as a CSV at ``path``.

    All value lists must be the same length. A trailing ``SimosTools [...]``
    tag column (empty in the data rows, matching real logs) is appended unless
    ``tag=False``.
    """
    headers = list(columns)
    lengths = {len(v) for v in columns.values()}
    if len(lengths) != 1:
        raise ValueError(f"ragged columns: lengths {lengths}")
    n = lengths.pop()

    header_row = list(headers)
    if tag:
        header_row.append(SIMOSTOOLS_TAG)

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header_row)
        for i in range(n):
            row = [_fmt(columns[h][i]) for h in headers]
            if tag:
                row.append("")
            writer.writerow(row)
    return path


def _fmt(v: float) -> str:
    return repr(float(v))


def clean_pull_columns(
    n: int = 60,
    *,
    gear_header: str = "Gear (gear)",
    gear_value: float = 3.0,
    airmass_header: str = "Airmass (mg/stk)",
    dt: float = 0.05,
    t0: float = 0.0,
    put_overshoot: float = 0.0,
    knock_cyl3: float = 0.0,
    lambda_error: float = 0.0,
) -> dict[str, list[float]]:
    """A clean 3rd-gear WOT pull: rpm sweep 3000->6500 with tracking channels.

    Optional injections let a check test add a defect on top of the clean base:
    ``put_overshoot`` kPa added to actual PUT, ``knock_cyl3`` deg retard on
    cylinder 3, and ``lambda_error`` added to actual lambda.
    """
    time = [t0 + i * dt for i in range(n)]
    rpm = ramp(3000.0, 6500.0, n)
    put_sp = ramp(230.0, 250.0, n)
    put = [sp + put_overshoot for sp in put_sp]
    lam_sp = const(0.85, n)
    lam = [sp + lambda_error for sp in lam_sp]
    # Airmass values follow the header's declared unit (mg/stk vs g/stk).
    airmass = ramp(0.95, 1.49, n) if airmass_header.endswith("(g/stk)") else ramp(950.0, 1490.0, n)
    cols: dict[str, list[float]] = {
        "Time": time,
        "Engine Speed (rpm)": rpm,
        gear_header: const(gear_value, n),
        airmass_header: airmass,
        "Pedal Pos (%)": const(100.0, n),
        "TPS (%)": const(85.0, n),
        "PUT (kpa)": put,
        "PUT SP (kpa)": put_sp,
        "Lambda (l)": lam,
        "Lambda SP (l)": lam_sp,
        "Knock Cyl 1 (°)": const(0.0, n),
        "Knock Cyl 2 (°)": const(0.0, n),
        "Knock Cyl 3 (°)": const(knock_cyl3, n),
        "Knock Cyl 4 (°)": const(0.0, n),
        "Torque (Nm)": ramp(300.0, 415.0, n),
        "Torque Req (Nm)": ramp(305.0, 420.0, n),
        "IAT (°C)": const(25.0, n),
        "Coolant Temp (°C)": const(95.0, n),
        "Ambient Temp (°C)": const(15.0, n),
        "Ambient Press (kpa)": const(101.0, n),
        "Eth Content (%)": const(0.0, n),
    }
    return cols


def idle_columns(n: int = 40, *, gear_header: str = "Gear (gear)", dt: float = 0.05,
                 t0: float = 0.0) -> dict[str, list[float]]:
    """A no-WOT idle stretch (low pedal, flat rpm) for pull-boundary tests."""
    time = [t0 + i * dt for i in range(n)]
    return {
        "Time": time,
        "Engine Speed (rpm)": const(850.0, n),
        gear_header: const(0.0, n),
        "Airmass (mg/stk)": const(120.0, n),
        "Pedal Pos (%)": const(0.0, n),
        "TPS (%)": const(0.0, n),
        "PUT (kpa)": const(100.0, n),
        "PUT SP (kpa)": const(100.0, n),
    }
