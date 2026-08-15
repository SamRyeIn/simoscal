"""Renderer-independent build service — the gate chain as a data model.

:func:`~simoscal.tune.pipeline.build` is the desktop build: it runs the safety
spine and then *renders* it — comparison PNGs (matplotlib), ``report.md``, and
``report.html`` written next to the bin. None of that can run on the phone, and
none of it is the verification. The Quick Edit app needs the same gates and the
same verdicts, but returned as one machine-readable object a Compose screen (or
a bridge, or a test) reads — not as files a browser opens.

So this module runs :func:`~simoscal.tune.pipeline.run_gates` — the exact same
save → checksum-verify → readback → blocked-write → coherence → post-check →
byte-audit spine, imported with no matplotlib — and turns the outcome into a
:class:`BuildReport`: a frozen, JSON-serializable model of what changed and how
every gate voted. Like :func:`simoscal.preflight`, it returns a verdict rather
than raising on a failed gate — a build that failed verification is a *report*
the UI shows, with :attr:`BuildReport.verified` false, not an exception thrown
across a bridge.

Two safety properties are structural here, not conventions:

* **The report is derived from the journal**, exactly as ``report.md`` and
  ``report.html`` are, so the model a reviewer approves cannot describe
  something other than what the build did.
* **Sharing is gated on the verdict.** :attr:`BuildReport.share_path` is the
  staged bin only when every gate passed; on any failure it is ``None``. A
  failed build has no shareable bin — the staged file still exists (the gates
  read it back off disk), but nothing hands it onward.
* **A shared candidate is immutable.** Each build gets its own directory under
  the staging root and no build ever writes a path another build used, so bytes
  handed to another app cannot be rewritten afterwards by a later (or failing)
  build behind the same content URI.

Never flashes. Nothing in this package does.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional, Union

from ..checksum import ChecksumReport
from .audit import RawDiffAudit
from .journal import (
    KIND_CHECK,
    VERDICT_BLOCKED,
    VERDICT_SUPERSEDED,
    Journal,
)
from .pipeline import (
    CHECKSUM_CLEAN,
    GateOutcome,
    JournalFingerprint,
    run_gates,
)
from .project import Tune

__all__ = [
    "SCHEMA_VERSION",
    "AuditModel",
    "BuildReport",
    "ChecksumModel",
    "EditModel",
    "GateResult",
    "TableRef",
    "build_report",
    "build_revision",
]

#: The report model's version. The Kotlin bridge (V6) pins against this, so a
#: field rename or removal must bump it — a UI reading an unexpected shape is a
#: contract break, not a best-effort parse.
SCHEMA_VERSION = "1"

#: How many unexplained-byte offsets to include in the model. The full set can
#: be large; a bounded sample is enough for a human to see *that* the audit
#: failed and roughly where — :attr:`AuditModel.unexplained_count` carries the
#: true total.
_UNEXPLAINED_SAMPLE = 32


@dataclass(frozen=True)
class GateResult:
    """One verification gate's verdict, uniform across gate kinds for the UI.

    ``ran`` distinguishes a gate that *passed* from one that never applied — a
    byte audit with no reference, or coherence rules with no recipe. A gate that
    did not run is not a pass; the UI shows it as such.
    """

    name: str
    passed: bool
    ran: bool
    detail: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ChecksumModel:
    """One embedded checksum's verdict, JSON-safe (ints rendered as hex)."""

    name: str
    can_verify: bool
    is_stale: bool
    stored_hex: Optional[str]
    computed_hex: Optional[str]
    detail: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class AuditModel:
    """The byte-level audit verdict, without the raw offset arrays."""

    ran: bool
    clean: bool
    reference: Optional[str]
    changed: int
    unexplained_count: int
    unexplained_sample_hex: tuple[str, ...]
    #: allowance label → how many of its bytes actually changed
    attributed: dict[str, int]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class TableRef:
    """A table that moved bytes this build: where it lives and its `ID` — desc."""

    space: str
    key: str
    label: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class EditModel:
    """One journal entry as UI-ready data — no numpy arrays cross this boundary.

    ``before`` / ``after`` are the journal's own one-cell-wide summaries (the
    same text ``report.md`` shows), and ``verdict`` already carries the
    superseded substitution the reports apply, so a skip a later write covered
    does not read as a contradiction.
    """

    label: str
    name: str
    kind: str
    verdict: str
    units: str
    intent: str
    scope: str
    before: str
    after: str
    detail: str
    warning: str
    moved_bytes: int
    cells_changed: int

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class BuildReport:
    """The whole build verdict for one revision, as one serializable object.

    :attr:`verified` is the single fact the caller acts on; :attr:`share_path`
    is the staged bin iff verified, else ``None``. Everything else is evidence:
    :attr:`gates` for the pass/fail row, :attr:`edits` for the journal table,
    :attr:`changed_tables` for what to draw, :attr:`problems` for the failure
    banner. Nothing here references the app — it is a data object a Compose
    screen (or a test) reads, mirroring :class:`simoscal.preflight.Verdict`.
    """

    schema_version: str
    revision: str
    verified: bool
    summary: str
    problems: tuple[str, ...]

    # provenance / artifacts
    staged_bin: str
    share_path: Optional[str]
    source_bin: Optional[str]
    reference_bin: Optional[str]

    # gate verdicts
    checksum_state: str
    gates: tuple[GateResult, ...]
    checksums: tuple[ChecksumModel, ...]
    readback_failures: tuple[str, ...]
    audit: AuditModel

    # what changed
    counts: dict[str, int]
    edits: tuple[EditModel, ...]
    changed_tables: tuple[TableRef, ...]

    def __bool__(self) -> bool:  # pragma: no cover - convenience
        return self.verified

    def to_dict(self) -> dict:
        """A JSON-safe nested dict — the wire form for the bridge and tests."""
        return {
            "schema_version": self.schema_version,
            "revision": self.revision,
            "verified": self.verified,
            "summary": self.summary,
            "problems": list(self.problems),
            "staged_bin": self.staged_bin,
            "share_path": self.share_path,
            "source_bin": self.source_bin,
            "reference_bin": self.reference_bin,
            "checksum_state": self.checksum_state,
            "gates": [g.to_dict() for g in self.gates],
            "checksums": [c.to_dict() for c in self.checksums],
            "readback_failures": list(self.readback_failures),
            "audit": self.audit.to_dict(),
            "counts": dict(self.counts),
            "edits": [e.to_dict() for e in self.edits],
            "changed_tables": [t.to_dict() for t in self.changed_tables],
        }

    def to_json(self) -> str:
        """Deterministic JSON — sorted keys, so identical builds serialize
        byte-identically (the cross-runtime golden gate compares this)."""
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)


