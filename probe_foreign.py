"""Characterise a foreign bin + XDF (+ optional switch patch). READ-ONLY.

Answers, in one run, everything T4B needs to know about a newly-supplied file
structure: what box code it is, whether it is a full flashable image, whether
its checksums are self-consistent, what preflight says, and — the useful part —
*how far* its structure diverges from SC8S50, expressed as the list of profile
tables that fail to resolve.

Nothing is written. No session is opened. No bin is modified.

Usage:
    python probe_foreign.py <bin> <base-xdf> [switch-patch-xdf]
"""

from __future__ import annotations

import hashlib
import re
import struct
import sys
import time
from pathlib import Path

FULL_BIN_SIZE = 0x400000


def sha256(path: Path) -> str:
    d = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            d.update(chunk)
    return d.hexdigest()


def xdf_header(path: Path) -> dict:
    """Pull deftitle / BASEOFFSET / table count without a full parse."""
    head = path.read_text(errors="replace")
    title = re.search(r"<deftitle>(.*?)</deftitle>", head)
    base = re.search(r'<BASEOFFSET\s+offset="([^"]+)"\s+subtract="([^"]+)"', head)
    off_raw = base.group(1) if base else None
    off = int(off_raw, 0) if off_raw else None
    return {
        "deftitle": title.group(1) if title else "(none)",
        "baseoffset_raw": off_raw,
        "baseoffset": off,
        "subtract": base.group(2) if base else None,
        "tables": head.count("<XDFTABLE"),
        "constants": head.count("<XDFCONSTANT"),
    }


# ---- structure discovery ------------------------------------------------------ #
# This lived here first, as the U1 spike. It now lives in ``simoscal.checksum``
# as :func:`discover_structure`, because the library needs it to open a bin it
# was not written against. The probe calls it rather than keeping a second copy —
# a characterisation tool that disagrees with the library is worse than useless.

