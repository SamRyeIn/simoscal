"""U2 — WOT pull detection and the per-pull summary.

Segments wide-open-throttle pulls out of a :class:`~simoscal.analysis.log.LogSet`
and computes a per-pull summary matching the existing hand-written "Pull
Summary" tables, extended (R3) with per-pull **environment context** — ambient
temp/pressure, IAT at pull start, coolant temp, ethanol content — so pulls are
comparable across logs and revisions.

Detection is threshold-based on pedal (or TPS fallback) plus an rpm-sweep test,
with short dropouts bridged and a minimum-duration filter. Gear is *attributed*
from the resolved gear channel; when gear is unresolved the pull is still
detected but its gear field is marked unresolved (never guessed). Every
detection constant lives in :data:`PULL_DETECTION_CONSTANTS` so it prints
alongside the check battery.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np

from .log import LogFile, LogSet

__all__ = [
    "PULL_DETECTION_CONSTANTS",
    "PullEnvironment",
    "Pull",
    "detect_pulls",
]

# Detection constants — named module data, printable with the battery.
PEDAL_WOT_MIN = 95.0        # % pedal (or TPS fallback) for a sample to count as WOT
RPM_WOT_MIN = 2500.0        # rpm floor for a WOT sample (below this is launch/idle)
BRIDGE_SAMPLES = 3          # non-WOT samples bridged inside a pull (e.g. a DSG shift)
MIN_PULL_DURATION_S = 0.8   # a shorter segment is rejected (blip, not a pull)
MIN_PULL_SAMPLES = 15       # duration fallback when no time channel is present
MIN_RPM_SPAN = 800.0        # rpm the pull must sweep to qualify (rejects steady WOT)

PULL_DETECTION_CONSTANTS: dict[str, float] = {
    "pedal_wot_min_pct": PEDAL_WOT_MIN,
    "rpm_wot_min": RPM_WOT_MIN,
    "bridge_samples": float(BRIDGE_SAMPLES),
    "min_pull_duration_s": MIN_PULL_DURATION_S,
    "min_pull_samples": float(MIN_PULL_SAMPLES),
    "min_rpm_span": MIN_RPM_SPAN,
}

# The four per-cylinder knock channels, in order.
_KNOCK_CHANNELS = ("knock_1", "knock_2", "knock_3", "knock_4")


@dataclass(frozen=True)
class PullEnvironment:
    """Ambient / thermal context for a pull. ``None`` == channel not logged."""

    ambient_temp_c: Optional[float]
    ambient_press_kpa: Optional[float]
    iat_start_c: Optional[float]
    coolant_temp_c: Optional[float]
    eth_content_pct: Optional[float]


@dataclass(frozen=True)
class Pull:
    """One detected WOT pull with its summary. ``None`` metric == not logged."""

    index: int              # 1-based global pull number across the folder
    file: str               # LogFile.name the pull was found in
    start_row: int          # inclusive row index in that file
    end_row: int            # inclusive
    n_samples: int
    duration_s: Optional[float]
    gear: Optional[int]     # attributed actual gear; None if gear unresolved
    gear_resolved: bool
    rpm_min: float
    rpm_max: float
    airmass_min: Optional[float]
    airmass_max: Optional[float]
    min_knock: Optional[float]
    max_put: Optional[float]
    max_put_error: Optional[float]
    max_boost: Optional[float]
    lambda_error_min: Optional[float]
    lambda_error_max: Optional[float]
    lpfp_max: Optional[float]
    hpfp_eff_max: Optional[float]
    turbo_speed_max: Optional[float]
    environment: PullEnvironment


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _wot_signal(lf: LogFile) -> Optional[np.ndarray]:
    """The throttle signal to threshold on: pedal, else TPS, else ``None``."""
    for cid in ("pedal", "tps"):
        arr = lf.channel(cid)
        if arr is not None:
            return arr
    return None


def _nanmax(arr: Optional[np.ndarray]) -> Optional[float]:
    if arr is None:
        return None
    finite = arr[np.isfinite(arr)]
    return float(np.max(finite)) if finite.size else None


def _nanmin(arr: Optional[np.ndarray]) -> Optional[float]:
    if arr is None:
        return None
    finite = arr[np.isfinite(arr)]
    return float(np.min(finite)) if finite.size else None


def _slice(lf: LogFile, cid: str, lo: int, hi: int) -> Optional[np.ndarray]:
    arr = lf.channel(cid)
    return arr[lo : hi + 1] if arr is not None else None


def _first_finite(arr: Optional[np.ndarray]) -> Optional[float]:
    if arr is None:
        return None
    for v in arr:
        if np.isfinite(v):
            return float(v)
    return None


def _min_knock(lf: LogFile, lo: int, hi: int) -> Optional[float]:
    """Most-retarded value across all present knock cylinders over the span."""
    vals: list[float] = []
    for cid in _KNOCK_CHANNELS:
        m = _nanmin(_slice(lf, cid, lo, hi))
        if m is not None:
            vals.append(m)
    return min(vals) if vals else None


def _put_error(lf: LogFile, lo: int, hi: int) -> Optional[float]:
    put = _slice(lf, "put", lo, hi)
    sp = _slice(lf, "put_sp", lo, hi)
    if put is None or sp is None:
        return None
    err = put - sp
    finite = err[np.isfinite(err)]
    return float(np.max(finite)) if finite.size else None


def _lambda_error_range(lf: LogFile, lo: int, hi: int) -> tuple[Optional[float], Optional[float]]:
    lam = _slice(lf, "lambda", lo, hi)
    sp = _slice(lf, "lambda_sp", lo, hi)
    if lam is None or sp is None:
        return None, None
    err = lam - sp
    finite = err[np.isfinite(err)]
    if not finite.size:
        return None, None
    return float(np.min(finite)), float(np.max(finite))


def _attribute_gear(lf: LogFile, lo: int, hi: int) -> Optional[int]:
    if not lf.gear_resolved:
        return None
    g = _slice(lf, "gear", lo, hi)
    if g is None:
        return None
    finite = g[np.isfinite(g)]
    if not finite.size:
        return None
    rounded = np.round(finite).astype(int)
    values, counts = np.unique(rounded, return_counts=True)
    return int(values[int(np.argmax(counts))])


def _environment(lf: LogFile, lo: int, hi: int) -> PullEnvironment:
    return PullEnvironment(
        ambient_temp_c=_first_finite(_slice(lf, "ambient_temp", lo, hi)),
        ambient_press_kpa=_first_finite(_slice(lf, "ambient_press", lo, hi)),
        iat_start_c=_first_finite(_slice(lf, "iat", lo, hi)),
        coolant_temp_c=_first_finite(_slice(lf, "coolant_temp", lo, hi)),
        eth_content_pct=_first_finite(_slice(lf, "eth_content", lo, hi)),
    )


def _raw_segments(wot_mask: np.ndarray) -> list[tuple[int, int]]:
    """Contiguous WOT runs (inclusive index pairs), bridging short dropouts."""
    segments: list[tuple[int, int]] = []
    n = wot_mask.size
    i = 0
    while i < n:
        if not wot_mask[i]:
            i += 1
            continue
        start = i
        last_wot = i
        gap = 0
        j = i + 1
        while j < n:
            if wot_mask[j]:
                last_wot = j
                gap = 0
            else:
                gap += 1
                if gap > BRIDGE_SAMPLES:
                    break
            j += 1
        segments.append((start, last_wot))
        i = last_wot + 1
    return segments


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #
def _qualifies(lf: LogFile, lo: int, hi: int) -> bool:
    """Duration and rpm-sweep gate separating a pull from a blip/steady WOT."""
    rpm = _slice(lf, "rpm", lo, hi)
    if rpm is None:
        return False
    finite = rpm[np.isfinite(rpm)]
    if not finite.size or (float(np.max(finite)) - float(np.min(finite))) < MIN_RPM_SPAN:
        return False
    time = lf.time
    if time is not None and np.isfinite(time[lo]) and np.isfinite(time[hi]):
        return (float(time[hi]) - float(time[lo])) >= MIN_PULL_DURATION_S
    return (hi - lo + 1) >= MIN_PULL_SAMPLES


def detect_pulls(logset: LogSet) -> list[Pull]:
    """Detect WOT pulls across every file in the set, in file/row order.

    A pull is a contiguous run of WOT samples (pedal or TPS ≥
    ``PEDAL_WOT_MIN`` and rpm ≥ ``RPM_WOT_MIN``), short dropouts bridged, that
    sweeps at least ``MIN_RPM_SPAN`` rpm over at least ``MIN_PULL_DURATION_S``.
    Pulls are numbered globally (1-based) in the order they are found.
    """
    pulls: list[Pull] = []
    index = 0
    for lf in logset.files:
        signal = _wot_signal(lf)
        rpm = lf.channel("rpm")
        if signal is None or rpm is None:
            continue
        wot_mask = np.isfinite(signal) & (signal >= PEDAL_WOT_MIN) & np.isfinite(rpm) & (rpm >= RPM_WOT_MIN)
        for lo, hi in _raw_segments(wot_mask):
            if not _qualifies(lf, lo, hi):
                continue
            index += 1
            rpm_seg = _slice(lf, "rpm", lo, hi)
            rmin, rmax = _nanmin(rpm_seg), _nanmax(rpm_seg)
            time = lf.time
            duration = None
            if time is not None and np.isfinite(time[lo]) and np.isfinite(time[hi]):
                duration = float(time[hi]) - float(time[lo])
            lam_lo, lam_hi = _lambda_error_range(lf, lo, hi)
            pulls.append(
                Pull(
                    index=index,
                    file=lf.name,
                    start_row=lo,
                    end_row=hi,
                    n_samples=hi - lo + 1,
                    duration_s=duration,
                    gear=_attribute_gear(lf, lo, hi),
                    gear_resolved=lf.gear_resolved,
                    rpm_min=rmin if rmin is not None else float("nan"),
                    rpm_max=rmax if rmax is not None else float("nan"),
                    airmass_min=_nanmin(_slice(lf, "airmass", lo, hi)),
                    airmass_max=_nanmax(_slice(lf, "airmass", lo, hi)),
                    min_knock=_min_knock(lf, lo, hi),
                    max_put=_nanmax(_slice(lf, "put", lo, hi)),
                    max_put_error=_put_error(lf, lo, hi),
                    max_boost=_nanmax(_slice(lf, "boost", lo, hi)),
                    lambda_error_min=lam_lo,
                    lambda_error_max=lam_hi,
                    lpfp_max=_nanmax(_slice(lf, "lpfp_duty", lo, hi)),
                    hpfp_eff_max=_nanmax(_slice(lf, "hpfp_eff_vol", lo, hi)),
                    turbo_speed_max=_nanmax(_slice(lf, "turbo_speed", lo, hi)),
                    environment=_environment(lf, lo, hi),
                )
            )
    return pulls
