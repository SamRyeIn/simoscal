"""The boost-curve read model — data for the hero per-slot editor.

The five switch-patch slots each cap boost with a per-rpm ``PUT setpoint`` grid,
and the effective target at any point is ``min(base ceiling, slot)`` — the R09
semantics. The interactive editor needs that whole picture as data it can draw:
the five slot curves in psi gauge, the shared rpm axis they sit on, and the base
``IP_PUT_SP`` — Pressure up throttle setpoint full-load ceiling as the reference
line (with the region above it shaded "capped by base").

This module only *reads* — it turns the tune's tables into a
:class:`BoostCurveModel`. The *writes* the editor performs already exist as
first-class, guarded operations on ``tune.switchpatch``:

* :meth:`~simoscal.tune.domains.switchpatch.SwitchPatch.slot_curve` — a per-rpm
  curve or a flat cap for one slot, psi **floored** (a cap asked as 10 psi
  cannot encode above 10), and refused if it reaches the base ceiling (the
  min() invariant, enforced at write time as it is at the fingertip);
* :meth:`~simoscal.tune.domains.switchpatch.SwitchPatch.slot_rpm_axis` — the
  shared rpm breakpoints (Advanced), enforced strictly increasing.

:func:`slot_curve_result` wraps ``slot_curve`` to additionally report
requested-vs-encoded psi, so the editor can show where the psi floor bit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np

from .domains.switchpatch import PATCH_SPACE
from .journal import EditEntry
from .profiles.switchpatch_2933 import SLOTS
from .project import Tune
from .units import psi_from_hpa

__all__ = [
    "SlotCurve",
    "BoostCurveModel",
    "boost_curve_model",
    "slot_curve_result",
    "SlotCurveResult",
]

#: The base full-load ceiling table and its rpm axis, in the base space.
_BASE_CEILING = "put_setpoint"
_BASE_CEILING_RPM_AXIS = "put_setpoint_rpm_axis"
#: The shared slot rpm axis, in the patch space.
_SLOT_RPM_AXIS = "slot_put_rpm_axis"


@dataclass(frozen=True)
class SlotCurve:
    """One slot's boost cap, as per-rpm psi gauge (the grid is row-tiled)."""

    slot: int
    psi: tuple[float, ...]

    @property
    def is_flat(self) -> bool:
        return len(set(round(v, 6) for v in self.psi)) <= 1


@dataclass(frozen=True)
class BoostCurveModel:
    """Everything the per-slot editor draws, in psi gauge on a shared rpm axis."""

    rpm_axis: tuple[float, ...]
    slots: tuple[SlotCurve, ...]
    base_ceiling_psi: tuple[float, ...]   # base ceiling interpolated onto rpm_axis
    base_rpm_axis: tuple[float, ...]      # the base table's own (coarser) rpm axis
    base_ceiling_own_psi: tuple[float, ...]  # base ceiling on its own axis

    def effective_psi(self, slot: int) -> tuple[float, ...]:
        """The min(base, slot) the ECU would actually target for ``slot``."""
        curve = next(c for c in self.slots if c.slot == slot)
        return tuple(min(s, b) for s, b in zip(curve.psi, self.base_ceiling_psi))


def _row0_psi(tune: Tune, name: str) -> tuple[float, ...]:
    grid = np.asarray(tune.values(name, space=PATCH_SPACE), dtype=np.float64)
    return tuple(psi_from_hpa(float(v)) for v in grid[0])


def boost_curve_model(tune: Tune) -> BoostCurveModel:
    """Read the five slot curves, the shared rpm axis, and the base ceiling.

    Requires the switch-patch space to be present (the tune was opened with the
    patch profile); a bin without it has no slots to edit.
    """
    if PATCH_SPACE not in tune.spaces:
        raise ValueError(
            "boost_curve_model needs the switch-patch space; open the tune with "
            "the switch-patch profile (extra_spaces) to edit slot curves."
        )

    rpm_axis = tuple(
        float(v) for v in np.asarray(
            tune.values(_SLOT_RPM_AXIS, space=PATCH_SPACE), dtype=np.float64
        ).ravel()
    )
    slots = tuple(
        SlotCurve(slot=s, psi=_row0_psi(tune, f"slot{s}_put_setpoint")) for s in SLOTS
    )

    # Base ceiling: the full-load (last) row of IP_PUT_SP, in psi, on its own
    # coarser rpm axis — then interpolated onto the fine slot axis so the editor
    # can draw the reference line and compute min(base, slot) point-for-point.
    base_grid = np.asarray(tune.values(_BASE_CEILING), dtype=np.float64)
    base_row_psi = np.array([psi_from_hpa(float(v)) for v in base_grid[-1]])
    base_rpm = np.asarray(
        tune.values(_BASE_CEILING_RPM_AXIS), dtype=np.float64
    ).ravel()

    interp = np.interp(np.array(rpm_axis), base_rpm, base_row_psi)
    return BoostCurveModel(
        rpm_axis=rpm_axis,
        slots=slots,
        base_ceiling_psi=tuple(float(v) for v in interp),
        base_rpm_axis=tuple(float(v) for v in base_rpm),
        base_ceiling_own_psi=tuple(float(v) for v in base_row_psi),
    )


@dataclass(frozen=True)
class SlotCurveResult:
    """A slot-curve edit plus the requested-vs-encoded psi it produced."""

    entry: EditEntry
    slot: int
    requested_psi: tuple[float, ...]
    encoded_psi: tuple[float, ...]

    @property
    def floored(self) -> bool:
        """Whether the psi floor moved any point below what was requested."""
        return any(
            round(e, 6) < round(r, 6)
            for r, e in zip(self.requested_psi, self.encoded_psi)
        )


def slot_curve_result(
    tune: Tune,
    slot: int,
    *,
    psi,
    intent: str = "",
    **kwargs,
) -> SlotCurveResult:
    """Apply ``switchpatch.slot_curve`` and report requested-vs-encoded psi.

    A thin wrapper so the editor gets both the journaled entry and the psi the
    cap actually encoded to — the floor means an asked-for 10.0 psi can encode a
    hair under 10.0, and the UI shows that.
    """
    requested = np.atleast_1d(np.asarray(psi, dtype=np.float64))
    cols = len(boost_curve_model(tune).rpm_axis)
    if requested.size == 1:
        requested = np.full(cols, float(requested[0]))

    entry = tune.switchpatch.slot_curve(slot, psi=psi, intent=intent, **kwargs)
    encoded = _row0_psi(tune, f"slot{slot}_put_setpoint")
    return SlotCurveResult(
        entry=entry,
        slot=slot,
        requested_psi=tuple(float(v) for v in requested),
        encoded_psi=encoded,
    )
