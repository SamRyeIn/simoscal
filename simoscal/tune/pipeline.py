"""``build()`` — the one entry point that turns edits into a reviewable revision.

Every R00–R12 revision script ends with the same ~60 lines: save, correct
checksums, reopen, read everything back, diff against the previous revision,
draw comparison plots, assemble a report, collect the failures, exit non-zero
if there are any. Re-typing that per revision is how a gate gets dropped —
and a dropped gate is not visible in the output, because the output looks
exactly the same right up until the bin is wrong.

So it lives here, once, and runs in a fixed order:

1. **save** the shared buffer with checksums corrected;
2. **verify** the saved bin's checksums independently of the save;
3. **read back** every journaled table off the saved file and compare it to
   what the journal says it should be — proving the edits survived the round
   trip, not merely that they were staged;
4. **audit** the saved bin against a declared reference, byte for byte, with
   the allowance derived from the journal;
5. **draw** before/after comparison plots for the changed tables;
6. **report** the journal, the verdicts, and the artifact list as Markdown.

Any failed gate raises :class:`BuildFailed` *after* the report is written, so
the failure is reviewable rather than merely reported on the console. No
partial success: a build either produced a bin that passed every gate, or it
raised.

Never flashes. Nothing in this package does.
"""

from __future__ import annotations

import datetime as _dt
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence, Union

import numpy as np

from ..calfile import CalFile
from ..checksum import ChecksumReport, StaleChecksumWarning
from ..model import SimosCalError
from ..render import render_table
from . import audit
from .report_html import render_report_html
from .journal import (
    KIND_CHECK,
    VERDICT_BLOCKED,
    VERDICT_SUPERSEDED,
    VERDICT_UNCHANGED,
    EditEntry,
    Journal,
)
from .project import BASE_SPACE, Tune

__all__ = ["BuildFailed", "BuildResult", "GateOutcome", "build", "run_gates"]

#: Physical-unit tolerance for the final-bin readback. A table is stored
#: quantized, so a written 3.1 reads back as 3.100098; anything past this is a
#: write that did not land as intended.
READBACK_ATOL = 5e-3

#: The three checksum verdicts a build can reach. Only ``CLEAN`` may flash.
#: ``UNVERIFIABLE`` is distinct from ``STALE`` on purpose: a stale checksum was
#: verified and found wrong, while an unverifiable one could not be checked at
#: all (a malformed, short, or unsupported layout — real states the checksum
#: layer returns, not mock-only). Both fail the build; conflating "could not
#: check" with "checked and fine" is the CR-20260720-01 hazard.
CHECKSUM_CLEAN = "CLEAN"
CHECKSUM_STALE = "STALE — DO NOT FLASH"
CHECKSUM_UNVERIFIABLE = "UNVERIFIABLE — DO NOT FLASH"


def _checksum_state(checksums: Sequence[ChecksumReport]) -> str:
    """Classify a set of checksum reports into one flash-gating verdict.

    Clean requires the reports to be *present* and every one of them verified
    and current. No reports at all is ``UNVERIFIABLE``, not vacuously clean: a
    build that could not produce a single checksum verdict has not passed the
    checksum gate.
    """
    if not checksums:
        return CHECKSUM_UNVERIFIABLE
    if any(r.can_verify and r.is_stale for r in checksums):
        return CHECKSUM_STALE
    if any(not r.can_verify for r in checksums):
        return CHECKSUM_UNVERIFIABLE
    return CHECKSUM_CLEAN


class BuildFailed(SimosCalError):
    """One or more build gates failed. The report is still on disk."""

    def __init__(self, revision: str, problems: Sequence[str], out_dir: Path):
        self.revision = revision
        self.problems = tuple(problems)
        self.out_dir = out_dir
        super().__init__(
            f"{revision} verification failed: {'; '.join(problems)}. "
            f"Report written to {out_dir / 'report.md'} — the bin is NOT "
            "flash-ready."
        )


