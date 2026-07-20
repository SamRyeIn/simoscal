"""Fueling — the lambda targets that have to keep up with a boost change.

Named ``fueling`` rather than ``lambda`` because ``lambda`` is a Python
keyword; ``tune.lambda.grid(...)`` will not parse.

The load-bearing fact in this domain is that the three basic lambda grids —
BAS, HPDI, MPI — **share one pair of axis tables**. Re-breakpointing to the
tuning guide's grid moves all three at once, which is exactly why the guide's
lambda map cannot be written onto a stock bin without doing it: the guide
authored its map on an already-re-breakpointed bin, so writing those cells
against stock breakpoints would put the guide's numbers at the wrong loads.

That is a lean-risk failure, not a cosmetic one, so :meth:`Fueling.lambda_grid`
refuses to write a grid whose declared breakpoints do not match the table's.
"""

from __future__ import annotations

from typing import Optional, Sequence

import numpy as np

from ..journal import KIND_AXIS, EditEntry
from ..profiles.sc8s50 import LAMBDA_FAMILY, LAMBDA_FLOORS
from ..sop_bridge import positional_axis_match
from ._common import Domain, require_shape

__all__ = ["Fueling"]


class Fueling(Domain):
    """Reached as ``tune.fueling``."""

    def rebreakpoint_lambda_axes(
        self,
        *,
        rpm: Sequence[float],
        load: Sequence[float],
        intent: str = "",
    ) -> tuple[EditEntry, ...]:
        """Re-breakpoint the rpm and load axes shared by all three lambda grids.

        Must run **before** writing any lambda grid, so the cells land on the
        breakpoints they were authored for. Both axes move together because a
        grid written across two mismatched axes is wrong in both directions.
        """
        entries = []
        for name, values, what in (
            ("lambda_rpm_axis", rpm, "engine speed"),
            ("lambda_load_axis", load, "airmass load"),
        ):
            current = self._values(name)
            new = require_shape(
                np.asarray(values, dtype=np.float64).reshape(current.shape),
                current.shape, f"fueling.rebreakpoint_lambda_axes({what})",
            )
            if not np.all(np.diff(new.ravel()) > 0):
                raise ValueError(
                    f"fueling.rebreakpoint_lambda_axes: {what} breakpoints must "
                    f"strictly increase, got {[float(v) for v in new.ravel()]}"
                )
            entries.append(self._tune.write(
                name, new, kind=KIND_AXIS,
                intent=intent or (
                    f"re-breakpoint the shared lambda {what} axis to the "
                    "tuning guide's grid"
                ),
                detail=("shared by the BAS, HPDI, and MPI lambda setpoint "
                        "grids — all three are reinterpreted by this write"),
            ))
        return tuple(entries)

    def lambda_grid(
        self,
        cells,
        *,
        tables: Sequence[str] = ("lambda_basic",),
        rpm_keys: Optional[Sequence[float]] = None,
        load_keys: Optional[Sequence[float]] = None,
        intent: str = "",
    ) -> tuple[EditEntry, ...]:
        """Write a full lambda setpoint grid in physical lambda units.

        Pass ``rpm_keys``/``load_keys`` — the breakpoints the grid was authored
        against — to have them checked against the table's live axes. A
        mismatch raises rather than writing: putting the guide's enrichment at
        the wrong loads is a lean-risk error at full load, and it is invisible
        in the resulting table.
        """
        entries = []
        for name in tables:
            current = self._values(name)
            grid = require_shape(
                np.asarray(cells, dtype=np.float64), current.shape,
                f"fueling.lambda_grid({name})",
            )
            self._check_axes(name, rpm_keys, load_keys)
            entries.append(self._tune.write(
                name, grid,
                intent=intent or "write the tuning guide's lambda setpoint grid",
                detail=(
                    f"{grid.shape[0]}×{grid.shape[1]} grid, richest cell "
                    f"{grid.min():.2f} lambda; axis-matched to the table's own "
                    "breakpoints"
                ),
            ))
        return tuple(entries)

    def _check_axes(
        self,
        name: str,
        rpm_keys: Optional[Sequence[float]],
        load_keys: Optional[Sequence[float]],
    ) -> None:
        for which, keys, label in (
            ("x", rpm_keys, "rpm"), ("y", load_keys, "load"),
        ):
            if keys is None:
                continue
            axis = self._tune.axis(name, which)
            if positional_axis_match(axis, tuple(float(k) for k in keys)) is None:
                found = "none (label-only axis)" if axis is None else (
                    ", ".join(f"{v:g}" for v in axis)
                )
                raise ValueError(
                    f"fueling.lambda_grid: {self._tune.table(name).label} has "
                    f"{label} breakpoints [{found}], which do not match the "
                    f"grid's [{', '.join(f'{float(k):g}' for k in keys)}]. "
                    "Refusing to write fuelling cells at the wrong loads — "
                    "re-breakpoint the axes first."
                )

    def lambda_floors(
        self, value: float, *, tables: Sequence[str] = LAMBDA_FLOORS,
        intent: str = "",
    ) -> tuple[EditEntry, ...]:
        """Flatten the lambda minimum-value floors to ``value``.

        These clamp how rich the ECU is willing to go under catalyst and
        turbocharger overheat protection. Stock sits richer than the guide's
        0.80, so writing 0.80 makes them *less* permissive — a deliberate
        choice about where protection ends, not a free enrichment.
        """
        entries = []
        for name in tables:
            current = self._values(name)
            entries.append(self._tune.write(
                name, np.full(current.shape, float(value)),
                intent=intent or (
                    f"set the lambda minimum-value floors to {value:g}"
                ),
                detail=("a floor on how rich the protection strategies allow; "
                        "raising it toward 1.0 reduces protective enrichment"),
            ))
        return tuple(entries)

    def pedal_threshold(self, percent: float, *, intent: str = "") -> EditEntry:
        """Flatten the full-load pedal threshold, in percent.

        Stock sits near 99.9%, so heavy-throttle enrichment only arrives at
        effectively wide-open throttle. Lowering it brings enrichment in
        earlier in the pedal travel.
        """
        current = self._values("pedal_threshold_full_load")
        return self._tune.write(
            "pedal_threshold_full_load", np.full(current.shape, float(percent)),
            intent=intent or (
                f"bring full-load enrichment in from {percent:g}% pedal"
            ),
        )

    #: Convenience: every grid sharing the lambda axes, for callers that mean all.
    FAMILY = LAMBDA_FAMILY
