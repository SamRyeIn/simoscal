"""Acceptance suite — AE1-AE8 from the other-file-structures requirements.

`Docs/brainstorms/2026-08-22-other-file-structures-requirements.md`. AE numbering
in this repo is **per-plan**: `test_acceptance.py`, `_plot`, `_export`, `_sop`,
`_btp` and `_analysis` each scope their own AE1-AEn to the effort they came from,
declared in the file docstring. This file is that scope for the A05 port, so
`test_ae4_*` here means this plan's AE4 and nothing else.

The claims:

* **AE1** — opening the A05 bin and saving with no edits is byte-identical;
* **AE2** — preflight returns `READY`/`writable`, naming profile `SCGA05`;
* **AE3** — `verify()` reports both checksums verifiable and clean, through the
  profile's own CAL base and ECM3 location;
* **AE4** — an edit built through the real gate chain produces a bin whose
  checksums verify clean and whose byte audit attributes every changed byte;
* **AE5** — the nine ignition tables are (16, 18) here and (16, 16) on SC8S50,
  and a wrong declared shape is still refused on both;
* **AE6** — the SC8S50 suite passes with no test body edited, and the foreign
  suite's structural claims were rewritten rather than deleted;
* **AE7** — `correct()` raises when a checksum cannot be located, rather than
  handing back bytes that look corrected;
* **AE8** — a bin matching no registered profile is `INSPECT_ONLY`, and the
  refusal names the software it detected.

Several of these are pinned in depth elsewhere — `test_foreign_structure.py`'s
F-numbered tests, `test_checksum.py`, `test_preflight.py`. Where that is so, the
test here asserts the AE's headline claim and its docstring names the detailed
test, so the acceptance list is enumerable in one place without the detail being
stated twice and drifting.

Like `test_foreign_structure.py`, every test skips cleanly when the A05 files are
absent, and `SIMOSCAL_REQUIRE_FOREIGN=1` turns that skip into a failure.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import numpy as np
import pytest

from simoscal import checksum as ck
from simoscal.calfile import CalFile
from simoscal.preflight import INSPECT_ONLY, READY, preflight
from simoscal.tune import Tune
from simoscal.tune.pipeline import run_gates
from simoscal.tune.profile import ProfileResolutionError, resolve
from simoscal.tune.profiles import SC8S50, SCGA05

CODE_ROOT = Path(__file__).resolve().parents[1]

#: The foreign set, same files `test_foreign_structure.py` uses. Restated rather
#: than imported: pytest test modules are not a package here, and the other
#: acceptance suites are likewise self-contained.
A05_BIN = CODE_ROOT / "bin" / "3CN906259B__0002_SCGA05.bin"
A05_XDF = CODE_ROOT / "xdf" / "SCGa05_cal.xdf"


def _sha(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _require(*paths: Path) -> None:
    """Skip — or, under ``SIMOSCAL_REQUIRE_FOREIGN=1``, fail — if any is absent."""
    import os

    missing = [p for p in paths if not p.is_file()]
    if not missing:
        return
    names = ", ".join(str(p) for p in missing)
    if os.environ.get("SIMOSCAL_REQUIRE_FOREIGN") == "1":
        pytest.fail(
            f"SIMOSCAL_REQUIRE_FOREIGN=1 but the foreign fixture is absent: {names}"
        )
    pytest.skip(f"foreign A05 fixture not present: {names}")

IGNITION_GRIDS = tuple(SCGA05.table_set("ignition_base_vvl0"))


def _a05_tune() -> Tune:
    _require(A05_BIN, A05_XDF)
    return Tune.open(SCGA05, xdf=A05_XDF, bin=A05_BIN)


# ---- AE1 — a save with no edits changes nothing ----------------------------- #
def test_ae1_open_and_save_unedited_is_byte_identical(tmp_path) -> None:
    """Detail: `test_f6_ae1_opening_and_saving_the_a05_bin_changes_nothing`.

    The floor the whole port stands on. If merely opening and re-saving moved a
    byte, every later claim about *what* an edit changed would be measuring the
    wrong thing.
    """
    _require(A05_BIN, A05_XDF)
    out = tmp_path / "a05_untouched.bin"
    Tune.open(SCGA05, xdf=A05_XDF, bin=A05_BIN).save(out)
    assert _sha(out) == _sha(A05_BIN)


# ---- AE2 — preflight recognises the car and clears it for editing ---------- #
def test_ae2_preflight_is_ready_and_writable_naming_scga05() -> None:
    """Detail: `test_f2_a05_is_ready_and_writable_through_its_declared_convention`."""
    _require(A05_BIN, A05_XDF)
    v = preflight(A05_BIN, A05_XDF)
    assert v.status == READY
    assert v.writable is True and v.profile_matched is True
    assert v.profile_name == "SCGA05"


# ---- AE3 — both checksums verifiable and clean on the stock bin ------------ #
def test_ae3_both_checksums_verify_clean_on_the_stock_bin() -> None:
    """Detail: `test_f6_both_a05_checksums_verify_under_the_declared_structure`.

    Both, not one: U1's go/no-go was whether ECM3 could be located at all on
    this car. A suite that only checked CAL_CRC would pass on a bin nobody can
    actually flash.
    """
    _require(A05_BIN, A05_XDF)
    reports = _a05_tune().space("base").cal.verify_checksums()
    assert {r.name for r in reports} == {"CAL_CRC", "ECM3"}
    assert all(r.can_verify and not r.is_stale for r in reports)
    assert all(r.stored == r.computed for r in reports)


# ---- AE4 — an edit through the real gate chain ----------------------------- #
def test_ae4_an_a05_edit_passes_every_build_gate(tmp_path) -> None:
    """The acceptance that matters: A05 through the spine a real build runs.

    `test_f6_an_a05_edit_lands_in_the_table_and_nowhere_else` proves the bytes
    land in the right place, but it does that through `CalFile.save` — it never
    exercises the gate chain the tuning loop actually calls. This does: save with
    checksums corrected, verify them *independently off the written file*, read
    every journaled table back off the saved bin, and audit it byte for byte
    against the stock bin with an allowance derived from the journal.

    Every gate has to vote, and `problems` has to be empty — a build that ran the
    chain and collected a complaint is not a passing build.
    """
    tune = _a05_tune()
    values = tune.values("put_setpoint")
    values[-1] = values[-1] + 100.0
    tune.write("put_setpoint", values, intent="raise the full-load boost row")

    outcome = run_gates(tune, tmp_path / "a05_edited.bin", reference_bin=A05_BIN)

    assert outcome.problems == (), f"gates complained: {outcome.problems}"
    assert outcome.ok is True
    assert outcome.checksums_clean and outcome.checksum_state == "CLEAN"
    assert {r.name for r in outcome.checksums} == {"CAL_CRC", "ECM3"}
    assert outcome.readback_failures == ()
    # The byte audit ran (it only does when a reference is declared) and nothing
    # changed that the journal does not account for.
    assert outcome.diff is not None, "no reference_bin means no byte audit"
    assert outcome.diff.clean, outcome.diff.summary()
    assert outcome.diff.unexplained == ()
    assert outcome.diff.changed > 0, "an edit that changed nothing proves nothing"


def test_ae4_an_undeclared_change_still_fails_the_audit(tmp_path) -> None:
    """The gate has to be capable of failing, or AE4's pass means nothing.

    Same edit and the same spine, but the declared reference carries one extra
    changed byte in the CAL block that no journal entry accounts for. The
    allowance is derived from the journal, so that byte is unattributable and
    has to surface as `unexplained` *and* as a build problem — the audit is a
    gate, not a report.
    """
    tune = _a05_tune()
    values = tune.values("put_setpoint")
    values[-1] = values[-1] + 100.0
    tune.write("put_setpoint", values, intent="raise the full-load boost row")

    # A reference differing from the stock bin at one byte the edit never
    # touches: padding well past the tables, inside the CAL block.
    victim = SCGA05.structure.cal_file_offset + 0x90000
    data = bytearray(A05_BIN.read_bytes())
    data[victim] ^= 0xFF
    reference = tmp_path / "a05_reference_with_an_extra_change.bin"
    reference.write_bytes(bytes(data))

    outcome = run_gates(tune, tmp_path / "a05_edited.bin", reference_bin=reference)

    assert outcome.diff is not None and not outcome.diff.clean
    assert victim in outcome.diff.unexplained
    assert any("unexplained" in p for p in outcome.problems)
    assert outcome.ok is False


# ---- AE5 — the shape claim, both directions -------------------------------- #
def test_ae5_the_nine_ignition_grids_differ_in_shape_between_the_cars() -> None:
    """Detail: `test_f6_the_nine_ignition_grids_are_16x18_here_and_16x16_there`.

    The positive claim behind the safety story: shapes are declared per car, so
    they are data that can be *wrong*, which is why the negative test below has
    to exist alongside it.
    """
    assert len(IGNITION_GRIDS) == 9
    for name in IGNITION_GRIDS:
        assert SCGA05.specs[name].shape == (16, 18)
        assert SC8S50.specs[name].shape == (16, 16)


def test_ae5_a_wrong_declared_shape_is_still_refused() -> None:
    """Detail: `test_f6_declaring_the_wrong_shape_for_a05_still_fails`.

    Per-car shapes must not become a way to wave a table through. Declaring
    SC8S50's (16, 16) for an A05 grid has to fail resolution the same way any
    other shape mismatch does.
    """
    from dataclasses import replace

    _require(A05_BIN, A05_XDF)
    cal = CalFile.open(
        str(A05_XDF), str(A05_BIN),
        structure=SCGA05.structure, base_offset=SCGA05.xdf_base_offset,
    )
    name = IGNITION_GRIDS[0]
    wrong = replace(
        SCGA05, name="WrongShape",
        specs={**SCGA05.specs, name: replace(SCGA05.specs[name], shape=(16, 16))},
    )
    with pytest.raises(ProfileResolutionError):
        resolve(wrong, cal, xdf_label=str(A05_XDF))


# ---- AE7 — an unlocatable checksum raises ---------------------------------- #
def test_ae7_correct_raises_when_a_checksum_cannot_be_located() -> None:
    """Detail: `test_checksum.py::test_correct_refuses_a_bin_whose_ecm3_cannot_be_located`.

    Correcting what it can and returning the buffer would hand back a bin that
    looks corrected and is not flash-ready. That silent no-op is the failure mode
    this raise exists to prevent, so it is listed as an acceptance rather than
    left as an internal detail.
    """
    _require(A05_BIN)
    # The A05 bin read under SC8S50's layout: neither checksum is where that
    # structure says, which is precisely the "cannot be located" case.
    with pytest.raises(ck.ChecksumNotLocatable) as excinfo:
        ck.correct(A05_BIN.read_bytes(), ck.SC8S50_STRUCTURE)
    assert "SC8S50" in str(excinfo.value)

    # And the contrast, so the raise is about location rather than about A05:
    # under its own structure the same bytes correct without complaint.
    corrected, pre = ck.correct(A05_BIN.read_bytes(), SCGA05.structure)
    assert all(not r.is_stale for r in ck.verify(corrected, SCGA05.structure))


# ---- AE8 — an unrecognised bin is inspectable, never writable -------------- #
def test_ae8_a_bin_matching_no_profile_is_inspect_only_and_names_the_software(
    monkeypatch, real_bin, real_xdf,
) -> None:
    """Detail: `test_preflight.py::test_no_registered_profile_at_all_is_inspect_only`.

    The distinction the whole registry rests on: "this library does not know your
    car" is not "your file is broken". The verdict stays readable, refuses to be
    writable, and names the software it detected — so someone can tell which car
    to go and add, rather than only being told it is not SC8S50.
    """
    monkeypatch.setattr("simoscal.tune.profiles.BASE_PROFILES", ())
    v = preflight(real_bin, real_xdf)
    assert v.status == INSPECT_ONLY
    assert v.writable is False and v.ok_to_edit is False
    assert v.profile_name is None
    assert "SC8S50.a2l" in v.summary, "the refusal must name the detected software"


# ---- AE6 — the SC8S50 suite survived the port ------------------------------ #
#: The structural claims F1-F5 made before A05 became writable. The plan's
#: commitment was that these are **rewritten, not deleted** — the file structures
#: really do differ, and that stays true however A05's status changed. Deleting
#: one to make a suite green is the failure this list exists to catch, so the
#: names are pinned here rather than trusted to review.
F1_TO_F5_STRUCTURAL_CLAIMS = (
    "test_f1_a05_base_xdf_declares_zero_base_offset",
    "test_f1_s50_and_a05_base_offsets_actually_differ",
    "test_f1_a05_patch_xdf_uses_a_third_base_offset",
    "test_f3_sc8s50_profile_does_not_resolve_against_a05",
    "test_f3_partial_match_is_still_a_refusal",
    "test_f3_shape_mismatched_ignition_tables_are_refused_for_shape",
    "test_f3_a_shape_mismatch_is_never_a_mere_name_miss",
    "test_f4a_simoscal_style_patch_xdf_fails_to_parse",
    "test_f4b_bintoolz_patch_parses_but_resolves_nothing",
    "test_f5_neither_checksum_verifies_under_the_sc8s50_structure",
    "test_f5_correct_refuses_rather_than_silently_changing_nothing",
    "test_f5_s50_still_verifies_clean_side_by_side",
)


def test_ae6_the_structural_claims_were_rewritten_not_deleted() -> None:
    """A05 becoming writable must not have cost a single structural assertion.

    F2's verdict changed (`INSPECT_ONLY` → `READY`) because the port changed it,
    and F4b's reading was reassigned when A05 got its own patch map. Neither is
    a licence to drop the claims that are still true: the base offsets differ,
    the shapes differ, S50's profile and patch map still resolve nothing against
    A05, and neither checksum is locatable under the other car's structure.
    """
    source = (Path(__file__).parent / "test_foreign_structure.py").read_text()
    missing = [
        name for name in F1_TO_F5_STRUCTURAL_CLAIMS
        if f"def {name}(" not in source
    ]
    assert not missing, (
        "structural claims deleted rather than rewritten: " + ", ".join(missing)
    )


def test_ae6_the_foreign_suite_still_fails_loudly_when_its_fixtures_vanish(
    monkeypatch,
) -> None:
    """The regression the plan names: absent fixtures must be failable, not silent.

    A skip-if-absent suite can quietly not run, which for a safety suite is the
    failure mode that matters. `SIMOSCAL_REQUIRE_FOREIGN=1` converts that skip
    into a failure — checked here by pointing `_require` at a file that is not
    there, since the real fixtures are present on this machine.
    """
    absent = Path("/nonexistent/not-a-real-bin.bin")

    monkeypatch.setenv("SIMOSCAL_REQUIRE_FOREIGN", "1")
    # `pytest.fail` raises an OutcomeException, which is a BaseException — the
    # named classes rather than `Exception`, so this cannot pass by catching
    # some unrelated error.
    with pytest.raises(pytest.fail.Exception) as failed:
        _require(absent)
    assert "SIMOSCAL_REQUIRE_FOREIGN=1" in str(failed.value)

    # And without it, the same call skips rather than failing.
    monkeypatch.delenv("SIMOSCAL_REQUIRE_FOREIGN", raising=False)
    with pytest.raises(pytest.skip.Exception) as skipped:
        _require(absent)
    assert "not present" in str(skipped.value)
