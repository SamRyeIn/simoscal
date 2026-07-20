"""Unit conversions a tuner thinks in, done once and correctly.

Boost is *discussed* in psi gauge and *stored* in hPa absolute. Every revision
in the R09–R12 lineage re-derived that conversion inline, and each one had to
re-decide which way to round — a decision with real consequences: a cap meant
to be "no more than 10 psi" that rounds up is a cap above 10 psi.

So the conversion lives here, the rounding is an explicit argument, and the
default is the safe direction for a limit.
"""

from __future__ import annotations

import math

__all__ = [
    "AMBIENT_HPA",
    "HPA_PER_PSI",
    "hpa_from_psi",
    "psi_from_hpa",
]

#: Reference ambient pressure used to convert gauge ↔ absolute. Sea-level
#: nominal; the car is driven between sea level and ~6000 ft, so a target
#: expressed in gauge psi is a *nominal* target, not a promise about what the
#: manifold sees on a given day.
AMBIENT_HPA = 1016.0

#: hPa per psi. The R09–R12 scripts called this ``PSI_PER_HPA``, which reads
#: backwards; the arithmetic (``hPa_abs = psi_gauge × this + ambient``) is
#: unchanged, so conversions match the frozen lineage exactly.
HPA_PER_PSI = 68.95


def hpa_from_psi(
    psi: float, *, ambient: float = AMBIENT_HPA, rounding: str = "floor"
) -> float:
    """Convert psi gauge to the hPa absolute the ECU stores.

    ``rounding`` defaults to ``"floor"`` because the usual reason to write a
    boost number is to cap something, and a cap must never land above the
    number a human asked for. Use ``"nearest"`` when reproducing a value that
    was originally derived that way, or ``"exact"`` for no rounding at all.
    """
    raw = psi * HPA_PER_PSI + ambient
    if rounding == "floor":
        return float(math.floor(raw))
    if rounding == "nearest":
        return float(math.floor(raw + 0.5))
    if rounding == "exact":
        return float(raw)
    raise ValueError(
        f"rounding must be 'floor', 'nearest', or 'exact', got {rounding!r}"
    )


def psi_from_hpa(hpa: float, *, ambient: float = AMBIENT_HPA) -> float:
    """Convert stored hPa absolute back to psi gauge, for display only."""
    return (hpa - ambient) / HPA_PER_PSI
