"""Generic, app-neutral edit operations over a table selection.

The domain modules (:mod:`~simoscal.tune.domains`) express *what a tuner means*
for the safety-critical tables. This module is the other half: the plain
grid-editor operations a UI needs — set a cell, scale a selection, fill, paste,
interpolate, restore — over any reversible table in the catalog, each one
journaled exactly like a domain call so it flows through the same build → verify
→ audit.

Two properties matter and are guaranteed here:

* **Atomic.** An operation computes the whole target array first and writes it in
  one call. If a guard rejects the result, the table is left byte-identical *and*
  the journal is left unchanged — a rejected edit never leaves a ``blocked`` entry
  behind that would later fail the build. A rejection raises
  :class:`EditRejected` with the reason, so the UI can show it.
* **Requested-vs-encoded is reported.** A physical value rarely lands on an exact
  representable step, so what encodes into the bin can differ from what was asked
  (1500 hPa → 1499.978). Every :class:`EditResult` carries both, so the UI can
  show "requested 1500, encoded 1499.98" rather than silently rounding.

Non-reversible (non-linear) tables are refused here — they are raw-only, and a
physical-unit edit of one would be off by the scaling. The catalog marks them
``reversible=False`` so the UI never offers this path for them.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Sequence, Union

import numpy as np

from .journal import EditEntry, KIND_CELLS, VERDICT_BLOCKED
from .project import Tune

__all__ = ["EditOp", "Selection", "EditResult", "EditRejected", "apply_op"]


class EditRejected(Exception):
    """An edit was refused; the table and journal are unchanged."""


class EditOp(str, Enum):
    """The generic operations. ``FILL`` is ``SET`` with a scalar over a selection."""

    SET = "set"
    ADD = "add"
    SUB = "sub"
    MUL = "mul"
    DIV = "div"
    FILL = "fill"
    INTERPOLATE = "interpolate"
    PASTE = "paste"
    RESTORE = "restore"


@dataclass(frozen=True)
class Selection:
    """Which cells an operation touches, as a boolean mask over the table.

    Construct with the classmethods; they all resolve to a mask at apply time,
    validated against the table's real shape so an out-of-range row/col fails
    loud rather than silently touching nothing.
    """

    kind: str
    args: tuple = ()

    @classmethod
    def all(cls) -> "Selection":
        return cls("all")

    @classmethod
    def cells(cls, cells: Sequence[tuple[int, int]]) -> "Selection":
        return cls("cells", (tuple((int(r), int(c)) for r, c in cells),))

    @classmethod
    def row(cls, row: int) -> "Selection":
        return cls("row", (int(row),))

    @classmethod
    def col(cls, col: int) -> "Selection":
        return cls("col", (int(col),))

    @classmethod
    def region(cls, r0: int, r1: int, c0: int, c1: int) -> "Selection":
        """Rows ``[r0, r1]`` × cols ``[c0, c1]``, inclusive."""
        return cls("region", (int(r0), int(r1), int(c0), int(c1)))

    def mask(self, shape: tuple[int, int]) -> np.ndarray:
        rows, cols = shape
        m = np.zeros(shape, dtype=bool)
        if self.kind == "all":
            m[:, :] = True
        elif self.kind == "row":
            (r,) = self.args
            _check(0 <= r < rows, f"row {r} out of range for shape {shape}")
            m[r, :] = True
        elif self.kind == "col":
            (c,) = self.args
            _check(0 <= c < cols, f"col {c} out of range for shape {shape}")
            m[:, c] = True
        elif self.kind == "region":
            r0, r1, c0, c1 = self.args
            _check(0 <= r0 <= r1 < rows and 0 <= c0 <= c1 < cols,
                   f"region {self.args} out of range for shape {shape}")
            m[r0:r1 + 1, c0:c1 + 1] = True
        elif self.kind == "cells":
            (cells,) = self.args
            for r, c in cells:
                _check(0 <= r < rows and 0 <= c < cols,
                       f"cell ({r},{c}) out of range for shape {shape}")
                m[r, c] = True
        else:  # pragma: no cover - unreachable
            raise EditRejected(f"unknown selection kind {self.kind!r}")
        if not m.any():
            raise EditRejected("selection is empty")
        return m


@dataclass(frozen=True)
class EditResult:
    """The outcome of one applied operation, including requested-vs-encoded."""

    entry: EditEntry
    requested: np.ndarray     # the full target array we asked to write
    encoded: np.ndarray       # what the bin actually holds now (re-decoded)
    warning: str

    @property
    def quantized(self) -> bool:
        """Whether any cell encoded to a different value than requested."""
        return not np.array_equal(
            np.asarray(self.requested, dtype=np.float64),
            np.asarray(self.encoded, dtype=np.float64),
        )

    def max_abs_quantization(self) -> float:
        """Largest |requested − encoded| across the table (0 if exact)."""
        req = np.asarray(self.requested, dtype=np.float64)
        enc = np.asarray(self.encoded, dtype=np.float64)
        return float(np.max(np.abs(req - enc))) if req.shape == enc.shape else float("nan")


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise EditRejected(msg)


def _restore_target(tune: Tune, name: str, space: str, current: np.ndarray) -> np.ndarray:
    """The source (session-start) values for a table, from the journal.

    The earliest journal entry that wrote this table recorded its ``before`` —
    which, at session start, is the imported bin's value. If the table was never
    edited, it is already at source, so ``current`` is returned unchanged.
    """
    key = tune.table(name, space=space).spec.key
    for entry in tune.journal.entries:
        if entry.space == space and entry.key == key and entry.before is not None:
            return np.asarray(entry.before, dtype=np.float64).reshape(current.shape)
    return current


def _interpolate(target: np.ndarray, mask: np.ndarray) -> None:
    """Linearly ramp the selected cells between their two endpoints, in place.

    The selection must lie on a single row or a single column and be contiguous;
    the two endpoint cells keep their current values and the interior is filled
    on a straight line. This is the "smooth a run" operation the boost curve and
    grid editors use.
    """
    idx = np.argwhere(mask)
    rows = np.unique(idx[:, 0])
    cols = np.unique(idx[:, 1])
    if rows.size == 1:
        r = int(rows[0])
        cs = np.sort(idx[:, 1])
        _check(np.array_equal(cs, np.arange(cs[0], cs[-1] + 1)),
               "interpolate needs a contiguous run of cells")
        target[r, cs[0]:cs[-1] + 1] = np.linspace(
            target[r, cs[0]], target[r, cs[-1]], cs[-1] - cs[0] + 1)
    elif cols.size == 1:
        c = int(cols[0])
        rs = np.sort(idx[:, 0])
        _check(np.array_equal(rs, np.arange(rs[0], rs[-1] + 1)),
               "interpolate needs a contiguous run of cells")
        target[rs[0]:rs[-1] + 1, c] = np.linspace(
            target[rs[0], c], target[rs[-1], c], rs[-1] - rs[0] + 1)
    else:
        raise EditRejected("interpolate needs a selection on one row or one column")


def apply_op(
    tune: Tune,
    name: str,
    op: Union[EditOp, str],
    *,
    space: str = "base",
    selection: Optional[Selection] = None,
    value: Optional[float] = None,
    array: Optional[Sequence] = None,
    intent: str = "",
) -> EditResult:
    """Apply ``op`` to ``name`` over ``selection`` and journal it, atomically.

    ``value`` is the scalar for SET/FILL/ADD/SUB/MUL/DIV; ``array`` is the source
    grid for PASTE (and an optional elementwise operand for the arithmetic ops).
    Returns an :class:`EditResult` with requested-vs-encoded. Raises
    :class:`EditRejected` — leaving the table and journal untouched — on a bad
    selection, a non-reversible table, a division by zero, a non-finite result,
    or a guard rejection.
    """
    op = EditOp(op)
    resolved = tune.table(name, space=space)
    if not _is_reversible(resolved.view):
        raise EditRejected(
            f"{resolved.label} is not reversible from physical units "
            "(non-linear or no embedded data); it is raw-only and cannot be "
            "edited through this generic path."
        )

    current = np.asarray(tune.values(name, space=space), dtype=np.float64)
    sel = selection or Selection.all()
    mask = sel.mask(current.shape)
    target = current.copy()

    operand = None
    if array is not None:
        operand = np.asarray(array, dtype=np.float64)

    if op in (EditOp.SET, EditOp.FILL):
        if value is not None:
            target[mask] = float(value)
        elif operand is not None:
            _check(operand.size == int(mask.sum()) or operand.shape == current.shape,
                   "SET array does not match the selection size")
            target[mask] = operand.ravel() if operand.size == int(mask.sum()) \
                else operand[mask]
        else:
            raise EditRejected("SET/FILL needs a value or an array")
    elif op in (EditOp.ADD, EditOp.SUB, EditOp.MUL, EditOp.DIV):
        rhs = _operand_for(op, value, operand, mask, current.shape)
        if op is EditOp.ADD:
            target[mask] = current[mask] + rhs
        elif op is EditOp.SUB:
            target[mask] = current[mask] - rhs
        elif op is EditOp.MUL:
            target[mask] = current[mask] * rhs
        else:  # DIV
            if np.any(np.asarray(rhs) == 0):
                raise EditRejected("division by zero")
            target[mask] = current[mask] / rhs
    elif op is EditOp.PASTE:
        _check(operand is not None, "PASTE needs an array")
        _check(operand.size == int(mask.sum()) or operand.shape == current.shape,
               "PASTE array does not match the selection")
        target[mask] = operand.ravel() if operand.size == int(mask.sum()) else operand[mask]
    elif op is EditOp.INTERPOLATE:
        _interpolate(target, mask)
    elif op is EditOp.RESTORE:
        source = _restore_target(tune, name, space, current)
        target[mask] = source[mask]

    if not np.all(np.isfinite(target)):
        raise EditRejected("result contains non-finite values")

    # Atomic write. A guard rejection journals a blocked entry and leaves the
    # table byte-identical; we roll that entry back so the failed edit leaves no
    # trace — the definition of atomic here.
    n_before = len(tune.journal)
    entry = tune.write(
        name, target, space=space, kind=KIND_CELLS,
        intent=intent or f"{op.value} over {_describe(sel)}",
    )
    if entry.verdict == VERDICT_BLOCKED:
        del tune.journal._entries[n_before:]
        raise EditRejected(f"{resolved.label}: {entry.detail or 'guard rejected the edit'}")

    encoded = np.asarray(tune.values(name, space=space), dtype=np.float64)
    return EditResult(entry=entry, requested=target, encoded=encoded, warning=entry.warning)


def _operand_for(op, value, operand, mask, shape) -> Union[float, np.ndarray]:
    if value is not None:
        return float(value)
    if operand is not None:
        if operand.shape == shape:
            return operand[mask]
        if operand.size == int(mask.sum()):
            return operand.ravel()
    raise EditRejected(f"{op.value} needs a value or a matching array")


def _is_reversible(view) -> bool:
    z = view.table.z
    if z is None or z.embedded is None:
        return False
    scaling = getattr(z, "scaling", None)
    return bool(scaling is None or scaling.is_linear)


def _describe(sel: Selection) -> str:
    if sel.kind == "all":
        return "the whole table"
    if sel.kind == "row":
        return f"row {sel.args[0]}"
    if sel.kind == "col":
        return f"column {sel.args[0]}"
    if sel.kind == "region":
        return f"rows {sel.args[0]}–{sel.args[1]}, cols {sel.args[2]}–{sel.args[3]}"
    if sel.kind == "cells":
        return f"{len(sel.args[0])} cell(s)"
    return "a selection"