# --------------------------------------------------------------------------- #
# the service
# --------------------------------------------------------------------------- #
def build_revision(
    tune: Tune,
    revision: str,
    *,
    staging_dir: Union[str, Path],
    reference_bin: Union[str, Path],
    bin_name: str = "",
    source_bin: Optional[Union[str, Path]] = None,
    write_json: bool = True,
) -> BuildReport:
    """Run the full gate chain over ``tune`` and return a :class:`BuildReport`.

    Writes the candidate bin into a fresh per-build directory under
    ``staging_dir`` (the gates read it back off disk) and, unless ``write_json``
    is false, a ``build_report.json`` beside it. Returns the report for *both* a
    passed and a failed build — a failed build is a report with
    :attr:`~BuildReport.verified` false and :attr:`~BuildReport.share_path`
    ``None``, never an exception.

    ``reference_bin`` is required: the service's contract is to audit an edit
    against its source, and for v1 the imported bin is both the baseline and the
    byte-audit reference. A build with no reference makes no byte-level claim,
    which is not something this service will present as verified — that path is
    :func:`simoscal.tune.build`, for a first-ever revision on the desktop.

    The service deliberately exposes **no** caller-supplied audit allowance. The
    only bytes the audit may forgive are the journaled edits, declared restores,
    and stored checksums :func:`~simoscal.tune.run_gates` derives itself; an
    arbitrary allowance could forgive an unjournaled write and leave it invisible
    in the model, so that escape hatch stays on the desktop build only
    (CR-20260724-01).

    **Every build writes to a path of its own**, ``staging_dir/<revision>-<id>/``,
    and never to a path a previous build used. A candidate that was verified and
    shared is therefore immutable: once its bytes are handed to another app as a
    content URI, no later build can rewrite the file behind that grant, not even
    mid-gate (CR-20260813-02). ``bin_name`` only names the file *inside* that
    fresh directory, and both it and ``revision`` are validated as bare filename
    components — a name carrying a path separator is refused loudly rather than
    resolving somewhere outside the staging tree (CR-20260813-05).
    """
    staging_dir = Path(staging_dir)
    revision_part = _filename_component(revision, what="revision")
    name = (
        _filename_component(bin_name, what="bin_name")
        if bin_name
        else f"{revision_part}.bin"
    )

    # A fresh directory per build. uuid4 rather than a counter: the staging tree
    # outlives the process, so "next unused number" would have to be derived from
    # whatever is on disk, and two builds racing that read would collide.
    build_dir = staging_dir / f"{revision_part}-{uuid.uuid4().hex[:12]}"
    build_dir.mkdir(parents=True, exist_ok=False)
    bin_path = build_dir / name
    # Belt and braces: whatever the components were, the file we are about to
    # write must be a direct child of this build's own directory.
    if bin_path.resolve().parent != build_dir.resolve():
        raise ValueError(
            f"build_revision: the candidate path {bin_path} does not resolve "
            f"inside {build_dir} — refusing to write outside the staging tree"
        )

    outcome = run_gates(tune, bin_path, reference_bin=reference_bin)
    report = build_report(
        tune, revision, outcome,
        source_bin=source_bin, reference_bin=reference_bin,
    )
    if write_json:
        (build_dir / "build_report.json").write_text(
            report.to_json(), encoding="utf-8"
        )
    return report


