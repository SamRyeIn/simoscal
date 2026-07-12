"""simoscal.analysis — the log-analysis battery.

Runs an identical, enumerable battery of checks against a
``Logs/<Tune>_R<NN>/`` folder of SimosTools datalog CSVs and writes a
machine-readable findings file, a rendered Markdown summary, an explicit
SKIPPED list, and evidence plots into that folder. Claude consumes the output
to write ``log_review.md``; **nothing here writes or proposes calibration
changes** — this is a read-only, findings-only tool.

Operating principle, inherited from the rest of ``simoscal``: *fail loud,
change nothing silently, never guess.* A channel that cannot be confidently
resolved is reported unmapped rather than mis-scaled; a check whose required
channels are absent lands in SKIPPED rather than firing on wrong data.

Layout (this subpackage):

- ``log.py``      — parse a folder of CSVs into a :class:`LogSet` of canonical,
  unit-normalized channels, with a log-quality preflight riding along as
  metadata (U1).
- ``pulls.py``    — segment WOT pulls and compute the per-pull summary (U2).
- ``registry.py`` — the ``Check`` type, the runner, and the findings data
  model (U3).
- ``report.py``   — deterministic JSON + Markdown emitters (U3).
- ``checks.py``   — the v1 check battery (U4).
- ``coverage.py`` — per-table cell-coverage maps (U7).
- ``evidence.py`` — evidence plots + the ``analyze_folder`` entry point (U5).
- ``__main__.py`` — ``python -m simoscal.analysis <log-folder>`` (U5).
"""

from __future__ import annotations

from .log import (
    AnalysisError,
    CHANNEL_SPECS,
    ChannelSpec,
    DuplicateChannelError,
    GapEvent,
    GearResolution,
    LogFile,
    LogQuality,
    LogSet,
    SPEC_BY_ID,
    load_logfile,
    load_logset,
)
from .pulls import (
    PULL_DETECTION_CONSTANTS,
    Pull,
    PullEnvironment,
    detect_pulls,
)
from .registry import (
    BatteryResult,
    Check,
    CheckContext,
    Finding,
    Severity,
    Skipped,
    format_battery,
    run_battery,
)
from .report import (
    findings_to_dict,
    md_table,
    render_markdown,
    write_findings,
)
from .checks import default_battery
from .evidence import AnalyzeResult, analyze_folder, resolve_bin, resolve_xdf

__all__ = [
    "AnalysisError",
    "DuplicateChannelError",
    "ChannelSpec",
    "CHANNEL_SPECS",
    "SPEC_BY_ID",
    "GearResolution",
    "GapEvent",
    "LogQuality",
    "LogFile",
    "LogSet",
    "load_logfile",
    "load_logset",
    "PULL_DETECTION_CONSTANTS",
    "Pull",
    "PullEnvironment",
    "detect_pulls",
    "BatteryResult",
    "Check",
    "CheckContext",
    "Finding",
    "Severity",
    "Skipped",
    "format_battery",
    "run_battery",
    "findings_to_dict",
    "md_table",
    "render_markdown",
    "write_findings",
    "default_battery",
    "AnalyzeResult",
    "analyze_folder",
    "resolve_bin",
    "resolve_xdf",
]