def main(argv: list[str]) -> int:
    if not 2 <= len(argv) <= 3:
        print(__doc__)
        return 2
    binp = Path(argv[0]).expanduser().resolve()
    xdfp = Path(argv[1]).expanduser().resolve()
    patchp = Path(argv[2]).expanduser().resolve() if len(argv) == 3 else None

    for p in [binp, xdfp] + ([patchp] if patchp else []):
        if not p.is_file():
            print(f"missing: {p}")
            return 2

    print("=" * 72)
    print("FILES")
    print("=" * 72)
    for label, p in [("bin", binp), ("base xdf", xdfp)] + ([("patch xdf", patchp)] if patchp else []):
        print(f"  {label:10s} {p.name}")
        print(f"  {'':10s} {p.stat().st_size:,} bytes   sha256 {sha256(p)[:16]}…")

    print()
    print("=" * 72)
    print("BIN SHAPE")
    print("=" * 72)
    size = binp.stat().st_size
    print(f"  size            {size:,} bytes ({size / 1024 / 1024:.2f} MiB)")
    print(f"  full 4 MiB?     {'YES' if size == FULL_BIN_SIZE else f'NO — expected {FULL_BIN_SIZE:,}'}")

    print()
    print("=" * 72)
    print("XDF HEADERS")
    print("=" * 72)
    for label, p in [("base", xdfp)] + ([("patch", patchp)] if patchp else []):
        h = xdf_header(p)
        print(f"  [{label}] {p.name}")
        print(f"      deftitle    {h['deftitle']}")
        print(f"      BASEOFFSET  {h['baseoffset_raw']}  = 0x{h['baseoffset']:X}"
              if h["baseoffset"] is not None else "      BASEOFFSET  (none)")
        print(f"      subtract    {h['subtract']}")
        print(f"      tables      {h['tables']:,}   constants {h['constants']:,}")
    # The SC8S50 comparison point, so divergence is visible rather than implied.
    print("      (SC8S50.V1.0 for reference: deftitle SC8S50.a2l, "
          "BASEOFFSET 0x200000, 3,912 tables)")

    print()
    print("=" * 72)
    print("CHECKSUMS")
    print("=" * 72)
    from simoscal import checksum
    data = binp.read_bytes()
    try:
        for r in checksum.verify(data):
            stored = "None" if r.stored is None else f"0x{r.stored:08X}"
            comp = "None" if r.computed is None else f"0x{r.computed:08X}"
            state = "STALE" if r.is_stale else ("ok" if r.can_verify else "cannot verify")
            print(f"  {r.name:12s} stored={stored} computed={comp}  → {state}")
    except Exception as exc:
        print(f"  RAISED {type(exc).__name__}: {exc}")

    print()
    print("=" * 72)
    print("STRUCTURE DISCOVERY — where is the CAL block, and where is ECM3?")
    print("=" * 72)
    print("  Candidates come from the ECM3 accumulator seed; a candidate is only")
    print("  accepted when its stored ECM3 value recomputes exactly. Run this on a")
    print("  known bin as a negative control.")
    print()
    from simoscal.checksum import StructureNotFound, discover_structure, verify
    try:
        spec = discover_structure(data)
    except StructureNotFound as exc:
        print(f"  NOT FOUND — {exc}")
    else:
        print(f"    cal_file_offset    0x{spec.cal_file_offset:06X}")
        print(f"    cal_base_address   0x{spec.cal_base_address:08X}")
        print(f"    cal crc covers to  0x{spec.cal_block_length:X} "
              f"(a declared block length is larger — 0x200 more on SC8S50)")
        print(f"    ecm3 header        file 0x{spec.ecm3_file_offset:06X} "
              f"(CAL-rel 0x{spec.ecm3_header:X})")
        print(f"    ecm3 addresses     file "
              f"0x{spec.asw_file_offset + spec.ecm3_addr_locs[0]:06X}")
        print()
        for r in verify(data, spec):
            print(f"    {r.message()}")

    print()
    print("=" * 72)
    print("PREFLIGHT VERDICT")
    print("=" * 72)
    from simoscal.preflight import preflight
    t0 = time.time()
    try:
        v = preflight(binp, xdfp, switch_patch_xdf=patchp)
        print(f"  ({time.time() - t0:.1f}s)")
        print(f"  status      {v.status}")
        print(f"  ok_to_edit  {v.ok_to_edit}")
        print(f"  writable    {v.writable}")
        print(f"  profile     {v.profile_name}  (matched={v.profile_matched})")
        print(f"  summary     {v.summary}")
        detail = getattr(v, "detail", None) or getattr(v, "advanced", None)
        if detail:
            print(f"  detail      {detail}")
    except Exception as exc:
        print(f"  ({time.time() - t0:.1f}s) RAISED {type(exc).__name__}: {str(exc)[:300]}")

    print()
    print("=" * 72)
    print("STRUCTURAL DIVERGENCE FROM SC8S50")
    print("=" * 72)
    print("  How many SC8S50 profile tables fail to resolve against this XDF.")
    print("  0 misses = same structure. Many misses = genuinely different.")
    print()
    from simoscal.calfile import CalFile
    from simoscal.tune.profile import resolve, ProfileResolutionError
    from simoscal.tune.profiles import SC8S50
    try:
        cal = CalFile.open(str(xdfp), str(binp))
    except Exception as exc:
        print(f"  CalFile.open RAISED {type(exc).__name__}: {str(exc)[:200]}")
        cal = None
    if cal is not None:
        try:
            resolve(SC8S50, cal, xdf_label=str(xdfp))
            print(f"  SC8S50: ALL {len(SC8S50.names())} tables resolved — same structure.")
        except ProfileResolutionError as exc:
            total = len(SC8S50.names())
            print(f"  SC8S50: {len(exc.misses)} of {total} tables did NOT resolve.")
            for m in exc.misses[:15]:
                print(m.format())
            if len(exc.misses) > 15:
                print(f"      … and {len(exc.misses) - 15} more")

        if patchp is not None:
            from simoscal.tune.profiles.switchpatch_2933 import SWITCH_PATCH_2933
            print()
            try:
                patch_cal = CalFile.open(str(patchp), str(binp))
                resolve(SWITCH_PATCH_2933, patch_cal, xdf_label=str(patchp))
                n = len(SWITCH_PATCH_2933.names())
                print(f"  SWITCH_PATCH_2933: ALL {n} tables resolved against the patch XDF.")
            except ProfileResolutionError as exc:
                total = len(SWITCH_PATCH_2933.names())
                print(f"  SWITCH_PATCH_2933: {len(exc.misses)} of {total} did NOT resolve.")
                for m in exc.misses[:10]:
                    print(m.format())
            except Exception as exc:
                print(f"  SWITCH_PATCH_2933 RAISED {type(exc).__name__}: {str(exc)[:200]}")

    print()
    print("=" * 72)
    print("INTEGRITY — the source files must be untouched")
    print("=" * 72)
    for label, p in [("bin", binp), ("base xdf", xdfp)] + ([("patch xdf", patchp)] if patchp else []):
        print(f"  {label:10s} sha256 {sha256(p)[:16]}…  (compare to the FILES block above)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
