"""`advice_bundle`: what leaves the device, and what must never leave with it.

A bundle is the only thing the answering side ever sees. That gives these tests
two jobs, and they are opposites:

1. **Everything needed to answer is in it.** Every table the profile resolves —
   domain-owned ones included, or nothing could be recommended about boost —
   with current physical values and decoded axes; the journal; the picked logs
   as the analysis battery describes them; the safety brief; and provenance that
   says which calibration, which structure, and which address convention.
2. **The calibration itself is not.** The bin's and the XDF's bytes never appear;
   their hashes do. That is asserted against the real files rather than trusted,
   because "we would never serialize the bin" is not a mechanism.

Determinism (D7) sits between the two: the same session state exported twice is
byte-identical, which is what makes the back-test reproducible and lets two
revisions' bundles be diffed.

Real SC8S50/SCGA05 files are gitignored → tests skip (never fail) when absent.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from pathlib import Path

from simoscal.advice.brief import authored_half
from simoscal.advice.bundle import (
    BUNDLE_VERSION,
    bundle,
    logs_section,
    render,
    source_calibration,
    summary_of,
    write_bundle,
)
from simoscal.tune import SC8S50, Tune
from simoscal.tune.domains.switchpatch import PATCH_SPACE
from simoscal.tune.editing import EditOp, Selection, apply_op
from simoscal.tune.profiles import PROFILES
from simoscal.tune.profiles.switchpatch_2933 import SWITCH_PATCH_2933

CODE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = CODE_ROOT.parent

STOCK_BIN = CODE_ROOT / "bin" / "5G0906259L__0002.bin"
XDF = CODE_ROOT / "xdf" / "SC8S50.V1.0.xdf"
A05_BIN = CODE_ROOT / "bin" / "3CN906259B__0002_SCGA05.bin"
A05_XDF = CODE_ROOT / "xdf" / "SCGa05_cal.xdf"
SWITCH_XDF = REPO_ROOT / "BinToolz-main" / "definitions" / "S50 Switch Patch.29.33.V2.xdf"
PATCHED_BIN = (
    REPO_ROOT / "Tunes" / "TuningBasicsGuide" / "BinToolz-patched"
    / "CB_HSL_SP2933_5G0906259L_0002_BasicsGuide_R04.bin"
)

GRID = "pressure_quotient_max"
GRID_SYMBOL = "IP_PQ_CHA_MAX"      # the same table, as `CalFile.get` addresses it

PROVENANCE = {
    "profile": "SC8S50",
    "bin_sha256": "a" * 64,
    "xdf_sha256": "b" * 64,
    "has_switch_patch": False,
}


def _skip_unless(*paths: Path) -> None:
    missing = [str(p) for p in paths if not p.is_file()]
    if missing:
        pytest.skip(f"fixture absent: {', '.join(missing)}")


@pytest.fixture
def base_tune() -> Tune:
    _skip_unless(STOCK_BIN, XDF)
    return Tune.open(SC8S50, xdf=XDF, bin=STOCK_BIN)


@pytest.fixture
def patched_tune() -> Tune:
    _skip_unless(PATCHED_BIN, SWITCH_XDF, XDF)
    return Tune.open(
        SC8S50, xdf=XDF, bin=PATCHED_BIN,
        extra_spaces={PATCH_SPACE: (SWITCH_PATCH_2933, SWITCH_XDF)},
    )


@pytest.fixture
def a05_tune() -> Tune:
    _skip_unless(A05_BIN, A05_XDF)
    return Tune.open(PROFILES["SCGA05"], xdf=A05_XDF, bin=A05_BIN)


def _by_name(payload: dict) -> dict:
    return {(t["space"], t["name"]): t for t in payload["tables"]}


# --------------------------------------------------------------------------- #
# happy path — the whole session travels
# --------------------------------------------------------------------------- #
def test_a_session_with_two_edits_exports_both_of_them_and_the_whole_catalog(base_tune):
    first = base_tune.values(GRID)[0][0]
    apply_op(base_tune, GRID, EditOp.SET, selection=Selection.cells([(0, 0)]),
             value=float(first) + 0.02, intent="raise the first cell")
    apply_op(base_tune, "put_setpoint", EditOp.MUL, selection=Selection.row(0),
             value=1.01, intent="one percent more boost on the first row")

    payload = bundle(base_tune, provenance=PROVENANCE)

    assert payload["bundle_version"] == BUNDLE_VERSION
    assert [e["intent"] for e in payload["journal"]] == [
        "raise the first cell", "one percent more boost on the first row",
    ]
    assert payload["journal_counts"]
    # the catalog is whole: every resolved table, with values and both halves of
    # its name
    tables = _by_name(payload)
    assert len(tables) == len(list(base_tune.space("base").tables.names()))
    grid = tables[("base", GRID)]
    assert grid["label"].startswith("`") and " — " in grid["label"]
    assert grid["values"] and grid["units_description"]
    assert payload["safety_brief"].strip()


def test_an_edited_table_carries_the_grid_the_logs_were_recorded_on(base_tune):
    """`source_values` is the imported bin — the calibration that was flashed.

    Only where it differs from the working values, which is only where this
    session edited. Everywhere else it would be a byte-for-byte duplicate of
    `values` and a bundle twice the size for no second fact.
    """
    payload = bundle(base_tune, provenance=PROVENANCE)
    assert all("source_values" not in t for t in payload["tables"])

    stock = float(base_tune.values(GRID)[0][0])
    apply_op(base_tune, GRID, EditOp.SET, selection=Selection.cells([(0, 0)]),
             value=stock + 0.05, intent="move one cell")

    tables = _by_name(bundle(base_tune, provenance=PROVENANCE))
    edited = tables[("base", GRID)]
    assert edited["source_values"][0][0] == pytest.approx(stock)
    assert edited["values"][0][0] != edited["source_values"][0][0]
    assert sum("source_values" in t for t in tables.values()) == 1


def test_the_bundle_carries_domain_owned_tables_too(base_tune):
    """A courier that could not see the boost maps could not advise on boost.

    The generic catalog omits an owner-locked table because it may not *write*
    it; the answering side has the opposite need, and each table says who owns
    it so the distinction survives the trip.
    """
    payload = bundle(base_tune, provenance=PROVENANCE)
    owned = [t for t in payload["tables"] if t["owner"]]
    assert owned, "no domain-owned table reached the bundle"
    assert all(t["values"] for t in owned)


def test_a_two_dimensional_table_carries_its_decoded_axes(base_tune):
    payload = bundle(base_tune, provenance=PROVENANCE)
    grids = [t for t in payload["tables"] if t["ndim"] == 2 and t["x_axis"]]
    assert grids, "no gridded table carried an x axis"
    axis = grids[0]["x_axis"]
    assert axis["values"] and axis["label"]
    assert len(axis["values"]) == grids[0]["shape"][1]


def test_an_untouched_session_with_no_logs_still_exports_a_valid_bundle(base_tune):
    payload = bundle(base_tune, provenance=PROVENANCE)
    assert payload["journal"] == []
    assert payload["logs"] is None and payload["log_names"] == []
    assert json.loads(render(payload))["tables"]
    assert summary_of(payload)["journal_entries"] == 0


# --------------------------------------------------------------------------- #
# the logs
# --------------------------------------------------------------------------- #
def test_a_picked_log_travels_as_the_batterys_own_findings(base_tune, r04_log_dir: Path):
    """The battery is the library's one description of a log; a bundle reuses it."""
    paths = sorted(r04_log_dir.glob("simostools-*.csv"))[:1]
    if not paths:
        pytest.skip("no CSV in the R04 log folder")
    section = logs_section(paths, names={str(paths[0]): "R04 pull"})
    payload = bundle(base_tune, provenance=PROVENANCE, logs=section,
                     log_names=["R04 pull"])

    logs = payload["logs"]
    assert logs["logs"][0]["name"] == "R04 pull"
    assert logs["battery"] and "skipped" in logs
    assert summary_of(payload)["logs"] == ["R04 pull"]
    # a directory name on the exporting device is not a fact about the logs
    assert "folder" not in logs
    # plot series are deliberately absent: nothing off-device is going to draw them
    assert "plots" not in logs
    # but the channel list is present: a reader must be able to tell an absent
    # channel from one no finding happened to mention.
    assert "rpm" in logs["logs"][0]["channels"]


