"""Read-only table catalog and detail models for the editing surface.

The Quick Edit app browses and edits only the tables the SC8S50 (+ switch-patch)
profiles resolve — the curated set, not all ~3,800 XDF tables. That is a safety
choice: every table a user can reach came through a profile map, so its plain-
English description, units, and guard tags are always in force, and a stranger
table with a surprising layout is simply not offered.

Everything here is read-only. A :class:`TableInfo` is a description a Compose
screen (or a test) renders; the *edits* live in :mod:`simoscal.tune.editing` and
the domain modules. Keeping the two apart means listing the catalog can never
change a byte.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np

from .project import Tune

__all__ = ["AxisInfo", "TableInfo", "catalog", "table_detail"]


@dataclass(frozen=True)
class AxisInfo:
    """One decoded breakpoint axis of a table (x = columns, y = rows)."""

    units: str
    values: tuple[float, ...]


@dataclass(frozen=True)
class TableInfo:
    """A read-only description of one editable table.

    ``values`` is included so a detail view has the grid without a second call;
    :func:`catalog` fills it lazily-cheaply (the decode is cached on the view).
    ``reversible`` is whether the table can be written back from physical units:
    a linear equation round-trips, a non-linear one is raw-only and the app
    presents it read-only rather than risk a scaled-vs-raw mistake.
    """

    space: str
    name: str                 # logical (profile) name
    symbol: Optional[str]     # A2L symbol, when the table has one
    title: Optional[str]      # XDF title
    description: str          # profile's plain-English description
    key: object               # XDF key used to resolve it (symbol or uniqueid)
    uniqueid_hex: str
    units: str
    shape: tuple[int, int]
    ndim: int                 # 0 scalar · 1 vector · 2 grid
    reversible: bool          # physical-unit writes round-trip (linear equation)
    categories: tuple[str, ...]
    x_axis: Optional[AxisInfo]
    y_axis: Optional[AxisInfo]
    values: tuple            # nested tuples of floats (JSON/data friendly)

    @property
    def id_and_description(self) -> str:
        """``` `ID` — Description ``` — the project's mandated naming form."""
        ident = self.symbol or self.uniqueid_hex
        return f"`{ident}` — {self.description or self.title or '(no description)'}"


def _ndim(shape: tuple[int, int]) -> int:
    rows, cols = shape
    if rows <= 1 and cols <= 1:
        return 0
    if rows <= 1 or cols <= 1:
        return 1
    return 2


def _axis_info(view, which: str) -> Optional[AxisInfo]:
    values = view.axis_values(which)
    if values is None:
        return None
    axis = getattr(view.table, which, None)
    units = (axis.units if axis is not None else "") or ""
    return AxisInfo(units=units, values=tuple(float(v) for v in np.asarray(values).ravel()))


def _reversible(view) -> bool:
    z = view.table.z
    scaling = getattr(z, "scaling", None) if z is not None else None
    # No embedded z (nothing to write) or a non-linear equation → not reversible
    # from physical units.
    if z is None or z.embedded is None:
        return False
    return bool(scaling is None or scaling.is_linear)


def _nested(values: np.ndarray) -> tuple:
    arr = np.asarray(values, dtype=np.float64)
    if arr.ndim <= 1:
        return tuple(float(v) for v in arr.ravel())
    return tuple(tuple(float(v) for v in row) for row in arr)


def _table_info(tune: Tune, space: str, name: str) -> TableInfo:
    resolved = tune.table(name, space=space)
    view = resolved.view
    shape = tuple(view.shape) if view.shape is not None else (1, 1)
    categories = tuple(
        c.name for c in getattr(view.table, "categories", ()) or ()
    ) if hasattr(view.table, "categories") else ()
    return TableInfo(
        space=space,
        name=name,
        symbol=view.symbol,
        title=view.title,
        description=resolved.spec.description if resolved.spec else (view.title or ""),
        key=resolved.spec.key if resolved.spec else (view.symbol or view.uniqueid_hex),
        uniqueid_hex=view.uniqueid_hex,
        units=view.units or "",
        shape=shape,
        ndim=_ndim(shape),
        reversible=_reversible(view),
        categories=categories,
        x_axis=_axis_info(view, "x"),
        y_axis=_axis_info(view, "y"),
        values=_nested(view.values),
    )


def catalog(tune: Tune, *, space: Optional[str] = None) -> list[TableInfo]:
    """Every editable table, as read-only :class:`TableInfo`, in profile order.

    ``space`` restricts to one table space (e.g. ``"patch"``); by default every
    space the tune has is listed. The order is the profile's declared order,
    which is the reviewable order the maps are written in.
    """
    spaces = [space] if space is not None else list(tune.spaces)
    out: list[TableInfo] = []
    for sp in spaces:
        table_space = tune.space(sp)
        for name in table_space.tables.names():
            out.append(_table_info(tune, sp, name))
    return out


def table_detail(tune: Tune, name: str, *, space: str = "base") -> TableInfo:
    """The :class:`TableInfo` for one table, including its current values."""
    return _table_info(tune, space, name)
