#!/usr/bin/env python3
"""Export every calibration table in the CAL block to xlsx and CSV.

The CAL block is the upper 2 MB of the bin (XDF ``BASEOFFSET offset="0x200000"``
— see docs/plans/2026-07-05-001-feat-xdf-bin-library-plan.md, "CAL block").
Every table the XDF declares lives in that block, so exporting
``all_tables=True`` against the bundled XDF/bin is exporting the whole CAL
block: xlsx groups tables onto one sheet per XDF category, CSV stacks every
table into a single file — both in physical units.
"""

from __future__ import annotations

from pathlib import Path

from simoscal import CalFile, export_tables
from simoscal.checksum import SC8S50_STRUCTURE

CODE_ROOT = Path(__file__).resolve().parents[1]
XDF_PATH = CODE_ROOT / "xdf" / "SC8S50.V1.0.xdf"
BIN_PATH = CODE_ROOT / "bin" / "5G0906259L__0002.bin"
DEMOS_DIR = Path(__file__).resolve().parent
XLSX_PATH = DEMOS_DIR / "cal_export.xlsx"
CSV_PATH = DEMOS_DIR / "cal_export.csv"


def main() -> None:
    cal = CalFile.open(XDF_PATH, BIN_PATH, structure=SC8S50_STRUCTURE)
    table_count = len(cal.unique_tables())

    export_tables(cal, XLSX_PATH, all_tables=True)
    print(f"Exported {table_count} tables to {XLSX_PATH}")

    export_tables(cal, CSV_PATH, all_tables=True)
    print(f"Exported {table_count} tables to {CSV_PATH}")


if __name__ == "__main__":
    main()
