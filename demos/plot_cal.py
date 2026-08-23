#!/usr/bin/env python3
"""Render a small gallery of calibration tables to PNGs (Phase 3 visualization).

Produces ~9 images under ``demos/plots/`` to show every plot kind:

* a **gallery/** folder — for a few real tables: a 3D surface + a value-overlaid
  heatmap per 2D table, and a line plot for a 1D table (:func:`plot_table`);
* a **before_after/** folder — one table edited in-session, then compared
  pre- vs. post-edit through :func:`compare_tables` (surface + heatmap
  composites). The pre-edit ``RenderedTable`` is a snapshot: ``render_table``
  captures the values, so the edit does not disturb it (no second bin needed).

Everything is read-only except the in-memory before/after edit, which is never
saved — so no bin on disk is touched.
"""

from __future__ import annotations

import warnings
from pathlib import Path

from simoscal import CalFile, compare_tables, plot_table, render_table
from simoscal.safety import EditRangeWarning
from simoscal.checksum import SC8S50_STRUCTURE
from simoscal.tune.profiles.sc8s50 import SC8S50

CODE_ROOT = Path(__file__).resolve().parents[1]
XDF_PATH = CODE_ROOT / "xdf" / "SC8S50.V1.0.xdf"
BIN_PATH = CODE_ROOT / "bin" / "5G0906259L__0002.bin"
PLOTS_DIR = Path(__file__).resolve().parent / "plots"


def _pick_tables(cal: CalFile) -> tuple[list, object]:
    """Choose three 2D tables (with relief) and one 1D table from the real cal."""
    two_d, one_d = [], None
    for view in cal.unique_tables():
        shape = view.shape
        if shape is None:
            continue
        rows, cols = shape
        if rows > 1 and cols > 1 and len(two_d) < 3:
            values = view.values
            if float(values.max()) - float(values.min()) > 0:  # skip flat maps
                two_d.append(view)
        elif rows == 1 and cols > 1 and one_d is None:
            one_d = view
        if len(two_d) == 3 and one_d is not None:
            break
    return two_d, one_d


def main() -> None:
    cal = CalFile.open(
        XDF_PATH, BIN_PATH, structure=SC8S50_STRUCTURE,
        float_bug_symbols=SC8S50.float_bug_symbols,
        stock_references=SC8S50.stock_references,
    )
    two_d, one_d = _pick_tables(cal)

    gallery = PLOTS_DIR / "gallery"
    written: list[Path] = []
    for view in two_d:
        paths = plot_table(view, gallery)  # surface + heatmap
        written += paths
        print(f"  {view.symbol} {view.shape} -> {[p.name for p in paths]}")
    if one_d is not None:
        paths = plot_table(one_d, gallery)  # line
        written += paths
        print(f"  {one_d.symbol} {one_d.shape} -> {[p.name for p in paths]}")

    # Before/after: snapshot, edit in-session, compare through the same path.
    target = two_d[0]
    before = render_table(target)                 # pre-edit snapshot
    with warnings.catch_warnings():               # a demo edit may exceed XDF limits
        warnings.simplefilter("ignore", EditRangeWarning)
        target.set(before.values * 1.05 + 1.0)    # a visible, harmless in-memory bump
    compare_paths = compare_tables(before, target, PLOTS_DIR / "before_after")
    written += compare_paths
    print(f"  before/after {target.symbol} -> {[p.name for p in compare_paths]}")

    print(f"\nWrote {len(written)} plots under {PLOTS_DIR}")


if __name__ == "__main__":
    main()
