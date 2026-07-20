"""The edit journal — every change a revision makes, as typed data.

A domain call does two things: it moves bytes, and it appends an
:class:`EditEntry` saying what it moved and why. The entry is not a log line —
it is the record the rest of the build is derived from:

* ``report.md`` is rendered from the journal, so what a reviewer reads is
  necessarily what the code did, not a parallel prose description of it;
* the raw-diff audit's allowance set is built from the journal, so an
  unjournaled edit shows up as unexplained bytes and fails the build;
* the final-bin readback re-reads every journaled table and compares it against
  the recorded ``after``, so a write that did not survive the save is caught.

That makes journaling the load-bearing habit of the whole layer: an edit path
that forgets to journal fails loudly rather than shipping quietly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Optional, Union

import numpy as np

__all__ = [
    "EditEntry",
    "Journal",
    "KIND_AXIS",
    "KIND_CELLS",
    "KIND_CHECK",
    "KIND_GUARDED_CEILING",
    "KIND_PATCH",
    "KIND_RAW",
    "KIND_SOP",
    "KIND_TABLE",
    "VERDICT_APPLIED",
    "VERDICT_BLOCKED",
    "VERDICT_GUARDED_SKIP",
    "VERDICT_SKIPPED",
    "VERDICT_UNCHANGED",
    "summarize",
]

# ---- what kind of change this was ----------------------------------------- #
KIND_TABLE = "table"                     # whole grid written in physical units
KIND_CELLS = "cells"                     # selected cells of a grid
KIND_AXIS = "axis"                       # breakpoint (re-breakpoint) write
KIND_RAW = "raw"                         # raw-element write (float-bug tables)
KIND_GUARDED_CEILING = "guarded_ceiling"  # raise a limiter, never lower it
KIND_PATCH = "patch"                     # a .btp patch application
KIND_SOP = "sop"                         # adapted sop_recipe TableOutcome
KIND_CHECK = "check"                     # a recorded verdict; moves no bytes

# ---- how it turned out ------------------------------------------------------ #
VERDICT_APPLIED = "applied"              # bytes staged
VERDICT_UNCHANGED = "unchanged"          # target already met; nothing staged
VERDICT_GUARDED_SKIP = "guarded_skip"    # a guard declined to lower/alter a value
VERDICT_BLOCKED = "blocked"              # a guard rejected the write outright
VERDICT_SKIPPED = "skipped"              # deliberately not done; reason recorded

_ORDER = (
    VERDICT_APPLIED,
    VERDICT_UNCHANGED,
    VERDICT_GUARDED_SKIP,
    VERDICT_BLOCKED,
    VERDICT_SKIPPED,
)


def summarize(values: Optional[np.ndarray], *, max_inline: int = 12) -> str:
    """A one-cell-wide human summary of a table's values.

    Scalars print as themselves, a flat grid as ``flat 0.8``, a short vector
    inline, and anything larger as its range — enough for a reviewer to see a
    change at a glance without a 16×16 grid in a Markdown cell.
    """
    if values is None:
        return ""
    arr = np.asarray(values, dtype=np.float64)
    if arr.size == 0:
        return ""
    flat = arr.ravel()
    if arr.size == 1:
        return f"{flat[0]:.6g}"
    if np.allclose(flat, flat[0], rtol=0, atol=1e-9):
        return f"flat {flat[0]:.6g}"
    if arr.size <= max_inline:
        return ", ".join(f"{v:.6g}" for v in flat)
    return f"{flat.min():.6g}..{flat.max():.6g}"


@dataclass(frozen=True)
class EditEntry:
    """One journaled change.

    ``space`` names which table space (and therefore which XDF) the table lives
    in, so the readback can re-resolve it against the finished bin.

    ``offsets`` is the set of file bytes this edit *actually* changed, measured
    by diffing the table's byte extent across the write rather than inferred
    from which cells the author meant to touch. That measurement is what makes
    the audit allowance exact: a re-encode that quietly moves a byte in a cell
    nobody meant to change is inside the allowance and reported, instead of
    failing the build as a mystery.
    """

    space: str
    name: str                       # logical name
    label: str                      # `ID` — Description
    key: Union[str, int]            # XDF key, for re-resolution
    kind: str
    verdict: str
    units: str = ""
    intent: str = ""                # why, in the author's words
    before: Optional[np.ndarray] = None
    after: Optional[np.ndarray] = None
    offsets: frozenset[int] = frozenset()
    rows_changed: tuple[int, ...] = ()   # display aid; derived from the values
    detail: str = ""
    warning: str = ""

    @property
    def touched_bytes(self) -> bool:
        """Whether this entry moved bytes — measured, not claimed.

        Keyed off the measured offsets rather than the verdict, so the only
        entries the audit and readback take responsibility for are the ones
        that demonstrably changed the buffer. A patch application, a skip, and
        a check all fall out naturally.
        """
        return bool(self.offsets)

    @property
    def cells_changed(self) -> int:
        if self.before is None or self.after is None:
            return 0
        before = np.asarray(self.before, dtype=np.float64)
        after = np.asarray(self.after, dtype=np.float64)
        if before.shape != after.shape:
            return int(after.size)
        return int(np.count_nonzero(~np.isclose(before, after, rtol=0, atol=1e-12)))

    def before_text(self) -> str:
        return summarize(self._changed_region(self.before))

    def after_text(self) -> str:
        return summarize(self._changed_region(self.after))

    def _changed_region(self, values: Optional[np.ndarray]) -> Optional[np.ndarray]:
        """Narrow a summary to the rows that changed, when only some did.

        A whole-table min..max hides a one-row edit completely: flattening the
        full-load row of a boost map moves the table's maximum by a hair and
        looks like noise. Summarizing just the touched rows shows the edit.
        """
        if values is None or not self.rows_changed:
            return values
        arr = np.asarray(values)
        if arr.ndim != 2 or len(self.rows_changed) >= arr.shape[0]:
            return values
        return arr[list(self.rows_changed)]

    def scope_text(self) -> str:
        """``kind``, qualified by which rows moved when it was not all of them."""
        after = np.asarray(self.after) if self.after is not None else None
        if (
            not self.rows_changed
            or after is None
            or after.ndim != 2
            or len(self.rows_changed) >= after.shape[0]
        ):
            return self.kind
        rows = ", ".join(str(r) for r in self.rows_changed)
        plural = "rows" if len(self.rows_changed) > 1 else "row"
        return f"{self.kind} ({plural} {rows})"


class Journal:
    """Append-only sequence of :class:`EditEntry`, in the order calls were made."""

    def __init__(self) -> None:
        self._entries: list[EditEntry] = []

    def record(self, entry: EditEntry) -> EditEntry:
        self._entries.append(entry)
        return entry

    @property
    def entries(self) -> tuple[EditEntry, ...]:
        return tuple(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self) -> Iterator[EditEntry]:
        return iter(self._entries)

    def __bool__(self) -> bool:
        return bool(self._entries)

    def touching(self) -> tuple[EditEntry, ...]:
        """Entries whose bytes the audit and readback must account for."""
        return tuple(e for e in self._entries if e.touched_bytes)

    def by_space(self, space: str) -> tuple[EditEntry, ...]:
        return tuple(e for e in self._entries if e.space == space)

    def spaces(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for entry in self._entries:
            seen.setdefault(entry.space, None)
        return tuple(seen)

    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for entry in self._entries:
            counts[entry.verdict] = counts.get(entry.verdict, 0) + 1
        return {v: counts[v] for v in _ORDER if v in counts} | {
            k: v for k, v in counts.items() if k not in _ORDER
        }

    def blocked(self) -> tuple[EditEntry, ...]:
        """Entries a guard rejected — a build gate, not just a report row."""
        return tuple(e for e in self._entries if e.verdict == VERDICT_BLOCKED)

    def tables_touched(self) -> tuple[tuple[str, str], ...]:
        """``(space, logical name)`` per byte-touching entry, de-duplicated.

        The readback set: every table the build must re-read off the saved bin
        and compare against its recorded ``after``.
        """
        seen: dict[tuple[str, str], None] = {}
        for entry in self.touching():
            seen.setdefault((entry.space, entry.name), None)
        return tuple(seen)

    def changed_offsets(self) -> frozenset[int]:
        """Every byte offset the journal claims responsibility for."""
        offsets: set[int] = set()
        for entry in self._entries:
            offsets |= entry.offsets
        return frozenset(offsets)
