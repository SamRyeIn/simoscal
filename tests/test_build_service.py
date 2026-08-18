"""Tests for the renderer-independent build service.

The service runs the *same* gate chain as :func:`simoscal.tune.build` — the
spine is now factored into ``run_gates`` and shared — but returns a
:class:`BuildReport` model instead of writing PNGs/Markdown/HTML. So these tests
assert two things the desktop build tests do not:

* the **verdict is data, not an exception** — a failed gate yields a report with
  ``verified`` false and ``share_path`` ``None``, never a raise;
* the model is **JSON-serializable, derived from the journal, and
  deterministic** — identical inputs produce byte-identical bins.

They run against the real stock bin (skipping cleanly when it is absent), which
for v1 is both the edit baseline and the byte-audit reference — the exact Quick
Edit flow: import stock, edit, build. A synthetic fixture cannot exercise
checksum correction or the byte-level audit.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from simoscal import CalFile
from simoscal.tune import (
    SC8S50,
    BuildReport,
    Tune,
    build_report,
    build_revision,
    run_gates,
)
from simoscal.tune.audit import RawDiffAudit


@pytest.fixture
def tune(real_xdf: Path, real_bin: Path) -> Tune:
    """A freshly opened tune over the stock bin (unedited each time)."""
    return Tune.open(SC8S50, xdf=real_xdf, bin=real_bin)


def _fresh(tune: Tune) -> Tune:
    """Another independent tune over the same source files."""
    return Tune.open(SC8S50, xdf=tune.space("base").xdf, bin=tune.source_bin)


# --------------------------------------------------------------------------- #
# happy path — a verified build
# --------------------------------------------------------------------------- #
def test_a_clean_edit_verifies_and_is_shareable(
    tune: Tune, tmp_path: Path, real_bin: Path
) -> None:
    """The core happy path: one cell edited, every gate passes, bin is shareable."""
    tune.write_cells("pressure_quotient_max", {(0, 0): 1.7},
                     intent="lower the 1000 rpm pressure-quotient cap")

    report = build_revision(
        tune, "R01", staging_dir=tmp_path, reference_bin=real_bin,
        source_bin=real_bin, bin_name="r01.bin",
    )

    assert isinstance(report, BuildReport)
    assert report.verified
    assert report.checksum_state == "CLEAN"
    assert report.readback_failures == ()
    # Shareable: the share path is the staged bin, and it exists on disk.
    assert report.share_path == report.staged_bin
    assert Path(report.share_path).is_file()
    # The audit ran against the imported bin and found nothing unexplained.
    assert report.audit.ran and report.audit.clean
    assert report.audit.unexplained_count == 0
    # Every gate voted pass.
    assert all(g.passed for g in report.gates)
    # The edited table is named, `ID` — Description, in changed_tables.
    labels = [t.label for t in report.changed_tables]
    assert any("IP_PQ_CHA_MAX" in lbl for lbl in labels)


def test_a_boost_edit_verifies(tune: Tune, tmp_path: Path, real_bin: Path) -> None:
    """A boost-domain edit (the hero surface's table) builds and verifies."""
    tune.boost.put_ceiling_psi(24.0, intent="park the full-load ceiling")

    report = build_revision(
        tune, "R01", staging_dir=tmp_path, reference_bin=real_bin,
    )

    assert report.verified and report.share_path is not None
    assert any("IP_PUT_SP" in t.label for t in report.changed_tables)


# --------------------------------------------------------------------------- #
# the verdict is data, not an exception
# --------------------------------------------------------------------------- #
def test_an_unjournaled_byte_is_not_shareable(
    tune: Tune, tmp_path: Path, real_bin: Path
) -> None:
    """A change made behind the journal fails the audit — as a report, not a raise."""
    tune.write_cells("pressure_quotient_max", {(0, 0): 1.7}, intent="declared")
    # Edit straight through the CalFile, bypassing Tune.write and the journal.
    smuggled = tune.space("base").cal.get("IP_TQI_REF_MAX_MON")
    smuggled.set(np.full(smuggled.shape, 900.0))

    report = build_revision(
        tune, "R01", staging_dir=tmp_path, reference_bin=real_bin,
    )

    assert not report.verified
    assert report.share_path is None                       # no shareable bin
    assert report.audit.ran and not report.audit.clean
    assert report.audit.unexplained_count > 0
    assert any("unexplained" in p for p in report.problems)
    audit_gate = next(g for g in report.gates if g.name == "Raw-diff audit")
    assert not audit_gate.passed
    # The staged file still exists — the gates read it back — it is just not
    # offered for sharing.
    assert Path(report.staged_bin).is_file()


def test_a_readback_fault_is_not_shareable(
    tune: Tune, tmp_path: Path, real_bin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A value that does not survive the save blocks the verdict, cleanly."""
    tune.write_cells("pressure_quotient_max", {(0, 0): 1.7}, intent="lower")

    real_save = Tune.save

    def save_then_corrupt(self, path, **kwargs):
        reports = real_save(self, path, **kwargs)
        cal = CalFile.open(str(self.space("base").xdf), str(path))
        view = cal.get("IP_PQ_CHA_MAX")
        values = np.array(view.values)
        values[0, 0] = 9.0
        view.set(values)
        cal.save(path, correct_checksums=True)
        return reports

    monkeypatch.setattr(Tune, "save", save_then_corrupt)

    report = build_revision(
        tune, "R01", staging_dir=tmp_path, reference_bin=real_bin,
    )

    assert not report.verified and report.share_path is None
    assert report.readback_failures
    readback_gate = next(g for g in report.gates if g.name == "Final-bin readback")
    assert not readback_gate.passed


def test_a_stale_checksum_is_not_shareable(
    tune: Tune, tmp_path: Path, real_bin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A save that skips checksum correction is caught and reported, not raised."""
    tune.write_cells("pressure_quotient_max", {(0, 0): 1.7}, intent="lower")

    def save_without_correcting(self, path, **kwargs):
        return self.space("base").cal.save(
            path, correct_checksums=False, warn_stale=False
        )

    monkeypatch.setattr(Tune, "save", save_without_correcting)

    report = build_revision(
        tune, "R01", staging_dir=tmp_path, reference_bin=real_bin,
    )

    assert not report.verified and report.share_path is None
    assert report.checksum_state.startswith("STALE")
    checksum_gate = next(g for g in report.gates if g.name == "Checksums")
    assert not checksum_gate.passed


# --------------------------------------------------------------------------- #
# the model: serializable, journal-derived, deterministic
# --------------------------------------------------------------------------- #
def test_report_is_json_serializable_and_round_trips(
    tune: Tune, tmp_path: Path, real_bin: Path
) -> None:
    """The whole model must survive json.dumps → json.loads (the bridge wire)."""
    tune.write_cells("pressure_quotient_max", {(0, 0): 1.7}, intent="lower")
    report = build_revision(
        tune, "R01", staging_dir=tmp_path, reference_bin=real_bin,
    )

    wire = json.loads(report.to_json())
    assert wire["schema_version"] == "1"
    assert wire["revision"] == "R01"
    assert wire["verified"] is True
    assert wire["checksum_state"] == "CLEAN"
    assert isinstance(wire["edits"], list) and wire["edits"]
    # The written artifact matches the returned model exactly. It lives beside
    # the candidate, in this build's own directory — not at the staging root,
    # where the next build would overwrite it (CR-20260813-02).
    build_dir = Path(report.staged_bin).parent
    assert build_dir.parent == tmp_path
    on_disk = json.loads((build_dir / "build_report.json").read_text())
    assert on_disk == wire


def test_edits_mirror_the_journal(
    tune: Tune, tmp_path: Path, real_bin: Path
) -> None:
    """The model's edit rows are one-per-journal-entry, in order, with labels."""
    tune.write_cells("pressure_quotient_max", {(0, 0): 1.7}, intent="lower the cap")
    tune.note("wastegate_feedforward_vvl0", "no flow-factor channels logged yet",
              intent="deliberate skip")

    report = build_revision(
        tune, "R01", staging_dir=tmp_path, reference_bin=real_bin,
    )

    assert len(report.edits) == len(tune.journal)
    assert [e.label for e in report.edits] == [e.label for e in tune.journal]
    # The deliberate non-change carries its verdict and intent, unmangled.
    skip = next(e for e in report.edits if "IP_FAC_BPA_SP[0]" in e.label)
    assert skip.verdict == "skipped"
    assert skip.moved_bytes == 0
    edited = next(e for e in report.edits if "IP_PQ_CHA_MAX" in e.label)
    assert edited.intent == "lower the cap"
    assert edited.moved_bytes > 0


def test_identical_inputs_rebuild_byte_identical(
    tune: Tune, tmp_path: Path, real_bin: Path
) -> None:
    """Determinism: the same edit built twice yields byte-identical bins."""
    edit = {(0, 0): 1.7}

    tune.write_cells("pressure_quotient_max", edit, intent="lower")
    a = build_revision(tune, "R01", staging_dir=tmp_path / "a",
                       reference_bin=real_bin)

    other = _fresh(tune)
    other.write_cells("pressure_quotient_max", edit, intent="lower")
    b = build_revision(other, "R01", staging_dir=tmp_path / "b",
                       reference_bin=real_bin)

    assert Path(a.share_path).read_bytes() == Path(b.share_path).read_bytes()
    # And the journal-derived model is identical once the run-specific paths
    # (staged/source/reference) are set aside.
    path_fields = {"staged_bin", "share_path", "source_bin", "reference_bin"}
    da = {k: v for k, v in a.to_dict().items() if k not in path_fields}
    db = {k: v for k, v in b.to_dict().items() if k not in path_fields}
    assert da == db


# --------------------------------------------------------------------------- #
# build_report on a bare outcome
# --------------------------------------------------------------------------- #
def test_build_report_without_a_reference_is_verified_but_not_shareable(
    tune: Tune, tmp_path: Path
) -> None:
    """No reference → the gates can pass, but the audit did not run, so the bin
    is never handed onward. verified and shareable are distinct facts."""
    tune.write_cells("pressure_quotient_max", {(0, 0): 1.7}, intent="lower")
    outcome = run_gates(tune, tmp_path / "r01.bin", reference_bin=None)
    report = build_report(tune, "R01", outcome)

    assert report.verified            # every gate that ran passed
    assert not report.audit.ran
    assert report.share_path is None  # but nothing to share without an audit
    audit_gate = next(g for g in report.gates if g.name == "Raw-diff audit")
    assert not audit_gate.passed and not audit_gate.ran


# --------------------------------------------------------------------------- #
# the report/share boundary — the V3 cross-vendor review findings
# --------------------------------------------------------------------------- #
def test_service_offers_no_caller_supplied_allowance(tune: Tune) -> None:
    """CR-20260724-01: the app service must not expose ``extra_allowances``.

    An arbitrary allowance could forgive an unjournaled write in the byte audit
    and leave it invisible in the model — a bin changed but the report did not.
    That escape hatch stays on the desktop build; the service has no such door.
    """
    import inspect

    assert "extra_allowances" not in inspect.signature(build_revision).parameters
    with pytest.raises(TypeError):
        build_revision(  # type: ignore[call-arg]
            tune, "R01", staging_dir="x", reference_bin="x", extra_allowances=(),
        )


def test_a_second_build_cannot_rewrite_the_first_candidate(
    tune: Tune, tmp_path: Path, real_bin: Path
) -> None:
    """CR-20260813-02: a shared candidate's bytes must be immutable.

    Once a verified bin's path has been handed to another app as a content URI,
    that grant cannot be revoked. So a later build must not be able to write the
    same path: the receiver could otherwise open the URI after the fact and read
    a different — possibly mid-gate or failed — candidate than the one approved.
    """
    tune.write_cells("pressure_quotient_max", {(0, 0): 1.7}, intent="first")
    first = build_revision(
        tune, "R01", staging_dir=tmp_path, reference_bin=real_bin,
        bin_name="candidate.bin",
    )
    assert first.share_path is not None
    shared_bytes = Path(first.share_path).read_bytes()

    # Same revision, same requested file name, same staging root — the exact
    # repeat that used to overwrite the shared file.
    tune.write_cells("pressure_quotient_max", {(0, 0): 1.9}, intent="second")
    second = build_revision(
        tune, "R01", staging_dir=tmp_path, reference_bin=real_bin,
        bin_name="candidate.bin",
    )

    assert second.share_path is not None
    assert second.share_path != first.share_path
    assert Path(first.share_path).read_bytes() == shared_bytes
    # Both live under the staging root the FileProvider shares, each in its own
    # directory, so neither build's report can clobber the other's either.
    first_dir, second_dir = Path(first.staged_bin).parent, Path(second.staged_bin).parent
    assert first_dir != second_dir
    assert first_dir.parent == tmp_path and second_dir.parent == tmp_path


@pytest.mark.parametrize(
    "bin_name",
    ["../escaped.bin", "/tmp/escaped.bin", "sub/dir.bin", "..", ".", "   "],
)
def test_a_candidate_name_that_is_not_a_bare_file_name_is_refused(
    tune: Tune, tmp_path: Path, real_bin: Path, bin_name: str
) -> None:
    """CR-20260813-05: an untrusted display name must never steer the path.

    On Android ``bin_name`` originates as ``OpenableColumns.DISPLAY_NAME`` — text
    a document provider chose. A provider returning ``../escaped.bin`` once put a
    verified candidate outside the staging tree. Refused loudly, and nothing is
    written: a silently sanitized name would be the same class of quiet
    substitution this library refuses everywhere else.
    """
    tune.write_cells("pressure_quotient_max", {(0, 0): 1.7}, intent="lower")
    staging = tmp_path / "staging"

    with pytest.raises(ValueError):
        build_revision(
            tune, "R01", staging_dir=staging, reference_bin=real_bin,
            bin_name=bin_name,
        )

    # No candidate anywhere: not outside the staging tree, and not inside it.
    assert not list(tmp_path.rglob("*.bin"))


def test_a_revision_label_that_is_not_a_bare_file_name_is_refused(
    tune: Tune, tmp_path: Path, real_bin: Path
) -> None:
    """The revision names the build directory, so it is validated the same way."""
    tune.write_cells("pressure_quotient_max", {(0, 0): 1.7}, intent="lower")
    with pytest.raises(ValueError):
        build_revision(
            tune, "../R01", staging_dir=tmp_path, reference_bin=real_bin,
        )
    assert not list(tmp_path.rglob("*.bin"))


def test_an_unclean_audit_is_never_shareable(
    tune: Tune, tmp_path: Path
) -> None:
    """CR-20260724-02: a failed audit that ``ran`` must not yield a share path.

    The direct assembler is handed a clean gate outcome, then an audit carrying
    an unexplained byte is attached — the exact inconsistency a hand-built or
    mistaken caller can present. ``share_path`` must stay ``None``: an audit that
    ran and *failed* is not a licence to share, even if the problem list is empty.
    """
    tune.write_cells("pressure_quotient_max", {(0, 0): 1.7}, intent="lower")
    outcome = run_gates(tune, tmp_path / "r01.bin", reference_bin=None)
    assert outcome.ok  # every gate that ran passed; no reference, so no audit

    # Attach an audit that ran and found an unexplained byte, but leave the
    # problem list untouched — the contradiction the finding describes.
    outcome.diff = RawDiffAudit(
        reference="stock.bin", candidate=str(outcome.bin_path),
        changed=1, unexplained=(0x1234,), changed_offsets=frozenset({0x1234}),
    )
    report = build_report(tune, "R01", outcome)

    assert report.share_path is None                 # the safety property
    assert report.audit.ran and not report.audit.clean
    audit_gate = next(g for g in report.gates if g.name == "Raw-diff audit")
    assert not audit_gate.passed                     # the row agrees with the model


def test_journal_mutated_after_gates_is_rejected(
    tune: Tune, tmp_path: Path, real_bin: Path
) -> None:
    """CR-20260724-02: a post-gate journal edit invalidates the whole report.

    The gates run and pass against one journal; another table is then written to
    the tune, so the live journal no longer matches the saved bin. The report is
    built from that live journal — it must be neither verified nor shareable, and
    must say why, rather than describe changes the gated bin never contained.
    """
    tune.write_cells("pressure_quotient_max", {(0, 0): 1.7}, intent="gated edit")
    outcome = run_gates(tune, tmp_path / "r01.bin", reference_bin=real_bin)
    assert outcome.ok and outcome.diff is not None and outcome.diff.clean

    # Mutate the journal *after* the gates saved and audited the bin.
    tune.write_cells("pressure_quotient_max", {(1, 0): 1.6}, intent="post-gate")
    report = build_report(tune, "R01", outcome, reference_bin=real_bin)

    assert not report.verified
    assert report.share_path is None
    assert any("journal changed after the gates" in p for p in report.problems)


def test_a_noop_declaration_is_not_a_changed_table(
    tune: Tune, tmp_path: Path, real_bin: Path
) -> None:
    """CR-20260724-03: a re-declared table that moved no bytes is not "changed".

    Writing a table's current value back journals it (the readback needs the
    declaration) but changes nothing versus the source. The byte audit finds zero
    changed bytes, so the model's ``changed_tables`` — the highest-value review
    list — must not list it, matching the desktop HTML renderer and the audit.
    The no-op edit still appears in the full ``edits`` journal, as a 0-byte move.
    """
    view = tune.space("base").cal.get("IP_PQ_CHA_MAX")
    current = float(np.asarray(view.values)[0, 0])
    tune.write_cells("pressure_quotient_max", {(0, 0): current},
                     intent="re-declare the current value — no change")

    report = build_revision(
        tune, "R01", staging_dir=tmp_path, reference_bin=real_bin,
    )

    assert report.verified
    assert report.audit.ran and report.audit.clean
    # Not in the changed list — nothing moved versus the reference.
    assert not any("IP_PQ_CHA_MAX" in t.label for t in report.changed_tables)
    # But still journaled, as a zero-byte declaration.
    noop = next(e for e in report.edits if "IP_PQ_CHA_MAX" in e.label)
    assert noop.moved_bytes == 0


# --------------------------------------------------------------------------- #
# the mobile closure: no matplotlib on the build-service import path
# --------------------------------------------------------------------------- #
def test_build_service_imports_without_matplotlib() -> None:
    """The whole point of the service: it runs the gates with no plotting stack.

    A fresh interpreter imports the service and asserts matplotlib never loaded —
    the on-device (Chaquopy) engine carries no matplotlib at all.
    """
    code = (
        "import sys; import simoscal.tune.build_service as bs; "
        "assert 'matplotlib' not in sys.modules, sorted(sys.modules); "
        "assert hasattr(bs, 'build_revision'); print('ok')"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ok"
