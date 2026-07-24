"""Export module: turn a selection of tables into flat-file output (CSV/xlsx).

Read-only and additive — does not touch ``writer.py``, ``checksum.py``, or any
bin-mutation path. :func:`select_tables` resolves a caller's selection spec
(explicit symbols/titles, a category, or "all") into a deduplicated list of
:class:`~simoscal.calfile.TableView`; :func:`render_table` (U1) then turns
each into a :class:`~simoscal.render.RenderedTable` for the writers.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Optional, Sequence, Union

from .calfile import CalFile, TableView
from .render import RenderedTable, render_table

__all__ = ["select_tables", "write_csv", "write_xlsx", "export_tables"]

_MAX_SHEET_NAME_LEN = 31
_INVALID_SHEET_CHARS = set("[]:*?/\\")


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


def _grid_rows(rt: RenderedTable) -> list[list]:
    """The grid rows for one ``RenderedTable``, shape-driven (no header for 1x1).

    Shared by both writers so CSV and xlsx stay trivially consistent — a
    format-agnostic list of rows, each a plain list of cell values.
    """
    rows, cols = rt.values.shape
    if rows == 1 and cols == 1:
        return [[rt.values[0, 0]]]
    if rt.y_labels is None:
        # Single row (1D, no y-axis): a bare header row + a bare data row —
        # no spurious leading blank cell for the missing row axis.
        return [list(rt.x_labels), list(rt.values[0])]
    grid = [[""] + list(rt.x_labels)]
    for i, y in enumerate(rt.y_labels):
        grid.append([y] + list(rt.values[i]))
    return grid


def _table_block_rows(rt: RenderedTable) -> list[list]:
    """A metadata row followed by the table's grid rows.

    The metadata row is ``symbol, title, x_units, y_units, units`` — axis
    units in x, y, z order, riding alongside the metadata rather than
    cluttering the grid header.
    """
    meta = [
        rt.symbol or "", rt.title or "",
        rt.x_units or "", rt.y_units or "", rt.units or "",
    ]
    return [meta, *_grid_rows(rt)]


def write_csv(tables: Sequence[RenderedTable], path: Union[str, Path]) -> None:
    """Write ``tables`` to a single CSV file as stacked, labeled grid blocks.

    Each table is preceded by a metadata row (symbol, title, x_units, y_units,
    units) and followed by a blank separator line, in call order. Values are
    written at full precision via Python's default float formatting — no
    rounding. Encoded ``utf-8-sig`` (a UTF-8 BOM) so spreadsheet apps that
    guess encoding by heuristic (Excel/Numbers) detect UTF-8 instead of
    mangling non-ASCII units (e.g. ``°CRK``) as Mac Roman/CP1252.
    """
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        for rt in tables:
            for row in _table_block_rows(rt):
                w.writerow(row)
            w.writerow([])


def _sanitize_sheet_name(name: str, used: set[str]) -> str:
    """A workbook-safe, unique sheet name truncated to Excel's 31-char limit."""
    cleaned = "".join("_" if c in _INVALID_SHEET_CHARS else c for c in name)
    cleaned = cleaned[:_MAX_SHEET_NAME_LEN] or "Sheet"
    candidate = cleaned
    n = 1
    while candidate in used:
        suffix = f"~{n}"
        candidate = cleaned[: _MAX_SHEET_NAME_LEN - len(suffix)] + suffix
        n += 1
    used.add(candidate)
    return candidate


def write_xlsx(tables: Sequence[RenderedTable], path: Union[str, Path]) -> None:
    """Write ``tables`` to a single xlsx workbook, sheets grouped by XDF category.

    One sheet per category represented across ``tables`` (union), named from
    the category and sanitized/truncated to Excel's 31-character sheet-name
    limit. A table in N categories is written onto N sheets in full — not
    linked or referenced once — matching how TunerPro itself cross-lists a
    table. A table with no categories is not written to any sheet (grouping
    is by category; there is nothing to group it under).
    """
    categories: list[str] = []
    seen: set[str] = set()
    for rt in tables:
        for cat in rt.categories:
            if cat not in seen:
                seen.add(cat)
                categories.append(cat)

    try:
        import openpyxl
    except ImportError as exc:
        raise ImportError(
            "xlsx export requires the 'export' extra; install simoscal[export]"
        ) from exc

    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    used_names: set[str] = set()
    for cat in categories:
        ws = wb.create_sheet(title=_sanitize_sheet_name(cat, used_names))
        for rt in tables:
            if cat in rt.categories:
                for row in _table_block_rows(rt):
                    ws.append(row)
                ws.append([])
    wb.save(str(path))


def export_tables(
    cal: CalFile,
    path: Union[str, Path],
    *,
    symbols: Optional[Sequence[str]] = None,
    category: Optional[str] = None,
    all_tables: bool = False,
) -> None:
    """Select, render, and write tables to ``path`` in one call.

    Resolves the selection (:func:`select_tables`), renders every match
    (:func:`~simoscal.render.render_table`), and dispatches to :func:`write_csv`
    or :func:`write_xlsx` by ``path``'s suffix. An unrecognized suffix raises
    ``ValueError`` rather than guessing a format.
    """
    views = select_tables(cal, symbols=symbols, category=category, all_tables=all_tables)
    tables = [render_table(v) for v in views]

    suffix = Path(path).suffix.lower()
    if suffix == ".csv":
        write_csv(tables, path)
    elif suffix == ".xlsx":
        write_xlsx(tables, path)
    else:
        raise ValueError(
            f"unrecognized export suffix {suffix!r} for {path!r}; expected .csv or .xlsx"
        )
