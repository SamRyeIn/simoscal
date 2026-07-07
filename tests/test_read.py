"""Tests for the U3 BIN read path (binimage + codec + calfile).

Correctness is proven **without TunerPro** (Mac-friendly), using several
independent oracles:

* **Independent struct cross-parser** — re-decode every real table with the
  stdlib ``struct`` module and assert it matches the numpy codec. This stands in
  for the (deferred) KingAi exporter cross-check and catches endianness / sign /
  width / offset / cell-order errors exactly. Endianness has teeth: most
  multi-byte tables decode differently LE vs BE, so agreement is meaningful.
* **Type-envelope bounds** — every non-float table's decoded values lie within
  the physical range its raw element type can represent.
* **Inverse round-trip** — ``to_raw(to_physical(raw)) == raw`` within 1 LSB for
  8-bit signed, 16-bit unsigned, and 32-bit float elements.
* **Known-value pins + shape** — concrete anchors against the real bin.

Fixture-level tests (codec/binimage units) use hand-built metadata + tiny
buffers and need no real files.
"""

from __future__ import annotations

import struct
from pathlib import Path

import numpy as np
import pytest

from simoscal import (
    Axis,
    BinImage,
    CalFile,
    CodecError,
    EmbeddedData,
    RegionBoundsError,
    ScalingEquation,
    Table,
    decode_physical,
    decode_raw,
    file_offset_for,
    numpy_dtype_for,
    parse_xdf,
)

FIXTURES = Path(__file__).parent / "fixtures"
MINI_XDF = FIXTURES / "mini.xdf"
REAL_XDF = Path(__file__).parents[1] / "xdf" / "SC8S50.V1.0.xdf"
REAL_BIN = Path(__file__).parents[1] / "bin" / "5G0906259L__0002.bin"

UNIQUE_TABLE_COUNT = 3814  # distinct uniqueids in SC8S50.V1.0.xdf


# --------------------------------------------------------------------------- #
# BinImage — region-checked reads
# --------------------------------------------------------------------------- #
def test_binimage_read_within_region():
    img = BinImage(bytes(range(16)))
    assert img.size == 16
    assert img.read(4, 4) == bytes([4, 5, 6, 7])


def test_binimage_read_past_end_raises():
    img = BinImage(bytes(8))
    with pytest.raises(RegionBoundsError):
        img.read(6, 4)


def test_binimage_read_below_region_start_raises():
    img = BinImage(bytes(32), region_start=8, region_size=16)
    with pytest.raises(RegionBoundsError):
        img.read(4, 2)  # before region_start


def test_binimage_read_past_region_end_raises():
    img = BinImage(bytes(64), region_start=0, region_size=16)
    with pytest.raises(RegionBoundsError):
        img.read(14, 4)  # crosses region_end even though file is bigger


def test_binimage_negative_length_raises():
    img = BinImage(bytes(16))
    with pytest.raises(RegionBoundsError):
        img.read(0, -1)


def test_binimage_from_path(tmp_path):
    p = tmp_path / "x.bin"
    p.write_bytes(bytes([0xAA, 0xBB, 0xCC, 0xDD]))
    img = BinImage.from_path(p)
    assert img.read(1, 2) == bytes([0xBB, 0xCC])


# --------------------------------------------------------------------------- #
# Address mapping + dtype selection
# --------------------------------------------------------------------------- #
def test_file_offset_add_and_subtract():
    assert file_offset_for(0x36EC, 0x200000, False) == 0x2036EC
    assert file_offset_for(0x2036EC, 0x200000, True) == 0x36EC


def test_numpy_dtype_for_all_types():
    def emb(bits, signed, is_float):
        return EmbeddedData(address=0, rows=1, cols=1, elem_bits=bits,
                            signed=signed, little_endian=True, is_float=is_float)

    assert numpy_dtype_for(emb(8, True, False)) == np.dtype("<i1")
    assert numpy_dtype_for(emb(8, False, False)) == np.dtype("<u1")
    assert numpy_dtype_for(emb(16, True, False)) == np.dtype("<i2")
    assert numpy_dtype_for(emb(16, False, False)) == np.dtype("<u2")
    assert numpy_dtype_for(emb(32, False, True)) == np.dtype("<f4")
    be = EmbeddedData(address=0, rows=1, cols=1, elem_bits=16, signed=True,
                      little_endian=False, is_float=False)
    assert numpy_dtype_for(be) == np.dtype(">i2")


