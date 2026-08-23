"""Tests for the tune journal, audit, and build pipeline (U2).

The pipeline's whole job is to fail when something is wrong, so most of these
tests inject a specific fault and assert the corresponding gate catches it:

* an edit made behind the journal's back → unexplained bytes in the audit;
* a save that skips checksum correction → the checksum gate;
* a write a guard rejects → the blocked-write gate;
* a table whose value does not survive the save → the readback gate.

They run against the real stock bin (skipping cleanly when it is absent), since
a synthetic fixture cannot exercise checksum correction or the byte-level
audit.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from simoscal import CalFile
from simoscal.tune import (
    SC8S50,
    BuildFailed,
    Tune,
    build,
)
from simoscal.tune import audit as tune_audit
from simoscal.tune.journal import (
    KIND_TABLE,
    VERDICT_APPLIED,
    VERDICT_BLOCKED,
    VERDICT_UNCHANGED,
    EditEntry,
    Journal,
    summarize,
)
from simoscal.checksum import SC8S50_STRUCTURE


@pytest.fixture
def tune(real_xdf: Path, real_bin: Path) -> Tune:
    """A freshly opened tune over the stock bin (unedited each time)."""
    return Tune.open(SC8S50, xdf=real_xdf, bin=real_bin)


@pytest.fixture
def baseline(tune: Tune, tmp_path: Path) -> Path:
    """The stock bin rebuilt through the pipeline — a reference to diff against."""
    return build(tune, "R00", out_root=tmp_path / "ref",
                 bin_name="baseline.bin", plots=False).bin_path


# --------------------------------------------------------------------------- #
# Journal unit behaviour (no bin needed)
# --------------------------------------------------------------------------- #
def test_summarize_prefers_the_most_readable_form() -> None:
    assert summarize(np.array([[2.5]])) == "2.5"
    assert summarize(np.full((8, 12), 0.8)) == "flat 0.8"
    assert summarize(np.array([[1.0, 2.0, 3.0]])) == "1, 2, 3"
    assert summarize(np.arange(100.0).reshape(10, 10)) == "0..99"
    assert summarize(None) == ""


def test_entry_summary_narrows_to_the_rows_that_changed() -> None:
    """A one-row edit must not hide behind a whole-table min..max."""
    before = np.array([[1.0, 1.0], [10.0, 10.0]])
    after = np.array([[1.0, 1.0], [5.0, 5.0]])
    entry = EditEntry(
        space="base", name="t", label="`T` — Test", key="T",
        kind=KIND_TABLE, verdict=VERDICT_APPLIED,
        before=before, after=after, rows_changed=(1,),
    )
    assert entry.before_text() == "flat 10"
    assert entry.after_text() == "flat 5"
    assert entry.scope_text() == "table (row 1)"


def test_journal_counts_and_touching_partition_entries() -> None:
    journal = Journal()
    applied = EditEntry(space="base", name="a", label="`A` — A", key="A",
                        kind=KIND_TABLE, verdict=VERDICT_APPLIED,
                        offsets=frozenset({1, 2}))
    unchanged = EditEntry(space="base", name="b", label="`B` — B", key="B",
                          kind=KIND_TABLE, verdict=VERDICT_UNCHANGED)
    blocked = EditEntry(space="base", name="c", label="`C` — C", key="C",
                        kind=KIND_TABLE, verdict=VERDICT_BLOCKED)
    for entry in (applied, unchanged, blocked):
        journal.record(entry)

    assert journal.counts() == {"applied": 1, "unchanged": 1, "blocked": 1}
    assert journal.touching() == (applied,)
    assert journal.blocked() == (blocked,)
    assert journal.changed_offsets() == frozenset({1, 2})
    # Keyed on the XDF key, not the logical name — one table can be journaled
    # under both, and the readback must treat it as one table.
    assert journal.tables_touched() == (("base", "A"),)


def test_one_table_journaled_under_two_names_is_still_one_table() -> None:
    """A domain call names a table logically; the SOP names it by symbol."""
    journal = Journal()
    journal.record(EditEntry(
        space="base", name="put_setpoint", label="`IP_PUT_SP` — …",
        key="IP_PUT_SP", kind=KIND_TABLE, verdict=VERDICT_APPLIED,
        offsets=frozenset({10}),
    ))
    journal.record(EditEntry(
        space="base", name="IP_PUT_SP", label="`IP_PUT_SP` — …",
        key="IP_PUT_SP", kind=KIND_TABLE, verdict=VERDICT_APPLIED,
        offsets=frozenset({11}),
    ))

    assert journal.tables_touched() == (("base", "IP_PUT_SP"),)


# --------------------------------------------------------------------------- #
# Writing + journaling
# --------------------------------------------------------------------------- #
def test_write_journals_the_id_description_and_before_after(tune: Tune) -> None:
    """AE4: the journal names the table as `ID` — Description with values."""
    values = tune.values("put_setpoint")
    values[3] = 2500.0
    entry = tune.write("put_setpoint", values, intent="flatten the full-load row")

    assert entry.verdict == VERDICT_APPLIED
    assert entry.label == "`IP_PUT_SP` — Pressure up throttle setpoint"
    assert entry.units == "hPa"
    assert entry.intent == "flatten the full-load row"
    assert entry.rows_changed == (3,)
    assert entry.offsets  # bytes were measured, not assumed
    assert tune.journal.entries == (entry,)


def test_write_measures_only_the_bytes_that_actually_moved(tune: Tune) -> None:
    """The allowance is measured from the buffer, not inferred from intent.

    Writing 2500 hPa over six neighbouring cells moves only 8 bytes, not 12:
    four of the six cells differ from the target in their low byte alone.
    """
    values = tune.values("put_setpoint")
    values[3] = 2500.0
    entry = tune.write("put_setpoint", values, intent="flatten")

    view = tune.table("put_setpoint").view
    extent = tune_audit.table_byte_offsets(view, rows=[3])
    assert entry.offsets < extent  # a strict subset of the row's byte extent
    assert len(entry.offsets) == 8


def test_writing_the_current_values_is_journaled_as_unchanged(tune: Tune) -> None:
    entry = tune.write(
        "put_setpoint", tune.values("put_setpoint"), intent="no-op"
    )
    assert entry.verdict == VERDICT_UNCHANGED
    assert entry.offsets == frozenset()


def test_a_guard_rejection_is_journaled_not_raised(tune: Tune) -> None:
    """A blocked write leaves the table byte-identical and fails the build."""
    before = tune.values("manifold_pressure_max")
    entry = tune.write(
        "manifold_pressure_max", [[350000.0]],
        intent="raise the manifold pressure ceiling",
    )

    assert entry.verdict == VERDICT_BLOCKED
    assert entry.detail  # carries the guard's own explanation
    assert entry.offsets == frozenset()
    assert np.allclose(tune.values("manifold_pressure_max"), before)


def test_write_cells_leaves_every_other_cell_alone(tune: Tune) -> None:
    before = tune.values("pressure_quotient_max")
    tune.write_cells("pressure_quotient_max", {(0, 0): 1.7},
                     intent="lower the 1000 rpm breakpoint")
    after = tune.values("pressure_quotient_max")

    assert after[0, 0] == pytest.approx(1.7, abs=1e-3)
    assert np.allclose(after[1:], before[1:])
    assert np.allclose(after[0, 1:], before[0, 1:])


def test_note_records_a_deliberate_non_change(tune: Tune) -> None:
    entry = tune.note(
        "wastegate_feedforward_vvl0", "no flow-factor channels logged yet",
        intent="deliberate skip",
    )
    assert entry.verdict == "skipped"
    assert not entry.touched_bytes
    assert "IP_FAC_BPA_SP[0]" in entry.label


# --------------------------------------------------------------------------- #
# build(): the happy path (AE6)
# --------------------------------------------------------------------------- #
def test_build_produces_the_full_artifact_set(tune: Tune, tmp_path: Path,
                                              baseline: Path) -> None:
    """AE6: domain calls plus build() alone yield bin, report, plots, CLEAN."""
    fresh = Tune.open(SC8S50, xdf=tune.space("base").xdf, bin=tune.source_bin)
    values = fresh.values("put_setpoint")
    values[3] = 2500.0
    fresh.write("put_setpoint", values, intent="flatten the full-load row")

    result = build(fresh, "R01", out_root=tmp_path, bin_name="r01.bin",
                   reference_bin=baseline)

    assert result.ok
    assert result.bin_path.name == "r01.bin" and result.bin_path.is_file()
    assert result.report_path.is_file()
    assert result.checksums_clean
    assert result.readback_failures == ()
    assert result.diff is not None and result.diff.clean
    assert result.plots and all(p.is_file() for p in result.plots)
    # A fresh timestamped folder per run, so prior runs are never overwritten.
    assert result.out_dir.name.startswith("R01_")


def test_report_renders_the_journal_with_ids_and_gates(
    tune: Tune, tmp_path: Path, baseline: Path
) -> None:
    fresh = Tune.open(SC8S50, xdf=tune.space("base").xdf, bin=tune.source_bin)
    fresh.write("pressure_quotient_max", np.full((8, 8), 3.1),
                intent="raise the compressor pressure-quotient cap")
    result = build(fresh, "R01", out_root=tmp_path, reference_bin=baseline,
                   plots=False)
    text = result.report_path.read_text()

    assert "`IP_PQ_CHA_MAX` — Maximum allowed pressure quotient" in text
    assert "raise the compressor pressure-quotient cap" in text
    assert "flat 3.1001" in text          # after value, as stored
    assert "Checksums: **CLEAN**" in text
    assert "unexplained = 0" in text
    assert "never flashes" in text


def test_build_with_no_edits_still_verifies_and_reports(
    tune: Tune, tmp_path: Path
) -> None:
    result = build(tune, "R00", out_root=tmp_path, plots=False)

    assert result.ok and result.checksums_clean
    assert len(result.journal) == 0
    assert "changed nothing" in result.report_path.read_text()


def test_build_without_a_reference_makes_no_byte_level_claim(
    tune: Tune, tmp_path: Path
) -> None:
    """A first revision has no predecessor; the report must say so, not imply clean."""
    tune.write_cells("pressure_quotient_max", {(0, 0): 1.7}, intent="lower")
    result = build(tune, "R00", out_root=tmp_path, plots=False)

    assert result.diff is None
    assert "Raw-diff audit: **not run**" in result.report_path.read_text()


# --------------------------------------------------------------------------- #
# build(): the gates (fault injection)
# --------------------------------------------------------------------------- #
def test_an_unjournaled_edit_shows_up_as_unexplained_bytes(
    tune: Tune, tmp_path: Path, baseline: Path
) -> None:
    """The core inversion: bytes changed outside the journal fail the build."""
    fresh = Tune.open(SC8S50, xdf=tune.space("base").xdf, bin=tune.source_bin)
    fresh.write_cells("pressure_quotient_max", {(0, 0): 1.7}, intent="declared")
    # Edit straight through the CalFile, bypassing Tune.write and the journal.
    smuggled = fresh.space("base").cal.get("IP_TQI_REF_MAX_MON")
    smuggled.set(np.full(smuggled.shape, 900.0))

    with pytest.raises(BuildFailed) as excinfo:
        build(fresh, "R01", out_root=tmp_path, reference_bin=baseline, plots=False)

    assert any("unexplained" in p for p in excinfo.value.problems)
    # The report is written before the raise, so the failure is reviewable.
    report = (excinfo.value.out_dir / "report.md").read_text()
    assert "VERIFICATION FAILED" in report
    assert "UNEXPLAINED BYTES" in report


def test_a_stale_checksum_fails_the_build(
    tune: Tune, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inject a save that skips checksum correction; the verify gate must catch it."""
    tune.write_cells("pressure_quotient_max", {(0, 0): 1.7}, intent="lower")

    def save_without_correcting(self, path, **kwargs):
        return self.space("base").cal.save(
            path, correct_checksums=False, warn_stale=False
        )

    monkeypatch.setattr(Tune, "save", save_without_correcting)

    with pytest.raises(BuildFailed, match="checksums STALE"):
        build(tune, "R01", out_root=tmp_path, plots=False)


