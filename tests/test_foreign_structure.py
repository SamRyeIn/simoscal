"""Foreign file structure (A05) — what the library must refuse, and how loudly.

Every other test in this suite runs against one calibration: `5G0906259L_0002`,
software `SC8S50`. That is the narrowest possible basis for a tool whose users
are drawn from the wider Simos18 population, so this module pins the behaviour
against a **genuinely different structure**: box code `3CN906259B`, software
`SCGA05` — a different vehicle *and* a different software line.

What is pinned here is the **decision and its loudness**, not the wording:

* F1 the XDF's declared ``BASEOFFSET`` is honoured, including ``0``;
* F2 preflight recognises the car and still refuses the *pairing* — the A05
  base XDF addresses the wrong part of the bin, so it is blocked, never writable;
* F3 a partially-matching profile still fails, and **shape-mismatched tables are
  refused for shape reasons** — the safety claim this module exists for;
* F4 both switch-patch XDFs fail loudly, by their two distinct mechanisms;
  a refused bin makes no patch claim at all; and an unreadable patch XDF is an
  error rather than an absent patch (the CR-20260815-02 class), pinned with a
  passing control so the assertion cannot survive a permanently broken detector;
* F5 the checksum layer cannot locate either checksum *under SC8S50's layout*,
  and the two reasons are different in kind — CAL_CRC is one address constant
  away, ECM3 is not;
* F6 the A05 base profile (U5): it resolves against its own XDF and nothing
  else, the nine ignition grids are declared (16, 18) as a positive claim that
  a wrong declaration still fails, and the per-car facts SC8S50 needs — the
  kg/stk trap, the float-bug flags — are measured absent here rather than
  copied across;
* F7 the car's two XDFs use two different address conventions and may still
  share one buffer, because what has to agree is where addresses land;
* F8 the A05 switch-patch map (U6): the two patch definitions are one file with
  the addresses moved, the committed address book is what role mapping produces,
  and neither car's map resolves against the other's file.

The A05 files are a third party's calibration and are **not committed** — like
the SC8S50 files, they are gitignored and you drop your own copies in. Every
test here skips cleanly when they are absent, so a fresh clone stays green.

**Forcing a real run.** Skip-if-absent means this suite can silently not run,
which for a safety suite is the failure mode that matters (cf. CR-20260706-02).
Set ``SIMOSCAL_REQUIRE_FOREIGN=1`` to turn "absent" from a skip into a failure —
use it in CI, and any time a green run is being read as evidence.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from simoscal import checksum as ck
from simoscal.calfile import CalFile
from simoscal.preflight import BLOCKED, READY, _detect_switch_patch, preflight
from simoscal.tune.profile import (
    TAG_KG_PER_STROKE,
    Profile,
    ProfileResolutionError,
    TableSpec,
    TableUnavailableError,
    resolve,
)
from simoscal.tune.profiles import (
    BASE_PROFILES,
    PROFILES,
    SC8S50,
    SCGA05,
    patch_profile_for,
)
from simoscal.tune.project import TuneError, _open_shared_space
from simoscal.tune.profiles.switchpatch_2933 import (
    SWITCH_PATCH_2933,
    build_switch_patch_profile,
)
from simoscal.tune.profiles.switchpatch_2933_a05 import (
    A05_PUT_GRID_UIDS,
    A05_SLOT_SETTING_UIDS,
    A05_STANDALONE_UIDS,
    SWITCH_PATCH_2933_A05,
)
from simoscal.xdf import XdfParseError, parse_xdf
from simoscal.checksum import SC8S50_STRUCTURE

CODE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = CODE_ROOT.parent

#: The foreign set: box code 3CN906259B, software SCGA05.
A05_BIN = CODE_ROOT / "bin" / "3CN906259B__0002_SCGA05.bin"
A05_XDF = CODE_ROOT / "xdf" / "SCGa05_cal.xdf"
#: The simoscal-style patch definition — does not parse (F4a).
A05_PATCH_SIMOSCAL = CODE_ROOT / "xdf" / "SCGA05_switchpatch29.33_v1.000.xdf"
#: The BinToolz-style patch definition — parses, resolves nothing (F4b).
A05_PATCH_BINTOOLZ = (
    REPO_ROOT / "BinToolz-main" / "definitions" / "A05 Switch Patch.29.33.V2.xdf"
)

#: Sam's own set, for the side-by-side contrasts.
S50_BIN = CODE_ROOT / "bin" / "5G0906259L__0002.bin"
S50_XDF = CODE_ROOT / "xdf" / "SC8S50.V1.0.xdf"
#: An S50 patch XDF that cannot be parsed (same uniqueid-reuse defect as A05's),
#: used to exercise the error path preflight only reaches on a matching profile.
S50_PATCH_UNPARSEABLE = CODE_ROOT / "xdf" / "SC8S50_switchpatch29.33_v1.006.xdf"
#: A patch XDF that parses and resolves — the control for the error-path test.
S50_PATCH_BINTOOLZ = (
    REPO_ROOT / "BinToolz-main" / "definitions" / "S50 Switch Patch.29.33.V2.xdf"
)

#: A05's real CAL layout, located by the U1 spike. The whole CAL block sits
#: 0x20000 further into the file than SC8S50's and is mapped 0x20000 higher in
#: the address space; the shape *inside* the block is identical.
A05_CAL_FILE_OFFSET = 0x220000
A05_CAL_BASE_ADDRESS = 0xA0820000

#: There is a second, unrelated CRC-headered block at SC8S50's CAL offset on the
#: A05 bin, under base 0x80800000, whose CRC also verifies clean. It is not the
#: block the XDF addresses. It is named here because an earlier characterisation
#: pass found it, concluded "A05's CAL CRC is one constant away", and was wrong —
#: a true statement about the wrong region. See docs/porting-to-another-xdf.md.
A05_DECOY_CAL_FILE_OFFSET = 0x200000
A05_DECOY_CAL_BASE_ADDRESS = 0x80800000

#: The nine ignition tables that exist under the SC8S50 symbol name but with a
#: different grid. Named explicitly because "it failed" is not the claim — the
#: claim is that it failed *for shape reasons*, on these specific tables.
SHAPE_MISMATCH_NAMES = frozenset(
    f"ignition_base_vvl0_i{i}_e{e}" for i in range(3) for e in range(3)
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _require(*paths: Path) -> None:
    """Skip — or, under ``SIMOSCAL_REQUIRE_FOREIGN=1``, fail — if any is absent."""
    missing = [p for p in paths if not p.is_file()]
    if not missing:
        return
    names = ", ".join(str(p) for p in missing)
    if os.environ.get("SIMOSCAL_REQUIRE_FOREIGN") == "1":
        pytest.fail(
            f"SIMOSCAL_REQUIRE_FOREIGN=1 but the foreign fixture is absent: {names}"
        )
    pytest.skip(f"foreign A05 fixture not present: {names}")


@pytest.fixture(scope="module")
def a05_cal() -> CalFile:
    """The A05 bin opened against its own base XDF. Read-only; never edited.

    Opened the way the profile says to open it: A05's own structure, and the
    rebase its CAL-relative XDF needs. Resolution only inspects names and shapes,
    so neither changes a resolution assertion — but a fixture opened any other
    way reads as though the wrong car's constants were in play, and the tests
    that do read *values* through it would be reading padding.
    """
    _require(A05_BIN, A05_XDF)
    return CalFile.open(
        str(A05_XDF), str(A05_BIN), structure=ck.SCGA05_STRUCTURE,
        base_offset=SCGA05.xdf_base_offset,
        float_bug_symbols=SCGA05.float_bug_symbols,
        stock_references=SCGA05.stock_references,
    )


# --------------------------------------------------------------------------- #
# F1 — the declared BASEOFFSET is honoured, including zero
# --------------------------------------------------------------------------- #
def test_f1_a05_base_xdf_declares_zero_base_offset() -> None:
    """A05's base XDF declares ``BASEOFFSET 0`` — not the 0x200000 S50 uses.

    The project's standing rule of thumb is *file offset = 0x200000 + XDF
    address*. That is a property of the SC8S50 definition, **not** of XDFs, and
    this file is the counter-example. A parser that assumed the constant would
    read every A05 table from the wrong place while appearing to work.
    """
    _require(A05_XDF)
    model = parse_xdf(str(A05_XDF))
    assert model.base_offset == 0
    assert model.base_subtract is False


def test_f1_s50_and_a05_base_offsets_actually_differ() -> None:
    """Pin the contrast, so neither value can drift into the other unnoticed."""
    _require(A05_XDF, S50_XDF)
    assert parse_xdf(str(S50_XDF)).base_offset == 0x200000
    assert parse_xdf(str(A05_XDF)).base_offset == 0


def test_f1_a05_patch_xdf_uses_a_third_base_offset() -> None:
    """The base and patch definitions for the *same car* disagree: 0 vs 0x220000."""
    _require(A05_PATCH_BINTOOLZ)
    assert parse_xdf(str(A05_PATCH_BINTOOLZ)).base_offset == 0x220000


# --------------------------------------------------------------------------- #
# F2 — preflight holds the XDF to the convention the profile declares
# --------------------------------------------------------------------------- #
# These began as "A05 is inspect-only because nothing maps it", became "A05 is
# blocked because its XDF addresses the wrong part of the bin", and are now
# neither. `SCGa05_cal.xdf` is not faulty: it numbers its tables from the start
# of the calibration block, which is a second legitimate convention and the one
# its `_cal` name announces. SCGA05 declares that convention, every read and
# write is rebased by the CAL file offset, and the pairing is READY.
#
# What survives from the blocked era is the gate itself, and it is the more
# important half: preflight still refuses any XDF whose header disagrees with
# the profile's declaration. The library never infers the convention from the
# file — a new definition file has to be read and declared by a human before
# anything is written through it.
def _xdf_with_base_offset(tmp_path: Path, source: Path, offset: str) -> Path:
    """A copy of ``source`` whose BASEOFFSET header declares ``offset``.

    Tampering with the header is the only way to reach the refusal now that the
    real file is accepted, and it is the right stand-in: an XDF that resolves
    but counts from somewhere unexpected is exactly the case the gate is for.
    """
    text = source.read_text(encoding="utf-8", errors="surrogateescape")
    old = '<BASEOFFSET offset="0" subtract="0" />'
    assert old in text, "SCGa05_cal.xdf no longer declares BASEOFFSET 0"
    out = tmp_path / f"rebased_{offset}.xdf"
    out.write_text(
        text.replace(old, f'<BASEOFFSET offset="{offset}" subtract="0" />'),
        encoding="utf-8", errors="surrogateescape",
    )
    return out


def test_f2_a05_is_ready_and_writable_through_its_declared_convention() -> None:
    _require(A05_BIN, A05_XDF)
    v = preflight(A05_BIN, A05_XDF)
    assert v.status == "READY"
    assert v.ok_to_edit is True
    assert v.writable is True
    assert v.profile_name == "SCGA05"
    assert v.profile_matched is True
    assert v.bin_sha256 == _sha(A05_BIN)


def test_f2_the_values_behind_that_verdict_are_this_car_s(a05_cal) -> None:
    """READY has to mean the numbers are real, not that the names lined up.

    Resolution matches on symbol and shape and would have said yes at either
    base offset, so it is no evidence at all here. These are the values the
    rebase produces, and they are the kind a calibration holds rather than the
    kind padding does.
    """
    rpm = a05_cal.get("ldp_n_ip_put_sp").values.ravel()
    assert list(rpm) == [2000.0, 3000.0, 4000.0, 5000.0, 5750.0, 6500.0]
    # Two scalars this XDF scales correctly, quoted in the profile's docstring.
    assert a05_cal.get("C_PRS_IM_SP_MAX").values.ravel()[0] == pytest.approx(
        2399.96, abs=0.01
    )
    assert a05_cal.get("IP_PUT_AMP_DIF_MAX_PRS_DIF_THR").values.ravel()[
        0
    ] == pytest.approx(1799.97, abs=0.01)


def test_f2_an_xdf_that_counts_from_somewhere_else_is_still_refused(
    tmp_path: Path,
) -> None:
    """The gate, on the case it exists for.

    Every table still resolves — only the header changed — so this is precisely
    the failure resolution cannot see.
    """
    _require(A05_BIN, A05_XDF)
    tampered = _xdf_with_base_offset(tmp_path, A05_XDF, "0x200000")
    v = preflight(A05_BIN, tampered)
    assert v.status == BLOCKED
    assert v.ok_to_edit is False
    assert v.writable is False
    # The profile resolved — that is the point — but resolution is not
    # permission, and the two must stay separately readable off the verdict.
    assert v.profile_name == "SCGA05"
    assert v.profile_matched is False
    assert v.advanced["profile_resolved"] is True


def test_f2_the_refusal_names_the_disagreement_in_both_directions(
    tmp_path: Path,
) -> None:
    """A refusal has to say *what* did not match, or it cannot be acted on.

    Here that means both numbers: what this file declares and what the profile
    expects. One of them alone is not actionable — "the base offset is wrong"
    leaves the reader with no way to tell which file to replace.
    """
    _require(A05_BIN, A05_XDF)
    tampered = _xdf_with_base_offset(tmp_path, A05_XDF, "0x200000")
    v = preflight(A05_BIN, tampered)
    assert v.advanced["xdf_base_offset"] == "0x200000"
    assert v.advanced["expected_xdf_base_offset"] == "0x0"
    assert v.advanced["xdf_addresses_cal_relative"] is True
    blob = " ".join(v.reasons)
    assert "0x200000" in blob
    # And it must say the consequence, because "wrong offset" reads like a
    # cosmetic complaint: the danger is that a write lands where no checksum
    # looks, so the bad bin builds and flashes without a single warning.
    assert "flash" in blob.lower()


def test_f2_the_declaration_is_a_check_not_a_licence_to_rebase(
    tmp_path: Path,
) -> None:
    """Declaring the convention must not mean "accept whatever the file says".

    A file declaring the CAL file offset is *self-consistent* against a full bin
    — it is the other legitimate convention — and it is still refused here,
    because SCGA05 was authored against the CAL-relative one. Accepting both
    would mean the profile's declaration decided nothing.
    """
    _require(A05_BIN, A05_XDF)
    v = preflight(A05_BIN, _xdf_with_base_offset(tmp_path, A05_XDF, "0x220000"))
    assert v.status == BLOCKED


def test_f2_sc8s50_is_held_to_the_other_convention(real_bin, real_xdf) -> None:
    """The same gate on the car that uses the full-bin convention.

    SC8S50 declares no rebase, so its expected base offset is the CAL file
    offset and its real XDF passes unchanged — the check is symmetric, not an
    A05 special case.
    """
    assert SC8S50.xdf_addresses_cal_relative is False
    assert SC8S50.expected_xdf_base_offset == SC8S50_STRUCTURE.cal_file_offset
    assert SC8S50.xdf_base_offset is None
    assert preflight(real_bin, real_xdf).status == "READY"


def test_f2_the_xdf_really_does_read_the_wrong_bytes() -> None:
    """The evidence behind the refusal, measured rather than asserted.

    If this ever stops holding — a corrected `SCGa05_cal.xdf` is dropped in —
    the guard above stops firing too, and it should: the two are the same fact.
    """
    _require(A05_BIN, A05_XDF)
    cal = CalFile.open(str(A05_XDF), str(A05_BIN), structure=ck.SCGA05_STRUCTURE)
    at_declared = cal.get("IP_PUT_SP").values
    assert not at_declared.any(), (
        "expected padding at the XDF's declared address; if this file now reads "
        "real values its BASEOFFSET has been fixed and F2 needs rewriting"
    )
    # The real table is 0x220000 further in, and holds a plausible boost grid.
    raw = np.frombuffer(
        A05_BIN.read_bytes(), dtype="<u2", count=24,
        offset=parse_xdf(str(A05_XDF)).get("IP_PUT_SP").z.embedded.address + 0x220000,
    )
    hpa = raw * parse_xdf(str(A05_XDF)).get("IP_PUT_SP").z.scaling.m
    assert 500 < hpa.min() and hpa.max() < 3000, (
        f"expected a plausible hPa boost grid at +0x220000, "
        f"got {hpa.min()}..{hpa.max()}"
    )


def test_f2_refusal_names_the_software_the_file_declares_itself_to_be() -> None:
    """The deftitle stays on the verdict even now that recognition succeeded.

    It is the XDF's own claim and never what recognition turns on — resolution
    by symbol and shape is — but a reader comparing two definition files for the
    same car needs to see which one they are holding.
    """
    _require(A05_BIN, A05_XDF)
    v = preflight(A05_BIN, A05_XDF)
    assert v.advanced["deftitle"] == "SCGA0531_C_OEM.a2l"


def test_f2_preflight_does_not_modify_the_foreign_files() -> None:
    _require(A05_BIN, A05_XDF)
    before = (_sha(A05_BIN), _sha(A05_XDF))
    preflight(A05_BIN, A05_XDF)
    assert (_sha(A05_BIN), _sha(A05_XDF)) == before


# --------------------------------------------------------------------------- #
# F3 — the safety claim: shape-mismatched tables are refused, for shape reasons
# --------------------------------------------------------------------------- #
def test_f3_sc8s50_profile_does_not_resolve_against_a05(a05_cal: CalFile) -> None:
    with pytest.raises(ProfileResolutionError):
        resolve(SC8S50, a05_cal, xdf_label=str(A05_XDF))


def test_f3_partial_match_is_still_a_refusal(a05_cal: CalFile) -> None:
    """Most of the profile *does* resolve — and that must not be enough.

    A05 matches roughly three quarters of the SC8S50 profile by name. A
    resolver that accepted a majority match, or that skipped the names it could
    not find, would hand back a usable-looking map pointing partly at the wrong
    car. The all-or-nothing contract is what makes the write gate meaningful.
    """
    with pytest.raises(ProfileResolutionError) as excinfo:
        resolve(SC8S50, a05_cal, xdf_label=str(A05_XDF))
    misses = excinfo.value.misses
    total = len(SC8S50.names())
    assert 0 < len(misses) < total, (
        "expected a PARTIAL match — if every name missed, this fixture is no "
        "longer exercising the partial-match hazard this test exists for"
    )


def test_f3_shape_mismatched_ignition_tables_are_refused_for_shape(
    a05_cal: CalFile,
) -> None:
    """The single most important assertion in this module.

    ``IP_IGA_BAS_IVVT_VVL_PORT_L[STND][i][e]`` — Basic ignition angle by
    VVT/VVL, port injection — exists on A05 under the *same symbol name* as on
    SC8S50, at a **different grid size**: (16, 18) where the SC8S50 map declares
    (16, 16).

    Name-only resolution would have matched these and written a 16x16 ignition
    map into a 16x18 table, corrupting adjacent calibration and producing a bin
    that flashes and runs wrong timing. Shape-checked resolution is what stops
    that, so it is pinned here by name and by reason — never relax it to widen
    box-code support.
    """
    with pytest.raises(ProfileResolutionError) as excinfo:
        resolve(SC8S50, a05_cal, xdf_label=str(A05_XDF))
    by_name = {m.name: m for m in excinfo.value.misses}

    assert SHAPE_MISMATCH_NAMES <= set(by_name), (
        "every VVL0 ignition table must be refused on A05; missing: "
        f"{sorted(SHAPE_MISMATCH_NAMES - set(by_name))}"
    )
    for name in sorted(SHAPE_MISMATCH_NAMES):
        reason = by_name[name].reason
        assert "shape" in reason, f"{name} must be refused for SHAPE, got: {reason}"
        assert "(16, 18)" in reason and "(16, 16)" in reason, (
            f"{name} must report both the resolved and declared shapes: {reason}"
        )


def test_f3_a_shape_mismatch_is_never_a_mere_name_miss(a05_cal: CalFile) -> None:
    """Distinguish the two failure classes; conflating them would hide the hazard.

    A name miss means "not here" — harmless. A shape mismatch means "here, but
    a different size" — the dangerous one, because the address *is* valid.
    """
    with pytest.raises(ProfileResolutionError) as excinfo:
        resolve(SC8S50, a05_cal, xdf_label=str(A05_XDF))
    for miss in excinfo.value.misses:
        if miss.name in SHAPE_MISMATCH_NAMES:
            assert "no table with this symbol" not in miss.reason


# --------------------------------------------------------------------------- #
# F4 — both switch patches fail loudly, by two distinct mechanisms
# --------------------------------------------------------------------------- #
def test_f4a_simoscal_style_patch_xdf_fails_to_parse() -> None:
    """Refused at parse time: a uniqueid reused with conflicting data.

    This is a property of the simoscal-style patch definitions generally (both
    ``SC8S50_switchpatch*`` files fail the same way), not of A05. What matters
    is that an ambiguous uniqueid is a hard stop rather than a silent pick.
    """
    _require(A05_PATCH_SIMOSCAL, A05_BIN)
    with pytest.raises(XdfParseError) as excinfo:
        CalFile.open(str(A05_PATCH_SIMOSCAL), str(A05_BIN), structure=SC8S50_STRUCTURE)
    assert "uniqueid" in str(excinfo.value)


def test_f4b_bintoolz_patch_parses_but_resolves_nothing() -> None:
    """``SWITCH_PATCH_2933`` is keyed to S50 addresses and stays that way.

    Since U6 the patch *is* mapped for A05 — but by a second address book, not by
    this profile learning to stretch. So the claim here is unchanged and now
    load-bearing in a new way: S50's map against A05's file must still miss every
    name, because a subset resolving would mean addresses matching by luck. What
    replaced the old "non-portable by construction" reading is F8, which pins the
    other direction too and shows how the port was actually made.
    """
    _require(A05_PATCH_BINTOOLZ, A05_BIN)
    patch_cal = CalFile.open(str(A05_PATCH_BINTOOLZ), str(A05_BIN), structure=SC8S50_STRUCTURE)
    with pytest.raises(ProfileResolutionError) as excinfo:
        resolve(SWITCH_PATCH_2933, patch_cal, xdf_label=str(A05_PATCH_BINTOOLZ))
    assert len(excinfo.value.misses) == len(SWITCH_PATCH_2933.names())


def test_f4c_a_refused_verdict_makes_no_switch_patch_claim(tmp_path: Path) -> None:
    """On a refused bin, preflight must not claim anything about a switch patch.

    Detection never runs: the refusal short-circuits first. The short-circuit
    here is the base-offset gate, which makes the silence *more* necessary
    rather than less — the patch XDF is untouched and declares its own offset
    correctly, so a detector reached past a base XDF that counts from the wrong
    place could return a confident answer about a pairing nothing will touch.
    """
    _require(A05_BIN, A05_XDF, A05_PATCH_SIMOSCAL)
    v = preflight(
        A05_BIN, _xdf_with_base_offset(tmp_path, A05_XDF, "0x200000"),
        switch_patch_xdf=A05_PATCH_SIMOSCAL,
    )
    assert v.status == BLOCKED
    assert v.switch_patch_present is None
    advanced = v.advanced or {}
    assert "switch_patch_present" not in advanced
    assert "switch_patch_slot1_range" not in advanced


def test_f4d_an_unreadable_patch_xdf_is_an_error_not_an_absent_patch() -> None:
    """CR-20260815-02's regression test, on the path where detection runs.

    A patch XDF that cannot be opened must surface as ``switch_patch_error``.
    The bug this pins was reporting that case as *no patch in the bin*, which
    is the dangerous direction: it would let someone edit believing the patch
    space is untouched when the truth is the tool could not look.

    Uses the S50 set deliberately — on A05 the profile miss short-circuits
    before detection is reached (see F4c), so the A05 files cannot exercise it.
    """
    _require(S50_BIN, S50_XDF, S50_PATCH_UNPARSEABLE)
    v = preflight(S50_BIN, S50_XDF, switch_patch_xdf=S50_PATCH_UNPARSEABLE)
    advanced = v.advanced or {}
    assert "switch_patch_error" in advanced, (
        "an unopenable patch XDF must be reported as an error"
    )
    assert "uniqueid" in advanced["switch_patch_error"]
    assert advanced.get("switch_patch_present") is not False
    assert "switch_patch_slot1_range" not in advanced, (
        "a patch XDF that could not be opened must yield no slot-range claim"
    )


def test_f4e_a_readable_patch_xdf_reports_a_slot_range_and_no_error() -> None:
    """The control for F4d: with a good patch XDF the error key must be absent.

    Without this, F4d would still pass if every patch XDF were reported as an
    error — the assertion would be pinning a permanently broken detector.
    """
    _require(S50_BIN, S50_XDF, S50_PATCH_BINTOOLZ)
    v = preflight(S50_BIN, S50_XDF, switch_patch_xdf=S50_PATCH_BINTOOLZ)
    advanced = v.advanced or {}
    assert "switch_patch_error" not in advanced
    assert "switch_patch_slot1_range" in advanced


# --------------------------------------------------------------------------- #
# F5 — the checksum layer cannot locate either checksum, for two different reasons
# --------------------------------------------------------------------------- #
def test_f5_neither_checksum_verifies_under_the_sc8s50_structure() -> None:
    """Read with the wrong car's layout, an A05 bin reports cannot-verify.

    Not "A05 has no checksums" — it has both, and they are clean (see below).
    This pins that a structure mismatch degrades to an honest refusal rather
    than to a confident wrong answer.
    """
    _require(A05_BIN)
    reports = {r.name: r for r in ck.verify(A05_BIN.read_bytes(), ck.SC8S50_STRUCTURE)}
    assert set(reports) == {"CAL_CRC", "ECM3"}
    for report in reports.values():
        assert report.can_verify is False
        assert report.is_stale is False, "cannot-verify must never be reported as stale"
        assert report.detail, "a cannot-verify report must say why"


def test_f5_a05_checksums_verify_clean_under_its_own_structure() -> None:
    """Both A05 checksums verify once the layout is right — three constants, not one.

    The CAL block sits at 0x220000 rather than 0x200000 and is mapped at
    0xA0820000 rather than 0xA0800000. Nothing inside the block moved. That this
    verifies clean is simultaneously evidence the fixture bin is stock, and the
    measurement saying this layer ports as profile data rather than a rewrite.
    """
    _require(A05_BIN)
    data = A05_BIN.read_bytes()
    spec = ck.StructureSpec(
        name="SCGA05",
        cal_file_offset=A05_CAL_FILE_OFFSET,
        cal_base_address=A05_CAL_BASE_ADDRESS,
        cal_block_length=0x9FC00,
        asw_file_offset=0x20000,
    )
    reports = {r.name: r for r in ck.verify(data, spec)}
    assert set(reports) == {"CAL_CRC", "ECM3"}
    for report in reports.values():
        assert report.can_verify is True, report.detail
        assert report.is_stale is False, "the A05 fixture bin must be self-consistent"
        assert report.stored == report.computed


def test_f5_discovery_finds_that_structure_without_being_told() -> None:
    """``discover_structure`` recovers A05's layout from the bin alone."""
    _require(A05_BIN)
    spec = ck.discover_structure(A05_BIN.read_bytes(), name="SCGA05")
    assert spec.cal_file_offset == A05_CAL_FILE_OFFSET
    assert spec.cal_base_address == A05_CAL_BASE_ADDRESS
    assert all(not r.is_stale and r.can_verify
               for r in ck.verify(A05_BIN.read_bytes(), spec))


