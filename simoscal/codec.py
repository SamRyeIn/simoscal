"""Codec: decode a table/axis's raw bytes into a numpy array of physical values.

This is the read half of the byte↔physical pipeline (plan Decisions 5–7). Given
an :class:`~simoscal.model.Axis` (or :class:`~simoscal.model.Table`) and a
:class:`~simoscal.binimage.BinImage`, it:

1. computes ``file_offset = address ± BASEOFFSET`` (Decision 5);
2. reads the exact byte extent, region-checked (raises
   :class:`RegionBoundsError` on overrun);
3. interprets each element by width + endianness + signed/float
   (``mmedtypeflags`` bits ``0x02``/``0x04``/``0x10000``, Decision 6);
4. shapes the result ``(rows, cols)`` and applies the linear scaling
   (``phys = m·X + b``, Decision 7).

**Layout — packed contiguous; element order per ``column_major``.** Empirically
every axis in ``SC8S50.V1.0.xdf`` is tightly packed: ``mmedminorstridebits`` is
always ``0`` and ``mmedmajorstridebits`` always equals the element size (it is
*not* a row stride here). So cells are contiguous, and the ``0x04``
``mmedtypeflags`` bit (``column_major``) selects whether that contiguous run is
column-major (Y fastest — the case for every 2D data table in ``V1.0``) or
row-major. Rather than guess at an unfamiliar stride pattern, the codec **fails
loud** (:class:`CodecError`) on any stride that isn't packed — a mis-decode would
silently corrupt a tune, which the safety mandate forbids.
"""

from __future__ import annotations

from typing import Optional

import numpy as np

from .binimage import BinImage
from .model import Axis, EmbeddedData, RegionBoundsError, SimosCalError, Table

__all__ = [
    "CodecError",
    "file_offset_for",
    "numpy_dtype_for",
    "decode_raw",
    "decode_physical",
]


class CodecError(SimosCalError):
    """Raised when an embedded layout cannot be decoded safely.

    Covers unhandled (non-packed) stride patterns — anything the codec can't
    prove it will read correctly. Fail loud instead of mis-decoding.
    """


def file_offset_for(address: int, base_offset: int, base_subtract: bool) -> int:
    """Map an XDF ``mmedaddress`` to an absolute file offset (Decision 5).

    ``file_offset = address - base`` when ``subtract`` is set, else
    ``address + base``. For ``SC8S50`` the observed case is add, base
    ``0x200000`` (calibration lives in the upper 2 MB).
    """
    return address - base_offset if base_subtract else address + base_offset


def numpy_dtype_for(emb: EmbeddedData) -> np.dtype:
    """The numpy dtype that decodes one element per ``mmedtypeflags`` + width.

    Endianness from ``little_endian`` (``0x02``), sign from ``signed``
    (``0x04``), float from ``is_float`` (``0x10000``); width from ``elem_bits``.
    """
    endian = "<" if emb.little_endian else ">"
    if emb.is_float:
        # EmbeddedData already guarantees a float is 32-bit.
        return np.dtype(f"{endian}f4")
    kind = "i" if emb.signed else "u"
    return np.dtype(f"{endian}{kind}{emb.element_bytes}")


def _require_packed(emb: EmbeddedData) -> None:
    """Assert the layout is packed contiguous; raise :class:`CodecError` if not.

    Packed means: no gap between columns (``minor_stride_bits == 0``) and the
    major stride is one of the packed conventions — ``0`` (implicit),
    ``elem_bits`` (a2l2xdf's per-element value), or ``cols * elem_bits`` (a true
    row stride with no inter-row gap). Any other value implies a strided layout
    the codec does not handle, and it refuses rather than guess.
    """
    if emb.minor_stride_bits != 0:
        raise CodecError(
            f"unhandled minor stride {emb.minor_stride_bits} bits "
            f"(only packed columns, minor_stride=0, are supported)"
        )
    packed_major = (0, emb.elem_bits, emb.cols * emb.elem_bits)
    if emb.major_stride_bits not in packed_major:
        raise CodecError(
            f"unhandled major stride {emb.major_stride_bits} bits for a "
            f"{emb.rows}x{emb.cols} {emb.elem_bits}-bit table "
            f"(expected one of {packed_major}, i.e. packed contiguous)"
        )


def decode_raw(
    axis: Axis,
    binimage: BinImage,
    *,
    base_offset: int,
    base_subtract: bool = False,
) -> np.ndarray:
    """Decode an axis's embedded bytes into a ``(rows, cols)`` array of raw ints.

    Raw = the on-bin integer/float element values, *before* scaling. Raises
    :class:`CodecError` if the axis has no embedded data or an unhandled stride,
    and :class:`RegionBoundsError` if the extent overruns the region.
    """
    emb = axis.embedded
    if emb is None:
        raise CodecError(
            f"axis {axis.axis_id!r} has no embedded data — nothing to decode"
        )
    _require_packed(emb)

    offset = file_offset_for(emb.address, base_offset, base_subtract)
    extent = emb.count * emb.element_bytes
    try:
        raw = binimage.read(offset, extent)
    except RegionBoundsError as exc:
        raise RegionBoundsError(
            f"decoding axis {axis.axis_id!r} @ address {emb.address:#x}: {exc}"
        ) from exc

    dtype = numpy_dtype_for(emb)
    flat = np.frombuffer(raw, dtype=dtype)
    if emb.column_major:
        # Column-major on-bin order: each column's `rows` elements are contiguous
        # (Y fastest). Read as (cols, rows) then transpose to logical (rows, cols).
        arr = flat.reshape(emb.cols, emb.rows).T
    else:
        arr = flat.reshape(emb.rows, emb.cols)
    # frombuffer returns a read-only view over the bytes; copy so the caller
    # owns a mutable, contiguous array and the BinImage buffer is not aliased.
    return np.array(arr)


def decode_physical(
    axis: Axis,
    binimage: BinImage,
    *,
    base_offset: int,
    base_subtract: bool = False,
) -> np.ndarray:
    """Decode an axis into physical units: raw decoded, then linear scaling.

    Returns a float64 ``(rows, cols)`` array. When the axis scaling is linear,
    applies ``phys = m·X + b``. When there is no scaling or it is non-linear,
    falls back to the raw values as float64 (raw-only read) — never a silent
    approximation of a non-linear curve.
    """
    raw = decode_raw(axis, binimage, base_offset=base_offset, base_subtract=base_subtract)
    sc = axis.scaling
    if sc is not None and sc.is_linear:
        return sc.to_physical(raw)
    return raw.astype(np.float64)


def decode_table(
    table: Table,
    binimage: BinImage,
    *,
    base_offset: int,
    base_subtract: bool = False,
) -> np.ndarray:
    """Decode a table's z-axis cell values into physical units.

    Convenience over :func:`decode_physical` for the common "give me the table's
    values" call. Raises :class:`CodecError` if the table has no z embedded data.
    """
    z: Optional[Axis] = table.z
    if z is None or z.embedded is None:
        raise CodecError(
            f"table {table.uniqueid_hex} has no z-axis embedded data to decode"
        )
    return decode_physical(z, binimage, base_offset=base_offset, base_subtract=base_subtract)