# --------------------------------------------------------------------------- #
# Codec — hand-built axes over a tiny buffer (no real files)
# --------------------------------------------------------------------------- #
def _axis(emb, scaling=None, axis_id="z"):
    return Axis(axis_id=axis_id, embedded=emb, scaling=scaling)


def test_codec_decode_raw_16bit_signed_le_row_major():
    # 2x3 int16 LE, values 0..5 packed row-major at address 0.
    payload = struct.pack("<6h", 0, 1, 2, 3, 4, 5)
    img = BinImage(payload)
    emb = EmbeddedData(address=0, rows=2, cols=3, elem_bits=16,
                       major_stride_bits=16, minor_stride_bits=0,
                       signed=True, little_endian=True)
    raw = decode_raw(_axis(emb), img, base_offset=0)
    assert raw.shape == (2, 3)
    assert raw.dtype == np.dtype("<i2")
    np.testing.assert_array_equal(raw, [[0, 1, 2], [3, 4, 5]])


def test_codec_endianness_is_honored():
    payload = struct.pack("<h", 0x0102)  # LE bytes 02 01
    img = BinImage(payload)
    le = EmbeddedData(address=0, rows=1, cols=1, elem_bits=16, signed=True,
                      little_endian=True)
    be = EmbeddedData(address=0, rows=1, cols=1, elem_bits=16, signed=True,
                      little_endian=False)
    assert int(decode_raw(_axis(le), img, base_offset=0)[0, 0]) == 0x0102
    assert int(decode_raw(_axis(be), img, base_offset=0)[0, 0]) == 0x0201


def test_codec_signed_vs_unsigned():
    payload = bytes([0xFF])
    img = BinImage(payload)
    s = EmbeddedData(address=0, rows=1, cols=1, elem_bits=8, signed=True)
    u = EmbeddedData(address=0, rows=1, cols=1, elem_bits=8, signed=False)
    assert int(decode_raw(_axis(s), img, base_offset=0)[0, 0]) == -1
    assert int(decode_raw(_axis(u), img, base_offset=0)[0, 0]) == 255


def test_codec_float32():
    payload = struct.pack("<f", 3.5)
    img = BinImage(payload)
    emb = EmbeddedData(address=0, rows=1, cols=1, elem_bits=32,
                       is_float=True, little_endian=True)
    assert float(decode_raw(_axis(emb), img, base_offset=0)[0, 0]) == 3.5


def test_codec_applies_linear_scaling():
    payload = struct.pack("<2h", 0, 32768 - 32768 + 32767)  # 0 and 32767
    img = BinImage(payload)
    sc = ScalingEquation(m=1.0 / 327.68, b=0.0, expression="X/327.68")
    emb = EmbeddedData(address=0, rows=1, cols=2, elem_bits=16, signed=True)
    phys = decode_physical(_axis(emb, sc), img, base_offset=0)
    assert phys.dtype == np.float64
    np.testing.assert_allclose(phys, [[0.0, 32767 / 327.68]])


def test_codec_address_uses_base_offset():
    payload = bytes(4) + bytes([0x2A])  # value 42 at offset 4
    img = BinImage(payload)
    emb = EmbeddedData(address=0x2, rows=1, cols=1, elem_bits=8, signed=False)
    val = decode_raw(_axis(emb), img, base_offset=0x2)  # 0x2 + 0x2 = 4
    assert int(val[0, 0]) == 42


def test_codec_out_of_region_raises_regionbounds():
    img = BinImage(bytes(8))
    emb = EmbeddedData(address=6, rows=1, cols=2, elem_bits=16, signed=True)
    with pytest.raises(RegionBoundsError):
        decode_raw(_axis(emb), img, base_offset=0)


