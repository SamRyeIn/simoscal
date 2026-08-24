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

:meth:`Fueling.full_load_enrichment` carries the same risk from the other
direction. It writes the time-based full-load enrichment map, where the danger
has a *direction*: leaner is hotter, and at wide-open throttle the enrichment in
that map is what keeps the pistons and the turbine alive. So it refuses a
setpoint at or above :data:`LAMBDA_FL_LEAN_MAX` rather than clamping to it —
see that constant for why the bound sits where it does.
"""

from __future__ import annotations

from typing import Optional, Sequence

import numpy as np

from ..journal import KIND_AXIS, EditEntry
from ..sop_bridge import positional_axis_match
from ._common import Domain, nearest_index, require_shape

__all__ = ["Fueling", "LAMBDA_FL_LEAN_MAX", "LAMBDA_FL_RICH_MIN"]

#: The lean bound on full-load enrichment: a setpoint at or above this is
#: refused outright, never clamped.
#:
#: Stoichiometric, and the direction of danger. At full load this map is the
#: enrichment that carries heat out of the combustion chamber and off the
#: turbine; a setpoint of 1.00 asks for no enrichment at all at wide-open
#: throttle, which on a boosted engine is how pistons and turbine wheels are
#: destroyed. It is a hard refusal rather than a warning because there is no
#: legitimate reason to ask for it here — Sam's call, 2026-08-20. The UI warns
#: from 0.90 up; that softer band is a screen concern, not this one.
LAMBDA_FL_LEAN_MAX = 1.00

#: The other end: richer than anything this calibration has a use for, and the
#: shape a mistyped decimal takes (0.08 for 0.80). Refused for the same reason —
#: loudly, so the typo is visible rather than encoded.
LAMBDA_FL_RICH_MIN = 0.50


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
        self, value: float, *, tables: Optional[Sequence[str]] = None,
        intent: str = "",
    ) -> tuple[EditEntry, ...]:
        """Flatten the lambda minimum-value floors to ``value``.

        These clamp how rich the ECU is willing to go under catalyst and
        turbocharger overheat protection. Stock sits richer than the guide's
        0.80, so writing 0.80 makes them *less* permissive — a deliberate
        choice about where protection ends, not a free enrichment.
        """
        names = tuple(tables) if tables is not None else self._table_set(
            "lambda_floors"
        )
        entries = []
        for name in names:
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

    def full_load_enrichment(
        self,
        values,
        *,
        row: Optional[int] = None,
        seconds: Optional[float] = None,
        intent: str = "",
    ) -> EditEntry:
        """Write one time-row of the full-load enrichment map, in lambda.

        The map is engine speed (columns) against **time at full load** (rows):
        as a pull holds wide-open throttle, the ECU walks down the rows, so each
        row is "how rich, this many seconds in". Give ``row`` as an index or
        ``seconds`` to pick the row by its own breakpoint; ``values`` is one
        lambda per rpm breakpoint, or a scalar for a flat row.

        Stock is flat 1.00 across the whole map — this car does its enrichment
        through the basic lambda grids — so every value written here below 1.00
        is enrichment *added* on top of that.

        Refuses any value at or above :data:`LAMBDA_FL_LEAN_MAX`, and any below
        :data:`LAMBDA_FL_RICH_MIN`. Neither is clamped: a lean full-load
        setpoint is the failure mode this whole domain exists to prevent, and
        silently correcting one would hide that it was ever asked for.
        """
        name = "lambda_full_load"
        grid = self._values(name)
        rows, cols = grid.shape

        index = self._enrichment_row(name, rows, row, seconds)
        curve = np.atleast_1d(np.asarray(values, dtype=np.float64)).ravel()
        if curve.size == 1:
            curve = np.full(cols, float(curve[0]))
        curve = require_shape(curve, (cols,), "fueling.full_load_enrichment")

        lean = curve[curve >= LAMBDA_FL_LEAN_MAX]
        if lean.size:
            raise ValueError(
                f"fueling.full_load_enrichment: {lean.size} value(s) at or above "
                f"lambda {LAMBDA_FL_LEAN_MAX:.2f} (leanest {lean.max():.3f}). At "
                "full load this map is what carries heat out of the chamber and "
                "off the turbine, so a setpoint of 1.00 asks for no enrichment "
                "at wide-open throttle. Refusing — nothing was written."
            )
        rich = curve[curve <= LAMBDA_FL_RICH_MIN]
        if rich.size:
            raise ValueError(
                f"fueling.full_load_enrichment: {rich.size} value(s) at or below "
                f"lambda {LAMBDA_FL_RICH_MIN:.2f} (richest {rich.min():.3f}), "
                "richer than any use this calibration has — this is the shape a "
                "mistyped decimal takes. Refusing rather than encoding it."
            )

        staged = grid.copy()
        staged[index] = curve
        seconds_label = self._row_seconds(name, index)
        return self._tune.write(
            name, staged,
            intent=intent or (
                "set the full-load enrichment curve at "
                + (f"{seconds_label:g} s at full load" if seconds_label is not None
                   else f"time-row {index}")
            ),
            detail=(
                f"row {index}"
                + (f" ({seconds_label:g} s at full load)"
                   if seconds_label is not None else "")
                + ": "
                + ", ".join(f"{v:.3f}" for v in curve)
                + f" lambda; richest {curve.min():.3f}. Every other time-row "
                "left as it was."
            ),
        )

    def _enrichment_row(
        self, name: str, rows: int, row: Optional[int], seconds: Optional[float]
    ) -> int:
        if (row is None) == (seconds is None):
            raise ValueError(
                "fueling.full_load_enrichment: give exactly one of row= (index) "
                "or seconds= (time at full load)"
            )
        if seconds is not None:
            return nearest_index(
                self._tune.axis(name, "y"), float(seconds),
                "fueling.full_load_enrichment(seconds=)",
            )
        index = int(row)
        if not 0 <= index < rows:
            raise ValueError(
                f"fueling.full_load_enrichment: row {row!r} is outside the map's "
                f"{rows} time-rows (0–{rows - 1})"
            )
        return index

    def _row_seconds(self, name: str, index: int) -> Optional[float]:
        axis = self._tune.axis(name, "y")
        if axis is None or index >= axis.size:
            return None
        return float(axis[index])

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

    @property
    def FAMILY(self) -> tuple[str, ...]:
        """Every grid sharing this car's lambda axes, for callers meaning all.

        Read off the open bin's profile rather than fixed by this module:
        which grids share those axes is a fact about the ECU in front of
        you, and a tuple named here would assert one engine's family on
        every car that reaches this call.
        """
        return self._table_set("lambda_family")
