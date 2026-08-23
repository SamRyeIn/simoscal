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


# ---- ECM3 / CAL structure discovery ------------------------------------------- #
# The ECM3 monitor seeds its 64-bit accumulator from a fixed constant stored at
# header+8/+12. That makes the constant a searchable signature: every occurrence
# is a candidate header. A candidate is only *accepted* when the ECM3 value
# stored at it equals the value recomputed over the areas it points at, so the
# search cannot succeed by finding a plausible-looking byte pattern.

ECM3_HEADER_REL = 0x400  # ECM3 header, CAL-relative — the one constant assumed
ECM3_SEED_HI = 0x01234567
ECM3_SEED_LO = 0x89ABCDEF
ECM3_SEED = struct.pack("<II", ECM3_SEED_HI, ECM3_SEED_LO)
MAX_SANE_AREAS = 16
CACHED_ALIAS = 0x20000000


def _u32(buf, off: int) -> int:
    return struct.unpack_from("<I", buf, off)[0]


def _seed_candidates(data: bytes) -> list[int]:
    """Header offsets implied by every occurrence of the ECM3 seed."""
    out, i = [], data.find(ECM3_SEED)
    while i != -1:
        out.append(i - 8)
        i = data.find(ECM3_SEED, i + 1)
    return out


def _cal_crc_layout(data: bytes, cal_off: int):
    """Derive ``(base_address, areas)`` from the CAL CRC header at CAL-rel 0x300.

    Area 0 always starts at CAL offset 0, so its stored address *is* the CAL base
    address — the base is read off the file rather than assumed.
    """
    hdr = cal_off + 0x300
    if hdr + 12 > len(data):
        return None
    count = _u32(data, hdr + 8)
    if not 1 <= count <= MAX_SANE_AREAS:
        return None
    addrs = [_u32(data, hdr + 12 + k * 4) for k in range(2 * count)]
    base = addrs[0]
    areas = []
    for i in range(count):
        start, end = addrs[2 * i] - base, addrs[2 * i + 1] - base
        if not 0 <= start <= end:
            return None
        areas.append((start, end))
    return base, areas


def _resolve_pairs(src, loc: int, count: int, base: int, cal_len: int):
    """ECM3 area address pairs at ``loc``, as CAL-relative offsets, or None."""
    out = []
    for i in range(count):
        pair = []
        for k in range(2):
            a = _u32(src, loc + (2 * i + k) * 4)
            off = a - base
            if off < 0:
                off = a + CACHED_ALIAS - base
            if not 0 <= off <= cal_len:
                return None
            pair.append(off)
        if pair[0] > pair[1]:
            return None
        out.append((pair[0], pair[1]))
    return out


def _ecm3_sum(data: bytes, cal_off: int, areas) -> int:
    acc = (ECM3_SEED_HI << 32) + ECM3_SEED_LO
    for start, end in areas:
        for j in range(cal_off + start, cal_off + end, 4):
            acc += _u32(data, j)
    return acc & 0xFFFFFFFFFFFFFFFF


def discover_structure(data: bytes) -> list[dict]:
    """Locate the CAL block and ECM3 header in a full bin. Verification-gated.

    Run this on a *known* bin as a negative control: on the SC8S50 stock bin it
    must rediscover CAL 0x200000 / ECM3 0x200400 and accept nothing else.
    """
    accepted: list[dict] = []
    for hdr in _seed_candidates(data):
        cal_off = hdr - ECM3_HEADER_REL
        if cal_off < 0:
            print(f"    0x{hdr:06X}  rejected — implies a negative CAL offset")
            continue
        layout = _cal_crc_layout(data, cal_off)
        if layout is None:
            print(f"    0x{hdr:06X}  rejected — no sane CAL CRC header at "
                  f"0x{cal_off + 0x300:06X}, so this is not a CAL block start")
            continue
        base, crc_areas = layout
        cal_len = crc_areas[-1][1] + 1
        count = _u32(data, hdr + 16)
        if not 1 <= count <= MAX_SANE_AREAS:
            print(f"    0x{hdr:06X}  rejected — ECM3 area count {count} out of range")
            continue
        print(f"    0x{hdr:06X}  CAL block 0x{cal_off:06X}, base 0x{base:08X}, "
              f"CRC covers to 0x{cal_len:X}, ECM3 areas {count}")

        # Area addresses live inline in CAL, or in the ASW block near its own
        # ECM3 header. Every location is tried; arithmetic decides the winner.
        sources = []
        if _u32(data, hdr + 24) > 0:
            sources.append(("inline in CAL", hdr + 24))
        for other in _seed_candidates(data):
            if other != hdr:
                sources.extend(
                    (f"ASW block, 0x{d:X} below its header 0x{other:06X}", other - d)
                    for d in (0x20, 0x40) if other - d >= 0
                )

        stored_rel = ECM3_HEADER_REL + 56 if data[cal_off + ECM3_HEADER_REL + 56] else ECM3_HEADER_REL
        sloc = cal_off + stored_rel
        stored = (_u32(data, sloc) << 32) + _u32(data, sloc + 4)
        for note, loc in sources:
            areas = _resolve_pairs(data, loc, count, base, cal_len)
            if areas is None:
                continue
            computed = _ecm3_sum(data, cal_off, areas)
            verdict = "VERIFIES" if computed == stored else "mismatch"
            print(f"        addresses {note}")
            print(f"          areas    {[(hex(s), hex(e)) for s, e in areas]}")
            print(f"          stored   0x{stored:016X} (CAL-rel 0x{stored_rel:X})")
            print(f"          computed 0x{computed:016X}   -> {verdict}")
            if computed == stored:
                accepted.append({
                    "ecm3_header_file": hdr, "cal_file_offset": cal_off,
                    "cal_base_address": base, "cal_crc_last_covered": cal_len,
                    "ecm3_addr_file": loc, "areas": areas,
                })
                break
    return accepted



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
    found = discover_structure(data)
    print()
    if not found:
        print("  ACCEPTED: none — the ECM3 header could not be located in this bin.")
    else:
        print(f"  ACCEPTED: {len(found)}")
        for f in found:
            print(f"    CAL_FILE_OFFSET   0x{f['cal_file_offset']:06X}")
            print(f"    CAL_BASE_ADDRESS  0x{f['cal_base_address']:08X}")
            print(f"    CAL CRC covers to 0x{f['cal_crc_last_covered']:X} "
                  f"(declared CAL_BLOCK_LENGTH is larger — 0x200 more on SC8S50)")
            print(f"    ECM3 header       file 0x{f['ecm3_header_file']:06X} "
                  f"(CAL-rel 0x{f['ecm3_header_file'] - f['cal_file_offset']:X})")
            print(f"    ECM3 addresses    file 0x{f['ecm3_addr_file']:06X}")

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