def test_codec_rejects_unhandled_minor_stride():
    img = BinImage(bytes(16))
    emb = EmbeddedData(address=0, rows=1, cols=2, elem_bits=8,
                       minor_stride_bits=16)  # gap between columns
    with pytest.raises(CodecError):
        decode_raw(_axis(emb), img, base_offset=0)


def test_codec_rejects_unhandled_major_stride():
    img = BinImage(bytes(64))
    # major stride 24 bits is neither 0, elem_bits(8), nor cols*elem_bits(16).
    emb = EmbeddedData(address=0, rows=2, cols=2, elem_bits=8,
                       major_stride_bits=24)
    with pytest.raises(CodecError):
        decode_raw(_axis(emb), img, base_offset=0)


def test_codec_accepts_row_stride_convention():
    # major stride = cols*elem_bits (a true packed row stride) must be accepted.
    payload = struct.pack("<4b", 1, 2, 3, 4)
    img = BinImage(payload)
    emb = EmbeddedData(address=0, rows=2, cols=2, elem_bits=8, signed=True,
                       major_stride_bits=16, minor_stride_bits=0)
    raw = decode_raw(_axis(emb), img, base_offset=0)
    np.testing.assert_array_equal(raw, [[1, 2], [3, 4]])


def test_codec_axis_without_embedded_raises():
    img = BinImage(bytes(8))
    with pytest.raises(CodecError):
        decode_raw(Axis(axis_id="x"), img, base_offset=0)


# --------------------------------------------------------------------------- #
# CalFile façade over mini.xdf + a synthetic in-memory bin (no real files)
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def mini_cal() -> CalFile:
    model = parse_xdf(str(MINI_XDF))
    # Synthetic bin sized to cover the fixture's highest table (0x4000 + base).
    size = model.base_offset + 0x5000
    buf = bytearray(size)
    # 10x10 int16 LE table @ 0x1000 -> file 0x201000: values 0..99.
    off = model.base_offset + 0x1000
    buf[off : off + 200] = struct.pack("<100h", *range(100))
    # 1x1 uint8 scalar @ 0x2000 -> value 200.
    buf[model.base_offset + 0x2000] = 200
    # 1x1 float32 @ 0x4000 -> 12.5.
    foff = model.base_offset + 0x4000
    buf[foff : foff + 4] = struct.pack("<f", 12.5)
    img = BinImage(buf, region_start=model.region_start, region_size=len(buf))
    return CalFile(model, img)


def test_calfile_get_returns_bound_view(mini_cal: CalFile):
    v = mini_cal.get("SYM_10X10")
    assert v.uniqueid == 0x100
    assert v.symbol == "SYM_10X10"
    assert v.shape == (10, 10)
    assert v.units == "%"


def test_calfile_decodes_values_with_scaling(mini_cal: CalFile):
    v = mini_cal.get("SYM_10X10")
    assert v.raw.shape == (10, 10)
    assert v.raw[0, 0] == 0 and v.raw[9, 9] == 99
    # scaling m = 1/327.68, b = 0.
    np.testing.assert_allclose(v.values[0, 0], 0.0)
    np.testing.assert_allclose(v.values[9, 9], 99 / 327.68)


def test_calfile_values_cached_same_object(mini_cal: CalFile):
    v = mini_cal.get("SYM_10X10")
    assert v.values is v.values  # cached, not re-decoded
    v2 = mini_cal.get("SYM_10X10")
    assert v2 is v  # same view instance per uniqueid


def test_calfile_scalar_and_float(mini_cal: CalFile):
    s = mini_cal.get("SYM_SCALAR")
    assert s.shape == (1, 1)
    assert int(s.values[0, 0]) == 200
    # float table cross-listed as SYM_DUP; fetch by uniqueid.
    fv = mini_cal.get(0x400)
    assert fv.raw.dtype == np.dtype("<f4")
    assert float(fv.values[0, 0]) == 12.5