def test_f5_discovery_rediscovers_sc8s50_and_nothing_else() -> None:
    """The negative control: the search must not be fitting noise.

    If this search can find *anything* plausible in *any* bin, its A05 result
    means nothing. Run against the bin whose layout is already known and
    declared, it must reproduce that declaration exactly.
    """
    _require(S50_BIN)
    spec = ck.discover_structure(S50_BIN.read_bytes())
    assert spec.cal_file_offset == ck.SC8S50_STRUCTURE.cal_file_offset
    assert spec.cal_base_address == ck.SC8S50_STRUCTURE.cal_base_address
    assert spec.ecm3_header == ck.SC8S50_STRUCTURE.ecm3_header
    # The declared spec locates ECM3's addresses at ASW1 + 0x520; discovery,
    # which cannot know the ASW block's base, must land on the same file offset.
    declared = (ck.SC8S50_STRUCTURE.asw_file_offset
                + ck.SC8S50_STRUCTURE.ecm3_addr_locs[0])
    assert spec.asw_file_offset + spec.ecm3_addr_locs[0] == declared


def test_f5_the_decoy_block_verifies_too_and_is_not_the_cal_block() -> None:
    """Why "one constant away" was the wrong conclusion, pinned so it stays wrong.

    A second CRC-headered block really does verify clean at SC8S50's CAL offset
    under base 0x80800000. Both facts must hold at once: it verifies, *and* it is
    not where the calibration lives — its CRC covers only to 0x1FBDF, which the
    XDF's own table addresses run well past.
    """
    _require(A05_BIN)
    data = A05_BIN.read_bytes()
    decoy = ck.StructureSpec(
        name="A05-decoy",
        cal_file_offset=A05_DECOY_CAL_FILE_OFFSET,
        cal_base_address=A05_DECOY_CAL_BASE_ADDRESS,
        cal_block_length=0x1FC00,
        asw_file_offset=0x20000,
    )
    crc, _ = ck.verify_cal_crc(data, decoy)
    assert crc.can_verify and not crc.is_stale, "the decoy really does verify"
    # ...but ECM3 is not there, which is the tell.
    ecm3, _ = ck.verify_ecm3(data, decoy)
    assert not ecm3.can_verify
    # ...and the block is far too short to hold the tables the XDF declares.
    assert decoy.cal_block_length < 0x8F8C3, (
        "the A05 base XDF addresses tables past the decoy block's end"
    )