def test_without_a_calibration_the_cal_aware_checks_skip_and_say_why(
    base_tune, r04_log_dir: Path
):
    """The opt-out path: no cal, so those checks and coverage SKIP, loudly.

    A session recovered from its journal has no source snapshot, and a session
    can declare outright that its logs came from some other bin. Either way the
    answer is the same and it is stated, not silent.
    """
    paths = sorted(r04_log_dir.glob("simostools-*.csv"))[:1]
    if not paths:
        pytest.skip("no CSV in the R04 log folder")
    section = logs_section(paths)
    assert section["cal_resolved"] is False
    assert any(s["check_id"] for s in section["skipped"])
    # coverage is present either way — a promised section that sometimes
    # vanishes is worse than one that is sometimes all skips.
    assert section["coverage"]["results"] == []
    assert section["coverage"]["skipped"]
    assert "cal_notes" not in section


def test_the_logs_are_read_against_the_sessions_imported_bin(
    base_tune, r04_log_dir: Path
):
    """Back-test findings 1 and 2: `cal=None` cost three of four usable answers.

    The source space is the bin this session opened, before any edit made here —
    the calibration a log picked into the session was driven on. Passing it is
    what makes the two ``needs_cal`` checks and the coverage maps computable, and
    the note that travels with it names which bin, so a reader whose logs came
    from elsewhere can discount those findings.
    """
    paths = sorted(r04_log_dir.glob("simostools-*.csv"))[:1]
    if not paths:
        pytest.skip("no CSV in the R04 log folder")
    cal, notes = source_calibration(base_tune)
    assert cal is not None and notes
    section = logs_section(paths, cal=cal, cal_notes=notes)

    assert section["cal_resolved"] is True
    assert "boost_cal" in section["ran"] and "boost_p0234" in section["ran"]
    assert section["cal_notes"] == list(notes)
    # At least one coverage table resolved, and the ones that did not say why.
    assert section["coverage"]["results"]
    for entry in section["coverage"]["results"]:
        assert entry["x_breakpoints"] and entry["shape"]
    for skip in section["coverage"]["skipped"]:
        assert skip["reason"]