@dataclass
class GateOutcome:
    """The verified core of a build: the bin was saved and every mandatory gate
    voted, with nothing rendered yet.

    :func:`run_gates` is the safety spine — save, checksum verify, readback,
    blocked-write, recipe coherence, post-save checks, byte audit — factored out
    of :func:`build` so it lives *once*. Rendering (comparison PNGs, ``report.md``,
    ``report.html``, the app's report model) is layered on top of this outcome.
    Because it imports no matplotlib, an embedded runtime (the Android engine)
    can run the full gate chain and read the verdicts without the desktop plotting
    stack — which is what :mod:`simoscal.tune.build_service` is built on.
    """

    bin_path: Path
    checksums: tuple[ChecksumReport, ...]
    checksum_state: str
    readback_failures: tuple[str, ...]
    diff: Optional[audit.RawDiffAudit]
    problems: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def checksums_clean(self) -> bool:
        return self.checksum_state == CHECKSUM_CLEAN


@dataclass
class BuildResult:
    """What a successful build produced, and how each gate voted."""

    revision: str
    out_dir: Path
    bin_path: Path
    report_path: Path
    journal: Journal
    checksums: tuple[ChecksumReport, ...] = ()
    readback_failures: tuple[str, ...] = ()
    diff: Optional[audit.RawDiffAudit] = None
    plots: tuple[Path, ...] = ()
    #: Comparison plots grouped by the ``(space, XDF key)`` of the table they
    #: depict, in the order tables were touched. The flat :attr:`plots` is the
    #: same paths ungrouped; this mapping lets a reviewer-facing renderer show a
    #: changed table's plot next to that table without re-resolving names.
    plots_by_table: dict[tuple[str, Union[str, int]], tuple[Path, ...]] = None  # type: ignore[assignment]
    html_report_path: Optional[Path] = None
    problems: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.plots_by_table is None:
            self.plots_by_table = {}

    @property
    def checksum_state(self) -> str:
        """One of :data:`CHECKSUM_CLEAN` / ``STALE`` / ``UNVERIFIABLE``."""
        return _checksum_state(self.checksums)

    @property
    def checksums_clean(self) -> bool:
        return self.checksum_state == CHECKSUM_CLEAN

    @property
    def ok(self) -> bool:
        return not self.problems


def build(
    tune: Tune,
    revision: str,
    *,
    out_root: Union[str, Path],
    bin_name: str = "",
    reference_bin: Optional[Union[str, Path]] = None,
    title: str = "",
    summary: str = "",
    plots: bool = True,
    extra_allowances: Sequence[audit.Allowance] = (),
) -> BuildResult:
    """Verify and emit ``tune`` as revision ``revision``.

    Writes into a fresh timestamped ``<out_root>/<revision>_<stamp>/`` folder,
    matching the existing convention so prior runs are never overwritten.

    ``reference_bin`` is the previous revision's output. Supplying it turns on
    the byte-level audit, which is the gate that catches a change nobody
    declared; omitting it (a first revision has no predecessor) skips that gate
    and says so in the report.
    """
    out_root = Path(out_root)
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = out_root / f"{revision}_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    bin_path = out_dir / (bin_name or f"{revision}.bin")

    # 1–4. save + the mandatory verification gates, factored into run_gates so
    # the safety spine lives once and cannot drift between the desktop build and
    # the embedded build service. -------------------------------------------- #
    outcome = run_gates(
        tune, bin_path,
        reference_bin=reference_bin, extra_allowances=extra_allowances,
    )

    # 5. comparison plots ---------------------------------------------------- #
    plots_by_table: dict[tuple[str, Union[str, int]], tuple[Path, ...]] = {}
    if plots and reference_bin is not None:
        plots_by_table = _compare_plots(tune, reference_bin, bin_path, out_dir / "compare")
    plot_paths = tuple(p for group in plots_by_table.values() for p in group)

    # 6. report --------------------------------------------------------------- #
    result = BuildResult(
        revision=revision,
        out_dir=out_dir,
        bin_path=bin_path,
        report_path=out_dir / "report.md",
        journal=tune.journal,
        checksums=outcome.checksums,
        readback_failures=outcome.readback_failures,
        diff=outcome.diff,
        plots=plot_paths,
        plots_by_table=plots_by_table,
        html_report_path=out_dir / "report.html",
        problems=outcome.problems,
    )
    result.report_path.write_text(
        render_report(tune, result, title=title, summary=summary), encoding="utf-8"
    )
    # The reviewer-facing page: same journal and gate verdicts as report.md,
    # laid out for a browser at the flash gate. Rendered from the journal too,
    # so it cannot drift from report.md or from what the build did.
    result.html_report_path.write_text(
        render_report_html(tune, result, title=title, summary=summary),
        encoding="utf-8",
    )

    if outcome.problems:
        raise BuildFailed(revision, outcome.problems, out_dir)
    return result


