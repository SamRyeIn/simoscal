"""U3 — the check registry, the runner, and the findings data model.

A *check* is a piece of registry data (:class:`Check`): an id, a title, the
canonical channels it requires/prefers, a dict of inspectable thresholds, a
``needs_cal`` flag, and a ``compute`` function returning findings. The runner
(:func:`run_battery`) executes exactly the checks whose required channels are
present (and, for ``needs_cal`` checks, whose calibration is resolved),
collecting :class:`Finding`s from those that ran and :class:`Skipped` records —
naming the missing channel or reason — for those that could not. Everything the
battery emits is inspectable and enumerable without running any check
(:func:`format_battery`), which is what makes the battery auditable (R1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .log import LogSet
from .pulls import Pull

__all__ = [
    "Severity",
    "SEVERITY_RANK",
    "Finding",
    "Skipped",
    "Check",
    "CheckContext",
    "BatteryResult",
    "run_battery",
    "format_battery",
]


class Severity:
    """Finding severities, most to least urgent. Plain strings for display."""

    HIGH = "High"
    MEDIUM = "Medium"
    LOW = "Low"


# Deterministic ordering: High first. Unknown severities sort last.
SEVERITY_RANK: dict[str, int] = {Severity.HIGH: 0, Severity.MEDIUM: 1, Severity.LOW: 2}


@dataclass(frozen=True)
class Finding:
    """One result from a check that ran.

    ``evidence`` holds the supporting scalar values (floats/ints/strings);
    ``pull_refs`` are 1-based pull indices the finding draws on; ``plot_refs``
    are evidence-plot filenames (populated later by the evidence layer, U5).
    """

    check_id: str
    severity: str
    title: str
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)
    pull_refs: tuple[int, ...] = ()
    plot_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class Skipped:
    """A check that could not run — a required channel or the bin was absent."""

    check_id: str
    title: str
    reason: str
    missing_channels: tuple[str, ...] = ()


@dataclass
class CheckContext:
    """Everything a check's ``compute`` function reads.

    ``cal`` is an optional opened calibration (a ``simoscal.CalFile`` or None);
    only ``needs_cal`` checks touch it, and the runner guarantees it is present
    before such a check runs.
    """

    logset: LogSet
    pulls: list[Pull]
    cal: Optional[Any] = None


@dataclass(frozen=True)
class Check:
    """One registry entry. ``compute(ctx, check) -> list[Finding]``."""

    id: str
    title: str
    required_channels: tuple[str, ...]
    compute: Callable[[CheckContext, "Check"], list[Finding]]
    optional_channels: tuple[str, ...] = ()
    thresholds: dict[str, Any] = field(default_factory=dict)
    needs_cal: bool = False
    description: str = ""

    def availability(self, ctx: CheckContext) -> tuple[bool, list[str], str]:
        """Return ``(can_run, missing_channels, reason)`` for this check.

        A required channel absent from the log set's channel *union* blocks the
        check; so does a ``needs_cal`` check with no resolved calibration.
        """
        available = ctx.logset.channels()
        missing = [c for c in self.required_channels if c not in available]
        if missing:
            return False, missing, f"missing required channel(s): {', '.join(missing)}"
        if self.needs_cal and ctx.cal is None:
            return False, [], "calibration-aware check but no bin/XDF resolved"
        return True, [], ""


@dataclass(frozen=True)
class BatteryResult:
    """The outcome of running a battery: findings, skips, and provenance."""

    checks: tuple[Check, ...]        # the full battery, in registry order
    ran: tuple[str, ...]             # ids of checks that executed
    findings: tuple[Finding, ...]    # sorted: severity, then check id, then message
    skipped: tuple[Skipped, ...]     # sorted by check id
    pulls: tuple[Pull, ...]
    logset: LogSet
    cal_resolved: bool

    def findings_by_severity(self, severity: str) -> list[Finding]:
        return [f for f in self.findings if f.severity == severity]

    @property
    def high_findings(self) -> list[Finding]:
        return self.findings_by_severity(Severity.HIGH)


def _finding_sort_key(f: Finding) -> tuple[int, str, str]:
    return (SEVERITY_RANK.get(f.severity, 99), f.check_id, f.message)


def run_battery(
    checks: list[Check] | tuple[Check, ...],
    ctx: CheckContext,
) -> BatteryResult:
    """Run every available check in ``checks`` against ``ctx``.

    Checks whose required channels (or calibration) are absent are recorded as
    :class:`Skipped` rather than run. Findings are returned in a deterministic
    order (severity, then check id, then message) so identical inputs yield
    identical output (R6).
    """
    checks = tuple(checks)
    ran: list[str] = []
    findings: list[Finding] = []
    skipped: list[Skipped] = []

    for check in checks:
        can_run, missing, reason = check.availability(ctx)
        if not can_run:
            skipped.append(
                Skipped(check.id, check.title, reason, tuple(missing))
            )
            continue
        ran.append(check.id)
        findings.extend(check.compute(ctx, check))

    findings.sort(key=_finding_sort_key)
    skipped.sort(key=lambda s: s.check_id)

    return BatteryResult(
        checks=checks,
        ran=tuple(ran),
        findings=tuple(findings),
        skipped=tuple(skipped),
        pulls=tuple(ctx.pulls),
        logset=ctx.logset,
        cal_resolved=ctx.cal is not None,
    )


def format_battery(checks: list[Check] | tuple[Check, ...]) -> str:
    """Render the full battery (id, title, channels, thresholds) as text.

    Printable without running any check — the auditable enumeration of what the
    tool looks at (R1).
    """
    lines: list[str] = [f"Check battery ({len(tuple(checks))} checks):", ""]
    for check in checks:
        cal = " [needs cal]" if check.needs_cal else ""
        lines.append(f"- {check.id}: {check.title}{cal}")
        if check.description:
            lines.append(f"    {check.description}")
        lines.append(f"    required: {', '.join(check.required_channels) or '(none)'}")
        if check.optional_channels:
            lines.append(f"    optional: {', '.join(check.optional_channels)}")
        if check.thresholds:
            thr = ", ".join(f"{k}={v}" for k, v in check.thresholds.items())
            lines.append(f"    thresholds: {thr}")
    return "\n".join(lines)
