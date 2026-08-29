"""Wastegate feedforward — the open-loop guess the boost controller starts from.

Two tables, ``IP_FAC_BPA_SP[0]`` and ``[1]`` (VVL 0 and VVL 1), indexed by
exhaust flow factor (x) and intake flow factor (y). Cells are actuator
position: **1 = closed** (all flow through the turbine, maximum boost),
**0 = open**. Lowering a cell opens the wastegate sooner there, which is the
fix for boost overshooting its target in the rpm band that lands on that cell.

Two rules the R05 and R08 revisions both had to enforce by hand, now enforced
here:

1. **Both VVL tables get identical deltas.** They are the same calibration for
   two cam positions; a difference between them is a mistake, not a tune.
2. **Never clamp.** A delta that would push a cell past 0 or 1 means the delta
   map is wrong. Clamping it silently would produce a wastegate position
   nobody chose.
"""

from __future__ import annotations

from typing import Mapping, Optional, Sequence

import numpy as np

from ..journal import KIND_AXIS, EditEntry
from ._common import Domain, dry_runnable

__all__ = ["Wastegate", "WG_CLOSED", "WG_OPEN"]

#: Physical bounds of a wastegate-position cell.
WG_OPEN, WG_CLOSED = 0.0, 1.0

#: Two encoding steps of the feedforward table (its step is ~6.1e-5).
#:
#: Not a comfort margin — it is the floor of what "unchanged" can mean here. A
#: resampled cell is stored to the nearest step, and clamping one into the
#: physical [0, 1] range can move it a further step, so a bilinear blend of two
#: such cells can differ from the original surface by two steps without anything
#: being wrong. Two steps is 0.012 wastegate position points, on the order of
#: 0.0002 psi of boost. Anything above it is a resample defect, not rounding.
_REBREAKPOINT_TOLERANCE = 1.22e-4


def _bilinear(grid, x_axis, y_axis, x: float, y: float) -> float:
    """The ECU's own feedforward lookup, unrolled.

    Replaying this against the logged ``WG Pos Base (%)`` over the R18 sessions
    reproduces the commanded feedforward to 0.066 points RMS, which is what
    licenses using it to assert that a re-breakpoint changed nothing.
    """
    xi = float(np.clip(np.interp(x, x_axis, np.arange(len(x_axis))),
                       0, len(x_axis) - 1))
    yi = float(np.clip(np.interp(y, y_axis, np.arange(len(y_axis))),
                       0, len(y_axis) - 1))
    x0, y0 = int(np.floor(xi)), int(np.floor(yi))
    x1, y1 = min(x0 + 1, len(x_axis) - 1), min(y0 + 1, len(y_axis) - 1)
    fx, fy = xi - x0, yi - y0
    return (grid[y0, x0] * (1 - fx) * (1 - fy) + grid[y0, x1] * fx * (1 - fy)
            + grid[y1, x0] * (1 - fx) * fy + grid[y1, x1] * fx * fy)


