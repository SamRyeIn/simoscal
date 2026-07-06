"""Acceptance suite — the AE1-AE5 examples from the origin requirements.

This is the executable contract for Phase 1: each test maps to one acceptance
example and exercises the library end-to-end. Four of the five run entirely on
the Mac against the real bundled files (via the ``real_cal`` conftest fixture);
AE1 (TunerPro parity) is gated on a one-time Windows capture and skips cleanly
when it is absent.

    AE1  read parity      decoded values match TunerPro on a sampled set
    AE2  round-trip       load -> save-unchanged -> byte-identical
    AE3  minimal-diff     a one-cell edit changes exactly that cell's bytes
    AE4  warn-on-range    an out-of-XDF-range write succeeds *and* warns
    AE5  non-linear       a non-linear table rejects set(physical), allows set_raw

The safety spine (plan "Stakes & Safety"): AE2/AE3 prove the writer touches
*only* intended bytes; AE4 proves it warns loud rather than silently clamping;
AE5 proves it refuses a transform it cannot faithfully invert.
"""

from __future__ import annotations

import io

import numpy as np
import pytest

from simoscal import (
    BinImage,
    CalFile,
    EditRangeWarning,
    NonLinearEquationError,
    parse_xdf,
)

# A table known to exist in SC8S50.V1.0: ID_PORT_SP, 10x10 int8. Used by the
# real-file edit tests. Chosen because it is small, signed, and non-uniform.
ORACLE_ID = 0x11F9C


# --------------------------------------------------------------------------- #
# AE1 — read parity with TunerPro (gated on the one-time capture)
# --------------------------------------------------------------------------- #
@pytest.mark.tunerpro
def test_ae1_values_match_tunerpro(real_cal: CalFile, tunerpro_oracle: dict):
    """AE1: decoded physical values match the TunerPro-displayed oracle.

    The independent confirmation of the ``mmedtypeflags`` bit semantics
    (plan Decision 6). Reads ``tunerpro_oracle.json`` (schema in
    ``tests/fixtures/README.md``) and compares every captured table's cells
    against the library's decode. Skips when the capture is absent.
    """
    default_tol = float(tunerpro_oracle.get("tolerance", 0.01))
    checked = 0
    for entry in tunerpro_oracle["tables"]:
        uid = entry["uniqueid"]
        uid = int(uid, 16) if isinstance(uid, str) else int(uid)
        tol = float(entry.get("tol", default_tol))
        expected = np.array(entry["values"], dtype=float)

        view = real_cal.get(uid)
        actual = np.asarray(view.values, dtype=float)

        assert actual.shape == expected.shape, (
            f"{entry.get('symbol', hex(uid))}: shape {actual.shape} != "
            f"captured {expected.shape}"
        )
        np.testing.assert_allclose(
            actual,
            expected,
            atol=tol,
            rtol=0,
            err_msg=f"AE1 parity failed for {entry.get('symbol', hex(uid))}",
        )
        checked += 1
    assert checked >= 1, "oracle contained no comparable tables"


# --------------------------------------------------------------------------- #
# AE2 — round-trip byte-equality
# --------------------------------------------------------------------------- #
def test_ae2_save_unchanged_is_byte_identical(real_cal: CalFile, real_bin, tmp_path):
    """AE2: open -> save with no edits -> output byte-identical to input."""
    out = tmp_path / "unchanged.bin"
    real_cal.save(out)
    assert out.read_bytes() == real_bin.read_bytes()


def test_ae2_setting_current_values_back_is_noop(
    real_cal: CalFile, real_bin, tmp_path
):
    """AE2 (stronger): writing a table's own values back changes nothing."""
    view = real_cal.get(ORACLE_ID)
    view.set(view.values)
    out = tmp_path / "noop.bin"
    real_cal.save(out)
    assert out.read_bytes() == real_bin.read_bytes()


# --------------------------------------------------------------------------- #
# AE3 — minimal-diff edit
# --------------------------------------------------------------------------- #
def test_ae3_single_cell_edit_is_minimal_diff(
    real_cal: CalFile, real_bin, real_xdf, tmp_path
):
    """AE3: a one-cell edit changes exactly one byte, at that cell's offset,
    and the value round-trips within one LSB on re-open."""
    view = real_cal.get(ORACLE_ID)  # 10x10 int8
    emb = view.table.embedded
    original = np.array(view.raw)

    # Pick an in-range value that differs from cell (0,0) so the edit is a real
    # byte change with no incidental out-of-range warning.
    base = int(original[0, 0])
    others = np.argwhere(original != base)
    assert others.size, "oracle table is uniform; choose another"
    dr, dc = others[0]
    new_phys = float(view.values[dr, dc])
    view.set_cell(0, 0, new_phys)

    out = tmp_path / "edited.bin"
    real_cal.save(out)

    before, after = real_bin.read_bytes(), out.read_bytes()
    diff = [i for i in range(len(before)) if before[i] != after[i]]
    cell_offset = emb.address + real_cal.model.base_offset  # cell (0,0)
    assert diff == [cell_offset], (
        f"AE3: expected exactly one changed byte at {cell_offset:#x}, got {diff}"
    )

    reopened = CalFile.open(str(real_xdf), str(out))
    lsb = abs(float(view.table.scaling.m))
    assert abs(float(reopened.get(ORACLE_ID).values[0, 0]) - new_phys) <= lsb + 1e-9