# --------------------------------------------------------------------------- #
# gates
# --------------------------------------------------------------------------- #
def run_gates(
    tune: Tune,
    bin_path: Union[str, Path],
    *,
    reference_bin: Optional[Union[str, Path]] = None,
    extra_allowances: Sequence[audit.Allowance] = (),
) -> GateOutcome:
    """Save ``tune`` to ``bin_path`` and run every mandatory verification gate.

    This is the safety spine of a build, in a fixed order: save with checksums
    corrected; verify those checksums independently off the written file; read
    every journaled table back off the saved bin; fail on any guard-blocked
    write, any DO-NOT-FLASH recipe-coherence finding, and any failed post-save
    check; then, when a ``reference_bin`` is declared, audit the saved bin
    against it byte for byte with an allowance derived from the journal.

    Returns a :class:`GateOutcome` whose :attr:`~GateOutcome.problems` is the
    collected list of gate failures — the caller decides how to surface them
    (raise, render a report, or return a model). Renders nothing and imports no
    matplotlib, so an embedded runtime can run the whole chain and read the
    verdicts. Never flashes.
    """
    bin_path = Path(bin_path)
    problems: list[str] = []

    # 1. save (checksums corrected) --------------------------------------- #
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", StaleChecksumWarning)
        tune.save(bin_path, correct_checksums=True)

    # 2. verify — independently of the save, off the file that was written -- #
    verify_cal = CalFile.open(str(tune.space(BASE_SPACE).xdf), str(bin_path))
    checksums = tuple(verify_cal.verify_checksums())
    checksum_state = _checksum_state(checksums)
    if checksum_state != CHECKSUM_CLEAN:
        problems.append(f"checksums {checksum_state}")

    # 3. read back every journaled table off the saved bin ------------------ #
    readback_failures = _readback(tune, bin_path)
    if readback_failures:
        problems.append(f"{len(readback_failures)} table(s) failed readback")

    if tune.journal.blocked():
        blocked = ", ".join(e.label for e in tune.journal.blocked())
        problems.append(f"guard blocked an intended write: {blocked}")

    # The SOP's coherence rules, when the recipe ran — they catch a boost
    # change shipped without the matching fuelling, which no per-table gate can.
    coherence = tune.recipe_report.coherence() if tune.recipe_report else []
    for finding in coherence:
        if finding.severity == "DO NOT FLASH":
            problems.append(f"recipe coherence: {finding.message}")

    # 3b. gates that only the finished file can answer ---------------------- #
    for check in tune.post_checks:
        try:
            passed, detail = check.run(bin_path)
        except Exception as exc:  # noqa: BLE001 - a gate that cannot run is a failure
            passed, detail = False, f"check raised {type(exc).__name__}: {exc}"
        tune.journal.record(EditEntry(
            space=BASE_SPACE, name=check.name, label=f"**{check.name}**",
            key="", kind=KIND_CHECK,
            verdict=VERDICT_UNCHANGED if passed else VERDICT_BLOCKED,
            intent=check.description or "post-save verification",
            detail=f"{'PASS' if passed else 'FAIL'} — {detail}",
        ))
        if not passed:
            problems.append(f"{check.name} failed: {detail}")

    # 4. raw-diff audit vs the declared reference --------------------------- #
    diff: Optional[audit.RawDiffAudit] = None
    if reference_bin is not None:
        allowances = [
            # Measured moves: every byte a write changed away from the source.
            audit.Allowance("journaled edits", tune.journal.changed_offsets()),
            # Restores: declared bytes the build left equal to source, which a
            # prior revision may have changed. Tight — a byte moved away from
            # source is not here, so an undeclared change stays unexplained.
            audit.restore_to_source_allowance(
                tune.journal.declared_offsets(),
                tune.source_snapshot,
                bin_path,
            ),
            audit.checksum_storage_allowance(bin_path),
            *extra_allowances,
        ]
        diff = audit.raw_diff_audit(reference_bin, bin_path, allowances)
        if not diff.clean:
            problems.append(f"{len(diff.unexplained)} unexplained changed byte(s)")

    return GateOutcome(
        bin_path=bin_path,
        checksums=checksums,
        checksum_state=checksum_state,
        readback_failures=readback_failures,
        diff=diff,
        problems=tuple(problems),
    )


