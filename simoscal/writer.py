"""Writer: invert scaling, pack, and stage minimal-diff edits into a BinImage.

The write half of the byte↔physical pipeline (plan Decisions 7–10). Given an
:class:`~simoscal.model.Axis` and physical-unit values, it:

1. inverts the linear scaling — ``X = (phys - b) / m`` — rounding to the nearest
   raw integer for integer elements, or keeping the float for float elements;
2. range-checks the raw value against the element width (:class:`RawRangeError`
   on overflow — never a silent wrap);
3. packs to bytes at the element's width + endianness and stages **only** those
   bytes into the :class:`~simoscal.binimage.BinImage` (Decision 10: unedited
   bytes are never touched, which is what guarantees round-trip byte-equality).

Edit-safety (warn+allow, float-bug guard) lives in :mod:`simoscal.safety` and is
applied by the caller (:class:`~simoscal.calfile.TableView`) *before* inversion,
on the physical values, so warnings/guards speak in the user's units.
"""

from __future__ import annotations

import numpy as np

from . import safety
from .binimage import BinImage
from .codec import _require_packed, file_offset_for, numpy_dtype_for
from .model import Axis, EmbeddedData, NonLinearEquationError

__all__ = [
    "physical_to_raw",
    "pack_block",
    "stage_full",
    "stage_cell",
]


def physical_to_raw(axis: Axis, phys_values) -> np.ndarray:
    """Invert an axis's linear scaling: physical units → raw element values.

    Integer elements are rounded to the nearest representable raw int (via
    :meth:`ScalingEquation.to_raw`, round-half-to-even). Float elements keep the
    inverted value as float (``(phys - b) / m``, no rounding). Raises
    :class:`NonLinearEquationError` if the scaling is missing or non-linear —
    physical-unit editing of such a table is refused; use ``set_raw`` instead.
    """
    emb = axis.embedded
    sc = axis.scaling
    if emb is None:
        raise NonLinearEquationError("axis has no embedded data to write")
    if sc is None or not sc.is_linear:
        raise NonLinearEquationError(
            "table scaling is non-linear or absent; edit in physical units is "
            "refused — use set_raw() for raw-only editing."
        )
    phys_arr = np.asarray(phys_values, dtype=np.float64)
    if emb.is_float:
        if sc.m == 0.0:
            raise NonLinearEquationError("scaling slope m is zero; not invertible.")
        return (phys_arr - sc.b) / sc.m  # keep float precision, no rounding
    return sc.to_raw(phys_arr)  # int64, rounded


def pack_block(emb: EmbeddedData, raw_values) -> bytes:
    """Pack a full ``(rows, cols)`` raw array to bytes at the element layout.

    Range-checks integer values first (:class:`RawRangeError` on overflow), then
    casts to the element dtype and serializes in the table's element order
    (column-major when ``emb.column_major``, else row-major) — the exact inverse
    of :func:`~simoscal.codec.decode_raw`.
    """
    arr = np.asarray(raw_values)
    if arr.size != emb.count:
        raise ValueError(
            f"expected {emb.count} values for a {emb.rows}x{emb.cols} table, "
            f"got {arr.size}"
        )
    if not emb.is_float:
        safety.check_raw_fits(emb, arr)
        arr = arr.astype(np.int64)
    grid = arr.reshape(emb.rows, emb.cols).astype(numpy_dtype_for(emb))
    # tobytes(order=...) serializes from the logical (rows, cols) grid regardless
    # of memory layout: "F" emits column-major bytes, "C" row-major.
    return grid.tobytes(order="F" if emb.column_major else "C")


def stage_full(
    axis: Axis,
    binimage: BinImage,
    *,
    base_offset: int,
    base_subtract: bool = False,
    raw_values,
) -> tuple[int, int]:
    """Pack a whole table's raw array and stage it. Returns ``(offset, length)``.

    Unchanged cells serialize identically, so writing the whole block still
    yields a minimal byte-diff versus the original.
    """
    emb = axis.embedded
    _require_packed(emb)
    block = pack_block(emb, raw_values)
    offset = file_offset_for(emb.address, base_offset, base_subtract)
    binimage.write(offset, block)
    return offset, len(block)


def stage_cell(
    axis: Axis,
    binimage: BinImage,
    *,
    base_offset: int,
    base_subtract: bool = False,
    row: int,
    col: int,
    raw_value,
) -> tuple[int, int]:
    """Pack and stage a single cell's bytes only. Returns ``(offset, length)``.

    Writes exactly ``element_bytes`` bytes at the cell's offset — the tightest
    possible diff for a one-cell edit.
    """
    emb = axis.embedded
    _require_packed(emb)
    if not (0 <= row < emb.rows and 0 <= col < emb.cols):
        raise IndexError(
            f"cell ({row},{col}) out of range for {emb.rows}x{emb.cols} table"
        )
    if not emb.is_float:
        safety.check_raw_fits(emb, [raw_value])
        scalar = np.array(raw_value).astype(np.int64)
    else:
        scalar = np.array(raw_value, dtype=np.float64)
    data = scalar.astype(numpy_dtype_for(emb)).tobytes()
    # Linear position of cell (row, col) within the contiguous element run, in the
    # table's element order — must match decode_raw's reshape (column-major: each
    # column's `rows` elements contiguous; row-major: each row's `cols`).
    idx = (col * emb.rows + row) if emb.column_major else (row * emb.cols + col)
    offset = file_offset_for(emb.address, base_offset, base_subtract) + idx * emb.element_bytes
    binimage.write(offset, data)
    return offset, len(data)