def test_calfile_unique_tables_dedup(mini_cal: CalFile):
    # mini has 5 XDFTABLE entries but SYM_DUP twice under distinct uniqueids,
    # so all 5 are distinct ids -> 5 unique views.
    assert len(mini_cal.unique_tables()) == 5
    assert len(mini_cal) == 5


def test_calfile_axis_values_none_when_label_only(mini_cal: CalFile):
    v = mini_cal.get("SYM_10X10")
    # mini's x/y axes are label-only (no EMBEDDEDDATA).
    assert v.axis_values("x") is None


# --------------------------------------------------------------------------- #
# Real-file oracles — skip cleanly when the bundled files are absent
# --------------------------------------------------------------------------- #
requires_real = pytest.mark.skipif(
    not (REAL_XDF.exists() and REAL_BIN.exists()),
    reason=f"real XDF/BIN not present: {REAL_XDF}, {REAL_BIN}",
)


@pytest.fixture(scope="module")
def real_cal() -> CalFile:
    if not (REAL_XDF.exists() and REAL_BIN.exists()):
        pytest.skip("real XDF/BIN not present")
    return CalFile.open(str(REAL_XDF), str(REAL_BIN))


def _struct_decode(emb: EmbeddedData, buf: bytes, base_offset: int) -> np.ndarray:
    """Independent (stdlib ``struct``) re-decode of an embedded block."""
    off = emb.address + base_offset
    n = emb.rows * emb.cols
    w = emb.element_bytes
    endian = "<" if emb.little_endian else ">"
    if emb.is_float:
        code = "f"
    elif emb.signed:
        code = {1: "b", 2: "h", 4: "i"}[w]
    else:
        code = {1: "B", 2: "H", 4: "I"}[w]
    vals = struct.unpack(f"{endian}{n}{code}", buf[off : off + n * w])
    arr = np.array(vals)
    # Element order per the column-major (0x04) typeflag, matching decode_raw.
    if emb.column_major:
        return arr.reshape(emb.cols, emb.rows).T
    return arr.reshape(emb.rows, emb.cols)


@requires_real
def test_real_unique_table_count(real_cal: CalFile):
    assert len(real_cal.unique_tables()) == UNIQUE_TABLE_COUNT
    assert len(real_cal) == UNIQUE_TABLE_COUNT


@requires_real
def test_real_independent_struct_oracle(real_cal: CalFile):
    """Primary correctness oracle: numpy codec == stdlib struct, every table."""
    buf = bytes(real_cal.binimage._data)
    base = real_cal.model.base_offset
    mism = []
    for v in real_cal.unique_tables():
        emb = v.table.embedded
        if emb is None:
            continue
        expected = _struct_decode(emb, buf, base)
        if not np.array_equal(expected, v.raw):
            mism.append(v.uniqueid_hex)
    assert mism == [], f"{len(mism)} tables disagree with struct decode: {mism[:10]}"


@requires_real
def test_real_type_envelope_bounds(real_cal: CalFile):
    """Every non-float table's decoded values are within its raw type's range."""
    violations = []
    for v in real_cal.unique_tables():
        emb = v.table.embedded
        sc = v.table.scaling
        if emb is None or emb.is_float or sc is None or not sc.is_linear:
            continue
        bits = emb.elem_bits
        if emb.signed:
            lo_raw, hi_raw = -(2 ** (bits - 1)), 2 ** (bits - 1) - 1
        else:
            lo_raw, hi_raw = 0, 2 ** bits - 1
        a, b = sc.m * lo_raw + sc.b, sc.m * hi_raw + sc.b
        lo_env, hi_env = min(a, b), max(a, b)
        span = (hi_env - lo_env) or 1.0
        tol = 1e-6 * (abs(span) + 1.0)
        vals = v.values
        if float(vals.min()) < lo_env - tol or float(vals.max()) > hi_env + tol:
            violations.append(v.uniqueid_hex)
    assert violations == [], f"type-envelope violations: {violations[:10]}"