class Wastegate(Domain):
    """Reached as ``tune.wastegate``."""

    @dry_runnable
    def overlay(
        self,
        deltas: Mapping[tuple[int, int], float],
        *,
        intent: str = "",
        maps: Optional[Sequence[str]] = None,
    ) -> tuple[EditEntry, ...]:
        """Add ``{(row, col): delta}`` to both VVL feedforward tables.

        Deltas, not absolutes, because a wastegate overlay is always read as a
        correction to what the last flash did: "open this cell 0.06 more than
        it was". Negative opens the wastegate sooner.

        Fails loud if any cell would leave the physical [0, 1] range, if the
        two tables end up with different deltas, or if the number of changed
        cells does not match the number requested.
        """
        if not deltas:
            raise ValueError("wastegate.overlay: no deltas given")

        names = tuple(maps) if maps is not None else self._table_set(
            "wastegate_maps"
        )
        applied: list[np.ndarray] = []
        entries: list[EditEntry] = []
        for name in names:
            before = self._values(name)
            values = before.copy()
            for (row, col), delta in deltas.items():
                if not (0 <= row < values.shape[0] and 0 <= col < values.shape[1]):
                    raise ValueError(
                        f"wastegate.overlay: cell ({row}, {col}) is outside "
                        f"{self._tune.table(name).label} with shape {values.shape}"
                    )
                target = float(values[row, col]) + delta
                if not WG_OPEN <= target <= WG_CLOSED:
                    raise ValueError(
                        f"wastegate.overlay: cell ({row}, {col}) of "
                        f"{self._tune.table(name).label} would reach "
                        f"{target:.3f}, outside the physical "
                        f"[{WG_OPEN:g}, {WG_CLOSED:g}] wastegate range — "
                        "refusing to write a clamped position; fix the deltas"
                    )
                values[row, col] = target

            delta_map = values - before
            changed = int(np.count_nonzero(delta_map))
            if changed != len(deltas):
                raise ValueError(
                    f"wastegate.overlay: asked to change {len(deltas)} cell(s) "
                    f"of {self._tune.table(name).label}, but {changed} moved — "
                    "a delta of zero, or two deltas on one cell?"
                )
            applied.append(delta_map)
            entries.append(self._tune.write(
                name, values,
                intent=intent or (
                    f"open the wastegate sooner in {len(deltas)} feedforward "
                    "cell(s) along the measured overboost ridge"
                ),
                detail=(
                    f"{changed} cell(s) changed by "
                    f"{delta_map[delta_map != 0].min():+.3f}.."
                    f"{delta_map[delta_map != 0].max():+.3f}; cells are "
                    "actuator position (1 = closed, 0 = open), x = exhaust "
                    "flow factor, y = intake flow factor"
                ),
            ))

        if len(applied) > 1 and not all(
            np.array_equal(applied[0], other) for other in applied[1:]
        ):
            raise ValueError(
                "wastegate.overlay: the VVL tables received different deltas — "
                "they are one calibration for two cam positions and must match. "
                "Refusing to continue."
            )
        return tuple(entries)

    @dry_runnable
    def exh_flow_axis_last(self, value: float, *, intent: str = "") -> EditEntry:
        """Move the last exhaust-flow-factor breakpoint of the shared x axis.

        The stock top breakpoint clamps the map below the flow factors this car
        actually reaches at full load, so the top-end cells sit on a flat shelf
        instead of a slope. Moving the endpoint out unclamps that range.

        This axis is shared by **both** wastegate tables and nothing else, so
        one write re-breakpoints both — deliberate, and the reason the change
        is a named method rather than a raw axis write.
        """
        current = self._values("wastegate_exh_flow_axis")
        old = float(current.ravel()[-1])
        if value <= float(current.ravel()[-2]):
            raise ValueError(
                f"wastegate.exh_flow_axis_last: {value:g} is not above the "
                f"previous breakpoint {current.ravel()[-2]:g} — breakpoints "
                "must strictly increase"
            )
        current.ravel()[-1] = float(value)
        return self._tune.write(
            "wastegate_exh_flow_axis", current, kind=KIND_AXIS,
            intent=intent or (
                f"unclamp the top of the wastegate map: last exhaust flow "
                f"factor breakpoint {old:g} → {value:g}"
            ),
            detail=("shared by both VVL feedforward maps and referenced by "
                    "nothing else, so both are re-breakpointed together"),
        )

    @dry_runnable
    def move_intake_flow_breakpoint(
        self,
        index: int,
        value: float,
        *,
        preserve_to: float,
        exhaust_range: tuple[float, float],
        intent: str = "",
    ) -> tuple[EditEntry, ...]:
        """Move one intake-flow-factor breakpoint and resample both maps onto it.

        Why this exists as its own method rather than a raw axis write: moving a
        breakpoint changes what every cell in that row *means*. Writing the axis
        alone silently reinterprets the whole row, which is a calibration change
        nobody declared. So this does both halves at once — the axis move and the
        resample that holds the delivered surface still.

        ``preserve_to`` and ``exhaust_range`` together declare the operating
        envelope this car actually reaches: intake flow factor up to
        ``preserve_to``, exhaust flow factor within ``exhaust_range``. Inside
        that rectangle the commanded position is reproduced **exactly**;
        outside it the rows above the moved breakpoint are extrapolated and the
        surface deliberately is not preserved. That is a real trade — it spends
        the top of the axis — and it is only sound while the engine cannot
        reach there. Pass what the car has been logged at, with margin.

        The guarantee is *verified*, not argued: the ECU's bilinear lookup is
        replayed over a dense grid of that rectangle before and after, and any
        movement beyond half an encoding step raises. Both axes matter, which
        is why both are declared — rows above ``preserve_to`` still act as
        interpolation endpoints below it, so a cell clamped into the physical
        [0, 1] range up there can still move the delivered surface down here.

        Both VVL maps are resampled identically, as with :meth:`overlay`.
        """
        axis_name = "wastegate_intake_flow_axis"
        axis = self._values(axis_name).ravel().astype(float)
        if not 0 <= index < len(axis):
            raise ValueError(
                f"wastegate.move_intake_flow_breakpoint: index {index} is "
                f"outside the {len(axis)}-breakpoint axis"
            )
        old = float(axis[index])
        lower = float(axis[index - 1]) if index > 0 else float("-inf")
        upper = float(axis[index + 1]) if index + 1 < len(axis) else float("inf")
        if not lower < value < upper:
            raise ValueError(
                f"wastegate.move_intake_flow_breakpoint: {value:g} does not sit "
                f"strictly between its neighbours {lower:g} and {upper:g} — "
                "breakpoints must stay strictly increasing"
            )
        if preserve_to <= value:
            raise ValueError(
                f"wastegate.move_intake_flow_breakpoint: preserve_to "
                f"{preserve_to:g} must be above the new breakpoint {value:g}; "
                "there would be nothing left to preserve"
            )

        names = self._table_set("wastegate_maps")
        grids = {name: self._values(name).astype(float) for name in names}

        new_axis = axis.copy()
        new_axis[index] = float(value)
        resampled: dict[str, np.ndarray] = {}
        for name, grid in grids.items():
            out = grid.copy()
            for col in range(grid.shape[1]):
                column = grid[:, col]
                at_new = float(np.interp(value, axis, column))
                at_env = float(np.interp(preserve_to, axis, column))
                out[index, col] = at_new
                # Rows above the moved breakpoint carry the surface's own slope
                # through `preserve_to`, so the used range stays exact.
                for above in range(index + 1, len(axis)):
                    span = (new_axis[above] - value) / (preserve_to - value)
                    out[above, col] = at_new + (at_env - at_new) * span
            resampled[name] = np.clip(out, WG_OPEN, WG_CLOSED)

        # The guarantee, checked rather than reasoned about. Clamping a cell
        # above `preserve_to` can still move the surface below it, because that
        # row is an interpolation endpoint — so verify the rectangle directly.
        x_axis = self._values("wastegate_exh_flow_axis").ravel().astype(float)
        lo_x, hi_x = float(exhaust_range[0]), float(exhaust_range[1])
        if not lo_x < hi_x:
            raise ValueError(
                f"wastegate.move_intake_flow_breakpoint: exhaust_range "
                f"{exhaust_range!r} is not (low, high)"
            )
        probe_x = np.linspace(lo_x, hi_x, 64)
        probe_y = np.linspace(0.0, preserve_to, 64)
        worst, worst_at = 0.0, None
        for name in names:
            for x in probe_x:
                for y in probe_y:
                    moved = abs(
                        _bilinear(resampled[name], x_axis, new_axis, x, y)
                        - _bilinear(grids[name], x_axis, axis, x, y)
                    )
                    if moved > worst:
                        worst, worst_at = moved, (name, float(x), float(y))
        if worst > _REBREAKPOINT_TOLERANCE:
            name, x, y = worst_at
            raise ValueError(
                f"wastegate.move_intake_flow_breakpoint: the resample moved the "
                f"commanded position by {worst * 100:.4f} points at {name} "
                f"exhaust {x:.3f} / intake {y:.3f}, inside the declared "
                f"operating envelope (exhaust {lo_x:g}..{hi_x:g}, intake up to "
                f"{preserve_to:g}). Refusing to alter the delivered surface."
            )

        first = resampled[names[0]]
        for name in names[1:]:
            if not np.array_equal(resampled[name][index:], first[index:]):
                raise ValueError(
                    "wastegate.move_intake_flow_breakpoint: the VVL tables "
                    "resampled differently — they are one calibration for two "
                    "cam positions and must match. Refusing to continue."
                )

        reason = intent or (
            f"move intake flow factor breakpoint {index} from {old:g} to "
            f"{value:g} and resample both feedforward maps so the commanded "
            f"position is unchanged up to intake flow factor {preserve_to:g}"
        )
        detail = (
            f"commanded position verified unchanged (worst {worst * 100:.5f} "
            f"points) over exhaust flow {lo_x:g}..{hi_x:g} and intake flow up "
            f"to {preserve_to:g}; above that the rows are extrapolated along "
            f"the same slope, which this car is not logged to reach"
        )
        entries = [
            self._tune.write(
                axis_name, new_axis.reshape(self._values(axis_name).shape),
                kind=KIND_AXIS, intent=reason, detail=detail,
            )
        ]
        for name in names:
            entries.append(self._tune.write(
                name, resampled[name], intent=reason, detail=detail,
            ))
        return tuple(entries)