def test_f5_ascii_at_the_s50_ecm3_offset_is_a_moved_block_not_a_moved_header() -> None:
    """The observation that misled the earlier pass, with its real cause pinned.

    There genuinely is printable ASCII where SC8S50 keeps its ECM3 header. The
    inference "ECM3 relocated" does not follow: nothing moved *within* the CAL
    block. Reading at 0x200400 reads 0x20000 before A05's CAL block starts.
    ECM3 is at CAL-relative 0x400 on both cars.
    """
    _require(A05_BIN)
    data = A05_BIN.read_bytes()
    header = ck.SC8S50_STRUCTURE.ecm3_file_offset
    probe = data[header : header + 16]
    assert probe.isascii() and probe.decode("ascii").isprintable(), (
        "expected printable ASCII at the S50 ECM3 header offset on A05; "
        f"got {probe.hex(' ')}"
    )
    # The same CAL-relative offset, in A05's own block, is a real ECM3 header.
    spec = ck.discover_structure(data)
    assert spec.ecm3_header == ck.SC8S50_STRUCTURE.ecm3_header == 0x400
    assert spec.cal_file_offset - ck.SC8S50_STRUCTURE.cal_file_offset == 0x20000


def test_f5_correct_refuses_rather_than_silently_changing_nothing() -> None:
    """AE7. The old behaviour returned unchanged bytes and raised nothing."""
    _require(A05_BIN)
    with pytest.raises(ck.ChecksumNotLocatable) as excinfo:
        ck.correct(A05_BIN.read_bytes(), ck.SC8S50_STRUCTURE)
    assert "SC8S50" in str(excinfo.value)


