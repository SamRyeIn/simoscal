"""Boost: what the ECU is *asked* to deliver, and what caps that request.

Distilled from the R09–R11 ``IP_PUT_SP`` work and R10's compressor cap. The
recurring shape in this domain is a **full-load row**: boost setpoint tables
are load × rpm, and tuning targets the top load row while every part-load row
must stay exactly as it was. Doing that by hand means reading the table,
replacing one row, writing it back, and then proving the other rows did not
move — three steps, of which the third is the one that gets skipped.

Here it is one call, and the proof is automatic: the journal records which
rows moved, and ``build()``'s byte audit fails if any others did.
"""

from __future__ import annotations

from typing import Optional, Sequence

import numpy as np

from ..journal import KIND_AXIS, EditEntry
from ..units import hpa_from_psi, psi_from_hpa
from ._common import (
    Domain, dry_runnable, float_bug_write, guarded_ceiling, require_shape,
)

__all__ = ["Boost"]


class Boost(Domain):
    """Reached as ``tune.boost``."""

    # -- the requested boost curve ------------------------------------------ #
    @dry_runnable
    def put_ceiling_hpa(self, hpa: float, *, intent: str = "") -> EditEntry:
        """Flatten the full-load row of the PUT setpoint to ``hpa`` absolute.

        Part-load rows are left byte-identical. Parking this row high is only
        safe when something *below* it binds — on a switch-patched bin the
        per-slot grids do, under the min() semantics R09 established.
        """
        values = self._values("put_setpoint")
        values[-1] = float(hpa)
        return self._tune.write(
            "put_setpoint", values,
            intent=intent or (
                f"park the full-load boost setpoint at {hpa:.0f} hPa absolute "
                f"({psi_from_hpa(hpa):.1f} psi gauge)"
            ),
            detail=(f"full-load row only; {len(values) - 1} part-load row(s) "
                    "left untouched"),
        )

    @dry_runnable
    def put_ceiling_psi(
        self, psi: float, *, rounding: str = "nearest", intent: str = ""
    ) -> EditEntry:
        """As :meth:`put_ceiling_hpa`, but stated in psi gauge.

        ``rounding`` defaults to ``"nearest"`` here because a parked ceiling is
        a nominal target rather than a promise. Use ``"floor"`` when the number
        is a limit someone must not exceed.
        """
        hpa = hpa_from_psi(psi, rounding=rounding)
        return self.put_ceiling_hpa(
            hpa,
            intent=intent or f"park the full-load boost setpoint at {psi:g} psi gauge",
        )

    @dry_runnable
    def put_curve_hpa(
        self, curve: Sequence[float], *, intent: str = ""
    ) -> EditEntry:
        """Write a per-rpm full-load boost curve, in hPa absolute.

        One value per breakpoint of the table's own rpm axis — the shape is
        checked against the table, so a curve written for a differently
        breakpointed bin fails before it is written, not after.
        """
        values = self._values("put_setpoint")
        row = require_shape(
            np.asarray(curve, dtype=np.float64).ravel(),
            (values.shape[1],),
            "boost.put_curve_hpa",
        )
        values[-1] = row
        return self._tune.write(
            "put_setpoint", values,
            intent=intent or "shape the full-load boost curve across rpm",
            detail=(
                "full-load row across the table's rpm axis: "
                + ", ".join(f"{v:.0f}" for v in row)
                + " hPa absolute ("
                + ", ".join(f"{psi_from_hpa(v):.1f}" for v in row)
                + " psi gauge)"
            ),
        )

    @dry_runnable
    def put_rpm_axis(
        self, breakpoints: Sequence[float], *, intent: str = ""
    ) -> EditEntry:
        """Re-breakpoint the PUT setpoint's own rpm axis.

        Private to this table, so unlike the wastegate and lambda axes it has
        no blast radius — but it silently reinterprets every existing row, so
        the curve must be rewritten to match.
        """
        current = self._values("put_setpoint_rpm_axis")
        new = require_shape(
            np.asarray(breakpoints, dtype=np.float64).reshape(current.shape),
            current.shape, "boost.put_rpm_axis",
        )
        if not np.all(np.diff(new.ravel()) > 0):
            raise ValueError(
                f"boost.put_rpm_axis: breakpoints must strictly increase, got "
                f"{[float(v) for v in new.ravel()]}"
            )
        return self._tune.write(
            "put_setpoint_rpm_axis", new, kind=KIND_AXIS,
            intent=intent or "re-breakpoint the boost setpoint rpm axis",
            detail=("axis is private to the PUT setpoint table, so no other "
                    "table is reinterpreted by this change"),
        )

    # -- the caps that can defeat that curve --------------------------------- #
    @dry_runnable
    def pressure_quotient_max(
        self, plateau: float, *, low_rpm: Optional[float] = None, intent: str = ""
    ) -> EditEntry:
        """Set the compressor pressure-quotient cap: ``plateau`` everywhere.

        With ``low_rpm``, the first (lowest-rpm) column takes that value
        instead — the SOP's default shape, which keeps the cap tight where the
        compressor cannot use the headroom anyway.

        This cap can silently trim a boost curve short: R09's logs showed
        delivered boost plateauing at exactly the cap × measured pre-compressor
        pressure, reported as torque-limit code 128. Raising it moves closer to
        the compressor's map edge, so it is a deliberate risk, not a freebie.
        """
        values = self._values("pressure_quotient_max")
        values[:, :] = float(plateau)
        if low_rpm is not None:
            values[:, 0] = float(low_rpm)
        shape_note = (
            f"{low_rpm:g} at the lowest-rpm column, flat {plateau:g} across the rest"
            if low_rpm is not None
            else f"flat {plateau:g} across all cells"
        )
        return self._tune.write(
            "pressure_quotient_max", values,
            intent=intent or f"cap the compressor pressure quotient at {plateau:g}",
            detail=f"{shape_note}; watch turbo speed and rail pressure in the logs",
        )

    @dry_runnable
    def manifold_pressure_max(self, hpa: float, *, intent: str = "") -> EditEntry:
        """Raise the maximum requested intake-manifold pressure setpoint.

        A float-bug table: its XDF display maximum is a TunerPro editor
        artifact that the stock value already exceeds many times over, so the
        write goes through the raw path. See
        :func:`~simoscal.tune.domains._common.float_bug_write`.
        """
        return float_bug_write(
            self._tune, "manifold_pressure_max", hpa,
            intent=intent or (
                f"move the manifold pressure setpoint ceiling to {hpa:.0f} hPa "
                "so it cannot bind"
            ),
        )

    @dry_runnable
    def overboost_threshold(self, hpa: float, *, intent: str = "") -> EditEntry:
        """Raise the P0234 overpressure diagnosis threshold, never lower it.

        This is the table the basics guide means by "overboost limit" — not
        ``C_PRS_IM_SP_LIM``, which an early revision of the shared recipe
        pointed at by mistake. Raising it widens the margin before the
        turbocharger overpressure diagnostic trips; it does not itself raise
        boost.
        """
        return guarded_ceiling(
            self._tune, "overboost_threshold", hpa,
            intent=intent or (
                f"raise the P0234 overpressure diagnosis threshold to {hpa:.0f} hPa"
            ),
        )
