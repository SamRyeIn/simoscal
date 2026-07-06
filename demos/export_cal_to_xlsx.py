#!/usr/bin/env python3
"""Export every calibration table in the CAL block to a single xlsx workbook.

The CAL block is the upper 2 MB of the bin (XDF ``BASEOFFSET offset="0x200000"``
— see docs/plans/2026-07-05-001-feat-xdf-bin-library-plan.md, "CAL block").
Every table the XDF declares lives in that block, so exporting
``all_tables=True`` against the bundled XDF/bin is exporting the whole CAL
block: one sheet per XDF category, values in physical units.
"""

from __future__ import annotations

from pathlib import Path

from simoscal import CalFile, export_tables

CODE_ROOT = Path(__file__).resolve().parents[1]
XDF_PATH = CODE_ROOT / "xdf" / "SC8S50.V1.0.xdf"
BIN_PATH = CODE_ROOT / "bin" / "5G0906259L__0002.bin"
OUT_PATH = Path(__file__).resolve().parent / "cal_export.xlsx"


def main() -> None:
    cal = CalFile.open(XDF_PATH, BIN_PATH)
    export_tables(cal, OUT_PATH, all_tables=True)
    print(f"Exported {len(cal.unique_tables())} tables to {OUT_PATH}")


if __name__ == "__main__":
    main()
