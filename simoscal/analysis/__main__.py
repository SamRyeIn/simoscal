"""``python -m simoscal.analysis <log-folder>`` — run the battery on a folder.

A thin wrapper over :func:`simoscal.analysis.analyze_folder`. Writes
``analysis_findings.{json,md}`` and evidence plots into the folder and prints a
short summary. ``--print-battery`` enumerates the battery (with thresholds)
without running anything.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .checks import default_battery
from .evidence import analyze_folder
from .registry import Severity, format_battery


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m simoscal.analysis",
        description="Run the log-analysis battery over a Logs/<Tune>_R<NN>/ folder.",
    )
    parser.add_argument("folder", nargs="?", help="the log folder to analyze")
    parser.add_argument("--xdf", help="override the XDF definition path")
    parser.add_argument("--bin", dest="bin_path", help="override the flashed bin path")
    parser.add_argument("--no-plots", action="store_true", help="skip evidence plots")
    parser.add_argument("--print-battery", action="store_true",
                        help="print the check battery and exit (runs nothing)")
    args = parser.parse_args(argv)

    if args.print_battery:
        print(format_battery(default_battery()))
        return 0

    if not args.folder:
        parser.error("a log folder is required (or use --print-battery)")

    out = analyze_folder(
        Path(args.folder), xdf_path=args.xdf, bin_path=args.bin_path,
        make_plots=not args.no_plots,
    )
    r = out.result
    print(f"Analyzed {out.result.logset.folder}")
    print(f"  files: {len(r.logset)}   pulls: {len(r.pulls)}   "
          f"cal: {'resolved' if r.cal_resolved else 'not resolved'}")
    for sev in (Severity.HIGH, Severity.MEDIUM, Severity.LOW):
        n = len(r.findings_by_severity(sev))
        if n:
            print(f"  {sev}: {n}")
    print(f"  skipped: {len(r.skipped)}")
    print(f"  wrote: {out.json_path.name}, {out.md_path.name}, {len(out.plot_paths)} plots")
    return 0


if __name__ == "__main__":
    sys.exit(main())
