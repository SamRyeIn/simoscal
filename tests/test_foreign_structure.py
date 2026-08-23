"""Foreign file structure (A05) — what the library must refuse, and how loudly.

Every other test in this suite runs against one calibration: `5G0906259L_0002`,
software `SC8S50`. That is the narrowest possible basis for a tool whose users
are drawn from the wider Simos18 population, so this module pins the behaviour
against a **genuinely different structure**: box code `3CN906259B`, software
`SCGA05` — a different vehicle *and* a different software line.

What is pinned here is the **decision and its loudness**, not the wording:

* F1 the XDF's declared ``BASEOFFSET`` is honoured, including ``0``;
* F2 preflight refuses the bin as inspect-only and never writable;
* F3 a partially-matching profile still fails, and **shape-mismatched tables are
  refused for shape reasons** — the safety claim this module exists for;
* F4 both switch-patch XDFs fail loudly, by their two distinct mechanisms;
  a refused bin makes no patch claim at all; and an unreadable patch XDF is an
  error rather than an absent patch (the CR-20260815-02 class), pinned with a
  passing control so the assertion cannot survive a permanently broken detector;
* F5 the checksum layer cannot locate either checksum, and the two reasons are
  different in kind — CAL_CRC is one address constant away, ECM3 is not.

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
from pathlib import Path

import pytest

from simoscal import checksum as ck
from simoscal.calfile import CalFile
from simoscal.preflight import INSPECT_ONLY, preflight
from simoscal.tune.profile import ProfileResolutionError, resolve
from simoscal.tune.profiles import SC8S50
from simoscal.tune.profiles.switchpatch_2933 import SWITCH_PATCH_2933
from simoscal.xdf import XdfParseError, parse_xdf

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

#: The CAL base address this bin uses. SC8S50 uses 0xA0800000; discovering that
#: this is the *only* difference in the CAL CRC layer is F5's whole point.
A05_CAL_BASE_ADDRESS = 0x80800000

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
    """The A05 bin opened against its own base XDF. Read-only; never edited."""
    _require(A05_BIN, A05_XDF)
    return CalFile.open(str(A05_XDF), str(A05_BIN))


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
# F2 — preflight refuses it: inspect-only, never writable
# --------------------------------------------------------------------------- #
def test_f2_a05_bin_is_inspect_only_and_never_writable() -> None:
    _require(A05_BIN, A05_XDF)
    v = preflight(A05_BIN, A05_XDF)
    assert v.status == INSPECT_ONLY
    assert v.ok_to_edit is False
    assert v.writable is False
    assert v.profile_matched is False
    assert v.profile_name is None
    assert v.bin_sha256 == _sha(A05_BIN)


def test_f2_preflight_reports_the_profile_misses_that_caused_the_refusal() -> None:
    """A refusal has to say *what* did not match, or it cannot be acted on."""
    _require(A05_BIN, A05_XDF)
    v = preflight(A05_BIN, A05_XDF)
    misses = (v.advanced or {}).get("profile_misses")
    assert misses, "an INSPECT_ONLY verdict must carry the profile misses"
    assert any("IP_IGA_BAS_IVVT_VVL_PORT_L" in m for m in misses)


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
        CalFile.open(str(A05_PATCH_SIMOSCAL), str(A05_BIN))
    assert "uniqueid" in str(excinfo.value)


def test_f4b_bintoolz_patch_parses_but_resolves_nothing() -> None:
    """``SWITCH_PATCH_2933`` is keyed to hardcoded S50 addresses.

    It is not merely untested against another box code — it is non-portable by
    construction, and every one of its names must miss here. If some subset ever
    started resolving against a foreign patch, that would mean addresses were
    being matched by luck, which is far worse than failing.
    """
    _require(A05_PATCH_BINTOOLZ, A05_BIN)
    patch_cal = CalFile.open(str(A05_PATCH_BINTOOLZ), str(A05_BIN))
    with pytest.raises(ProfileResolutionError) as excinfo:
        resolve(SWITCH_PATCH_2933, patch_cal, xdf_label=str(A05_PATCH_BINTOOLZ))
    assert len(excinfo.value.misses) == len(SWITCH_PATCH_2933.names())


def test_f4c_an_inspect_only_verdict_makes_no_switch_patch_claim() -> None:
    """On a refused bin, preflight must not claim anything about a switch patch.

    Detection never runs: the profile miss short-circuits first. That is the
    right behaviour — there is nothing to patch on a bin this tool will not
    edit — but it must be *silence*, not a confident "no patch present".
    """
    _require(A05_BIN, A05_XDF, A05_PATCH_SIMOSCAL)
    v = preflight(A05_BIN, A05_XDF, switch_patch_xdf=A05_PATCH_SIMOSCAL)
    assert v.status == INSPECT_ONLY
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
def test_f5_neither_checksum_can_be_verified_on_a05() -> None:
    _require(A05_BIN)
    reports = {r.name: r for r in ck.verify(A05_BIN.read_bytes())}
    assert set(reports) == {"CAL_CRC", "ECM3"}
    for report in reports.values():
        assert report.can_verify is False
        assert report.is_stale is False, "cannot-verify must never be reported as stale"
        assert report.detail, "a cannot-verify report must say why"


def test_f5_cal_crc_is_one_address_constant_away(monkeypatch) -> None:
    """The CAL CRC layer is portable; only ``CAL_BASE_ADDRESS`` is S50-specific.

    A05's CAL CRC header is structurally identical to S50's — same location,
    same two-area count, same field layout. Only the CAL base address in the
    address space differs. With that one constant corrected the CRC verifies
    **clean**, which is simultaneously:

    * evidence this fixture bin is stock and self-consistent, and
    * the measurement that says porting this layer is a profile field, not a
      rewrite.

    This test does **not** endorse patching the global — it documents the value
    so a future port has a target, and so a regression in the area walk or in
    ``crc32_simos`` fails here.
    """
    _require(A05_BIN)
    data = A05_BIN.read_bytes()
    monkeypatch.setattr(ck, "CAL_BASE_ADDRESS", A05_CAL_BASE_ADDRESS)
    report, returned = ck.verify_cal_crc(data)
    assert report.can_verify is True
    assert report.is_stale is False, "the A05 fixture bin must be self-consistent"
    assert report.stored == report.computed
    assert returned is data, "a non-correcting verify must not copy or alter the bin"


def test_f5_ecm3_is_a_real_structural_difference() -> None:
    """ECM3 is *not* one constant away — its header is somewhere else entirely.

    At the SC8S50 ECM3 header offset, A05 holds an ASCII part-number string
    rather than a header, so the area count reads as garbage. Pinned so nobody
    concludes from F5's CAL_CRC result that the whole checksum layer ports by
    adjusting constants.
    """
    _require(A05_BIN)
    data = A05_BIN.read_bytes()
    header = ck.CAL_FILE_OFFSET + ck.ECM3_HEADER
    probe = data[header : header + 16]
    assert probe.isascii() and probe.decode("ascii").isprintable(), (
        "expected printable ASCII at the S50 ECM3 header offset on A05; "
        f"got {probe.hex(' ')}"
    )
    areas, note = ck._ecm3_areas(data)
    assert areas is None
    assert "out of range" in note


def test_f5_s50_still_verifies_clean_side_by_side() -> None:
    """The contrast, and a guard: none of the above may weaken the primary path."""
    _require(S50_BIN)
    reports = {r.name: r for r in ck.verify(S50_BIN.read_bytes())}
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
