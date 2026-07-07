#!/usr/bin/env python3
"""Apply the ecu-tuning-basics SOP to the stock bin and produce a review bundle.

Drives the full library pipeline for one recipe revision:

    CalFile.open (stock bin)
        │  snapshot every write-target table pre-edit (render_table)
        ▼
    apply_basics_sop(cal)                → RecipeReport (stages edits in memory)
        │
        ▼
    cal.save(out.bin, correct_checksums=True) → checksum-clean, flashable-shaped bin
    cal.verify_checksums()                → confirm CAL_CRC + ECM3 clean
        │
        ▼
    report.md   (format_report — DO NOT FLASH banner first)
    compare/    (compare_tables PNGs for every changed non-scalar table)

Everything is written into a fresh timestamped directory under
``demos/apply_sop_recipe_out/`` so prior revisions are kept for comparison
(``compare_bins`` works across any two saved bins). The recipe produces
**revision 0 — a starting point, not a finished calibration**: review the report
and PNGs, then flash → log → review → iterate. **This script never flashes** and
a saved bin is not flash-ready while the report shows DO NOT FLASH.
"""

from __future__ import annotations

import datetime as _dt
import warnings
from pathlib import Path

from simoscal import (
    CalFile,
    TableMismatchError,
    apply_basics_sop,
    compare_tables,
    format_report,
    render_table,
    resolve_symbol_map,
)
from simoscal.safety import EditRangeWarning
from simoscal.sop_recipe import (
    KIND_AXIS_WRITE,
    OUTCOME_APPLIED,
    OUTCOME_APPLIED_BUILDOUT,
    is_write_kind,
)

CODE_ROOT = Path(__file__).resolve().parents[1]
XDF_PATH = CODE_ROOT / "xdf" / "SC8S50.V1.0.xdf"
BIN_PATH = CODE_ROOT / "bin" / "5G0906259L__0002.bin"
OUT_ROOT = Path(__file__).resolve().parent / "apply_sop_recipe_out"


def _snapshot_write_targets(cal: CalFile) -> dict[str, object]:
    """Pre-edit ``RenderedTable`` per write-target symbol (for before/after PNGs).

    We can't know the final per-table outcome before applying, so we snapshot
    every resolved table belonging to a write-kind entry; snapshots whose entry
    ends up skipped are simply never looked up again.
    """
    snaps: dict[str, object] = {}
    for resolved in resolve_symbol_map(cal):
        entry = resolved.entry
        if not is_write_kind(entry.kind):
            continue
        for res in resolved.resolutions:
            if res.resolved and res.view is not None:
                snaps[res.symbol] = render_table(res.view)
        # axis_write also drives a separate axis table beyond its own symbol.
        if entry.kind == KIND_AXIS_WRITE and resolved.resolutions[0].resolved:
            axis_symbol = entry.target.axis_symbol
            try:
                snaps[axis_symbol] = render_table(cal.get(axis_symbol))
            except Exception:  # noqa: BLE001 - a missing axis table is simply not snapshotted
                pass
    return snaps


def _write_comparison_pngs(cal: CalFile, snaps: dict, report, png_dir: Path) -> tuple[int, int]:
    """Emit a before/after PNG per changed non-scalar table. Returns (pngs, skipped)."""
    png_count, axis_changed = 0, 0
    for out in report.outcomes:
        if out.outcome not in (OUTCOME_APPLIED, OUTCOME_APPLIED_BUILDOUT):
            continue
        before = snaps.get(out.symbol)
        if before is None:
            continue
        try:
            after = cal.get(out.symbol)
        except Exception:  # noqa: BLE001
            continue
        try:
            paths = compare_tables(before, after, png_dir)  # scalars → [] by design
        except TableMismatchError:
            # PUT setpoint's own Y axis was raised — before/after axes differ, so a
            # composite would be misleading. The report's detail covers it instead.
            axis_changed += 1
            continue
        png_count += len(paths)
    return png_count, axis_changed


def main() -> None:
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = OUT_ROOT / f"R0_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    cal = CalFile.open(XDF_PATH, BIN_PATH)
    snaps = _snapshot_write_targets(cal)

    # Apply the recipe (stages edits in memory) — squelch the expected
    # out-of-XDF-range warnings; they are captured into the report instead.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", EditRangeWarning)
        report = apply_basics_sop(cal)

    out_bin = out_dir / "tuned_R0.bin"
    save_reports = cal.save(out_bin, correct_checksums=True)
    verify_reports = cal.verify_checksums()
    clean = all((not r.can_verify) or (not r.is_stale) for r in verify_reports)

    (out_dir / "report.md").write_text(format_report(report), encoding="utf-8")
    png_count, axis_changed = _write_comparison_pngs(
        cal, snaps, report, out_dir / "compare"
    )

    counts = report.counts()
    print(f"Recipe applied — {len(report.outcomes)} table outcomes:")
    for key in sorted(counts):
        print(f"    {key:18s} {counts[key]}")
    print(f"\n  saved bin      : {out_bin}")
    print(f"  checksums      : {'CLEAN' if clean else 'STALE — NOT flash-ready'}"
          f" ({', '.join(r.name for r in save_reports)})")
    print(f"  report         : {out_dir / 'report.md'}")
    print(f"  comparison PNGs: {png_count} under {out_dir / 'compare'}"
          f" ({axis_changed} axis-changed table(s) reported in text instead)")
    if report.do_not_flash():
        print("\n  ⛔ DO NOT FLASH — see the report's coherence section. "
              "This is revision 0; review, then iterate.")


if __name__ == "__main__":
    main()
