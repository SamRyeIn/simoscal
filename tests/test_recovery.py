"""Session recovery persistence (V4).

Proves the acceptance criteria: serialize → restore reproduces the same live
session and its pending edits byte-exactly; a save/load round-trip survives a
simulated process kill; undo/redo restore prior state; and the source bin is
never modified and is re-verified on restore.

The real SC8S50 files are gitignored, so tests skip (never fail) when absent.
The patched cases additionally need the switch-patch XDF + a patched bin; they
exercise the raw/non-linear slot tables, which are exactly the ones a
re-encode-from-journal restore would corrupt — so byte-exactness there is the
load-bearing check.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from simoscal.tune import SC8S50, Tune
from simoscal.tune.domains.switchpatch import PATCH_SPACE
from simoscal.tune.profiles.switchpatch_2933 import SWITCH_PATCH_2933
from simoscal.tune.recovery import (
    RecoveryError,
    SessionHistory,
    load_session,
    restore_session,
    save_session,
    serialize_session,
)

CODE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = CODE_ROOT.parent

STOCK_BIN = CODE_ROOT / "bin" / "5G0906259L__0002.bin"
XDF = CODE_ROOT / "xdf" / "SC8S50.V1.0.xdf"
SWITCH_XDF = REPO_ROOT / "BinToolz-main" / "definitions" / "S50 Switch Patch.29.33.V2.xdf"
PATCHED_BIN = (
    REPO_ROOT / "Tunes" / "TuningBasicsGuide" / "BinToolz-patched"
    / "CB_HSL_SP2933_5G0906259L_0002_BasicsGuide_R04.bin"
)

_have_base = STOCK_BIN.is_file() and XDF.is_file()
_have_patch = PATCHED_BIN.is_file() and SWITCH_XDF.is_file() and XDF.is_file()

requires_base = pytest.mark.skipif(not _have_base, reason="real SC8S50 bin/XDF absent")
requires_patch = pytest.mark.skipif(not _have_patch, reason="patched bin / switch XDF absent")


def _buf_sha(tune: Tune) -> str:
    return hashlib.sha256(tune.space("base").cal.binimage.to_bytes()).hexdigest()


def _open_base() -> Tune:
    return Tune.open(SC8S50, xdf=XDF, bin=STOCK_BIN)


def _open_patched() -> Tune:
    return Tune.open(
        SC8S50, xdf=XDF, bin=PATCHED_BIN,
        extra_spaces={PATCH_SPACE: (SWITCH_PATCH_2933, SWITCH_XDF)},
    )


# --------------------------------------------------------------------------- #
# serialize / restore
# --------------------------------------------------------------------------- #
@requires_base
def test_restore_reproduces_session_byte_exactly() -> None:
    tune = _open_base()
    tune.boost.put_ceiling_psi(28.0)
    edited_sha = _buf_sha(tune)
    n = len(tune.journal)

    restored = restore_session(serialize_session(tune))
    assert _buf_sha(restored) == edited_sha
    assert len(restored.journal) == n


@requires_patch
def test_restore_is_byte_exact_for_raw_slot_tables() -> None:
    """The raw/non-linear slot tables are the re-encode trap — must round-trip."""
    tune = _open_patched()
    tune.boost.put_ceiling_psi(30.0)
    tune.switchpatch.slot_curve(5, psi=18.0, intent="cap slot5 at 18 psi")
    edited_sha = _buf_sha(tune)

    restored = restore_session(serialize_session(tune))
    assert _buf_sha(restored) == edited_sha, "raw slot bytes did not round-trip"
    # And the decoded values agree, not just the bytes.
    import numpy as np
    a = tune.values("slot5_put_setpoint", space=PATCH_SPACE)
    b = restored.values("slot5_put_setpoint", space=PATCH_SPACE)
    assert np.array_equal(a, b)


@requires_base
def test_source_bin_unchanged_and_reverified(tmp_path) -> None:
    before = hashlib.sha256(STOCK_BIN.read_bytes()).hexdigest()
    tune = _open_base()
    tune.boost.put_ceiling_psi(26.0)
    save_session(tune, tmp_path / "s.json")
    load_session(tmp_path / "s.json")
    assert hashlib.sha256(STOCK_BIN.read_bytes()).hexdigest() == before


@requires_base
def test_recover_after_simulated_process_kill(tmp_path) -> None:
    """Save mid-edit, drop the live tune, reload from disk in a fresh call."""
    tune = _open_base()
    tune.boost.put_ceiling_psi(29.0)
    tune.limits.airmass_cap_mg(2100)
    edited_sha = _buf_sha(tune)
    n = len(tune.journal)

    path = save_session(tune, tmp_path / "session.json")
    del tune  # process dies

    recovered = load_session(path)
    assert _buf_sha(recovered) == edited_sha
    assert len(recovered.journal) == n


@requires_base
def test_empty_session_round_trips(tmp_path) -> None:
    tune = _open_base()
    pristine = _buf_sha(tune)
    recovered = restore_session(serialize_session(tune))
    assert _buf_sha(recovered) == pristine
    assert len(recovered.journal) == 0


# --------------------------------------------------------------------------- #
# fail-loud integrity
# --------------------------------------------------------------------------- #
@requires_base
def test_restore_refuses_changed_source_bin(tmp_path) -> None:
    tune = _open_base()
    tune.boost.put_ceiling_psi(27.0)
    data = serialize_session(tune)

    # A different bin at the same logical path: point the source at a tampered copy.
    tampered = tmp_path / "tampered.bin"
    buf = bytearray(STOCK_BIN.read_bytes())
    buf[0x250000] ^= 0xFF
    tampered.write_bytes(buf)

    with pytest.raises(RecoveryError, match="changed since"):
        restore_session(data, source_bin=tampered)


@requires_base
def test_restore_detects_corrupt_diff(tmp_path) -> None:
    tune = _open_base()
    tune.boost.put_ceiling_psi(27.0)
    data = serialize_session(tune)

    # Corrupt one recovered byte so the reconstructed buffer won't match the hash.
    some_off = next(iter(data["byte_diff"]))
    data["byte_diff"][some_off] = (data["byte_diff"][some_off] + 1) & 0xFF

    with pytest.raises(RecoveryError, match="does not match"):
        restore_session(data)


@requires_base
def test_unsupported_format_version_is_refused() -> None:
    tune = _open_base()
    data = serialize_session(tune)
    data["format_version"] = 999
    with pytest.raises(RecoveryError, match="format_version"):
        restore_session(data)


@requires_base
def test_different_xdf_is_refused_on_restore() -> None:
    alternate = CODE_ROOT / "xdf" / "SC8S50.ALL.xdf"
    if not alternate.is_file():
        pytest.skip("alternate real XDF absent")
    data = serialize_session(_open_base())
    with pytest.raises(RecoveryError, match="XDF has changed"):
        restore_session(data, xdf_paths={"base": alternate})


@requires_base
def test_different_engine_version_is_refused() -> None:
    data = serialize_session(_open_base())
    data["engine_version"] = "0.0.0-incompatible"
    with pytest.raises(RecoveryError, match="engine_version"):
        restore_session(data)


@requires_patch
def test_switch_patch_post_check_survives_recovery() -> None:
    tune = _open_patched()
    tune.switchpatch.require_sanity()
    restored = restore_session(serialize_session(tune))
    assert [check.name for check in restored.post_checks] == ["switch-patch sanity"]


# --------------------------------------------------------------------------- #
# undo / redo
# --------------------------------------------------------------------------- #
@requires_base
def test_undo_redo_restores_prior_state() -> None:
    tune = _open_base()
    history = SessionHistory(tune)
    s0 = _buf_sha(tune)

    tune.boost.put_ceiling_psi(30.0)
    history.commit()
    s1 = _buf_sha(tune)

    tune.limits.airmass_cap_mg(2200)
    history.commit()
    s2 = _buf_sha(tune)
    assert len({s0, s1, s2}) == 3, "edits should produce distinct states"

    assert history.undo() is True
    assert _buf_sha(tune) == s1 and len(tune.journal) == 1
    assert history.undo() is True
    assert _buf_sha(tune) == s0 and len(tune.journal) == 0
    assert history.can_undo is False
    assert history.undo() is False

    assert history.redo() is True
    assert _buf_sha(tune) == s1
    assert history.redo() is True
    assert _buf_sha(tune) == s2
    assert history.can_redo is False


@requires_base
def test_commit_after_undo_discards_redo_tail() -> None:
    tune = _open_base()
    history = SessionHistory(tune)
    tune.boost.put_ceiling_psi(30.0)
    history.commit()
    tune.boost.put_ceiling_psi(31.0)
    history.commit()

    history.undo()  # back to the 30 psi state
    assert history.can_redo is True

    tune.limits.airmass_cap_mg(2000)  # a new branch
    history.commit()
    assert history.can_redo is False, "a commit after undo drops the redo tail"


@requires_base
def test_undo_redo_stack_survives_recovery() -> None:
    tune = _open_base()
    history = SessionHistory(tune)
    tune.boost.put_ceiling_psi(30.0)
    history.commit()
    first = _buf_sha(tune)
    tune.limits.airmass_cap_mg(2200)
    history.commit()
    second = _buf_sha(tune)

    restored = restore_session(serialize_session(tune))
    recovered_history = SessionHistory(restored)
    assert recovered_history.can_undo is True
    assert recovered_history.undo() is True
    assert _buf_sha(restored) == first
    assert recovered_history.redo() is True
    assert _buf_sha(restored) == second
