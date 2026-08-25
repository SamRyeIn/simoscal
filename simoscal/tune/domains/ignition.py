"""Ignition — base timing, addressed the way logs report knock.

Logs say "knock retard at 4000 rpm and 1400 mg/stk", not "cell [11][7] of
``IP_IGA_BAS_IVVT_VVL_PORT_L[STND][1][2]``". So :meth:`Ignition.retard_cells`
takes ``(rpm, load) → degrees`` and finds the cell itself, on each table's own
axes.

It writes **all nine** cam-position grids by default. The ECU interpolates
between intake and exhaust cam positions, so timing pulled from only some of
them leaves the knock-prone operating point reachable through the others —
which looks, in the next set of logs, exactly like a pull that did not work.
"""

from __future__ import annotations

from typing import Mapping, Optional, Sequence

from ..journal import EditEntry
from ._common import Domain, dry_runnable, nearest_index

__all__ = ["Ignition"]


class Ignition(Domain):
    """Reached as ``tune.ignition``."""

    @dry_runnable
    def retard_cells(
        self,
        targets: Mapping[tuple[float, float], float],
        *,
        tables: Optional[Sequence[str]] = None,
        intent: str = "",
    ) -> tuple[EditEntry, ...]:
        """Set absolute timing, in °CRK, at each ``(rpm, load)`` operating point.

        Absolute rather than a delta: a timing value is what the ECU will
        actually command there, and the number a reviewer needs to sanity-check
        is the resulting advance, not the size of the change.

        Each point snaps to the nearest breakpoint on the table's own axes, and
        the resolved cell is recorded in the journal so a point that landed on
        a neighbouring breakpoint is visible rather than assumed.
        """
        if not targets:
            raise ValueError("ignition.retard_cells: no targets given")

        names = tuple(tables) if tables is not None else self._table_set(
            "ignition_base_vvl0"
        )
        entries = []
        for name in names:
            values = self._values(name)
            x_axis = self._tune.axis(name, "x")
            y_axis = self._tune.axis(name, "y")
            label = self._tune.table(name).label
            resolved: list[str] = []
            for (rpm, load), degrees in targets.items():
                col = nearest_index(x_axis, rpm, f"ignition.retard_cells({name})")
                row = nearest_index(y_axis, load, f"ignition.retard_cells({name})")
                old = float(values[row, col])
                values[row, col] = float(degrees)
                resolved.append(
                    f"{x_axis[col]:.0f} rpm / {y_axis[row]:.0f} mg/stk: "
                    f"{old:.2f} → {degrees:.2f} ({degrees - old:+.2f})"
                )
            entries.append(self._tune.write(
                name, values,
                intent=intent or (
                    f"set base timing at {len(targets)} knock-prone operating "
                    "point(s)"
                ),
                detail=f"{label}: " + "; ".join(resolved),
            ))
        return tuple(entries)

    @dry_runnable
    def offset_cells(
        self,
        deltas: Mapping[tuple[float, float], float],
        *,
        tables: Optional[Sequence[str]] = None,
        intent: str = "",
    ) -> tuple[EditEntry, ...]:
        """Add ``(rpm, load) → degrees`` to the current timing at each point.

        The relative form, for pulling a fixed amount out of whatever is there.
        Prefer :meth:`retard_cells` when you know the value you want, since a
        delta applied twice is a mistake the bin cannot detect.
        """
        if not deltas:
            raise ValueError("ignition.offset_cells: no deltas given")

        names = tuple(tables) if tables is not None else self._table_set(
            "ignition_base_vvl0"
        )
        entries = []
        for name in names:
            values = self._values(name)
            x_axis = self._tune.axis(name, "x")
            y_axis = self._tune.axis(name, "y")
            targets: dict[tuple[float, float], float] = {}
            for (rpm, load), delta in deltas.items():
                col = nearest_index(x_axis, rpm, f"ignition.offset_cells({name})")
                row = nearest_index(y_axis, load, f"ignition.offset_cells({name})")
                targets[(rpm, load)] = float(values[row, col]) + float(delta)
            entries.extend(self.retard_cells(
                targets, tables=[name],
                intent=intent or (
                    f"shift base timing at {len(deltas)} operating point(s)"
                ),
            ))
        return tuple(entries)