@pytest.mark.parametrize("n_unverifiable", [1, 2])
def test_unverifiable_checksums_fail_the_build(
    tune: Tune, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, n_unverifiable: int
) -> None:
    """CR-20260720-01: a checksum that could not be verified is not clean.

    The checksum layer returns ``can_verify=False`` for a malformed, short, or
    unsupported layout. Treating that as a passing vote presents a bin that was
    never actually checked as ``Checksums: CLEAN`` — the exact silent hole this
    test pins shut, for one unverifiable report and for both.
    """
    from simoscal.checksum import ChecksumReport

    tune.write_cells("pressure_quotient_max", {(0, 0): 1.7}, intent="lower")

    def fake_verify(self):
        reports = [
            ChecksumReport("CAL_CRC", can_verify=False, is_stale=False,
                           detail="unsupported layout"),
        ]
        if n_unverifiable == 2:
            reports.append(ChecksumReport("ECM3", can_verify=False, is_stale=False,
                                          detail="bin too short"))
        else:
            # A genuinely-verified, current second checksum: the build must still
            # fail on the one it could not verify, not pass on the one it could.
            reports.append(ChecksumReport("ECM3", can_verify=True, is_stale=False,
                                          stored=0x1234, computed=0x1234))
        return reports

    monkeypatch.setattr(CalFile, "verify_checksums", fake_verify)

    with pytest.raises(BuildFailed, match="checksums UNVERIFIABLE") as excinfo:
        build(tune, "R01", out_root=tmp_path, plots=False)

    report = (excinfo.value.out_dir / "report.md").read_text()
    assert "Checksums: **UNVERIFIABLE — DO NOT FLASH**" in report


