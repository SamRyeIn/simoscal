"""Read-only table catalog and detail models for the editing surface.

An editing client browses and edits only the tables the SC8S50 (+ switch-patch)
profiles resolve — the curated set, not all ~3,800 XDF tables. That is a safety
choice: every table a user can reach came through a profile map, so its plain-
English description, units, and guard tags are always in force, and a stranger
table with a surprising layout is simply not offered.

The generic catalog is narrower still: a table whose spec names an ``owner`` is
writable only through that domain call, so :func:`catalog` omits it rather than
offering a grid the engine will refuse.

Everything here is read-only. A :class:`TableInfo` is a description a Compose
screen (or a test) renders; the *edits* live in :mod:`simoscal.tune.editing` and
the domain modules. Keeping the two apart means listing the catalog can never
change a byte.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np

from .profile import TAG_AXIS
from .project import Tune
from .quantities import axis_label, table_signature, units_label

__all__ = ["AxisInfo", "TableInfo", "catalog", "table_detail"]


@dataclass(frozen=True)
class AxisInfo:
    """One decoded breakpoint axis of a table (x = columns, y = rows).

    ``symbol`` and ``label`` are what make the axis nameable. The breakpoints
    alone say how the table is indexed but not *on what* — and an editor that
    cannot say which quantity a column stands for is one where someone edits the
    wrong column. See :mod:`simoscal.tune.quantities` for where the English
    comes from and what happens when it is not known.
    """

    units: str
    values: tuple[float, ...]
    #: The axis's own A2L symbol, from the standalone breakpoint table it is
    #: embedded from, or ``None`` when the XDF records no link.
    symbol: Optional[str] = None
    #: ``Quantity [unit]`` — the label an editor puts beside the breakpoints.
    label: str = ""


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
    is_axis: bool             # a breakpoint axis: writes must strictly increase
    #: The domain call that owns writes to this table, or ``""`` when the generic
    #: editor may write it. A non-empty owner means :func:`catalog` leaves the
    #: table out by default and ``apply_op`` refuses it.
    owner: str
    #: The domain heading this table is filed under — one of
    #: :data:`~simoscal.tune.profile.GROUPS`, or ``""`` for a table whose spec
    #: declares none (only ever an owner-locked one; see ``Profile.ungrouped``).
    #:
    #: This is the profile's curated group, deliberately **not** ``categories``.
    #: The XDF's own categories classify by shape as much as by domain — a
    #: table's axis is filed under "Axis", away from the map it indexes — and
    #: where they classify by domain they disagree with the tuner. Both are
    #: carried: ``group`` is what an editor groups by, ``categories`` is what the
    #: XDF said, and a search may match either.
    group: str
    categories: tuple[str, ...]
    x_axis: Optional[AxisInfo]
    y_axis: Optional[AxisInfo]
    values: tuple            # nested tuples of floats (JSON/data friendly)
    #: ``units`` spelled out — a bare XDF ``-`` becomes "dimensionless", so an
    #: intentionally unitless ratio never reads as missing metadata.
    units_description: str = ""
    #: The table as the **imported bin** held it, before this session wrote
    #: anything — the "stock ghost" an editor draws behind a working curve.
    #:
    #: ``None`` means not requested (:func:`catalog` omits it: decoding a second
    #: copy of every table to list them would be paid for by every browse) or not
    #: available, which is the honest answer when a session was recovered rather
    #: than opened and the pre-edit buffer is not in hand. A screen must treat
    #: ``None`` as "no ghost to draw", never as "unchanged".
    source_values: Optional[tuple] = None
    #: One line saying what the table *is*: cell unit against its axes, e.g.
    #: ``"hPa vs. Engine speed [rpm] and Manifold pressure setpoint [hPa]"``.
    #: The title names the table; this names its dimensions.
    signature: str = ""

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


def _axis_info(view, which: str, model) -> Optional[AxisInfo]:
    values = view.axis_values(which)
    if values is None:
        return None
    axis = getattr(view.table, which, None)
    units = (axis.units if axis is not None else "") or ""
    symbol = _axis_symbol(axis, model)
    return AxisInfo(
        units=units,
        values=tuple(float(v) for v in np.asarray(values).ravel()),
        symbol=symbol,
        label=axis_label(symbol, units),
    )


def _axis_symbol(axis, model) -> Optional[str]:
    """The A2L symbol of the standalone breakpoint table ``axis`` embeds.

    A breakpoint axis is stored once and shared: the symbol that names the
    quantity lives on that standalone table, not on the ``XDFAXIS`` element of
    every table that references it. A missing or dangling link yields ``None``
    rather than an error — an unlabelled axis is a worse editor, not an unsafe
    one.
    """
    link = getattr(axis, "link_uniqueid", None) if axis is not None else None
    if link is None or model is None:
        return None
    linked = model.by_id.get(link)
    return linked.symbol if linked is not None else None


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


def _source_values(tune: Tune, space: str, name: str) -> Optional[tuple]:
    """The table's values in the buffer the build started from, or ``None``.

    Read through :meth:`~simoscal.tune.project.Tune.source_space` — one
    read-only decoder per table space, built once and reused — so listing a
    whole catalog's ghosts costs one copy of the source buffer rather than one
    per table. That decoder holds the decode cache too, so asking twice for one
    table's ghost decodes it once.

    Every failure path returns ``None`` rather than raising or guessing. A ghost
    is a nicety; a table detail that could not be read at all because its
    optional reference copy would not decode is a real editing surface lost to a
    decoration.
    """
    try:
        cal = tune.source_space(space)
        if cal is None:
            return None
        return _nested(cal.get(tune.space(space).tables[name].spec.key).values)
    except Exception:  # noqa: BLE001 - a missing ghost must never break the read
        return None


def _table_info(
    tune: Tune, space: str, name: str, *, include_source: bool = False
) -> TableInfo:
    resolved = tune.table(name, space=space)
    view = resolved.view
    model = getattr(tune.space(space).cal, "model", None)
    shape = tuple(view.shape) if view.shape is not None else (1, 1)
    units = view.units or ""
    x_axis = _axis_info(view, "x", model)
    y_axis = _axis_info(view, "y", model)
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
        units=units,
        shape=shape,
        ndim=_ndim(shape),
        reversible=_reversible(view),
        # Surfaced so an editor can label an axis and check monotonicity as the
        # value is typed. The engine enforces it regardless (``apply_op`` rejects
        # a non-increasing axis outright) — this only lets the refusal arrive at
        # the keystroke instead of at Apply.
        is_axis=bool(resolved.has(TAG_AXIS)),
        owner=resolved.owner,
        group=resolved.group,
        categories=categories,
        x_axis=x_axis,
        y_axis=y_axis,
        values=_nested(view.values),
        source_values=_source_values(tune, space, name) if include_source else None,
        units_description=units_label(units),
        signature=table_signature(
            units,
            x_axis.label if x_axis else None,
            y_axis.label if y_axis else None,
            count=shape[0] * shape[1],
            is_axis=bool(resolved.has(TAG_AXIS)),
        ),
    )


def catalog(
    tune: Tune,
    *,
    space: Optional[str] = None,
    include_domain_owned: bool = False,
) -> list[TableInfo]:
    """Every **generically editable** table, as read-only :class:`TableInfo`.

    ``space`` restricts to one table space (e.g. ``"patch"``); by default every
    space the tune has is listed. The order is the profile's declared order,
    which is the reviewable order the maps are written in.

    Domain-owned tables (a non-empty :attr:`TableInfo.owner`) are left out. This
    is the catalog the generic grid editor browses, and offering a table it is
    not allowed to write would be an invitation to compose a proposal that can
    only ever be refused — worse, before CR-20260813-01 it was not refused at
    all. Pass ``include_domain_owned=True`` to list them anyway, for inspection
    and tests; :func:`table_detail` still reads any table by name, since reading
    one has never been the hazard.
    """
    spaces = [space] if space is not None else list(tune.spaces)
    out: list[TableInfo] = []
    for sp in spaces:
        table_space = tune.space(sp)
        for name in table_space.tables.names():
            info = _table_info(tune, sp, name)
            if info.owner and not include_domain_owned:
                continue
            out.append(info)
    return out


def table_detail(tune: Tune, name: str, *, space: str = "base") -> TableInfo:
    """The :class:`TableInfo` for one table, with current *and* source values.

    Unlike :func:`catalog`, this carries :attr:`TableInfo.source_values` — what
    the imported bin held before this session touched the table. One table's
    second decode is cheap; the whole catalog's would be paid for on every
    browse, which is why the list form does without it.
    """
    return _table_info(tune, space, name, include_source=True)
