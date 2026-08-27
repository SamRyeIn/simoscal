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
    put_shortfall: float = 0.0,
    put_shortfall_from: float = 0.3,
    wg_i_windup: float = 0.0,
    knock_cyl3: float = 0.0,
    lambda_error: float = 0.0,
    wheel_speeds: bool = False,
    ign_table: bool = False,
    wastegate: bool = False,
    torque_lim: bool = False,
) -> dict[str, list[float]]:
    """A clean 3rd-gear WOT pull: rpm sweep 3000->6500 with tracking channels.

    Optional injections let a check test add a defect on top of the clean base:
    ``put_overshoot`` kPa added to actual PUT, ``knock_cyl3`` deg retard on
    cylinder 3, and ``lambda_error`` added to actual lambda.

    ``put_shortfall`` kPa is subtracted from actual PUT over the *tail* of the
    pull, from ``put_shortfall_from`` (a fraction of it) onward — so PUT reaches
    setpoint first and falls short afterwards, which is the only shape the
    shortfall check counts. A whole-pull offset (a negative ``put_overshoot``)
    is the other case it must handle: boost that never made target at all.

    ``wheel_speeds`` adds the four per-wheel channels modeling a mild front-slip
    event in the middle third of the pull (front-driven, so FL/FR overrun RL/RR),
    giving the TC-activity plot a visible slip bump to draw. ``ign_table`` adds
    an ``Ign Table (°)`` reference channel a few degrees above delivered timing.
    ``wastegate`` adds ``WG Pos Final``/``WG Pos Base`` (final a touch above base)
    plus ``WG I Value``/``WG P-D Value`` (closed-loop trim terms; the integral sits
    near zero by default, is driven to a clamp via ``freeze`` in tests, and ramps
    across the pull under ``wg_i_windup``).
    """
    time = [t0 + i * dt for i in range(n)]
    rpm = ramp(3000.0, 6500.0, n)
    put_sp = ramp(230.0, 250.0, n)
    put = [sp + put_overshoot for sp in put_sp]
    if put_shortfall:
        first = int(round(put_shortfall_from * n))
        put = [v - put_shortfall if i >= first else v for i, v in enumerate(put)]
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
        "FP DI (bar)": const(200.0, n),
        "FP DI SP (bar)": const(200.0, n),
        "LPFP Duty (%)": const(70.0, n),
        "HPFP Eff Vol (%)": const(90.0, n),
        "Ign Avg (°)": const(8.0, n),
        "Turbo Speed (rpm)": ramp(120.0, 180.0, n),
        "IAT (°C)": const(25.0, n),
        "Coolant Temp (°C)": const(95.0, n),
        "Ambient Temp (°C)": const(15.0, n),
        "Ambient Press (kpa)": const(101.0, n),
        "Eth Content (%)": const(0.0, n),
    }
    if ign_table:
        # Table timing sits a few degrees above the delivered average.
        cols["Ign Table (°)"] = const(11.0, n)
    if wastegate:
        cols["WG Pos Base (%)"] = ramp(40.0, 70.0, n)
        cols["WG Pos Final (%)"] = ramp(45.0, 78.0, n)
        # A healthy integral sits near zero (small trim); a clamped one is driven
        # strongly negative (tests inject the clamp via ``freeze``), and a wound-up
        # one ramps away from its resting trim across the pull — the closed loop
        # steadily taking over from the position feedforward.
        cols["WG I Value (%)"] = (ramp(-2.0, -2.0 + wg_i_windup, n) if wg_i_windup
                                  else const(-2.0, n))
        cols["WG P-D Value (%)"] = const(0.0, n)
    if torque_lim:
        # No limiter active by default (code 0); tests inject a code via ``freeze``.
        cols["Torque Lim ()"] = const(0.0, n)
    if wheel_speeds:
        speed = ramp(60.0, 120.0, n)                 # rough vehicle speed over the pull
        slip = [0.0] * n
        lo, hi = n // 3, (2 * n) // 3
        for i in range(lo, hi):
            slip[i] = 4.0                            # km/h of front over-speed (mild slip)
        front = [s + sl for s, sl in zip(speed, slip)]
        cols["Wheel Speed FL (km/h)"] = list(front)
        cols["Wheel Speed FR (km/h)"] = list(front)
        cols["Wheel Speed RL (km/h)"] = list(speed)
        cols["Wheel Speed RR (km/h)"] = list(speed)
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
