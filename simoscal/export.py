"""Export module: turn a selection of tables into flat-file output (CSV/xlsx).

Read-only and additive — does not touch ``writer.py``, ``checksum.py``, or any
bin-mutation path. :func:`select_tables` resolves a caller's selection spec
(explicit symbols/titles, a category, or "all") into a deduplicated list of
:class:`~simoscal.calfile.TableView`; :func:`render_table` (U1) then turns
each into a :class:`~simoscal.render.RenderedTable` for the writers.
"""

from __future__ import annotations

from typing import Optional, Sequence

from .calfile import CalFile, TableView

__all__ = ["select_tables"]


def select_tables(
    cal: CalFile,
    *,
    symbols: Optional[Sequence[str]] = None,
    category: Optional[str] = None,
    all_tables: bool = False,
) -> list[TableView]:
    """Resolve a selection spec into a deduplicated ``list[TableView]``.

    ``symbols`` entries resolve via :meth:`CalFile.get`, reusing its existing
    ``KeyError``/``AmbiguousTableError`` semantics rather than reimplementing
    lookup. ``category`` filters :meth:`CalFile.unique_tables` by
    ``table.categories`` client-side (no new ``CalFile`` query surface — this
    need is local to export). ``all_tables=True`` returns
    ``cal.unique_tables()`` verbatim. Results from multiple selection inputs
    are unioned by ``uniqueid`` (same dedup semantics as
    :meth:`CalFile.unique_tables`), so a table matched by both an explicit
    symbol and a category filter is emitted once.

    Calling with none of ``symbols``/``category``/``all_tables`` is a usage
    error, since there is no sensible default selection.
    """
    if not symbols and not category and not all_tables:
        raise ValueError(
            "select_tables() requires at least one of symbols, category, "
            "or all_tables=True"
        )

    selected: dict[int, TableView] = {}

    if all_tables:
        for view in cal.unique_tables():
            selected[view.uniqueid] = view

    if category is not None:
        for view in cal.unique_tables():
            if any(c.name == category for c in view.table.categories):
                selected[view.uniqueid] = view

    if symbols:
        for key in symbols:
            view = cal.get(key)
            selected[view.uniqueid] = view

    return list(selected.values())
