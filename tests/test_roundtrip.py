"""Round-trip + minimal-diff acceptance against the real bin (AE2, AE3).

These are the safety-critical guarantees (plan Decision 10): saving an unedited
bin reproduces it byte-for-byte, and an edit changes *only* the intended bytes.
Skipped cleanly when the bundled XDF/BIN are absent.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from simoscal import CalFile, FloatBugGuardError

REAL_XDF = Path(__file__).parents[1] / "xdf" / "SC8S50.V1.0.xdf"
REAL_BIN = Path(__file__).parents[1] / "bin" / "5G0906259L__0002.bin"

requires_real = pytest.mark.skipif(
    not (REAL_XDF.exists() and REAL_BIN.exists()),
    reason=f"real XDF/BIN not present: {REAL_XDF}, {REAL_BIN}",
)


@requires_real
def test_ae2_save_unchanged_is_byte_identical(tmp_path):
    """AE2: open → save with no edits → output byte-identical to input."""
    cal = CalFile.open(str(REAL_XDF), str(REAL_BIN))
    out = tmp_path / "unchanged.bin"
    cal.save(out)
    assert out.read_bytes() == REAL_BIN.read_bytes()


@requires_real
def test_ae3_single_cell_edit_is_minimal_diff(tmp_path):
    """AE3: a one-cell edit changes exactly one byte, at that cell's offset."""
    cal = CalFile.open(str(REAL_XDF), str(REAL_BIN))
    v = cal.get(0x11F9C)  # ID_PORT_SP, 10x10 int8
    emb = v.table.embedded
    original = np.array(v.raw)

    # Pick an existing (in-range) cell value that differs from cell (0,0), so the
    # edit is a genuine byte change with no incidental out-of-range warning.
    old_raw = int(original[0, 0])
    diff_cells = np.argwhere(original != old_raw)
    assert diff_cells.size, "table is uniform; pick another oracle table"
    dr, dc = diff_cells[0]
    new_phys = float(v.values[dr, dc])
    v.set_cell(0, 0, new_phys)

    out = tmp_path / "edited.bin"
    cal.save(out)

    before = REAL_BIN.read_bytes()
    after = out.read_bytes()
    diff = [i for i in range(len(before)) if before[i] != after[i]]
    cell_offset = emb.address + cal.model.base_offset  # cell (0,0)
    assert diff == [cell_offset], f"expected 1 changed byte at {cell_offset:#x}, got {diff}"

    # Re-open and confirm the value round-trips within one LSB.
    reread = CalFile.open(str(REAL_XDF), str(out))
    assert abs(float(reread.get(0x11F9C).values[0, 0]) - new_phys) <= abs(
        float(v.table.scaling.m)
    ) + 1e-9


@requires_real
def test_ae3_setting_same_values_yields_no_diff(tmp_path):
    """Writing a table's current values back produces no byte change."""
    cal = CalFile.open(str(REAL_XDF), str(REAL_BIN))
    v = cal.get(0x11F9C)
    v.set(v.values)  # write current physical values back
    out = tmp_path / "noop.bin"
    cal.save(out)
    assert out.read_bytes() == REAL_BIN.read_bytes()


@requires_real
def test_ae3_multi_row_edit_bounded_to_table_range(tmp_path):
    """Editing a whole table changes only bytes inside that table's extent."""
    cal = CalFile.open(str(REAL_XDF), str(REAL_BIN))
    v = cal.get(0x11F9C)  # 10x10 int8 = 100 bytes
    emb = v.table.embedded
    start = emb.address + cal.model.base_offset
    end = start + emb.count * emb.element_bytes

    new = np.array(v.raw)
    new = np.clip(new + 1, -128, 127)  # nudge every cell one step, in range
    v.set_raw(new)

    out = tmp_path / "row.bin"
    cal.save(out)
    before, after = REAL_BIN.read_bytes(), out.read_bytes()
    diff = [i for i in range(len(before)) if before[i] != after[i]]
    assert diff, "expected some bytes to change"
    assert all(start <= i < end for i in diff), "edit escaped the table's byte range"


@requires_real
def test_real_float_bug_guard_on_actual_table():
    """The real C_PRS_IM_SP_MAX (float, flagged) rejects an over-limit write."""
    cal = CalFile.open(str(REAL_XDF), str(REAL_BIN))
    v = cal.get("C_PRS_IM_SP_MAX")
    with pytest.raises(FloatBugGuardError):
        v.set_cell(0, 0, 99999, override=True)