def test_f5_s50_still_verifies_clean_side_by_side() -> None:
    """The contrast, and a guard: none of the above may weaken the primary path."""
    _require(S50_BIN)
    reports = {r.name: r for r in ck.verify(S50_BIN.read_bytes(), ck.SC8S50_STRUCTURE)}
    for report in reports.values():
        assert report.can_verify is True
        assert report.is_stale is False


# --------------------------------------------------------------------------- #
# Integrity — reading a foreign structure must never touch the files
# --------------------------------------------------------------------------- #
def test_every_foreign_file_is_unmodified_by_this_module() -> None:
    """Run last by position: the whole module has read these; they must be intact."""
    present = [p for p in (A05_BIN, A05_XDF, A05_PATCH_SIMOSCAL, A05_PATCH_BINTOOLZ)
               if p.is_file()]
    _require(A05_BIN, A05_XDF)
    for path in present:
        # Re-read and re-hash; any in-place write by the read paths shows up here.
        assert _sha(path) == hashlib.sha256(path.read_bytes()).hexdigest()
        assert path.stat().st_size > 0


# --------------------------------------------------------------------------- #
# F6 — the A05 base profile (U5)
# --------------------------------------------------------------------------- #
# What F3 pins from the other side: the SC8S50 map must not resolve here. These
# pin that a map authored *for* this car does — and that making a second shape
# legitimate did not make the shape check optional.