def _readback(tune: Tune, bin_path: Path) -> tuple[str, ...]:
    """Re-read every journaled table off the saved file; report mismatches.

    Staging a write and *having written it* are different claims. This checks
    the second one, against the bytes that would actually be flashed.
    """
    failures: list[str] = []
    caches: dict[str, CalFile] = {}
    # Keyed on the XDF key, not the logical name: one table can be journaled
    # under both (a domain call names it logically, the basics SOP names it by
    # symbol), and only the last write to it describes the saved bin.
    latest: dict[tuple[str, object], EditEntry] = {}
    for entry in tune.journal.touching():
        latest[(entry.space, entry.key)] = entry

    for (space_name, _key), entry in latest.items():
        space = tune.space(space_name)
        cal = caches.get(space_name)
        if cal is None:
            cal = caches[space_name] = CalFile.open(str(space.xdf), str(bin_path))
        expected = entry.after
        if expected is None:
            continue
        # Resolve by the recorded XDF key rather than the logical name: the
        # basics SOP reaches tables the profile does not map, and they still
        # have to be read back.
        actual = np.asarray(cal.get(entry.key).values, dtype=np.float64)
        if actual.shape != expected.shape:
            failures.append(
                f"{entry.label}: read back shape {actual.shape}, expected "
                f"{expected.shape}"
            )
        elif not np.allclose(actual, expected, rtol=0, atol=READBACK_ATOL):
            worst = int(np.argmax(np.abs(actual - expected)))
            failures.append(
                f"{entry.label}: saved bin reads {actual.ravel()[worst]:.6g} "
                f"where the journal recorded {expected.ravel()[worst]:.6g}"
            )
    return tuple(failures)