def test_no_checksum_reports_is_not_vacuously_clean(
    tune: Tune, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty report set means the checksum gate never ran — not that it passed."""
    tune.write_cells("pressure_quotient_max", {(0, 0): 1.7}, intent="lower")
    monkeypatch.setattr(CalFile, "verify_checksums", lambda self: [])

    with pytest.raises(BuildFailed, match="checksums UNVERIFIABLE"):
        build(tune, "R01", out_root=tmp_path, plots=False)


def test_restoring_a_table_to_stock_passes_the_audit(
    tune: Tune, tmp_path: Path, baseline: Path
) -> None:
    """CR-20260720-02: backing out a prior revision's change is a clean build.

    Revision A changes one cell; revision B, built from stock, explicitly writes
    that cell back to its stock value. B stages no bytes versus stock, but its
    bin differs from A at that cell — a legitimate reversion the audit must
    attribute, not reject as unexplained.
    """
    sym = "pressure_quotient_max"
    stock = tune.values(sym)

    # Revision A: change one cell, audited against the stock baseline.
    tune_a = Tune.open(SC8S50, xdf=tune.space("base").xdf, bin=tune.source_bin)
    tune_a.write_cells(sym, {(0, 0): float(stock[0, 0]) + 0.05}, intent="nudge one cell")
    rev_a_bin = build(
        tune_a, "RA", out_root=tmp_path / "a", bin_name="ra.bin",
        reference_bin=baseline, plots=False,
    ).bin_path

    # Revision B: restore that cell to stock, audited against revision A.
    tune_b = Tune.open(SC8S50, xdf=tune.space("base").xdf, bin=tune.source_bin)
    entry = tune_b.write_cells(sym, {(0, 0): float(stock[0, 0])}, intent="restore stock")
    assert entry.verdict == VERDICT_UNCHANGED  # nothing moved vs stock
    assert entry.offsets == frozenset()
    assert entry.declares_table  # but it still declared the table's extent

    result = build(
        tune_b, "RB", out_root=tmp_path / "b", bin_name="rb.bin",
        reference_bin=rev_a_bin, plots=False,
    )
    assert result.ok, result.problems
    assert result.diff is not None and result.diff.clean
    assert "declared restore to stock" in result.diff.attributed
    # The restored table was read back off the saved bin, pinning its contents.
    reopened = CalFile.open(str(tune.space("base").xdf), str(result.bin_path), structure=SC8S50_STRUCTURE)
    assert np.allclose(reopened.get("IP_PQ_CHA_MAX").values, stock, atol=1e-3)


def test_a_smuggled_change_into_a_declared_table_is_still_caught(
    tune: Tune, tmp_path: Path, baseline: Path
) -> None:
    """The restore allowance is tight: it authorises only bytes equal to source.

    A change smuggled past the journal into a *different cell of a declared
    table* differs from source, so it is not a restore and must still fail the
    audit — the declared-extent widening must not become a blanket pass on the
    whole table.
    """
    fresh = Tune.open(SC8S50, xdf=tune.space("base").xdf, bin=tune.source_bin)
    # Declare a write to one cell of the table...
    fresh.write_cells("pressure_quotient_max", {(0, 0): 1.7}, intent="declared")
    # ...then smuggle an undeclared change into a different cell of the same table.
    smuggled = fresh.space("base").cal.get("IP_PQ_CHA_MAX")
    values = np.array(smuggled.values)
    values[2, 2] = float(values[2, 2]) + 0.5
    smuggled.set(values)

    with pytest.raises(BuildFailed) as excinfo:
        build(fresh, "R01", out_root=tmp_path, reference_bin=baseline, plots=False)
    assert any("unexplained" in p for p in excinfo.value.problems)


def test_a_blocked_write_fails_the_build(tune: Tune, tmp_path: Path) -> None:
    """A guard rejection is not a warning to skim past — it stops the build."""
    tune.write("manifold_pressure_max", [[350000.0]], intent="raise the ceiling")

    with pytest.raises(BuildFailed, match="guard blocked"):
        build(tune, "R01", out_root=tmp_path, plots=False)


def test_readback_catches_a_value_that_did_not_survive_the_save(
    tune: Tune, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Corrupt the saved file after the write; the readback gate must notice."""
    tune.write_cells("pressure_quotient_max", {(0, 0): 1.7}, intent="lower")

    real_save = Tune.save

    def save_then_corrupt(self, path, **kwargs):
        reports = real_save(self, path, **kwargs)
        cal = CalFile.open(str(self.space("base").xdf), str(path), structure=SC8S50_STRUCTURE)
        view = cal.get("IP_PQ_CHA_MAX")
        values = np.array(view.values)
        values[0, 0] = 9.0
        view.set(values)
        cal.save(path, correct_checksums=True)
        return reports

    monkeypatch.setattr(Tune, "save", save_then_corrupt)

    with pytest.raises(BuildFailed, match="readback"):
        build(tune, "R01", out_root=tmp_path, plots=False)


# --------------------------------------------------------------------------- #
# audit unit behaviour
# --------------------------------------------------------------------------- #
def test_audit_attributes_each_changed_byte_to_exactly_one_allowance(
    tmp_path: Path,
) -> None:
    """Overlapping allowances must not double-count a byte."""
    before, after = tmp_path / "a.bin", tmp_path / "b.bin"
    before.write_bytes(bytes(8))
    after.write_bytes(bytes([1, 1, 1, 0, 0, 0, 0, 1]))  # bytes 0,1,2,7 changed

    result = tune_audit.raw_diff_audit(before, after, [
        tune_audit.Allowance("first", frozenset({0, 1})),
        tune_audit.Allowance("second", frozenset({1, 2})),  # overlaps on byte 1
    ])

    assert result.changed == 4
    assert result.attributed == {"first": 2, "second": 1}
    assert result.unexplained == (7,)
    assert not result.clean
    assert "0x7" in result.summary()


def test_audit_is_clean_when_every_change_is_allowed(tmp_path: Path) -> None:
    before, after = tmp_path / "a.bin", tmp_path / "b.bin"
    before.write_bytes(bytes(4))
    after.write_bytes(bytes([0, 9, 0, 0]))

    result = tune_audit.raw_diff_audit(
        before, after, [tune_audit.Allowance("declared", frozenset({1}))]
    )

    assert result.clean and result.unexplained == ()
    assert result.summary() == "1 changed byte(s), all attributed; unexplained = 0"


def test_audit_refuses_to_compare_differently_sized_bins(tmp_path: Path) -> None:
    small, large = tmp_path / "a.bin", tmp_path / "b.bin"
    small.write_bytes(b"\x00" * 16)
    large.write_bytes(b"\x00" * 32)

    with pytest.raises(tune_audit.RawDiffError, match="file-size mismatch"):
        tune_audit.raw_diff_audit(small, large, [])


def test_table_byte_offsets_can_be_narrowed_to_rows(tune: Tune) -> None:
    view = tune.table("put_setpoint").view
    whole = tune_audit.table_byte_offsets(view)
    one_row = tune_audit.table_byte_offsets(view, rows=[3])

    assert one_row < whole
    assert len(whole) == 4 * len(one_row)  # 4 load rows, evenly sized

    with pytest.raises(ValueError, match="out of range"):
        tune_audit.table_byte_offsets(view, rows=[99])
