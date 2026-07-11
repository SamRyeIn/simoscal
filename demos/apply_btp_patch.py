#!/usr/bin/env python3
"""Apply a BinToolz ``.btp`` to the stock bin and produce a review bundle (plan U5).

Demonstrates the canonical pipeline order — **patch stock first** — end to end:

    btp.check(stock, patch)            read-only readiness (never writes)
        │
        ▼
    btp.apply(stock, patch, out.bin)   apply on a copy → confined-diff post-verify
        │                              + CAL_CRC/ECM3 checksum report (U1 contract)
        ▼
    btp.switch_patch_sanity(out.bin)   load the patched base against BinToolz's
        │                              S50 switch-patch XDF; slot tables decode
        ▼
    report.md                          human review gate (format_change_report)

Everything lands in a fresh timestamped folder under ``demos/apply_btp_patch_out/``
(gitignored via ``*_out/``). The saved patched bin is the **canonical base** future
tune revision scripts re-apply on top of. **This script never flashes**, and the
patched bin's ``CAL_CRC`` is left stale on purpose — correct it (``correct_checksums``)
before flashing, and a switch-patched bin needs a FULL flash (not CAL-only).
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
from pathlib import Path

from simoscal import btp

CODE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = CODE_ROOT.parent
STOCK_BIN = CODE_ROOT / "bin" / "5G0906259L__0002.bin"
SWITCH_PATCH = REPO_ROOT / "BinToolz-main" / "patches" / "SL PATCH.29.33 - S50.btp"
OUT_ROOT = Path(__file__).resolve().parent / "apply_btp_patch_out"


def main() -> None:
    for path in (STOCK_BIN, SWITCH_PATCH):
        if not path.is_file():
            raise SystemExit(f"missing required file: {path}")

    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = OUT_ROOT / f"switchpatch_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. Read-only readiness check.
    pre = btp.check(STOCK_BIN, SWITCH_PATCH)
    print(f"check           : {pre.readiness} "
          f"(hw {pre.bin_hardware}, sw {pre.bin_software_code})")
    if not pre.ready_to_apply:
        raise SystemExit(f"stock bin is not READY_TO_ACCEPT (state {pre.readiness}); aborting")

    # 2. Apply on a copy → post-verified output bin (input never touched).
    out_bin = out_dir / "5G0906259L__0002_switchpatch.bin"
    result = btp.apply(STOCK_BIN, SWITCH_PATCH, out_bin)

    # 3. Switch-patch XDF sanity load on the patched base (vs stock).
    sanity = btp.switch_patch_sanity(out_bin, stock_bin_path=STOCK_BIN)
    result = dataclasses.replace(result, sanity=sanity)

    # 4. Human-readable review report.
    (out_dir / "report.md").write_text(btp.format_change_report(result), encoding="utf-8")

    cal_crc = result.cal_crc
    ecm3 = result.ecm3
    print(f"apply           : {result.changed_bytes} bytes changed "
          f"({result.changed_in_cal} CAL), confined={result.confined}")
    print(f"CAL_CRC         : {'STALE — correct before flashing' if cal_crc and cal_crc.is_stale else 'clean'}")
    print(f"ECM3            : {'clean' if ecm3 and not ecm3.is_stale else 'STALE'}")
    print(f"XDF sanity      : resolved {sanity.tables_resolved}, decoded {sanity.tables_decoded}, "
          f"errors {len(sanity.decode_errors)}, differ-from-stock {sanity.differ_from_stock}, "
          f"plausible={sanity.plausible}")
    print(f"\n  patched base  : {out_bin}")
    print(f"  report        : {out_dir / 'report.md'}")
    print("\n  ⚠ DO NOT FLASH from here — correct CAL_CRC and full-flash externally. "
          "This patched bin is the canonical base for tune revisions.")


if __name__ == "__main__":
    main()
