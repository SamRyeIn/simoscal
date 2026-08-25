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
    "VERDICT_SUPERSEDED",
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

#: Display-only pseudo-verdict — never stored on an :class:`EditEntry`. The
#: report substitutes it for a bulk-SOP skip whose tables a later applied write
#: covers (see :meth:`Journal.superseded`), so a "skipped" row and an "applied"
#: row for the same table do not read as a contradiction. It is deliberately
#: absent from :data:`_ORDER` so :meth:`Journal.counts` — the raw verdict tally —
#: is unaffected; :meth:`Journal.summary_counts` is the count that reflects it.
VERDICT_SUPERSEDED = "superseded"

_ORDER = (
    VERDICT_APPLIED,
    VERDICT_UNCHANGED,
    VERDICT_GUARDED_SKIP,
    VERDICT_BLOCKED,
    VERDICT_SKIPPED,
)


def _symbol_tokens(entry: "EditEntry") -> frozenset[str]:
    """The symbol names an entry pertains to.

    A domain write names one symbol in :attr:`~EditEntry.name`; a bulk-SOP skip
    that covers several tables joins them ``"A, B, C"`` there. Splitting on
    commas recovers the set for both. The XDF ``key`` is folded in too so a
    write whose logical name differs from its symbol still matches.
    """
    tokens: set[str] = set()
    fields = [entry.name or ""]
    if isinstance(entry.key, str):
        fields.append(entry.key)
    for field in fields:
        for part in field.split(","):
            part = part.strip()
            if part:
                tokens.add(part)
    return frozenset(tokens)


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
    #: The full file-byte extent of the table this entry declared a write over,
    #: whether or not any byte moved. ``offsets`` (measured) is a subset of it.
    #: This is what lets a *restore-to-source* write — one whose target already
    #: equals the build's working buffer, so it stages nothing — still authorise
    #: its table to differ from an earlier revision that changed it. Empty for
    #: entries that are not a physical/raw table write (patches, skips, checks).
    declared: frozenset[int] = frozenset()
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
    def declares_table(self) -> bool:
        """Whether this entry took responsibility for a table's byte extent.

        True for any physical/raw table write, including one that staged no
        bytes because its target already matched the working buffer. Such an
        entry moved nothing versus the build's *source*, yet its table still
        differs from a *previous revision* that changed it — so the audit must
        authorise the declared extent and the readback must pin the saved
        contents, or a legitimate rollback fails as unexplained bytes
        (CR-20260720-02).
        """
        return bool(self.declared)

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

    def mark(self) -> int:
        """How many entries have been recorded — a point to roll back to."""
        return len(self._entries)

    def rollback_to(self, mark: int) -> None:
        """Drop every entry recorded after ``mark``.

        The journal is otherwise append-only, and deliberately so: an edit that
        happened but left no entry is the failure mode the whole build gate
        exists to prevent. This is the one sanctioned way back, for the two
        cases where the edit provably did *not* happen — a guard rejection that
        left the table byte-identical (:func:`~simoscal.tune.editing.apply_op`)
        and a dry run whose bytes are restored with it
        (:meth:`~simoscal.tune.project.Tune.dry_run`). Both put the buffer back
        in the same breath, so the journal and the bytes stay in step.
        """
        del self._entries[mark:]

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
        """Entries whose bytes the audit and readback must account for.

        Includes both entries that *moved* bytes and entries that *declared* a
        table write without moving any (a restore-to-source write): the latter
        still describe a table whose saved contents must be read back and whose
        extent the audit must be allowed to reconcile against a prior revision.
        """
        return tuple(
            e for e in self._entries if e.touched_bytes or e.declares_table
        )

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

    def superseded(self) -> dict[int, tuple[EditEntry, ...]]:
        """Map each bulk-SOP skip index to the later applied writes that cover it.

        The basics SOP is a bulk pass and is *expected* to skip tables a
        revision then writes deliberately by another route (see
        :meth:`blocked`). When that happens the report would otherwise show a
        ``skipped`` row and an ``applied`` row for the same table and read as a
        contradiction. This pairs them — keyed by the skip entry's position — so
        the skip can be shown as :data:`VERDICT_SUPERSEDED` and pointed at the
        write that stands. Only writes *after* the skip count, so the ordering
        the journal preserves (recipe first, override second) is respected.
        """
        writers = [
            (i, _symbol_tokens(e))
            for i, e in enumerate(self._entries)
            if e.verdict == VERDICT_APPLIED and (e.touched_bytes or e.declares_table)
        ]
        out: dict[int, tuple[EditEntry, ...]] = {}
        for i, entry in enumerate(self._entries):
            if entry.kind != KIND_SOP:
                continue
            if entry.verdict not in (VERDICT_SKIPPED, VERDICT_GUARDED_SKIP):
                continue
            symbols = _symbol_tokens(entry)
            if not symbols:
                continue
            covering = tuple(
                self._entries[j] for j, wsyms in writers
                if j > i and (wsyms & symbols)
            )
            if covering:
                out[i] = covering
        return out

    def summary_counts(self) -> dict[str, int]:
        """Verdict tally with superseded SOP skips moved to their own bucket.

        :meth:`counts` is the raw verdict tally; this is the count the report
        header shows, so a skip a later write superseded is not counted among
        the held-back skips (it was not held back). The moved entries collect
        under :data:`VERDICT_SUPERSEDED`.
        """
        counts = dict(self.counts())
        superseded = self.superseded()
        for i in superseded:
            verdict = self._entries[i].verdict
            counts[verdict] = counts.get(verdict, 0) - 1
            if counts[verdict] <= 0:
                counts.pop(verdict, None)
        if superseded:
            counts[VERDICT_SUPERSEDED] = len(superseded)
        return counts

    def blocked(self) -> tuple[EditEntry, ...]:
        """Directly-authored writes a guard rejected — a build gate.

        Excludes recipe entries (:data:`KIND_SOP`). The basics SOP is a bulk
        pass over the whole guide and is *expected* to hit guards on tables a
        revision then writes deliberately by another route; failing the build
        on those would make the recipe unusable. A guard rejecting a call the
        author wrote by hand is different — that intent did not happen, and the
        build must not pretend otherwise.
        """
        return tuple(
            e for e in self._entries
            if e.verdict == VERDICT_BLOCKED and e.kind != KIND_SOP
        )

    def tables_touched(self) -> tuple[tuple[str, Union[str, int]], ...]:
        """``(space, XDF key)`` per byte-touching entry, de-duplicated.

        Keyed on the XDF key rather than the logical name, because one table
        can be journaled under both — a domain call names it logically, the
        basics SOP names it by symbol — and it is still one table.
        """
        seen: dict[tuple[str, Union[str, int]], None] = {}
        for entry in self.touching():
            seen.setdefault((entry.space, entry.key), None)
        return tuple(seen)

    def changed_offsets(self) -> frozenset[int]:
        """Every byte offset the journal *measured* as moved by an edit."""
        offsets: set[int] = set()
        for entry in self._entries:
            offsets |= entry.offsets
        return frozenset(offsets)

    def declared_offsets(self) -> frozenset[int]:
        """Every byte offset the journal declared an explicit table write over.

        Wider than :meth:`changed_offsets` on exactly the restore case: a write
        whose target already equals the build's source stages no bytes, yet
        still authorises its table's extent to differ from an earlier revision.
        The saved contents of every such table are independently pinned by the
        final-bin readback, which is what makes authorising the extent safe.
        """
        offsets: set[int] = set()
        for entry in self._entries:
            offsets |= entry.declared
        return frozenset(offsets)