def test_the_source_calibration_is_the_imported_bin_not_the_working_buffer(base_tune):
    """An edit made in this session must not reach a log's calibration."""
    before = float(base_tune.values(GRID)[0][0])
    apply_op(base_tune, GRID, EditOp.SET, selection=Selection.cells([(0, 0)]),
             value=before + 0.2, intent="move the ceiling after the log was recorded")
    cal, _ = source_calibration(base_tune)
    assert cal is not None
    assert float(cal.get(GRID_SYMBOL).values[0][0]) == pytest.approx(before)


def test_a_shortfall_finding_reaches_the_bundle(base_tune, r04_log_dir: Path):
    """The check the back-test found missing has to be in a bundle's findings."""
    paths = sorted(r04_log_dir.glob("simostools-*.csv"))[:1]
    if not paths:
        pytest.skip("no CSV in the R04 log folder")
    section = logs_section(paths)
    assert "boost_shortfall" in section["ran"]
    assert any(f["check_id"] == "boost_shortfall" for f in section["findings"])


# --------------------------------------------------------------------------- #
# determinism (D7)
# --------------------------------------------------------------------------- #
def test_exporting_the_same_session_state_twice_is_byte_identical(base_tune, tmp_path):
    apply_op(base_tune, GRID, EditOp.SET, selection=Selection.cells([(0, 0)]),
             value=float(base_tune.values(GRID)[0][0]) + 0.02, intent="an edit")

    first = write_bundle(bundle(base_tune, provenance=PROVENANCE), tmp_path / "a.json")
    second = write_bundle(bundle(base_tune, provenance=PROVENANCE), tmp_path / "b.json")

    assert first.sha256 == second.sha256
    assert first.path.read_bytes() == second.path.read_bytes()
    assert first.bytes_written == second.bytes_written