def _filename_component(value: str, *, what: str) -> str:
    """Return ``value`` iff it is a bare, path-safe filename component.

    Raises rather than sanitizing. Both of this function's inputs originate
    outside the engine — ``revision`` is typed by a person, ``bin_name`` reaches
    Android as an untrusted ``OpenableColumns.DISPLAY_NAME`` a document provider
    chose — and a silently-rewritten name is exactly the kind of quiet
    substitution this library refuses everywhere else. ``"../escaped.bin"`` is a
    loud failure, not a file called ``escaped.bin``.
    """
    if not isinstance(value, str):
        raise ValueError(f"build_revision: {what} must be a string, got {type(value).__name__}")
    if not value or value != value.strip():
        # Trimming would be a silent rewrite, and a trailing space in a file name
        # is its own small hazard downstream.
        raise ValueError(
            f"build_revision: {what} {value!r} is empty or padded with whitespace"
        )
    if value in (".", ".."):
        raise ValueError(f"build_revision: {what} {value!r} is not a file name")
    if "\x00" in value:
        raise ValueError(f"build_revision: {what} contains a NUL byte")
    separators = {"/", os.sep, os.altsep} - {None}
    if any(sep in value for sep in separators):
        raise ValueError(
            f"build_revision: {what} {value!r} contains a path separator; it "
            "must be a bare file name"
        )
    if Path(value).is_absolute() or Path(value).name != value:
        raise ValueError(
            f"build_revision: {what} {value!r} is not a bare file name"
        )
    return value