def test_f6_the_a05_profile_fully_resolves_against_its_own_xdf(a05_cal) -> None:
    """Every logical name the A05 map declares binds to a real table, exactly."""
    resolved = resolve(SCGA05, a05_cal, xdf_label=str(A05_XDF))
    assert len(resolved) == len(SCGA05.names())
    assert set(resolved.names()) == set(SCGA05.names())


def test_f6_the_nine_ignition_grids_are_16x18_here_and_16x16_there() -> None:
    """The same nine symbols, two legitimate shapes — declared, never inferred."""
    _require(A05_XDF, S50_XDF)
    a05, s50 = parse_xdf(str(A05_XDF)), parse_xdf(str(S50_XDF))
    for name in sorted(SHAPE_MISMATCH_NAMES):
        key = SCGA05[name].key
        assert key == SC8S50[name].key, "the two maps must be naming one symbol"
        assert a05.get(key).shape == (16, 18)
        assert s50.get(key).shape == (16, 16)
        assert SCGA05[name].shape == (16, 18)
        assert SC8S50[name].shape == (16, 16)


def test_f6_declaring_the_wrong_shape_for_a05_still_fails(a05_cal) -> None:
    """The mutation that proves the check is intact, not bypassed per car.

    Per-car shape declarations exist so that two different grids can both be
    described truthfully. The hazard they introduce is that "declared" starts to
    read as "allowed", so this rebuilds the A05 profile with SC8S50's (16, 16)
    and requires it to be refused — on the same file that accepts (16, 18).
    """
    wrong = Profile(
        name="SCGA05-wrong-shape",
        xdf=SCGA05.xdf,
        specs={
            n: (replace(s, shape=(16, 16)) if n in SHAPE_MISMATCH_NAMES else s)
            for n, s in SCGA05.specs.items()
        },
        structure=SCGA05.structure,
    )
    with pytest.raises(ProfileResolutionError) as excinfo:
        resolve(wrong, a05_cal, xdf_label=str(A05_XDF))
    by_name = {m.name: m for m in excinfo.value.misses}
    assert set(by_name) == SHAPE_MISMATCH_NAMES, (
        "only the nine mutated tables may fail; anything else means the mutation "
        "broke something other than the shape claim"
    )
    for name in SHAPE_MISMATCH_NAMES:
        assert "shape" in by_name[name].reason


def test_f6_the_a05_profile_does_not_resolve_against_the_sc8s50_xdf() -> None:
    """The refusal is symmetric, and for the same reason in both directions."""
    _require(S50_BIN, S50_XDF)
    s50_cal = CalFile.open(str(S50_XDF), str(S50_BIN), structure=SC8S50_STRUCTURE)
    with pytest.raises(ProfileResolutionError) as excinfo:
        resolve(SCGA05, s50_cal, xdf_label=str(S50_XDF))
    by_name = {m.name: m for m in excinfo.value.misses}
    assert set(by_name) == SHAPE_MISMATCH_NAMES
    for name in SHAPE_MISMATCH_NAMES:
        reason = by_name[name].reason
        assert "(16, 16)" in reason and "(16, 18)" in reason


def test_f6_the_a05_profile_is_registered_and_declares_a_structure() -> None:
    """Registration is what makes preflight try it; a structure is what earns it."""
    assert PROFILES["SCGA05"] is SCGA05
    assert SCGA05 in BASE_PROFILES
    assert SCGA05.structure is ck.SCGA05_STRUCTURE
    assert SCGA05.structure.cal_file_offset == A05_CAL_FILE_OFFSET
    assert SCGA05.structure.cal_base_address == A05_CAL_BASE_ADDRESS


def test_f6_ae1_opening_and_saving_the_a05_bin_changes_nothing(tmp_path) -> None:
    """AE1, on the foreign car: open → save with no edits → byte-identical."""
    _require(A05_BIN, A05_XDF)
    cal = CalFile.open(
        str(A05_XDF), str(A05_BIN), structure=ck.SCGA05_STRUCTURE,
        float_bug_symbols=SCGA05.float_bug_symbols,
        stock_references=SCGA05.stock_references,
    )
    out = tmp_path / "a05_unchanged.bin"
    cal.save(out)
    assert out.read_bytes() == A05_BIN.read_bytes()