def test_ae3_full_table_edit_stays_within_extent(
    real_cal: CalFile, real_bin, tmp_path
):
    """AE3: editing a whole table changes only bytes inside its extent."""
    view = real_cal.get(ORACLE_ID)
    emb = view.table.embedded
    start = emb.address + real_cal.model.base_offset
    end = start + emb.count * emb.element_bytes

    nudged = np.clip(np.array(view.raw) + 1, -128, 127)  # every cell +1, in range
    view.set_raw(nudged)

    out = tmp_path / "row.bin"
    real_cal.save(out)
    before, after = real_bin.read_bytes(), out.read_bytes()
    diff = [i for i in range(len(before)) if before[i] != after[i]]
    assert diff, "AE3: expected some bytes to change"
    assert all(start <= i < end for i in diff), "AE3: edit escaped the table's range"


# --------------------------------------------------------------------------- #
# AE4 — warn + allow on out-of-declared-range
# --------------------------------------------------------------------------- #
def test_ae4_over_range_write_warns_and_writes(real_cal: CalFile):
    """AE4: a value above the XDF max is written anyway *and* warns, naming the
    table and cell. Uses a real int8 table so the raw type comfortably holds a
    value past the (conservative) display limit."""
    view = real_cal.get(ORACLE_ID)  # int8, holds up to 127
    zmax = float(view.table.z.max)
    assert zmax < 127, "pick a table whose display max leaves raw headroom"

    # Target just past the display max but well within int8 range.
    target = min(zmax + 1.0, 120.0)
    with pytest.warns(EditRangeWarning) as rec:
        view.set_cell(0, 0, target)

    assert abs(float(view.values[0, 0]) - target) <= abs(view.table.scaling.m) + 1e-9
    msg = str(rec[0].message)
    assert view.uniqueid_hex in msg  # warning names the table
    assert "(0,0)" in msg  # ...and the cell


# --------------------------------------------------------------------------- #
# AE5 — non-linear table: reject set(physical), allow set_raw
# --------------------------------------------------------------------------- #
# No non-linear equation exists in SC8S50.V1.0 (all 11,736 are linear), so AE5
# uses a synthetic definition — the acceptance example itself calls for a
# constructed non-linear table.
_NONLINEAR_XDF = """<XDFFORMAT version="1.60">
  <XDFHEADER>
    <BASEOFFSET offset="0x0" subtract="0" />
    <REGION size="0x100" startaddress="0x0" />
    <DEFAULTS datasizeinbits="8" signed="0" lsbfirst="1" float="0" />
    <CATEGORY index="0x0" name="Test" />
  </XDFHEADER>
  <XDFTABLE uniqueid="0x1" flags="0x30">
    <title>Nonlinear</title><description>NONLIN</description>
    <XDFAXIS id="z">
      <EMBEDDEDDATA mmedtypeflags="0x2" mmedaddress="0x10" mmedelementsizebits="8" mmedcolcount="1" mmedrowcount="1" mmedmajorstridebits="8" mmedminorstridebits="0" />
      <min>0.0</min><max>100.0</max><units>-</units>
      <MATH equation="X * X"><VAR id="X" /></MATH>
    </XDFAXIS>
  </XDFTABLE>
</XDFFORMAT>
"""


@pytest.fixture
def nonlinear_cal() -> CalFile:
    model = parse_xdf(io.StringIO(_NONLINEAR_XDF))
    img = BinImage(bytearray(0x100), region_start=0, region_size=0x100)
    return CalFile(model, img)


def test_ae5_nonlinear_rejects_physical_set(nonlinear_cal: CalFile):
    """AE5: a non-linear table refuses set(physical) — it cannot faithfully
    invert the equation, so it fails loud instead of writing a wrong value."""
    view = nonlinear_cal.get("NONLIN")
    with pytest.raises(NonLinearEquationError):
        view.set_cell(0, 0, 5)
    with pytest.raises(NonLinearEquationError):
        view.set([[5]])


def test_ae5_nonlinear_allows_raw_edit(nonlinear_cal: CalFile):
    """AE5: the same table still permits an explicit raw edit."""
    view = nonlinear_cal.get("NONLIN")
    view.set_raw_cell(0, 0, 7)
    assert int(view.raw[0, 0]) == 7
    assert nonlinear_cal.binimage.to_bytes()[0x10] == 7