def test_an_edit_changes_the_bundle_so_the_determinism_is_not_vacuous(base_tune, tmp_path):
    before = write_bundle(bundle(base_tune, provenance=PROVENANCE), tmp_path / "a.json")
    apply_op(base_tune, GRID, EditOp.SET, selection=Selection.cells([(0, 0)]),
             value=float(base_tune.values(GRID)[0][0]) + 0.02, intent="an edit")
    after = write_bundle(bundle(base_tune, provenance=PROVENANCE), tmp_path / "b.json")
    assert before.sha256 != after.sha256


def test_no_timestamp_or_device_path_reaches_the_payload(base_tune):
    """Both would break D7, and neither is a fact about the calibration."""
    text = render(bundle(base_tune, provenance={
        **PROVENANCE,
        "bin_path": str(STOCK_BIN),
        "xdf_path": str(XDF),
    }))
    assert str(STOCK_BIN) not in text and str(XDF) not in text
    assert "timestamp" not in text and "exported_at" not in text


# --------------------------------------------------------------------------- #
# provenance — which car, which structure, which addresses
# --------------------------------------------------------------------------- #
def test_the_bundle_names_the_profile_its_structure_and_its_address_convention(base_tune):
    prov = bundle(base_tune, provenance=PROVENANCE)["provenance"]
    assert prov["profile"] == "SC8S50"
    assert prov["structure"]["name"] == SC8S50.structure.name
    assert prov["structure"]["cal_file_offset"] == SC8S50.structure.cal_file_offset
    assert prov["xdf_addresses_from_cal"] is False
    assert "whole bin" in prov["address_note"]
    assert prov["spaces"] == ["base"]


def test_another_profiles_bundle_differs_in_exactly_those_fields(base_tune, a05_tune):
    """An address means different bytes depending on the answer to "which car".

    `SC8S50.V1.0.xdf` numbers from the start of the whole 4 MB bin;
    `SCGa05_cal.xdf` numbers from the start of the extracted CAL block. A bundle
    that did not say which convention it was written in would invite a reply
    that is confidently wrong about where it writes.
    """
    s50 = bundle(base_tune, provenance=PROVENANCE)["provenance"]
    a05 = bundle(a05_tune, provenance={**PROVENANCE, "profile": "SCGA05"})["provenance"]

    assert a05["profile"] == "SCGA05" != s50["profile"]
    assert a05["structure"]["name"] != s50["structure"]["name"]
    assert a05["structure"]["cal_file_offset"] != s50["structure"]["cal_file_offset"]
    assert a05["xdf_addresses_from_cal"] is True
    assert "CAL block" in a05["address_note"]


def test_the_provenance_echoes_the_three_fields_a_reply_is_matched_by(base_tune):
    prov = bundle(base_tune, provenance=PROVENANCE)["provenance"]
    payload = bundle(base_tune, provenance=PROVENANCE)
    for field in payload["reply"]["provenance_to_echo"]:
        assert prov[field] == PROVENANCE[field]


def test_a_recovered_session_says_so(base_tune):
    """The one case with no stock ghost anywhere — worth stating, not hiding."""
    prov = bundle(base_tune, provenance={**PROVENANCE, "recovered": True})["provenance"]
    assert prov["recovered"] is True