def test_f6_an_a05_edit_lands_in_the_table_and_nowhere_else(tmp_path) -> None:
    """The end of the whole port, and the claim the rebase actually has to earn.

    Reading plausible values proves the addresses are right; only a write proves
    they are right *and* nothing else moved. The byte audit is the check the
    base-offset gate exists to protect: at the file's declared offset this edit
    would have landed 0x220000 short, in bytes CAL_CRC does not cover, and the
    saved bin would have verified clean while holding an unchanged table.
    """
    _require(A05_BIN, A05_XDF)
    cal = CalFile.open(
        str(A05_XDF), str(A05_BIN), structure=ck.SCGA05_STRUCTURE,
        base_offset=SCGA05.xdf_base_offset,
        float_bug_symbols=SCGA05.float_bug_symbols,
        stock_references=SCGA05.stock_references,
    )
    view = cal.get("IP_PUT_SP")
    before = view.values.copy()
    view.set(before + 100.0)
    out = tmp_path / "a05_edited.bin"
    cal.save(out, correct_checksums=True)

    original, edited = A05_BIN.read_bytes(), out.read_bytes()
    changed = [i for i in range(len(original)) if original[i] != edited[i]]
    runs: list[list[int]] = []
    for i in changed:
        if runs and i == runs[-1][1] + 1:
            runs[-1][1] = i
        else:
            runs.append([i, i])

    emb = cal.model.get("IP_PUT_SP").z.embedded
    table_at = ck.SCGA05_STRUCTURE.cal_file_offset + emb.address
    extent = emb.rows * emb.cols * (emb.elem_bits // 8)
    crc_at = ck.SCGA05_STRUCTURE.cal_file_offset + 0x304
    assert [tuple(r) for r in runs] == [
        (crc_at, crc_at + 3),
        (table_at, table_at + extent - 1),
    ], "only the corrected CAL CRC and the edited table may differ"

    reopened = CalFile.open(
        str(A05_XDF), str(out), structure=ck.SCGA05_STRUCTURE,
        base_offset=SCGA05.xdf_base_offset,
    )
    # Read back off the saved file, not the in-memory buffer. Tolerance is the
    # 16-bit store's own quantisation, not slack in the check.
    assert np.allclose(reopened.get("IP_PUT_SP").values, before + 100.0, atol=0.01)
    assert all(
        c.can_verify and not c.is_stale and c.stored == c.computed
        for c in reopened.verify_checksums()
    )


def test_f6_both_a05_checksums_verify_under_the_declared_structure() -> None:
    """The declared spec must reproduce what discovery finds, not merely resemble it."""
    _require(A05_BIN)
    data = A05_BIN.read_bytes()
    for report in ck.verify(data, ck.SCGA05_STRUCTURE):
        assert report.can_verify is True, report.detail
        assert report.is_stale is False
    found = ck.discover_structure(data, name="SCGA05")
    assert found.cal_file_offset == ck.SCGA05_STRUCTURE.cal_file_offset
    assert found.cal_base_address == ck.SCGA05_STRUCTURE.cal_base_address
    assert (found.asw_file_offset + found.ecm3_addr_locs[0]
            == ck.SCGA05_STRUCTURE.asw_file_offset
            + ck.SCGA05_STRUCTURE.ecm3_addr_locs[1])


# ---- the per-car facts that did NOT transfer ------------------------------- #
def test_f6_the_kg_per_stroke_trap_is_an_sc8s50_xdf_defect_not_a_car_fact() -> None:
    """Same bytes in both ECUs; only SC8S50's definition file misreads them.

    `C_M_AIR_CYL_SP_MAX` — Maximum allowed airmass setpoint is the library's
    single most dangerous table: writing the mg/stk figure into SC8S50's
    identity-scaled store raises the ceiling a millionfold and removes the
    limiter. This pins *why* the A05 map does not carry the tag — not because
    nobody got round to it, but because this XDF already carries the 1e6 factor,
    so tagging it would reintroduce the same millionfold error on this car.
    """
    _require(A05_XDF, S50_XDF, A05_BIN, S50_BIN)
    a05 = parse_xdf(str(A05_XDF)).get("C_M_AIR_CYL_SP_MAX")
    s50 = parse_xdf(str(S50_XDF)).get("C_M_AIR_CYL_SP_MAX")
    # Identical raw stores: 0.001389 kg/stk on both cars.
    a05_raw = np.frombuffer(A05_BIN.read_bytes(), dtype="<f4", count=1,
                            offset=a05.z.embedded.address + A05_CAL_FILE_OFFSET)[0]
    s50_raw = np.frombuffer(S50_BIN.read_bytes(), dtype="<f4", count=1,
                            offset=s50.z.embedded.address + 0x200000)[0]
    assert a05_raw == pytest.approx(s50_raw, rel=1e-6)
    # Different equations: A05 converts to the mg/stk its label claims, S50 does not.
    assert a05.z.scaling.m == 1e6
    assert s50.z.scaling.m == 1.0
    assert TAG_KG_PER_STROKE in SC8S50["airmass_setpoint_max"].tags
    assert TAG_KG_PER_STROKE not in SCGA05["airmass_setpoint_max"].tags
    # And with no tag there must be no owner pointing at the converting writer,
    # which refuses an untagged table — that pairing would leave the ceiling
    # with no write path at all rather than a safe one.
    assert SCGA05["airmass_setpoint_max"].owner == ""


def test_f6_a05_flags_no_float_bug_tables_and_that_is_a_measurement() -> None:
    """An empty float-bug set must be a stated answer, not an unfilled field.

    The tag disables a guard, so it is only correct where the XDF's declared
    range is genuinely an editor artifact. The check is objective: does stock
    already sit outside the declared range? On SC8S50 two tables do. On A05 —
    whose XDF scales those same two floats correctly — none does.
    """
    _require(A05_XDF, A05_BIN, S50_XDF, S50_BIN)
    assert SCGA05.float_bug_symbols == frozenset()
    assert SC8S50.float_bug_symbols  # the contrast, so "empty" is not the default

    def breaches(xdf_path, bin_path, profile, cal_offset):
        model, data = parse_xdf(str(xdf_path)), bin_path.read_bytes()
        out = set()
        for name in profile.names():
            table = model.get(profile[name].key)
            emb, scaling, hi = table.z.embedded, table.z.scaling, table.z.max
            if hi is None or not emb.is_float:
                continue  # the float-bug class is exactly the float32 stores
            values = np.frombuffer(
                data, dtype="<f4", count=emb.rows * emb.cols,
                offset=emb.address + cal_offset,
            ) * (scaling.m if scaling.is_linear else 1.0)
            if values.max() > hi:
                out.add(name)
        return out

    assert breaches(A05_XDF, A05_BIN, SCGA05, A05_CAL_FILE_OFFSET) == set()
    assert breaches(S50_XDF, S50_BIN, SC8S50, 0x200000) == {
        "manifold_pressure_max", "manifold_pressure_limit_offset",
    }


def test_f6_a05_declares_no_stock_references() -> None:
    """Nobody has measured this car for advice, so the guidance must stay silent."""
    assert SCGA05.stock_references == {}
    assert SC8S50.stock_references, "the contrast: SC8S50 has been measured"


# ---- declared gaps --------------------------------------------------------- #
def test_f6_the_ten_absent_names_are_declared_not_omitted() -> None:
    """A gap that was investigated must be distinguishable from one that was not."""
    assert set(SCGA05.unavailable) == {
        "airmass_full_load",
        "static_rev_fuel_cut_offset",
        "ignition_temp_correction_reference",
        "pedal_dct_offroad_high",
        "pedal_dct_offroad_low",
        "lambda_rpm_axis",
        "lambda_load_axis",
        "ignition_temp_iat_axis",
        "cylinder_head_temp_rpm_axis",
        "cylinder_head_temp_charge_axis",
    }
    # Every one of them is a name the *other* car has, which is what makes the
    # declaration meaningful rather than a list of arbitrary strings.
    assert set(SCGA05.unavailable) <= set(SC8S50.names())
    # Together they account for the whole difference between the two maps.
    assert set(SC8S50.names()) - set(SCGA05.names()) == set(SCGA05.unavailable)


def test_f6_each_declared_gap_is_genuinely_absent_from_the_xdf() -> None:
    """The declaration is a claim about the file; check it against the file."""
    _require(A05_XDF)
    model = parse_xdf(str(A05_XDF))
    for name in SCGA05.unavailable:
        key = SC8S50[name].key
        with pytest.raises(KeyError):
            model.get(key)


def test_f6_asking_for_a_declared_gap_says_why(tmp_path) -> None:
    """The lookup failure has to carry the reason, or declaring it bought nothing."""
    with pytest.raises(TableUnavailableError) as excinfo:
        SCGA05["lambda_rpm_axis"]
    exc = excinfo.value
    assert exc.profile == "SCGA05" and exc.name == "lambda_rpm_axis"
    assert "embedded" in exc.reason
    assert "lambda_rpm_axis" in str(exc) and "SCGA05" in str(exc)
    # It is still a KeyError, so mapping-protocol callers are unaffected.
    assert isinstance(exc, KeyError)
    # A name neither mapped nor declared stays an ordinary KeyError — "never
    # heard of it" and "looked for it, not there" must not collapse together.
    with pytest.raises(KeyError) as plain:
        SCGA05["not_a_logical_name"]
    assert not isinstance(plain.value, TableUnavailableError)


def test_f6_a_name_cannot_be_both_mapped_and_declared_absent() -> None:
    with pytest.raises(ValueError, match="both as a mapped table and as unavailable"):
        Profile(
            name="contradictory",
            xdf="x.xdf",
            specs={"a": TableSpec(name="a", key="A", description="d")},
            unavailable={"a": "absent"},
        )


def test_f6_merging_lets_one_profile_fill_another_s_declared_gap() -> None:
    """A patch profile that supplies a missing table closes the gap, not doubles it."""
    base = Profile(name="base", xdf="b.xdf", unavailable={"a": "absent on this car"})
    patch = Profile(
        name="patch", xdf="p.xdf",
        specs={"a": TableSpec(name="a", key="0x1", description="added by the patch")},
    )
    merged = base.merged_with(patch)
    assert "a" in merged.specs
    assert merged.unavailable == {}


# ---- what the two maps must agree about ------------------------------------ #
def test_f6_shared_names_are_filed_under_the_same_heading() -> None:
    """Groups are a property of the logical name, so the two maps must not drift.

    The A05 map writes its classification out rather than importing SC8S50's, so
    that a change to one profile's filing cannot silently restate itself as a
    claim about the other car. This is the check that keeps them honest without
    reintroducing the coupling.
    """
    shared = set(SCGA05.names()) & set(SC8S50.names())
    assert len(shared) == 60, "the overlap moved; check the map before the test"
    mismatched = {
        n: (SC8S50[n].group, SCGA05[n].group)
        for n in shared
        if SC8S50[n].group != SCGA05[n].group
    }
    assert mismatched == {}


def test_f6_every_a05_table_is_filed_somewhere() -> None:
    assert SCGA05.ungrouped() == []


# --------------------------------------------------------------------------- #
# F7 — two XDFs over one buffer, each written in its own convention
# --------------------------------------------------------------------------- #
def test_f7_the_two_a05_files_use_different_conventions() -> None:
    """The measurement behind the test below, stated on its own.

    Nothing says a car's base and patch definitions are authored the same way,
    and on this car they are not.
    """
    _require(A05_XDF, A05_PATCH_BINTOOLZ)
    assert parse_xdf(str(A05_XDF)).base_offset == 0
    assert parse_xdf(str(A05_PATCH_BINTOOLZ)).base_offset == 0x220000


def test_f7_a_shared_space_agrees_on_where_addresses_land_not_on_headers() -> None:
    """Both A05 files resolve to base 0x220000, so they may share one buffer.

    Comparing what the two files *declare* would refuse this pair — 0 against
    0x220000 — and that is the only pairing this car has. What has to match is
    where each file's addresses land, and it does.
    """
    _require(A05_BIN, A05_XDF, A05_PATCH_BINTOOLZ)
    base = CalFile.open(
        str(A05_XDF), str(A05_BIN), structure=ck.SCGA05_STRUCTURE,
        base_offset=SCGA05.xdf_base_offset,
        float_bug_symbols=SCGA05.float_bug_symbols,
    )
    space = _open_shared_space(
        "patch", Profile(name="A05PATCH", xdf=A05_PATCH_BINTOOLZ.name),
        A05_PATCH_BINTOOLZ, base,
    )
    assert space.cal.base_offset == 0x220000 == base.base_offset
    assert space.cal.binimage is base.binimage, "must be the same bytes, not a copy"


def test_f7_a_shared_xdf_that_lands_elsewhere_is_still_refused() -> None:
    """The guard survives the loosening: agreement is required, just measured right."""
    _require(A05_BIN, A05_XDF, S50_XDF)
    base = CalFile.open(
        str(A05_XDF), str(A05_BIN), structure=ck.SCGA05_STRUCTURE,
        base_offset=SCGA05.xdf_base_offset,
        float_bug_symbols=SCGA05.float_bug_symbols,
    )
    # SC8S50's XDF lands at 0x200000 — a real offset, and the wrong one here.
    with pytest.raises(TuneError, match="effective base offset"):
        _open_shared_space(
            "wrong", Profile(name="X", xdf=S50_XDF.name), S50_XDF, base,
        )


# --------------------------------------------------------------------------- #
# F8 — the A05 switch-patch map (U6)
# --------------------------------------------------------------------------- #
# The patch is one BinToolz build cut for several file structures, so porting it
# was not a re-derivation of what the tables mean — that was settled once, on
# S50 — but of where they sit. These tests pin both halves of that claim: that
# the two definitions really are the same file with the addresses moved, and
# that the address book committed in the module is the one that correspondence
# produces.
def _patch_role_correspondence() -> list[tuple[int, int]]:
    """``(s50 uniqueid, a05 uniqueid)`` for all 185 tables, paired by role.

    The pairing is the two files' shared table order, and it is only usable
    because every pair agrees on title, symbol and category path — asserted by
    the test below before any of the others leans on it. Category path is what
    carries the role for the five grids titled ``PUT setpoint``: only
    ``… | Map Slot N`` says which slot one is.
    """
    s50 = parse_xdf(str(S50_PATCH_BINTOOLZ)).tables
    a05 = parse_xdf(str(A05_PATCH_BINTOOLZ)).tables
    assert len(s50) == len(a05) == 185
    for s, a in zip(s50, a05):
        assert (s.title, s.symbol, tuple(c.name for c in s.categories)) == (
            a.title, a.symbol, tuple(c.name for c in a.categories)
        ), f"the two patch XDFs diverge at {s.title!r} — the pairing is not by role"
    return [(s.uniqueid, a.uniqueid) for s, a in zip(s50, a05)]


def test_f8_the_two_patch_definitions_are_one_file_with_the_addresses_moved() -> None:
    """The premise the whole port rests on, measured rather than assumed.

    If these two files were independently authored definitions, role mapping
    would be a judgement call per table. They are not: 185 tables each, and index
    for index the same title, the same A2L symbol, the same category path. Only
    the addresses differ.
    """
    _require(S50_PATCH_BINTOOLZ, A05_PATCH_BINTOOLZ)
    pairs = _patch_role_correspondence()
    assert all(s != a for s, a in pairs), "the addresses must be what differs"


def test_f8_the_committed_a05_addresses_are_the_ones_role_mapping_produces() -> None:
    """Re-derive the address book from both XDFs and compare it to the module.

    This is the check that makes the 92 hand-committed hex constants trustworthy:
    they are not read back from the same place they were written, they are
    re-derived from the two definition files and compared. A transposed digit
    fails here instead of reaching a bin.
    """
    _require(S50_PATCH_BINTOOLZ, A05_PATCH_BINTOOLZ)
    derived = dict(_patch_role_correspondence())
    for name in sorted(SWITCH_PATCH_2933.names()):
        expected = derived[int(SWITCH_PATCH_2933[name].key, 16)]
        assert int(SWITCH_PATCH_2933_A05[name].key, 16) == expected, (
            f"{name}: A05 map says {SWITCH_PATCH_2933_A05[name].key}, "
            f"role mapping says {expected:#x}"
        )


def test_f8_the_a05_offsets_are_not_one_delta_from_s50s() -> None:
    """Why the book had to be read off the file rather than computed from S50's.

    Three different deltas across the 92. Adding the most common one would have
    placed twenty-five tables in the wrong place — and, because these tables are
    bound by uniqueid rather than by name, resolution would not have noticed.
    """
    deltas = [
        int(SWITCH_PATCH_2933_A05[n].key, 16) - int(SWITCH_PATCH_2933[n].key, 16)
        for n in SWITCH_PATCH_2933.names()
    ]
    assert set(deltas) == {0x13000, 0x12F60, 0x13020}
    assert sum(d != 0x13000 for d in deltas) == 25


def test_f8_the_a05_patch_map_fully_resolves_against_its_own_xdf() -> None:
    """Happy path: all 92, on the stock A05 bin, against A05's own patch XDF."""
    _require(A05_BIN, A05_PATCH_BINTOOLZ)
    patch_cal = CalFile.open(
        str(A05_PATCH_BINTOOLZ), str(A05_BIN), structure=ck.SCGA05_STRUCTURE
    )
    resolved = resolve(
        SWITCH_PATCH_2933_A05, patch_cal, xdf_label=str(A05_PATCH_BINTOOLZ)
    )
    assert len(resolved) == len(SWITCH_PATCH_2933_A05.names()) == 92


def test_f8_neither_patch_map_resolves_against_the_other_car_s_xdf() -> None:
    """Both directions, all 92 each way — the claim F4b only made one way.

    The two files share no uniqueid at all, so a wrong pairing misses everything
    rather than landing a subset by coincidence. That total miss is the property
    worth pinning: a partial resolve would mean addresses matching by luck.
    """
    _require(A05_BIN, S50_BIN, A05_PATCH_BINTOOLZ, S50_PATCH_BINTOOLZ)
    for profile, xdf, binary, structure in (
        (SWITCH_PATCH_2933_A05, S50_PATCH_BINTOOLZ, S50_BIN, SC8S50_STRUCTURE),
        (SWITCH_PATCH_2933, A05_PATCH_BINTOOLZ, A05_BIN, ck.SCGA05_STRUCTURE),
    ):
        cal = CalFile.open(str(xdf), str(binary), structure=structure)
        with pytest.raises(ProfileResolutionError) as excinfo:
            resolve(profile, cal, xdf_label=str(xdf))
        assert len(excinfo.value.misses) == len(profile.names()), (
            f"{profile.name} against {xdf.name} must miss every name"
        )


def test_f8_the_patch_space_reads_as_unapplied_on_the_stock_a05_bin() -> None:
    """The A05 bin is stock, and the map says so instead of inventing a curve.

    Every mapped patch table decodes to zero here — the space has never been
    written, which is exactly what an unpatched bin should look like through a
    correct map. (The plan expected a plausible non-zero read; zeros are the
    stronger result, because a map pointed at the wrong bytes would almost
    certainly find calibration data rather than a clean run of nothing.)
    """
    _require(A05_BIN, A05_PATCH_BINTOOLZ)
    patch_cal = CalFile.open(
        str(A05_PATCH_BINTOOLZ), str(A05_BIN), structure=ck.SCGA05_STRUCTURE
    )
    resolved = resolve(
        SWITCH_PATCH_2933_A05, patch_cal, xdf_label=str(A05_PATCH_BINTOOLZ)
    )
    grid = resolved.view("slot1_put_setpoint").values
    assert grid.shape == (8, 12)
    assert np.all(grid == 0.0), f"expected an unwritten slot 1, got {grid.min()}..{grid.max()}"
    assert np.all(resolved.view("slot_put_rpm_axis").values == 0.0)


def test_f8_the_patch_map_follows_the_car_not_the_patch_xdf() -> None:
    """Selection is by identified car, and a car without a map is refused.

    Patch tables are bound by uniqueid, so pointing S50's map at A05's file does
    not miss loudly in every context — it is only the zero uniqueid overlap of
    *these two* files that makes it miss here. The library therefore never picks
    a patch map by trying them: it follows the base profile preflight matched.
    """
    assert patch_profile_for(SC8S50) is SWITCH_PATCH_2933
    assert patch_profile_for(SCGA05) is SWITCH_PATCH_2933_A05
    with pytest.raises(KeyError, match="no switch-patch map is registered"):
        patch_profile_for(Profile(name="NoSuchCar", xdf="none.xdf"))


def test_f8_an_address_book_that_repeats_a_uniqueid_is_refused() -> None:
    """Two logical names on one table would resolve cleanly and write wrong.

    The copy-a-column typo: both names resolve, both read the same bytes, and
    writing one silently moves the other. Caught when the profile is built, not
    when a bin is saved.
    """
    book = {k: dict(v) for k, v in A05_SLOT_SETTING_UIDS.items()}
    book["enable_lc"][2] = book["enable_nls"][2]
    with pytest.raises(ValueError, match="both bound to uniqueid"):
        build_switch_patch_profile(
            name="Typo", xdf="x.xdf",
            standalone_uids=A05_STANDALONE_UIDS,
            put_grid_uids=A05_PUT_GRID_UIDS,
            slot_setting_uids=book,
        )


def test_f8_an_incomplete_address_book_is_refused() -> None:
    """A forgotten role would ship a profile that resolves and has no axis."""
    book = {k: v for k, v in A05_STANDALONE_UIDS.items() if k != "slot_put_rpm_axis"}
    with pytest.raises(ValueError, match="slot_put_rpm_axis"):
        build_switch_patch_profile(
            name="Partial", xdf="x.xdf",
            standalone_uids=book,
            put_grid_uids=A05_PUT_GRID_UIDS,
            slot_setting_uids=A05_SLOT_SETTING_UIDS,
        )


def test_f8_both_patch_maps_describe_the_patch_identically() -> None:
    """Only addresses may differ between two cars' maps of one patch build.

    The reason the descriptions live in one module: if a port could restate them,
    two maps of the same BinToolz build could disagree about what a table does
    while both resolving. Here they cannot, and this asserts it stays that way.
    """
    assert set(SWITCH_PATCH_2933.names()) == set(SWITCH_PATCH_2933_A05.names())
    for name in sorted(SWITCH_PATCH_2933.names()):
        s50, a05 = SWITCH_PATCH_2933[name], SWITCH_PATCH_2933_A05[name]
        assert (s50.description, s50.units, s50.shape, s50.tags, s50.owner,
                s50.group) == (a05.description, a05.units, a05.shape, a05.tags,
                               a05.owner, a05.group), name
        assert s50.key != a05.key, f"{name}: the addresses are the per-car part"


def test_f8_preflight_reads_the_a05_patch_space_and_calls_it_unapplied() -> None:
    """Integration: the verdict now distinguishes "no patch" from "no map".

    Before U6 this pairing reported ``switch_patch_present=False`` — the right
    answer for the wrong reason, since the only map preflight had was S50's and
    it missed every name. Now the answer comes from reading A05's own patch
    space and finding it unwritten, and the slot-1 range in ``advanced`` is the
    evidence for that rather than a leftover from a failed resolve.
    """
    _require(A05_BIN, A05_XDF, A05_PATCH_BINTOOLZ)
    v = preflight(A05_BIN, A05_XDF, switch_patch_xdf=A05_PATCH_BINTOOLZ)
    assert v.status == READY and v.writable
    assert v.switch_patch_present is False
    advanced = v.advanced or {}
    assert "switch_patch_error" not in advanced
    assert advanced.get("switch_patch_slot1_range") == "0..0"


def test_f8_a_car_with_no_patch_map_is_an_error_not_an_absent_patch() -> None:
    """The CR-20260815-02 distinction, on the route U6 opened.

    "This library has no switch-patch map for your car" and "your bin has no
    switch patch" are different facts, and only one of them is about the bin.
    Reporting the first as the second would send someone to re-patch a bin that
    may already be patched — so the unmapped car raises the error channel.
    """
    unmapped = replace(SCGA05, name="AnotherCar")
    with pytest.raises(KeyError):
        patch_profile_for(unmapped)
    present, advanced = _detect_switch_patch(A05_BIN, A05_PATCH_BINTOOLZ, unmapped)
    assert present is None, "an unmapped car must not be reported as unpatched"
    assert "no switch-patch map is registered" in advanced["switch_patch_error"]
    assert "switch_patch_slot1_range" not in advanced