def _compare_plots(
    tune: Tune, reference_bin: Union[str, Path], bin_path: Path, png_dir: Path
) -> dict[tuple[str, Union[str, int]], tuple[Path, ...]]:
    """Before/after PNGs for each changed table, reference bin vs this build.

    Returns the written PNGs grouped by the ``(space, XDF key)`` of the table
    they depict — the association is exact here, where the plot's name is
    resolved, so a reviewer-facing renderer never has to guess which file goes
    with which table.

    A table whose own axis was re-breakpointed raises
    :class:`TableMismatchError` — a composite of two different axes would be
    misleading, so it is skipped here (an empty entry) and covered by the
    report's text instead.
    """
    # Imported here rather than at module scope so that building, verifying, and
    # auditing a bin never require matplotlib. Only *rendering* PNGs does, and
    # that is the one caller of this function. Embedded runtimes (the Android
    # engine) carry no matplotlib at all, so a top-level import would make the
    # whole pipeline unimportable there.
    from ..plot import TableMismatchError, compare_tables

    by_table: dict[tuple[str, Union[str, int]], tuple[Path, ...]] = {}
    before_cals: dict[str, CalFile] = {}
    after_cals: dict[str, CalFile] = {}
    for space_name, key in tune.journal.tables_touched():
        space = tune.space(space_name)
        if space_name not in before_cals:
            before_cals[space_name] = CalFile.open(str(space.xdf), str(reference_bin))
            after_cals[space_name] = CalFile.open(str(space.xdf), str(bin_path))
        try:
            written = compare_tables(
                render_table(before_cals[space_name].get(key)),
                after_cals[space_name].get(key),
                png_dir,
            )
        except TableMismatchError:
            written = []  # axis re-breakpointed; the report's detail covers it
        by_table[(space_name, key)] = tuple(written)
    return by_table


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #
def render_report(
    tune: Tune, result: BuildResult, *, title: str = "", summary: str = ""
) -> str:
    """Render the journal and every gate verdict to Markdown.

    The report is *derived* from the journal rather than written alongside it,
    so it cannot drift from what the build actually did.
    """
    lines = [f"# {title or result.revision}", ""]
    if not result.ok:
        lines += ["## ⛔ VERIFICATION FAILED — DO NOT FLASH", ""]
        lines += [f"- {p}" for p in result.problems] + [""]
    else:
        lines += [
            "## ⚠ Human review required before flashing", "",
            "Every automated gate passed. That makes this bin *reviewable*, "
            "not approved: read the journal and the comparison plots below "
            "before flashing. This tool never flashes an ECU.", "",
        ]
    if summary:
        lines += [summary, ""]

    lines += ["## Edit journal", ""]
    counts = tune.journal.summary_counts()
    if counts:
        lines += [
            "  ".join(f"**{k}**: {v}" for k, v in counts.items()), "",
        ]
    lines += [_journal_table(tune.journal), ""]

    lines += ["## Verification gates", ""]
    lines.append(
        f"- Checksums: **{result.checksum_state}** "
        f"({', '.join(r.name for r in result.checksums) or 'none verifiable'})."
    )
    if result.readback_failures:
        lines.append(f"- Final-bin readback: **FAILED** ({len(result.readback_failures)}):")
        lines += [f"    - {f}" for f in result.readback_failures]
    else:
        lines.append(
            f"- Final-bin readback: **PASS** — "
            f"{len(tune.journal.tables_touched())} table(s) re-read off the "
            "saved bin and matched the journal."
        )
    if result.diff is None:
        lines.append(
            "- Raw-diff audit: **not run** — no reference bin was declared, so "
            "no byte-level claim is made about what else may have changed."
        )
    else:
        lines.append(f"- Raw-diff audit vs `{Path(result.diff.reference).name}`: "
                     f"**{'CLEAN' if result.diff.clean else 'UNEXPLAINED BYTES'}** — "
                     f"{result.diff.summary()}.")
        for label, count in sorted(result.diff.attributed.items()):
            lines.append(f"    - {count} byte(s): {label}")
    lines.append("")

    lines += ["## Artifacts", "",
              f"- Bin: `{result.bin_path.name}`",
              f"- Report: `{result.report_path.name}`"]
    for path in result.plots:
        lines.append(f"- Plot: `{path.relative_to(result.out_dir)}`")
    lines += ["",
              "Every revision is a starting point, not a finished calibration: "
              "only logs validate it. Flash (human step) → log → review → "
              "iterate.", ""]
    return "\n".join(lines)


def _journal_table(journal: Journal) -> str:
    headers = ["Table", "Change", "Verdict", "Before", "After", "Why / detail"]
    superseded = journal.superseded()
    rows = []
    for i, entry in enumerate(journal):
        detail = entry.intent
        if entry.detail:
            detail = f"{detail} — {entry.detail}" if detail else entry.detail
        if entry.warning:
            detail = f"{detail} ⚠ {entry.warning}".strip()
        verdict = entry.verdict
        if i in superseded:
            verdict = VERDICT_SUPERSEDED
            note = _superseded_note(superseded[i])
            detail = f"{note}{' — ' + detail if detail else ''}"
        rows.append([
            entry.label,
            entry.scope_text(),
            verdict,
            entry.before_text(),
            entry.after_text(),
            detail,
        ])
    if not rows:
        return "_No edits were journaled — this build changed nothing._"
    return _md_table(headers, rows)


def _superseded_note(writers: Sequence[EditEntry]) -> str:
    """Prose linking a deferred SOP skip to the writes that stand in its place."""
    names = ", ".join(dict.fromkeys(f"`{w.name}`" for w in writers))
    return (
        "the base recipe deferred this; this revision writes it below "
        f"({names})"
    )


def _md_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    """Aligned GitHub-Markdown table (padded columns), matching the repo style."""
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def fmt(cells: Sequence[str]) -> str:
        return "| " + " | ".join(
            str(c).ljust(widths[i]) for i, c in enumerate(cells)
        ) + " |"

    sep = "|-" + "-|-".join("-" * w for w in widths) + "-|"
    return "\n".join([fmt(headers), sep, *(fmt(r) for r in rows)])