# --------------------------------------------------------------------------- #
# the switch-patch space
# --------------------------------------------------------------------------- #
def test_the_patch_space_travels_and_the_provenance_names_both_profiles(patched_tune):
    payload = bundle(patched_tune, provenance={**PROVENANCE, "has_switch_patch": True})
    spaces = {t["space"] for t in payload["tables"]}
    assert PATCH_SPACE in spaces and "base" in spaces

    prov = payload["provenance"]
    assert prov["spaces"] == sorted(patched_tune.spaces)
    assert prov["has_switch_patch"] is True
    assert prov["profiles"][PATCH_SPACE] == SWITCH_PATCH_2933.name
    assert prov["profiles"]["base"] == "SC8S50"
    # the patch profile adds tables to the base space's structure; the bundle
    # still states one CAL layout, the base one
    assert prov["structure"]["name"] == SC8S50.structure.name


def test_the_brief_describes_the_patch_added_tables_too(patched_tune, base_tune):
    patched = bundle(patched_tune, provenance=PROVENANCE)["safety_brief"]
    base = bundle(base_tune, provenance=PROVENANCE)["safety_brief"]
    assert len(patched) > len(base)


# --------------------------------------------------------------------------- #
# what must never travel
# --------------------------------------------------------------------------- #
def test_no_bin_or_xdf_byte_sequence_appears_anywhere_in_the_bundle(base_tune, tmp_path):
    """The hashes travel; the bytes do not.

    Sampled at several offsets rather than at one, and in chunks long enough
    that a match could not be coincidence. The XDF matters as much as the bin
    here and is the easier mistake: it is *text*, so a careless implementation
    that embedded a slab of it would produce a bundle that still looked fine.
    """
    written = write_bundle(bundle(base_tune, provenance=PROVENANCE), tmp_path / "b.json")
    data = written.path.read_bytes()

    for source in (STOCK_BIN, XDF):
        raw = source.read_bytes()
        for fraction in (0.1, 0.3, 0.5, 0.7, 0.9):
            start = int(len(raw) * fraction)
            chunk = raw[start:start + 256]
            assert chunk not in data, f"{source.name} bytes at {start:#x} reached the bundle"


def test_the_bundle_carries_the_file_hashes_it_refuses_to_carry_the_files_of(base_tune):
    prov = bundle(base_tune, provenance=PROVENANCE)["provenance"]
    assert prov["bin_sha256"] == PROVENANCE["bin_sha256"]
    assert prov["xdf_sha256"] == PROVENANCE["xdf_sha256"]


def test_the_authored_half_of_the_brief_is_embedded_verbatim(base_tune):
    """U3's contract, asserted where the embedding actually happens."""
    text = bundle(base_tune, provenance=PROVENANCE)["safety_brief"]
    assert authored_half().rstrip() in text


# --------------------------------------------------------------------------- #
# writing
# --------------------------------------------------------------------------- #
def test_writing_reports_the_hash_size_and_summary_of_what_landed(base_tune, tmp_path):
    payload = bundle(base_tune, provenance=PROVENANCE)
    written = write_bundle(payload, tmp_path / "nested" / "bundle.json")

    data = written.path.read_bytes()
    assert written.sha256 == hashlib.sha256(data).hexdigest()
    assert written.bytes_written == len(data)
    assert written.summary["tables"] == len(payload["tables"])
    assert written.summary["profile"] == "SC8S50"
    assert json.loads(data.decode("utf-8"))["bundle_version"] == BUNDLE_VERSION


def test_a_payload_that_cannot_be_rendered_leaves_no_file(base_tune, tmp_path):
    """Rendered in full before the file is opened — a half-written bundle is worse
    than none, because it looks like a file."""
    payload = bundle(base_tune, provenance=PROVENANCE)
    payload["notes"] = object()
    dest = tmp_path / "bundle.json"
    with pytest.raises(TypeError):
        write_bundle(payload, dest)
    assert not dest.exists()


def test_the_reply_contract_travels_with_the_bundle_that_prompts_it(base_tune):
    """A reader has this file, not the repository the guide lives in."""
    reply = bundle(base_tune, provenance=PROVENANCE)["reply"]
    assert reply["schema_version"] == 1
    assert reply["provenance_to_echo"] == ["profile", "bin_sha256", "xdf_sha256"]
    assert any("Evidence is mandatory" in rule for rule in reply["rules"])
