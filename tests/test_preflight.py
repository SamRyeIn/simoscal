"""Compatibility preflight (V2) — the gate that refuses an unusable bin/XDF.

The real SC8S50 XDF and bins are Sam's own and gitignored, so tests that need
them ``skip`` (never fail) when absent, per the repo-wide convention. The
malformed-input cases build their fixtures by truncating/tampering a copy of the
real bin in a tmp dir, so they also depend on the real bin being present.

What is pinned here is the *decision*, not the wording: a stale-but-correctable
checksum is reportable (still editable), while an unrecognised layout, a
truncated file, a CAL-only image, an unparseable XDF, or a corrupt checksum
blocks — and nothing the preflight does ever modifies the source file.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from simoscal.preflight import (
    BLOCKED,
    INSPECT_ONLY,
    READY,
    READY_STALE_CHECKSUM,
    AmbiguousProfileError,
    preflight,
)
from simoscal.tune.profiles import BASE_PROFILES, PATCH_PROFILES, PROFILES, SC8S50

CODE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = CODE_ROOT.parent

STOCK_BIN = CODE_ROOT / "bin" / "5G0906259L__0002.bin"
XDF = CODE_ROOT / "xdf" / "SC8S50.V1.0.xdf"
SWITCH_XDF = REPO_ROOT / "BinToolz-main" / "definitions" / "S50 Switch Patch.29.33.V2.xdf"
PATCHED_BIN = (
    REPO_ROOT / "Tunes" / "TuningBasicsGuide" / "BinToolz-patched"
    / "CB_HSL_SP2933_5G0906259L_0002_BasicsGuide_R04.bin"
)

_have_real = STOCK_BIN.is_file() and XDF.is_file()
requires_real = pytest.mark.skipif(not _have_real, reason="real SC8S50 bin/XDF absent")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@requires_real
def test_stock_bin_is_ready_with_valid_checksums() -> None:
    v = preflight(STOCK_BIN, XDF)
    assert v.status == READY
    assert v.ok_to_edit is True
    assert v.profile_matched is True
    assert v.writable is True
    assert v.profile_name == "SC8S50"
    assert all(c.can_verify and not c.is_stale for c in v.checksums)
    assert v.bin_sha256 == _sha(STOCK_BIN)


@requires_real
def test_preflight_never_modifies_the_source() -> None:
    before = _sha(STOCK_BIN)
    preflight(STOCK_BIN, XDF)
    assert _sha(STOCK_BIN) == before, "preflight must not touch the source bin"


@requires_real
def test_body_edit_makes_checksum_stale_but_correctable_still_editable(tmp_path) -> None:
    """A CAL-body change leaves CAL_CRC stale-correctable — reportable, not blocking."""
    buf = bytearray(STOCK_BIN.read_bytes())
    buf[0x250000] ^= 0xFF  # flip a byte inside the CAL block
    edited = tmp_path / "body_edited.bin"
    edited.write_bytes(buf)

    v = preflight(edited, XDF)
    assert v.status == READY_STALE_CHECKSUM
    assert v.ok_to_edit is True, "a correctable stale checksum must not block editing"
    cal_crc = next(c for c in v.checksums if c.name == "CAL_CRC")
    assert cal_crc.is_stale and cal_crc.correctable


@requires_real
def test_truncated_bin_is_blocked(tmp_path) -> None:
    trunc = tmp_path / "trunc.bin"
    trunc.write_bytes(STOCK_BIN.read_bytes()[:0x200000])
    v = preflight(trunc, XDF)
    assert v.status == BLOCKED
    assert v.ok_to_edit is False
    assert "truncated" in v.summary.lower() or "too small" in v.summary.lower()


@requires_real
def test_missing_bin_is_blocked(tmp_path) -> None:
    v = preflight(tmp_path / "nope.bin", XDF)
    assert v.status == BLOCKED
    assert v.ok_to_edit is False


@requires_real
def test_unparseable_xdf_is_blocked(tmp_path) -> None:
    garbage = tmp_path / "garbage.xdf"
    garbage.write_text("not xml at all <<<")
    v = preflight(STOCK_BIN, garbage)
    assert v.status == BLOCKED
    assert v.ok_to_edit is False
    assert "xdf" in v.summary.lower()


@pytest.mark.skipif(not SWITCH_XDF.is_file() or not _have_real,
                    reason="switch-patch XDF or real bin absent")
def test_non_sc8s50_xdf_is_inspect_only() -> None:
    """A valid XDF that is not the SC8S50 layout is readable, never editable."""
    v = preflight(STOCK_BIN, SWITCH_XDF)
    assert v.status == INSPECT_ONLY
    assert v.ok_to_edit is False
    assert v.profile_matched is False
    assert v.writable is False
    assert v.advanced.get("profile_misses"), "misses should be reported for a human"


@pytest.mark.skipif(not PATCHED_BIN.is_file() or not SWITCH_XDF.is_file(),
                    reason="patched bin or switch-patch XDF absent")
def test_patched_bin_detects_switch_patch_present() -> None:
    v = preflight(PATCHED_BIN, XDF, switch_patch_xdf=SWITCH_XDF)
    assert v.ok_to_edit is True
    assert v.status in (READY, READY_STALE_CHECKSUM)
    assert v.switch_patch_present is True


@requires_real
def test_switch_patch_not_checked_without_patch_xdf() -> None:
    """Omitting the patch XDF reports 'not checked' (None), never a guess."""
    v = preflight(STOCK_BIN, XDF)
    assert v.switch_patch_present is None


@requires_real
def test_no_state_carries_between_calls(tmp_path) -> None:
    """A rejected file must not taint the next call on a good file."""
    trunc = tmp_path / "trunc.bin"
    trunc.write_bytes(STOCK_BIN.read_bytes()[:0x100000])
    bad = preflight(trunc, XDF)
    assert bad.status == BLOCKED

    good = preflight(STOCK_BIN, XDF)
    assert good.status == READY
    assert good.ok_to_edit is True
    # The good verdict's provenance is its own, not the rejected file's.
    assert good.bin_path == str(STOCK_BIN)
    assert good.bin_size == STOCK_BIN.stat().st_size


@requires_real
def test_verdict_bool_is_ok_to_edit() -> None:
    assert bool(preflight(STOCK_BIN, XDF)) is True


# --------------------------------------------------------------------------- #
# U4 — the profile registry replaces the hardcoded SC8S50 check
# --------------------------------------------------------------------------- #
def test_base_profiles_are_the_ones_that_declare_a_structure() -> None:
    """Registry membership is derived, so a shipped profile cannot go unregistered.

    A profile with no ``structure`` only adds tables to another profile's space
    and could never identify a bin on its own, so the switch patch is excluded —
    by the rule, not by being left off a hand-written list.
    """
    assert SC8S50 in BASE_PROFILES
    assert [p.name for p in BASE_PROFILES] == [
        p.name for p in PROFILES.values() if p.structure is not None
    ]
    unregistered = [p.name for p in PROFILES.values() if p not in BASE_PROFILES]
    # Exactly the switch-patch maps, one per car — named by the registry that
    # says which car each belongs to, so adding a car cannot make this stale.
    assert set(unregistered) == {p.name for p in PATCH_PROFILES.values()}
    assert all(p.structure is None for p in PROFILES.values()
               if p.name in unregistered)


@requires_real
def test_matching_profile_is_named_and_is_what_makes_it_writable() -> None:
    """`writable` follows from a profile matching, not from a hardcoded name."""
    v = preflight(STOCK_BIN, XDF)
    assert v.profile_name == "SC8S50"
    assert v.profile_matched is True and v.writable is True
    assert v.advanced["profile"] == "SC8S50"
    # The XDF's self-declared title is carried as evidence, never as the basis
    # for the match — it is reported on a success too, not only on a refusal.
    assert v.advanced["deftitle"] == "SC8S50.a2l"


@requires_real
def test_resolution_order_does_not_change_the_outcome(monkeypatch) -> None:
    """Exactly one match means order is irrelevant — both orders, same verdict.

    The decoy is a real profile whose declared shapes cannot resolve here, so the
    loop genuinely has two candidates to distinguish rather than one.
    """
    from dataclasses import replace

    decoy = replace(SC8S50, name="Decoy", specs={
        n: replace(spec, shape=(spec.shape[0] + 1, spec.shape[1] + 1))
        for n, spec in list(SC8S50.specs.items())[:3]
    # Three specs cannot satisfy a table set, and a profile whose sets name
    # tables it does not map is rejected at construction — so the decoy
    # declares none rather than inheriting SC8S50's.
    }, table_sets={})

    verdicts = []
    for order in ((SC8S50, decoy), (decoy, SC8S50)):
        monkeypatch.setattr("simoscal.tune.profiles.BASE_PROFILES", order)
        verdicts.append(preflight(STOCK_BIN, XDF))

    assert [v.status for v in verdicts] == [READY, READY]
    assert {v.profile_name for v in verdicts} == {"SC8S50"}
    assert all(v.writable for v in verdicts)


@requires_real
def test_two_matching_profiles_is_a_loud_failure_not_a_first_match_win(
    monkeypatch,
) -> None:
    """Ambiguity raises rather than silently picking one car's safety rules.

    Editing under the wrong car's rules is the substitution preflight exists to
    prevent, and no choice of file would fix a registry that ships two maps for
    one calibration — so this is an error, not a verdict.
    """
    from dataclasses import replace

    twin = replace(SC8S50, name="SC8S50Twin")
    monkeypatch.setattr("simoscal.tune.profiles.BASE_PROFILES", (SC8S50, twin))

    with pytest.raises(AmbiguousProfileError) as excinfo:
        preflight(STOCK_BIN, XDF)
    message = str(excinfo.value)
    assert "SC8S50" in message and "SC8S50Twin" in message


@pytest.mark.skipif(not SWITCH_XDF.is_file() or not _have_real,
                    reason="switch-patch XDF or real bin absent")
def test_refusal_reads_cleanly_when_the_xdf_declares_no_title(tmp_path) -> None:
    """A missing deftitle changes the sentence, never leaves a hole in it.

    The title is the file's own claim and may simply be absent; the refusal has
    to stay a readable sentence rather than quoting an empty string or a
    parenthetical placeholder at the user.
    """
    import re

    stripped = tmp_path / "no_title.xdf"
    stripped.write_text(
        re.sub(r"<deftitle>.*?</deftitle>", "", SWITCH_XDF.read_text(), count=1)
    )

    v = preflight(STOCK_BIN, stripped)
    assert v.status == INSPECT_ONLY
    assert v.advanced["deftitle"] == ""
    assert "declares no title" in v.summary
    assert "identifies itself as" not in v.summary


@requires_real
def test_no_registered_profile_at_all_is_inspect_only(monkeypatch) -> None:
    """An empty registry recognises nothing — and says so without crashing."""
    monkeypatch.setattr("simoscal.tune.profiles.BASE_PROFILES", ())
    v = preflight(STOCK_BIN, XDF)
    assert v.status == INSPECT_ONLY
    assert v.ok_to_edit is False and v.writable is False
    assert v.profile_name is None
    assert v.advanced["profiles_tried"] == []
    # Nothing was tried, so there is no near miss to report — and the verdict
    # says that by omission rather than by inventing an empty miss list.
    assert "profile_misses" not in v.advanced
    assert "SC8S50.a2l" in v.summary
