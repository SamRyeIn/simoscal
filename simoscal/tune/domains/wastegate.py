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
from ._common import Domain

__all__ = ["Wastegate", "WG_CLOSED", "WG_OPEN"]

#: Physical bounds of a wastegate-position cell.
WG_OPEN, WG_CLOSED = 0.0, 1.0


class Wastegate(Domain):
    """Reached as ``tune.wastegate``."""

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
