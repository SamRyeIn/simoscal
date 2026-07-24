"""Generate the host-side V0 parity golden, or compare a device report against it.

The V0 gate is a *comparison*, so it needs two runs of the same code: this
script produces the host half on desktop CPython, and the Android
instrumentation harness produces the device half by importing the very same
``simoscal_v0_parity`` module out of the Chaquopy source set.

Usage::

    # Write the host golden
    python android/parity/run_host_parity.py --out android/parity/golden_host.json

    # Compare a report pulled off the device
    python android/parity/run_host_parity.py --compare device_report.json

Run this under the **same Python minor version Chaquopy embeds** (3.13). A 3.14
host and a 3.13 device disagreeing tells you nothing about Chaquopy, so the
script refuses the mismatch by default rather than producing a golden that would
fail the gate for the wrong reason.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# The parity payload is the single shared source of truth; import it from the
# Chaquopy source set rather than keeping a desktop copy that could drift.
PARITY_MODULE_DIR = Path(__file__).resolve().parent.parent / "engine/src/main/python"
sys.path.insert(0, str(PARITY_MODULE_DIR))

import simoscal_v0_parity as parity  # noqa: E402

#: The Python minor version Chaquopy embeds for this project (see V0 pinning).
EXPECTED_PYTHON = (3, 13)

CODE_ROOT = Path(__file__).resolve().parent.parent.parent
REPO_ROOT = CODE_ROOT.parent

XDF_PATH = CODE_ROOT / "xdf" / "SC8S50.V1.0.xdf"
BIN_PATH = CODE_ROOT / "bin" / "5G0906259L__0002.bin"
SWITCH_XDF = REPO_ROOT / "BinToolz-main" / "definitions" / "S50 Switch Patch.29.33.V2.xdf"
PATCHED_BIN = (
    REPO_ROOT / "Tunes" / "TuningBasicsGuide" / "BinToolz-patched"
    / "CB_HSL_SP2933_5G0906259L_0002_BasicsGuide_R04.bin"
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, help="write the host golden report here")
    ap.add_argument("--compare", type=Path, help="compare this device report to a fresh host run")
    ap.add_argument("--work-dir", type=Path, default=CODE_ROOT / "build" / "v0_parity")
    ap.add_argument("--xdf", type=Path, default=XDF_PATH)
    ap.add_argument("--bin", type=Path, default=BIN_PATH)
    ap.add_argument("--patch-xdf", type=Path, default=SWITCH_XDF)
    ap.add_argument("--patched-bin", type=Path, default=PATCHED_BIN)
    ap.add_argument(
        "--no-decode-all", action="store_true",
        help="skip the full-table decode sweep (metadata only, much faster)",
    )
    ap.add_argument(
        "--allow-python-mismatch", action="store_true",
        help="run even if this interpreter is not the Chaquopy-pinned minor version",
    )
    args = ap.parse_args()

    actual = sys.version_info[:2]
    if actual != EXPECTED_PYTHON and not args.allow_python_mismatch:
        print(
            f"refusing to run: host Python is {actual[0]}.{actual[1]}, but Chaquopy "
            f"embeds {EXPECTED_PYTHON[0]}.{EXPECTED_PYTHON[1]}. A golden from a "
            "different minor version cannot answer the Chaquopy question. Re-run "
            "under the pinned version, or pass --allow-python-mismatch if you are "
            "deliberately measuring the version delta.",
            file=sys.stderr,
        )
        return 2

    # The boost leg is skipped explicitly (and visibly, in the compared report)
    # when the switch-patch inputs are not present in this checkout.
    have_patch = args.patch_xdf.exists() and args.patched_bin.exists()
    if not have_patch:
        print(
            f"note: boost-curve leg will record SKIPPED — "
            f"patch XDF present={args.patch_xdf.exists()}, "
            f"patched bin present={args.patched_bin.exists()}",
            file=sys.stderr,
        )

    for label, path in (("XDF", args.xdf), ("bin", args.bin)):
        if not path.exists():
            print(f"missing required {label}: {path}", file=sys.stderr)
            return 2

    report = parity.run_parity(
        xdf_path=str(args.xdf),
        bin_path=str(args.bin),
        work_dir=str(args.work_dir),
        patch_xdf_path=str(args.patch_xdf) if have_patch else None,
        patched_bin_path=str(args.patched_bin) if have_patch else None,
        decode_all=not args.no_decode_all,
    )

    if args.compare:
        device = json.loads(args.compare.read_text())
        result = parity.compare(report, device)
        print(json.dumps(result, indent=2, sort_keys=True))
        if result["match"]:
            print("\nPARITY: MATCH — host and device agree on every compared field.")
            return 0
        print(
            f"\nPARITY: MISMATCH — {result['diff_count']} differing field(s). "
            "This is a V0 no-go; see the plan's go/no-go clause.",
            file=sys.stderr,
        )
        return 1

    text = json.dumps(report, indent=2, sort_keys=True)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text)
        print(f"wrote host golden: {args.out}")
    else:
        print(text)

    print(f"\ndigest: {report['digest']}", file=sys.stderr)
    for key, value in sorted(report["timings"].items()):
        print(f"  {key}: {float(value):.2f}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