def build_report(
    tune: Tune,
    revision: str,
    outcome: GateOutcome,
    *,
    source_bin: Optional[Union[str, Path]] = None,
    reference_bin: Optional[Union[str, Path]] = None,
) -> BuildReport:
    """Assemble the report model from a :class:`GateOutcome` and the journal.

    Split out from :func:`build_revision` so the model can be built (and tested)
    from any gate outcome without re-running the gates.

    Both :attr:`~BuildReport.verified` and :attr:`~BuildReport.share_path` are
    derived from the gate verdicts here, not merely from ``outcome.ok`` — an
    outcome whose problem list disagrees with its own gate facts (an unclean
    audit, a stale checksum) cannot slip through as verified/shareable. And
    because this assembler reads the *live* journal, it first checks that journal
    still matches the one the gates ran against; a post-gate mutation makes the
    build neither verified nor shareable (CR-20260724-02).
    """
    journal = tune.journal

    # Freshness: the model below is derived from the live journal, but the gate
    # verdicts describe the journal as it stood when the bin was saved. If those
    # differ, the journal was edited after the gates ran and the model would
    # describe a bin nothing verified — reject it rather than present it.
    drifted = (
        outcome.journal is not None
        and JournalFingerprint.of(journal) != outcome.journal
    )

    problems = outcome.problems
    if drifted:
        problems = problems + (
            "journal changed after the gates ran — the report would describe a "
            "bin that was never verified",
        )

    verified = outcome.ok and not drifted
    # Shareable is stricter than verified: every real gate verdict must be clean
    # (not just an empty problem list), the byte audit must have actually run,
    # and it must be clean. A failed, unaudited, or drifted build hands nothing
    # onward — this predicate does not trust that ``problems`` was populated
    # consistently with the gate facts.
    shareable = (
        verified
        and outcome.checksum_state == CHECKSUM_CLEAN
        and not outcome.readback_failures
        and outcome.diff is not None
        and outcome.diff.clean
    )

    report = BuildReport(
        schema_version=SCHEMA_VERSION,
        revision=revision,
        verified=verified,
        summary=_summary(revision, verified, problems),
        problems=problems,
        staged_bin=str(outcome.bin_path),
        # The one structural share gate: a failed, unaudited, or drifted build
        # hands nothing onward.
        share_path=str(outcome.bin_path) if shareable else None,
        source_bin=str(source_bin) if source_bin is not None else None,
        reference_bin=str(reference_bin) if reference_bin is not None else None,
        checksum_state=outcome.checksum_state,
        gates=_gates(tune, outcome),
        checksums=tuple(_checksum_model(c) for c in outcome.checksums),
        readback_failures=outcome.readback_failures,
        audit=_audit_model(outcome),
        counts=journal.summary_counts(),
        edits=_edit_models(journal),
        changed_tables=_changed_tables(journal, outcome.diff),
    )
    return report


# --------------------------------------------------------------------------- #
# model derivation (all pure functions of the journal + outcome)
# --------------------------------------------------------------------------- #
def _summary(revision: str, verified: bool, problems: tuple[str, ...]) -> str:
    if verified:
        return (
            f"{revision}: verified — every gate passed. This makes the bin "
            "reviewable, not approved: read the journal before flashing. This "
            "tool never flashes."
        )
    return (
        f"{revision}: NOT verified — {len(problems)} gate(s) failed. DO NOT "
        f"FLASH: {'; '.join(problems)}."
    )


def _gates(tune: Tune, outcome: GateOutcome) -> tuple[GateResult, ...]:
    """The named gates, reconstructed as uniform pass/fail rows for the UI.

    Every verdict here is re-derived from the same data ``run_gates`` used —
    the checksum reports, readback failures, journal, and audit — so the rows a
    reviewer sees are the votes the build actually cast.
    """
    gates: list[GateResult] = []

    names = ", ".join(c.name for c in outcome.checksums) or "none verifiable"
    gates.append(GateResult(
        name="Checksums",
        passed=outcome.checksum_state == CHECKSUM_CLEAN,
        ran=True,
        detail=f"{outcome.checksum_state} ({names})",
    ))

    rb = outcome.readback_failures
    gates.append(GateResult(
        name="Final-bin readback",
        passed=not rb,
        ran=True,
        detail=(
            f"{len(tune.journal.tables_touched())} table(s) re-read off the "
            "saved bin and matched the journal"
            if not rb else f"{len(rb)} table(s) did not survive the save"
        ),
    ))

    blocked = tune.journal.blocked()
    gates.append(GateResult(
        name="Blocked writes",
        passed=not blocked,
        ran=True,
        detail=(
            "no guard rejected an intended write"
            if not blocked
            else "a guard blocked: " + ", ".join(e.label for e in blocked)
        ),
    ))

    coherence = tune.recipe_report.coherence() if tune.recipe_report else None
    if coherence is None:
        gates.append(GateResult(
            name="Recipe coherence", passed=True, ran=False,
            detail="not run — no SOP recipe in this build",
        ))
    else:
        do_not_flash = [f for f in coherence if f.severity == "DO NOT FLASH"]
        gates.append(GateResult(
            name="Recipe coherence",
            passed=not do_not_flash,
            ran=True,
            detail=(
                "no coherence conflict"
                if not do_not_flash
                else "; ".join(f.message for f in do_not_flash)
            ),
        ))

    # Post-save checks were journaled as KIND_CHECK entries; surface each.
    for entry in tune.journal:
        if entry.kind != KIND_CHECK:
            continue
        gates.append(GateResult(
            name=entry.name,
            passed=entry.verdict != VERDICT_BLOCKED,
            ran=True,
            detail=entry.detail,
        ))

    diff = outcome.diff
    if diff is None:
        gates.append(GateResult(
            name="Raw-diff audit", passed=False, ran=False,
            detail="not run — no reference bin declared, so no byte-level claim",
        ))
    else:
        gates.append(GateResult(
            name="Raw-diff audit",
            passed=diff.clean,
            ran=True,
            detail=diff.summary(),
        ))
    return tuple(gates)