@requires_real
def test_real_most_tables_within_declared_limits(real_cal: CalFile):
    """Sanity: the large majority of tables decode inside their display min/max.

    Not all do (the XDF <min>/<max> are conservative display limits, and float
    sentinels legitimately exceed them), but a systematic scale/endianness/sign
    error would push nearly *every* table out. >75% inside is the guard.
    """
    inside = total = 0
    for v in real_cal.unique_tables():
        z = v.table.z
        if z is None or z.min is None or z.max is None:
            continue
        total += 1
        vals = v.values
        span = (z.max - z.min) or 1.0
        tol = 1e-6 * (abs(span) + 1.0)
        if float(vals.min()) >= z.min - tol and float(vals.max()) <= z.max + tol:
            inside += 1
    assert total > 0
    assert inside / total > 0.75, f"only {inside}/{total} within declared limits"


@requires_real
@pytest.mark.parametrize("bits,signed,is_float", [(8, False, False),
                                                  (16, False, False),
                                                  # Every table in V1.0 is unsigned:
                                                  # the sign bit (0x01) is never set;
                                                  # 0x6 = LE|col-major, 0x10006 adds
                                                  # float. (See CR-20260706-22.)
                                                  (32, False, True)])
def test_real_inverse_roundtrip_per_type(real_cal: CalFile, bits, signed, is_float):
    """to_raw(to_physical(raw)) == raw within 1 LSB for each element type."""
    target = None
    for t in real_cal.model.unique_tables():
        for ax in (t.x, t.y, t.z):
            e = ax.embedded if ax is not None else None
            sc = ax.scaling if ax is not None else None
            if (e is not None and sc is not None and sc.is_linear
                    and e.elem_bits == bits and e.signed == signed
                    and e.is_float == is_float):
                target = (t, ax)
                break
        if target:
            break
    assert target is not None, f"no linear {bits}-bit signed={signed} float={is_float} axis"
    _t, ax = target
    raw = decode_raw(ax, real_cal.binimage,
                     base_offset=real_cal.model.base_offset,
                     base_subtract=real_cal.model.base_subtract)
    phys = ax.scaling.to_physical(raw)
    back = ax.scaling.to_raw(phys)
    assert np.max(np.abs(back - raw.astype(np.int64))) <= 1


@requires_real
def test_real_known_value_pin_16bit_unsigned(real_cal: CalFile):
    # C_FAC_POW_PUT_CTL_BOL @ 0x36ec: uint16 LE (typeflags 0x6, no sign bit),
    # raw 33423, m=1/327.68. TunerPro shows 102.00 (oracle AE1). Pinning the
    # *unsigned* decode guards CR-20260706-22 against regression.
    v = real_cal.get(0x36EC)
    assert v.raw.dtype == np.dtype("<u2")
    assert int(v.raw[0, 0]) == 33423
    assert float(v.values[0, 0]) == pytest.approx(33423 / 327.68, abs=1e-4)


@requires_real
def test_real_known_value_pin_float(real_cal: CalFile):
    v = real_cal.get("C_M_AIR_CYL_FL")
    assert v.raw.dtype == np.dtype("<f4")
    # identity scaling -> physical equals raw float.
    np.testing.assert_allclose(v.values, v.raw.astype(np.float64))


@requires_real
def test_real_shape_of_10x10(real_cal: CalFile):
    v = real_cal.get(0x11F9C)  # ID_PORT_SP, a 10x10 int8 table
    assert v.shape == (10, 10)
    assert v.raw.shape == (10, 10)
    assert v.values.shape == (10, 10)


@requires_real
def test_real_region_overrun_is_caught(real_cal: CalFile):
    # A synthetic table whose extent runs past the 4 MB region must fail loud.
    huge = Table(
        uniqueid=0xDEAD,
        z=Axis(axis_id="z", embedded=EmbeddedData(
            address=0x1FFFFE, rows=1, cols=8, elem_bits=32, signed=False)),
    )
    with pytest.raises(RegionBoundsError):
        decode_raw(huge.z, real_cal.binimage,
                   base_offset=real_cal.model.base_offset,
                   base_subtract=real_cal.model.base_subtract)
