"""RenderedTable + render_table(): the shared table -> grid rendering layer.

The one source of truth for "table -> grid," consumed by the CSV/xlsx writers
(:mod:`simoscal.export`) and, in a later phase, visualization directly. Purely
a read-side transform: no bin bytes are written and no scaling/rounding is
applied beyond what :class:`~simoscal.calfile.TableView` already decoded.

Degeneracy is shape-driven, not schema-driven: there is no ``XDFCONSTANT`` in
this project (see plan Research Findings) — every "scalar" is simply a table
whose shape is ``(1, 1)``, and every "1D" table is simply one whose ``rows``
or ``cols`` is 1. :func:`render_table` branches purely on ``shape``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np

from .calfile import TableView

__all__ = ["RenderedTable", "render_table"]


def _axis_labels(view: TableView, which: str, count: int) -> tuple[float, ...]:
    """Decoded breakpoints for the ``x``/``y`` axis, or a ``0..count-1`` index.

    Falls back to a raw index when the axis has no embedded data (label-only
    axes carry no real text — see plan Research Findings, resolves
    requirements doc Q2).
    """
    values = view.axis_values(which)
    if values is None:
        return tuple(float(i) for i in range(count))
    return tuple(float(v) for v in np.asarray(values).ravel())


@dataclass(frozen=True)
class RenderedTable:
    """A table rendered into grid form: metadata + axis labels + values.

    ``values`` is always a 2D ``(rows, cols)`` numpy array, even in degenerate
    cases. ``x_labels`` is always present; ``y_labels`` is ``None`` when the
    table has a single row (1D or scalar) — there is no row axis to label.
    ``x_units``/``y_units`` are the axis's declared units (``None`` when the
    axis has none), independent of whether the axis is embedded or label-only.
    """

    symbol: Optional[str]
    title: Optional[str]
    units: Optional[str]
    categories: tuple[str, ...]
    x_labels: tuple[float, ...]
    y_labels: Optional[tuple[float, ...]]
    x_units: Optional[str]
    y_units: Optional[str]
    values: np.ndarray


def render_table(view: TableView) -> RenderedTable:
    """Render a :class:`~simoscal.calfile.TableView` into a :class:`RenderedTable`.

    Branches on ``view.shape`` (rows == 1 and/or cols == 1), never on
    presence/absence of ``Table.x``/``Table.y`` — see plan Key Decision 2.
    """
    rows, cols = view.shape
    values = view.values

    x_labels = _axis_labels(view, "x", cols)
    y_labels = None if rows == 1 else _axis_labels(view, "y", rows)

    return RenderedTable(
        symbol=view.symbol,
        title=view.title,
        units=view.units,
        categories=tuple(c.name for c in view.table.categories),
        x_labels=x_labels,
        y_labels=y_labels,
        x_units=view.table.x.units if view.table.x is not None else None,
        y_units=view.table.y.units if view.table.y is not None else None,
        values=values,
    )