def _checksum_model(report: ChecksumReport) -> ChecksumModel:
    return ChecksumModel(
        name=report.name,
        can_verify=report.can_verify,
        is_stale=report.is_stale,
        stored_hex=None if report.stored is None else hex(report.stored),
        computed_hex=None if report.computed is None else hex(report.computed),
        detail=report.detail,
    )


def _audit_model(outcome: GateOutcome) -> AuditModel:
    diff = outcome.diff
    if diff is None:
        return AuditModel(
            ran=False, clean=False, reference=None, changed=0,
            unexplained_count=0, unexplained_sample_hex=(), attributed={},
        )
    return AuditModel(
        ran=True,
        clean=diff.clean,
        reference=str(Path(diff.reference).name),
        changed=diff.changed,
        unexplained_count=len(diff.unexplained),
        unexplained_sample_hex=tuple(
            hex(o) for o in diff.unexplained[:_UNEXPLAINED_SAMPLE]
        ),
        attributed=dict(diff.attributed),
    )


def _edit_models(journal: Journal) -> tuple[EditModel, ...]:
    """Every journal entry as UI data, with the superseded substitution applied.

    Mirrors ``render_report`` / ``render_report_html``: a bulk-SOP skip a later
    applied write covers is shown as :data:`VERDICT_SUPERSEDED`, so the model
    matches the Markdown and HTML the desktop path renders from the same journal.
    """
    superseded = journal.superseded()
    models: list[EditModel] = []
    for i, entry in enumerate(journal):
        verdict = VERDICT_SUPERSEDED if i in superseded else entry.verdict
        models.append(EditModel(
            label=entry.label,
            name=entry.name,
            kind=entry.kind,
            verdict=verdict,
            units=entry.units,
            intent=entry.intent,
            scope=entry.scope_text(),
            before=entry.before_text(),
            after=entry.after_text(),
            detail=entry.detail,
            warning=entry.warning,
            moved_bytes=len(entry.offsets),
            cells_changed=entry.cells_changed,
        ))
    return tuple(models)


def _changed_tables(
    journal: Journal, diff: Optional[RawDiffAudit]
) -> tuple[TableRef, ...]:
    """The tables that changed *this* build, each with its `ID` — Description label.

    Keyed on ``(space, XDF key)`` like :meth:`Journal.tables_touched`, so one
    table journaled under both a logical name and its symbol appears once. The
    label is taken from the first entry that names that key.

    When a byte audit ran, :meth:`Journal.tables_touched` overstates the answer:
    it includes declarations that moved no bytes (a restore-to-source re-write),
    which the readback needs but which did *not* change versus the reference. So
    a table is kept only when its measured/declared extent intersects the audit's
    changed-offset set — the bytes that actually differ from the reference bin —
    matching the desktop HTML renderer (``report_html._changed_tables_section``)
    and the byte audit itself (CR-20260724-03). With no reference (no audit),
    there is no previous flash to diff against, so every touched table is kept.
    """
    label_for: dict[tuple[str, object], str] = {}
    extent_for: dict[tuple[str, object], frozenset[int]] = {}
    for entry in journal.touching():
        key = (entry.space, entry.key)
        label_for.setdefault(key, entry.label)
        extent_for[key] = extent_for.get(key, frozenset()) | entry.offsets | entry.declared

    delta = diff.changed_offsets if diff is not None else None
    refs: list[TableRef] = []
    for space, key in journal.tables_touched():
        if delta is not None and not (extent_for.get((space, key), frozenset()) & delta):
            continue  # declared but no byte differs from the reference — not a change
        refs.append(
            TableRef(space=space, key=str(key), label=label_for.get((space, key), ""))
        )
    return tuple(refs)
