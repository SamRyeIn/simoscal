"""Contract tests for the versioned Python↔Kotlin bridge (protocol V6).

The bridge is the one boundary the Android app calls, so these tests assert the
*contract* the app depends on, not just that the underlying ops work (they have
their own suites). Concretely:

* **Every op has a happy path** returning a well-formed ``ok`` envelope — the
  per-op parity fixtures the plan's Verification list names.
* **Every failure maps to a stable code**, never an exception or a traceback:
  malformed JSON, version mismatch, unknown op, bad params, a missing or changed
  private file, an unknown session, a rejected edit, an unexpected error.
* **Files cross by verified path + hash only.** A path whose bytes do not hash to
  the value the app recorded is refused before anything is opened.
* **The wire form is deterministic.** Identical requests serialize byte-identical
  — the property the cross-runtime golden gate turns on.
* **A session survives serialize → recover**, reproducing the edited buffer.

They run against the real stock bin (and, for the boost ops, a real patched bin),
skipping cleanly when those are absent — the same policy the rest of the suite
uses. A synthetic fixture cannot exercise real checksum correction or the
switch-patch boost tables.
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

import pytest

import simoscal.bridge as bridge
from simoscal import btp
from simoscal.bridge import BRIDGE_VERSION, ErrorCode, dispatch, dispatch_obj


# --------------------------------------------------------------------------- #
# helpers + fixtures
# --------------------------------------------------------------------------- #
def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@pytest.fixture(autouse=True)
def _clean_registry():
    """Every test starts and ends with an empty session registry."""
    bridge.reset()
    yield
    bridge.reset()


def call(operation: str, *, version: int = BRIDGE_VERSION, request_id=None, **params) -> dict:
    """Build a request, dispatch it through the JSON boundary, return the parsed envelope."""
    req = {"bridge_version": version, "op": operation, "params": params}
    if request_id is not None:
        req["request_id"] = request_id
    return json.loads(dispatch(json.dumps(req)))


def ok_result(env: dict) -> dict:
    """Assert an envelope is a success and return its result."""
    assert env["ok"] is True, env
    assert env["bridge_version"] == BRIDGE_VERSION
    return env["result"]


def err_code(env: dict) -> str:
    """Assert an envelope is a failure and return its stable code."""
    assert env["ok"] is False, env
    assert env["bridge_version"] == BRIDGE_VERSION
    return env["error"]["code"]


@pytest.fixture
def files(real_xdf: Path, real_bin: Path) -> dict:
    """The base bin + XDF with their hashes, as the app would pass them."""
    return {
        "bin_path": str(real_bin), "bin_sha256": _sha256(real_bin),
        "xdf_path": str(real_xdf), "xdf_sha256": _sha256(real_xdf),
    }


@pytest.fixture
def session(files: dict) -> str:
    """An open base-only edit session; returns its id."""
    return ok_result(call("session_create", **files))["session_id"]


@pytest.fixture
def patched_files(golden_multipatch: dict, real_xdf: Path, switch_patch_xdf: Path) -> dict:
    """A real *patched* bin + base XDF + switch-patch XDF, all with hashes."""
    patched = golden_multipatch["result"]
    return {
        "bin_path": str(patched), "bin_sha256": _sha256(patched),
        "xdf_path": str(real_xdf), "xdf_sha256": _sha256(real_xdf),
        "switch_patch_xdf_path": str(switch_patch_xdf),
        "switch_patch_xdf_sha256": _sha256(switch_patch_xdf),
    }


@pytest.fixture
def boost_session(patched_files: dict) -> str:
    """An open session with the switch-patch space, ready for the boost editor."""
    res = ok_result(call("session_create", **patched_files))
    assert res["provenance"]["has_switch_patch"] is True
    return res["session_id"]


# --------------------------------------------------------------------------- #
# envelope + version handshake
# --------------------------------------------------------------------------- #
def test_bridge_info_reports_version_and_ops():
    res = ok_result(call("bridge_info"))
    assert res["bridge_version"] == BRIDGE_VERSION
    assert "preflight" in res["ops"] and "build" in res["ops"]
    # the op table and the advertised list agree
    assert set(res["ops"]) == set(bridge.OPS)


def test_request_id_is_echoed_when_present():
    env = call("bridge_info", request_id="req-42")
    assert env["request_id"] == "req-42"


def test_malformed_json_is_a_bad_request_not_a_crash():
    env = json.loads(dispatch("{not valid json"))
    assert err_code(env) == ErrorCode.BAD_REQUEST.value


def test_non_object_json_is_a_bad_request():
    env = json.loads(dispatch("[1, 2, 3]"))
    assert err_code(env) == ErrorCode.BAD_REQUEST.value


def test_missing_op_is_a_bad_request():
    env = json.loads(dispatch(json.dumps({"bridge_version": BRIDGE_VERSION, "params": {}})))
    assert err_code(env) == ErrorCode.BAD_REQUEST.value


def test_version_mismatch_is_rejected_before_the_op_runs():
    # a newer app than this engine understands
    assert err_code(call("bridge_info", version=BRIDGE_VERSION + 1)) == ErrorCode.VERSION_MISMATCH.value
    # and an older one
    assert err_code(call("bridge_info", version=0)) == ErrorCode.VERSION_MISMATCH.value


def test_unknown_op_is_rejected():
    assert err_code(call("frobnicate")) == ErrorCode.UNKNOWN_OP.value


def test_params_must_be_an_object():
    req = {"bridge_version": BRIDGE_VERSION, "op": "bridge_info", "params": [1, 2]}
    env = json.loads(dispatch(json.dumps(req)))
    assert err_code(env) == ErrorCode.BAD_REQUEST.value


# --------------------------------------------------------------------------- #
# file verification — path + hash, never bytes
# --------------------------------------------------------------------------- #
def test_missing_file_is_file_not_found(real_xdf: Path):
    env = call(
        "preflight",
        bin_path="/no/such/bin.bin", bin_sha256="00" * 32,
        xdf_path=str(real_xdf), xdf_sha256=_sha256(real_xdf),
    )
    assert err_code(env) == ErrorCode.FILE_NOT_FOUND.value


def test_changed_file_is_a_hash_mismatch(files: dict):
    bad = dict(files, bin_sha256="00" * 32)
    assert err_code(call("preflight", **bad)) == ErrorCode.HASH_MISMATCH.value


def test_missing_hash_param_is_a_bad_param(real_bin: Path, real_xdf: Path):
    env = call(
        "preflight",
        bin_path=str(real_bin),  # no bin_sha256
        xdf_path=str(real_xdf), xdf_sha256=_sha256(real_xdf),
    )
    assert err_code(env) == ErrorCode.BAD_PARAMS.value


def test_file_path_and_hash_must_be_strings(files: dict):
    bad = dict(files, bin_sha256=123)
    assert err_code(call("preflight", **bad)) == ErrorCode.BAD_PARAMS.value


# --------------------------------------------------------------------------- #
# preflight
# --------------------------------------------------------------------------- #
def test_preflight_happy_path(files: dict):
    res = ok_result(call("preflight", **files))
    assert res["ok_to_edit"] is True
    assert res["profile_matched"] is True
    # a nested dataclass (ChecksumState) came across as plain JSON
    assert isinstance(res["checksums"], list)


def test_preflight_reports_switch_patch_when_asked(patched_files: dict):
    res = ok_result(call(
        "preflight",
        bin_path=patched_files["bin_path"], bin_sha256=patched_files["bin_sha256"],
        xdf_path=patched_files["xdf_path"], xdf_sha256=patched_files["xdf_sha256"],
        switch_patch_xdf_path=patched_files["switch_patch_xdf_path"],
        switch_patch_xdf_sha256=patched_files["switch_patch_xdf_sha256"],
    ))
    assert res["switch_patch_present"] is True


# --------------------------------------------------------------------------- #
# sessions + catalog
# --------------------------------------------------------------------------- #
def test_session_create_rechecks_preflight_before_opening_patch_space(
    files: dict, switch_patch_xdf: Path,
):
    """A stock bin must never expose switch-patch addresses as editable tables."""
    env = call(
        "session_create",
        **files,
        switch_patch_xdf_path=str(switch_patch_xdf),
        switch_patch_xdf_sha256=_sha256(switch_patch_xdf),
    )
    assert err_code(env) == ErrorCode.PREFLIGHT_BLOCKED.value


def test_session_create_returns_an_id_and_provenance(session: str):
    assert isinstance(session, str) and session


def test_unknown_session_is_rejected():
    assert err_code(call("catalog", session_id="deadbeef")) == ErrorCode.UNKNOWN_SESSION.value


def test_catalog_lists_tables(session: str):
    tables = ok_result(call("catalog", session_id=session))["tables"]
    assert tables and all("name" in t and "shape" in t for t in tables)
    # no numpy leaked: values are nested plain lists/numbers
    assert isinstance(tables[0]["values"], list)


def test_table_detail_returns_one_table(session: str):
    tables = ok_result(call("catalog", session_id=session))["tables"]
    name = tables[0]["name"]
    detail = ok_result(call("table_detail", session_id=session, name=name))["table"]
    assert detail["name"] == name


def test_table_detail_unknown_table_is_a_tune_error(session: str):
    assert err_code(call("table_detail", session_id=session, name="nope")) == ErrorCode.TUNE_ERROR.value


def test_session_close_is_idempotent(session: str):
    assert ok_result(call("session_close", session_id=session))["existed"] is True
    # second close is not an error
    assert ok_result(call("session_close", session_id=session))["existed"] is False
    # and the session is really gone
    assert err_code(call("catalog", session_id=session)) == ErrorCode.UNKNOWN_SESSION.value


# --------------------------------------------------------------------------- #
# generic edits + undo/redo
# --------------------------------------------------------------------------- #
def test_edit_applies_and_records_undo(session: str):
    res = ok_result(call(
        "edit", session_id=session, name="pressure_quotient_max", op="set",
        selection={"kind": "cells", "args": [[0, 0]]}, value=1.7,
        intent="lower the 1000 rpm pressure-quotient cap",
    ))
    assert res["can_undo"] is True and res["can_redo"] is False
    assert res["encoded"][0][0] == pytest.approx(1.7, abs=1e-2)
    assert res["quantized"] is True  # fractional scaling can't hold 1.7 exactly


def test_undo_then_redo_round_trips(session: str):
    call("edit", session_id=session, name="pressure_quotient_max", op="set",
         selection={"kind": "cells", "args": [[0, 0]]}, value=1.7)
    before = ok_result(call("table_detail", session_id=session, name="pressure_quotient_max"))["table"]["values"]

    u = ok_result(call("undo", session_id=session))
    assert u["done"] is True and u["can_undo"] is False and u["can_redo"] is True

    r = ok_result(call("redo", session_id=session))
    assert r["done"] is True
    after = ok_result(call("table_detail", session_id=session, name="pressure_quotient_max"))["table"]["values"]
    assert after == before


def test_undo_with_nothing_to_undo_is_a_noop_not_an_error(session: str):
    res = ok_result(call("undo", session_id=session))
    assert res["done"] is False and res["can_undo"] is False


def test_edit_unknown_op_is_a_bad_param(session: str):
    env = call("edit", session_id=session, name="pressure_quotient_max", op="obliterate",
               selection={"kind": "all"}, value=1.0)
    assert err_code(env) == ErrorCode.BAD_PARAMS.value


def test_edit_non_string_op_is_a_bad_param(session: str):
    env = call(
        "edit", session_id=session, name="pressure_quotient_max", op=["set"],
        selection={"kind": "all"}, value=1.0,
    )
    assert err_code(env) == ErrorCode.BAD_PARAMS.value


def test_edit_bad_selection_kind_is_a_bad_param(session: str):
    env = call("edit", session_id=session, name="pressure_quotient_max", op="set",
               selection={"kind": "diagonal"}, value=1.0)
    assert err_code(env) == ErrorCode.BAD_PARAMS.value


def test_edit_out_of_range_selection_is_rejected(session: str):
    env = call("edit", session_id=session, name="pressure_quotient_max", op="set",
               selection={"kind": "cells", "args": [[999, 999]]}, value=1.0)
    assert err_code(env) == ErrorCode.EDIT_REJECTED.value


def test_rejected_edit_leaves_no_undo_point(session: str):
    env = call("edit", session_id=session, name="pressure_quotient_max", op="set",
               selection={"kind": "cells", "args": [[999, 999]]}, value=1.0)
    assert err_code(env) == ErrorCode.EDIT_REJECTED.value
    # nothing was committed: a following undo has nothing to do
    assert ok_result(call("undo", session_id=session))["done"] is False


# --------------------------------------------------------------------------- #
# journal — the read-only running list the changes screen renders
# --------------------------------------------------------------------------- #
def test_journal_is_empty_before_any_edit(session: str):
    res = ok_result(call("journal", session_id=session))
    assert res["entries"] == []
    assert res["counts"] == {}
    assert res["can_undo"] is False and res["can_redo"] is False


def test_journal_records_an_edit_with_its_intent_and_values(session: str):
    call("edit", session_id=session, name="pressure_quotient_max", op="set",
         selection={"kind": "cells", "args": [[0, 0]]}, value=1.7,
         intent="lower the 1000 rpm pressure-quotient cap")

    res = ok_result(call("journal", session_id=session))
    assert len(res["entries"]) == 1
    entry = res["entries"][0]
    assert entry["intent"] == "lower the 1000 rpm pressure-quotient cap"
    assert entry["verdict"] == "applied"
    assert entry["touched"] is True
    assert entry["cells_changed"] == 1
    # The label is the project's `ID` — Description form, and before/after are
    # text: no numpy array ever crosses this boundary.
    assert "PQ" in entry["label"] or "—" in entry["label"]
    assert isinstance(entry["before"], str) and entry["before"]
    assert isinstance(entry["after"], str) and entry["after"]
    assert res["counts"] == {"applied": 1}


def test_journal_follows_undo_and_redo(session: str):
    """The engine's journal is the only thing that knows what a session holds.

    An app-side tally accumulated from edit replies would still show the edit
    after an undo; the journal does not, because undo restores the entry list
    from its snapshot. This is the property the changes screen depends on.
    """
    call("edit", session_id=session, name="pressure_quotient_max", op="set",
         selection={"kind": "cells", "args": [[0, 0]]}, value=1.7)
    assert len(ok_result(call("journal", session_id=session))["entries"]) == 1

    ok_result(call("undo", session_id=session))
    assert ok_result(call("journal", session_id=session))["entries"] == []

    ok_result(call("redo", session_id=session))
    assert len(ok_result(call("journal", session_id=session))["entries"]) == 1


def test_journal_carries_no_gate_verdict(session: str):
    """It is a running list, never a report (CR-20260724-02).

    Nothing here may look like a build verdict: no verified flag, no share path,
    no checksum state. Only ``build`` gets to say a bin is verified.
    """
    res = ok_result(call("journal", session_id=session))
    for forbidden in ("verified", "shareable", "share_path", "checksum_state", "ok", "gates"):
        assert forbidden not in res


def test_journal_survives_serialize_and_recover(files: dict, session: str):
    """A recovered session's changes screen shows the same list it showed before."""
    call("edit", session_id=session, name="pressure_quotient_max", op="set",
         selection={"kind": "cells", "args": [[0, 0]]}, value=1.7,
         intent="survive a process kill")
    before = ok_result(call("journal", session_id=session))["entries"]

    record = ok_result(call("session_serialize", session_id=session))["record"]
    ok_result(call("session_close", session_id=session))

    recovered = ok_result(call(
        "session_recover",
        record=record,
        source_bin_path=files["bin_path"], source_bin_sha256=files["bin_sha256"],
        xdf_paths={"base": {"path": files["xdf_path"], "sha256": files["xdf_sha256"]}},
    ))["session_id"]
    assert ok_result(call("journal", session_id=recovered))["entries"] == before


def test_journal_of_an_unknown_session_is_rejected():
    assert err_code(call("journal", session_id="not-a-session")) == ErrorCode.UNKNOWN_SESSION.value


# --------------------------------------------------------------------------- #
# domain-owned tables are unreachable from the generic edit path (CR-20260813-01)
# --------------------------------------------------------------------------- #
def test_generic_edit_of_a_slot_grid_is_refused(boost_session: str):
    """The release blocker: a partial write to a slot grid breaks its tiling.

    All eight Y rows of a slot's ``PUT setpoint`` grid must hold the same curve —
    the Y axis is uncharacterized, so a per-row difference makes the cap depend
    on an axis nobody calibrated. ``slot_curve()`` tiles; a generic cell write
    does not, and used to be accepted and then certified verified and shareable.
    """
    env = call(
        "edit", session_id=boost_session, space="patch",
        name="slot1_put_setpoint", op="set",
        selection={"kind": "cells", "args": [[0, 0]]}, value=5000.0,
        intent="break the eight-row tiling",
    )
    assert err_code(env) == ErrorCode.EDIT_REJECTED.value
    # Refused before anything was written: no undo point, no journal entry.
    assert ok_result(call("undo", session_id=boost_session))["done"] is False


def test_generic_edit_of_the_slot_rpm_axis_is_refused(boost_session: str):
    """The shared axis has its own op, which also checks the length header."""
    axis = ok_result(call("boost_curve", session_id=boost_session))["boost_curve"]["rpm_axis"]
    env = call(
        "edit", session_id=boost_session, space="patch",
        name="slot_put_rpm_axis", op="paste",
        selection={"kind": "all"}, array=[v + 10.0 for v in axis],
    )
    assert err_code(env) == ErrorCode.EDIT_REJECTED.value


def test_generic_edit_of_the_axis_length_header_is_refused(boost_session: str):
    """The header is the patch's breakpoint count; it is checked, never written."""
    env = call(
        "edit", session_id=boost_session, space="patch",
        name="slot_put_rpm_axis_header", op="set",
        selection={"kind": "all"}, value=13.0,
    )
    assert err_code(env) == ErrorCode.EDIT_REJECTED.value


# --------------------------------------------------------------------------- #
# the per-slot switchboard
# --------------------------------------------------------------------------- #
def test_slot_settings_reports_every_scalar_against_every_slot(boost_session: str):
    rows = ok_result(call("slot_settings", session_id=boost_session))["settings"]

    assert len(rows) == 16
    by_key = {r["key"]: r for r in rows}
    assert by_key["enable_sl_tc"]["writable"] is True
    assert by_key["rpm_limiter"]["writable"] is False
    for row in rows:
        assert len(row["values"]) == 5
        assert row["title"] and row["description"]


def test_a_flag_write_returns_the_whole_board(boost_session: str):
    """The reply carries the new state so the app never redraws from a guess."""
    res = ok_result(call(
        "slot_flag", session_id=boost_session,
        key="enable_lc", slots=[2, 4], on=True, intent="test",
    ))

    by_key = {r["key"]: r for r in res["settings"]}
    assert by_key["enable_lc"]["values"] == [0.0, 1.0, 0.0, 1.0, 0.0]
    assert len(res["entries"]) == 2
    assert res["can_undo"] is True


def test_a_flag_write_is_one_undo_point_per_slot(boost_session: str):
    ok_result(call("slot_flag", session_id=boost_session,
                   key="pops_enable", slots=[1], on=True))
    ok_result(call("undo", session_id=boost_session))

    rows = ok_result(call("slot_settings", session_id=boost_session))["settings"]
    by_key = {r["key"]: r for r in rows}
    assert by_key["pops_enable"]["values"] == [0.0] * 5


@pytest.mark.parametrize(
    "key", ["rpm_limiter", "speed_limiter", "manual_afu", "gauge_settings"]
)
def test_a_read_only_setting_is_refused_over_the_bridge(boost_session: str, key: str):
    env = call("slot_flag", session_id=boost_session, key=key, slots=[1], on=True)

    assert err_code(env) == ErrorCode.EDIT_REJECTED.value
    # Refused before anything was written: no undo point, no journal entry.
    assert ok_result(call("undo", session_id=boost_session))["done"] is False


def test_an_unknown_flag_is_refused(boost_session: str):
    env = call("slot_flag", session_id=boost_session,
               key="enable_launch_control", slots=[1], on=True)
    assert err_code(env) == ErrorCode.EDIT_REJECTED.value


def test_slot_flag_needs_at_least_one_slot(boost_session: str):
    env = call("slot_flag", session_id=boost_session,
               key="enable_lc", slots=[], on=True)
    assert err_code(env) == ErrorCode.BAD_PARAMS.value


def test_the_switchboard_needs_the_switch_patch_space(session: str):
    """A base-only session has none of these tables; say so rather than crash."""
    assert err_code(call("slot_settings", session_id=session)) == \
        ErrorCode.TUNE_ERROR.value
    assert err_code(call("slot_flag", session_id=session,
                         key="enable_lc", slots=[1], on=True)) == \
        ErrorCode.TUNE_ERROR.value


def test_generic_edit_of_a_slot_flag_is_refused(boost_session: str):
    """The switchboard's tables are owned like every other patch table.

    These sixteen sit within a few bytes of each other, so a generic write that
    skipped the domain call's "does this byte actually read 0 or 1" check would
    quietly overwrite a neighbour if a binding were ever wrong.
    """
    env = call(
        "edit", session_id=boost_session, space="patch",
        name="slot1_enable_lc", op="set",
        selection={"kind": "all"}, value=1.0,
    )
    assert err_code(env) == ErrorCode.EDIT_REJECTED.value


def test_generic_restore_of_a_domain_owned_table_is_refused(boost_session: str):
    """Every generic op, not only the obviously destructive ones.

    A partial restore breaks the same tiling a partial write does, so RESTORE is
    refused alongside the rest rather than carved out as a safe-looking exception.
    """
    env = call(
        "edit", session_id=boost_session, space="patch",
        name="slot1_put_setpoint", op="restore", selection={"kind": "all"},
    )
    assert err_code(env) == ErrorCode.EDIT_REJECTED.value


def test_the_catalog_does_not_offer_domain_owned_tables(boost_session: str):
    """The generic editor is never handed a table it is not allowed to write."""
    tables = ok_result(call("catalog", session_id=boost_session))["tables"]
    assert tables, "the base space still lists its tables"
    assert all(t["owner"] == "" for t in tables)
    assert not [t for t in tables if t["space"] == "patch"]


def test_a_domain_owned_table_is_still_readable(boost_session: str):
    """Reading one was never the hazard — the boost editor shows these values."""
    detail = ok_result(call(
        "table_detail", session_id=boost_session,
        name="slot1_put_setpoint", space="patch",
    ))["table"]
    assert detail["shape"] == [8, 12]
    assert "slot_curve" in detail["owner"]


def test_the_domain_call_still_writes_the_table_the_generic_path_cannot(
    boost_session: str,
):
    """The block is on the *generic* route only: the guarded route still works."""
    res = ok_result(call("boost_edit", session_id=boost_session, slot=1, psi=12.0,
                         intent="the guarded route still applies"))
    assert res["can_undo"] is True
    values = ok_result(call(
        "table_detail", session_id=boost_session,
        name="slot1_put_setpoint", space="patch",
    ))["table"]["values"]
    # Tiled: every one of the eight rows holds the same curve.
    assert all(row == values[0] for row in values)


# --------------------------------------------------------------------------- #
# unreadable patch XDF vs absent patch (CR-20260815-02)
# --------------------------------------------------------------------------- #
def test_an_unreadable_switch_patch_xdf_blames_the_xdf_not_the_bin(
    patched_files: dict, real_xdf: Path,
):
    """The two refusals have opposite remedies, so they must read differently.

    ``switch_patch_present`` is None when preflight could not *open* the patch
    XDF and False when it opened it and the bin lacks the patch. Both used to
    produce "the switch-patch tables are not present in this bin" — which, given
    a patched bin and an unreadable XDF, is a false statement that sends a
    person to re-patch a bin that is already patched.

    The v1.005/v1.006 XDFs reuse a uniqueid across slots and genuinely do not
    load; ``switch_patch_sanity``'s own docstring says so.
    """
    bad_xdf = real_xdf.parent / "SC8S50_switchpatch29.33_v1.006.xdf"
    if not bad_xdf.is_file():
        pytest.skip(f"curated switch-patch XDF not present: {bad_xdf}")

    params = dict(patched_files)
    params["switch_patch_xdf_path"] = str(bad_xdf)
    params["switch_patch_xdf_sha256"] = _sha256(bad_xdf)
    env = call("session_create", **params)

    assert err_code(env) == ErrorCode.PREFLIGHT_BLOCKED.value
    message = env["error"]["message"]
    assert "XDF could not be read" in message
    assert "not present in this bin" not in message
    # The cause preflight already knew is passed on rather than discarded.
    assert "uniqueid" in env["error"]["advanced"]


def test_an_unpatched_bin_still_says_the_patch_is_absent(
    files: dict, switch_patch_xdf: Path,
):
    """The contrast case: a readable XDF over a stock bin is the other remedy."""
    params = dict(files)
    params["switch_patch_xdf_path"] = str(switch_patch_xdf)
    params["switch_patch_xdf_sha256"] = _sha256(switch_patch_xdf)
    env = call("session_create", **params)

    assert err_code(env) == ErrorCode.PREFLIGHT_BLOCKED.value
    assert "not present in this bin" in env["error"]["message"]


# --------------------------------------------------------------------------- #
# domain-owned base tables — the kg/stk trap (CR-20260815-04)
# --------------------------------------------------------------------------- #
def test_generic_edit_of_the_airmass_ceiling_is_refused(session: str):
    """The trap: 2000 is the *right* number in mg/stk and a catastrophe raw.

    ``C_M_AIR_CYL_SP_MAX`` is labelled mg/stk by the XDF and stores kg/stk, so
    the generic editor displays 0.001389 beside a "mg/stk" unit and the obvious
    correction — type 2000 — writes a ceiling 1.44 million times stock, i.e.
    removes the limiter. Nothing downstream catches it: the table is float-bug
    flagged, but its declared max is 20000, so 2000 breaches no range at all.
    """
    env = call(
        "edit", session_id=session, space="base",
        name="airmass_setpoint_max", op="set",
        selection={"kind": "cells", "args": [[0, 0]]}, value=2000.0,
        intent="type the guide's mg/stk figure into a kg/stk store",
    )
    assert err_code(env) == ErrorCode.EDIT_REJECTED.value
    assert ok_result(call("undo", session_id=session))["done"] is False


def test_generic_edit_of_the_unconfirmed_airmass_table_is_refused(session: str):
    """``C_M_AIR_CYL_FL`` shares the label and range but its units are unproven.

    It reads 0.0 in stock and in every patched bin, so nothing settles whether
    it is kg/stk like its sibling. Refusing costs nothing — no domain call
    writes it and no revision ever has — and the alternative is leaving a
    possibly-millionfold-wrong write one tap away.
    """
    env = call(
        "edit", session_id=session, space="base",
        name="airmass_full_load", op="set",
        selection={"kind": "cells", "args": [[0, 0]]}, value=2000.0,
        intent="write a table whose units nobody has confirmed",
    )
    assert err_code(env) == ErrorCode.EDIT_REJECTED.value


def test_the_catalog_does_not_offer_the_airmass_tables(session: str):
    """A base-space session must not hand the grid editor either of them."""
    names = {t["name"] for t in ok_result(call("catalog", session_id=session))["tables"]}
    assert names, "the base space still lists its other tables"
    assert "airmass_setpoint_max" not in names
    assert "airmass_full_load" not in names
    # The contrast case: genuinely mg/stk, unowned, still editable.
    assert "intake_air_max_vvl0" in names


def test_the_airmass_ceiling_is_still_readable_and_names_its_owner(session: str):
    """Reading was never the hazard, and the owner string is the remedy."""
    detail = ok_result(call(
        "table_detail", session_id=session, name="airmass_setpoint_max",
    ))["table"]
    assert detail["values"] == [[pytest.approx(0.0013889999827370048)]]
    assert "airmass_cap_mg" in detail["owner"]


# --------------------------------------------------------------------------- #
# boost editor
# --------------------------------------------------------------------------- #
def test_boost_curve_model_has_five_slots(boost_session: str):
    model = ok_result(call("boost_curve", session_id=boost_session))["boost_curve"]
    assert len(model["slots"]) == 5
    assert len(model["rpm_axis"]) == 12
    assert "base_ceiling_psi" in model


def test_boost_curve_on_a_base_only_session_is_a_tune_error(session: str):
    assert err_code(call("boost_curve", session_id=session)) == ErrorCode.TUNE_ERROR.value


def test_boost_edit_flat_cap_floors_psi(boost_session: str):
    res = ok_result(call("boost_edit", session_id=boost_session, slot=5, psi=10.0,
                         intent="flat 10 psi valet cap"))
    assert res["slot"] == 5
    assert res["floored"] is True                 # floor keeps encoded ≤ requested
    assert all(p <= 10.0 for p in res["encoded_psi"])
    assert res["can_undo"] is True


def test_boost_edit_above_base_ceiling_is_rejected(boost_session: str):
    # a cap at/above the base IP_PUT_SP full-load ceiling is a guard refusal
    env = call("boost_edit", session_id=boost_session, slot=5, psi=99.0)
    assert err_code(env) == ErrorCode.EDIT_REJECTED.value


def test_boost_edit_bad_slot_is_a_bad_param(boost_session: str):
    assert err_code(call("boost_edit", session_id=boost_session, slot="five", psi=10.0)) \
        == ErrorCode.BAD_PARAMS.value


def test_boost_rpm_axis_rewrites_the_shared_axis(boost_session: str):
    before = ok_result(call("boost_curve", session_id=boost_session))["boost_curve"]
    shifted = [v + 50.0 for v in before["rpm_axis"]]
    res = ok_result(call("boost_rpm_axis", session_id=boost_session,
                         breakpoints=shifted, intent="shift the slot axis up 50 rpm"))
    assert res["rpm_axis"] == pytest.approx(shifted)
    assert res["can_undo"] is True
    # the model the editor re-reads reports the new axis, not a cached one
    after = ok_result(call("boost_curve", session_id=boost_session))["boost_curve"]
    assert after["rpm_axis"] == pytest.approx(shifted)


def test_boost_rpm_axis_refuses_a_non_increasing_axis(boost_session: str):
    # The guard that only the domain call applies. One axis serves all five
    # slots, so a non-monotonic breakpoint reinterprets every curve at once.
    axis = list(ok_result(call("boost_curve", session_id=boost_session))["boost_curve"]["rpm_axis"])
    axis[5], axis[6] = axis[6], axis[5]
    assert err_code(call("boost_rpm_axis", session_id=boost_session, breakpoints=axis)) \
        == ErrorCode.EDIT_REJECTED.value


def test_rejected_rpm_axis_leaves_no_undo_point(boost_session: str):
    axis = list(ok_result(call("boost_curve", session_id=boost_session))["boost_curve"]["rpm_axis"])
    axis[0] = axis[-1] + 1000.0
    assert err_code(call("boost_rpm_axis", session_id=boost_session, breakpoints=axis)) \
        == ErrorCode.EDIT_REJECTED.value
    assert ok_result(call("undo", session_id=boost_session))["done"] is False


def test_boost_rpm_axis_wrong_length_is_rejected(boost_session: str):
    assert err_code(call("boost_rpm_axis", session_id=boost_session, breakpoints=[1000.0, 2000.0])) \
        == ErrorCode.EDIT_REJECTED.value


def test_boost_rpm_axis_on_a_base_only_session_is_a_tune_error(session: str):
    assert err_code(call("boost_rpm_axis", session_id=session, breakpoints=[1000.0])) \
        == ErrorCode.TUNE_ERROR.value


def test_boost_rpm_axis_needs_breakpoints(boost_session: str):
    assert err_code(call("boost_rpm_axis", session_id=boost_session)) == ErrorCode.BAD_PARAMS.value


# --------------------------------------------------------------------------- #
# build
# --------------------------------------------------------------------------- #
def test_build_verifies_and_is_shareable(session: str, files: dict, tmp_path: Path):
    ok_result(call("edit", session_id=session, name="pressure_quotient_max", op="set",
                   selection={"kind": "cells", "args": [[0, 0]]}, value=1.7,
                   intent="lower the 1000 rpm pressure-quotient cap"))
    report = ok_result(call(
        "build", session_id=session, revision="RTEST",
        staging_dir=str(tmp_path),
        reference_bin_path=files["bin_path"], reference_bin_sha256=files["bin_sha256"],
        source_bin_path=files["bin_path"], source_bin_sha256=files["bin_sha256"],
    ))["report"]
    assert report["verified"] is True
    assert report["share_path"] is not None
    assert Path(report["share_path"]).is_file()


def test_build_reference_bin_is_hash_verified(session: str, files: dict, tmp_path: Path):
    env = call(
        "build", session_id=session, revision="RTEST", staging_dir=str(tmp_path),
        reference_bin_path=files["bin_path"], reference_bin_sha256="00" * 32,
    )
    assert err_code(env) == ErrorCode.HASH_MISMATCH.value


def _build(session: str, files: dict, staging: Path, **extra) -> dict:
    """Run a build op with the session's own bin as reference and source."""
    return call(
        "build", session_id=session, staging_dir=str(staging),
        reference_bin_path=files["bin_path"], reference_bin_sha256=files["bin_sha256"],
        source_bin_path=files["bin_path"], source_bin_sha256=files["bin_sha256"],
        **extra,
    )


def test_build_refuses_a_name_that_escapes_the_staging_directory(
    session: str, files: dict, tmp_path: Path,
):
    """CR-20260813-05: a hostile provider's display name must not steer the path.

    ``bin_name`` reaches the engine as untrusted text. A traversal is a loud
    ``BAD_PARAMS``, and nothing is written — not outside the staging tree, and
    not inside it either.
    """
    staging = tmp_path / "staging"
    env = _build(session, files, staging, revision="RTEST", bin_name="../escaped.bin")
    assert err_code(env) == ErrorCode.BAD_PARAMS.value
    assert not list(tmp_path.rglob("*.bin"))


def test_build_refuses_a_revision_that_is_not_a_bare_file_name(
    session: str, files: dict, tmp_path: Path,
):
    """The revision names the build directory, so it is validated the same way."""
    env = _build(session, files, tmp_path, revision="../RTEST")
    assert err_code(env) == ErrorCode.BAD_PARAMS.value
    assert not list(tmp_path.rglob("*.bin"))


def test_two_builds_never_share_a_candidate_path(
    session: str, files: dict, tmp_path: Path,
):
    """CR-20260813-02: a granted URI must keep pointing at the approved bytes."""
    ok_result(call("edit", session_id=session, name="pressure_quotient_max", op="set",
                   selection={"kind": "cells", "args": [[0, 0]]}, value=1.7,
                   intent="first"))
    first = ok_result(_build(session, files, tmp_path, revision="RTEST"))["report"]
    assert first["share_path"] is not None
    shared = Path(first["share_path"]).read_bytes()

    ok_result(call("edit", session_id=session, name="pressure_quotient_max", op="set",
                   selection={"kind": "cells", "args": [[0, 0]]}, value=1.9,
                   intent="second"))
    second = ok_result(_build(session, files, tmp_path, revision="RTEST"))["report"]

    assert second["share_path"] != first["share_path"]
    assert Path(first["share_path"]).read_bytes() == shared


def test_a_patched_build_always_runs_the_switch_patch_gate(
    boost_session: str, patched_files: dict, tmp_path: Path,
):
    """CR-20260813-01, defense in depth: the patch sanity gate is not optional.

    Whatever route the session took to get here, a build of a patched bin
    re-checks on the finished file that the patch still loads and decodes.
    """
    ok_result(call("boost_edit", session_id=boost_session, slot=5, psi=10.0,
                   intent="flat 10 psi valet cap"))
    report = ok_result(call(
        "build", session_id=boost_session, revision="RPATCH",
        staging_dir=str(tmp_path),
        reference_bin_path=patched_files["bin_path"],
        reference_bin_sha256=patched_files["bin_sha256"],
        source_bin_path=patched_files["bin_path"],
        source_bin_sha256=patched_files["bin_sha256"],
    ))["report"]

    gate = next(g for g in report["gates"] if g["name"] == "switch-patch sanity")
    assert gate["ran"] and gate["passed"]
    # Registered exactly once: a duplicate would run and journal the check twice.
    assert len([g for g in report["gates"] if g["name"] == "switch-patch sanity"]) == 1
    assert report["verified"] is True


def test_the_switch_patch_gate_does_not_need_a_bintoolz_checkout(
    boost_session: str, patched_files: dict, tmp_path: Path, monkeypatch,
):
    """CR-20260815-05: on a phone there is no BinToolz tree, and there never will be.

    The gate used to resolve its XDF through ``default_switch_patch_xdf()``,
    which points inside a BinToolz checkout. That path exists on this machine
    and nowhere on Android, so every build of a patched bin failed with
    "switch-patch XDF not found" — loudly and unverified, but also
    unconditionally: the app could not produce a flashable bin at all.

    Simulating the phone by making the desktop default unresolvable. The gate
    must still run and pass, because the session's own patch XDF is the right
    reference anyway — it is the definition the edits were made through.
    """
    missing = tmp_path / "no-such-checkout" / "S50 Switch Patch.29.33.V2.xdf"
    monkeypatch.setattr(btp, "default_switch_patch_xdf", lambda *a, **k: missing)
    assert not missing.exists()

    ok_result(call("boost_edit", session_id=boost_session, slot=5, psi=10.0,
                   intent="flat 10 psi valet cap"))
    report = ok_result(call(
        "build", session_id=boost_session, revision="RNOBTP",
        staging_dir=str(tmp_path / "staging"),
        reference_bin_path=patched_files["bin_path"],
        reference_bin_sha256=patched_files["bin_sha256"],
        source_bin_path=patched_files["bin_path"],
        source_bin_sha256=patched_files["bin_sha256"],
    ))["report"]

    gate = next(g for g in report["gates"] if g["name"] == "switch-patch sanity")
    assert gate["ran"] and gate["passed"], gate
    assert report["verified"] is True
    assert report["share_path"] is not None


# --------------------------------------------------------------------------- #
# session recovery (serialize → recover round-trip)
# --------------------------------------------------------------------------- #
def test_session_survives_serialize_and_recover(session: str, files: dict):
    ok_result(call("edit", session_id=session, name="pressure_quotient_max", op="set",
                   selection={"kind": "cells", "args": [[0, 0]]}, value=1.7,
                   intent="edit to recover"))
    edited = ok_result(call("table_detail", session_id=session, name="pressure_quotient_max"))["table"]["values"]

    record = ok_result(call("session_serialize", session_id=session))["record"]

    # simulate a process kill: the old session is gone
    bridge.reset()
    assert err_code(call("catalog", session_id=session)) == ErrorCode.UNKNOWN_SESSION.value

    recovered = ok_result(call(
        "session_recover", record=record,
        source_bin_path=files["bin_path"], source_bin_sha256=files["bin_sha256"],
        xdf_paths={
            "base": {"path": files["xdf_path"], "sha256": files["xdf_sha256"]},
        },
    ))
    rid = recovered["session_id"]
    assert recovered["provenance"]["recovered"] is True
    assert recovered["can_undo"] is True

    after = ok_result(call("table_detail", session_id=rid, name="pressure_quotient_max"))["table"]["values"]
    assert after == edited
    assert ok_result(call("undo", session_id=rid))["done"] is True
    undone = ok_result(
        call("table_detail", session_id=rid, name="pressure_quotient_max")
    )["table"]["values"]
    assert undone != edited


def test_a_built_session_survives_serialize_and_recover(
    session: str, files: dict, tmp_path: Path
):
    """CR-20260816-01, at the boundary the app actually calls.

    Building is the normal end of a session, and the build writes corrected
    checksums into the same live buffer the record is serialized from. When the
    record could not account for those bytes, every recover of a built session
    failed — and because the app clears its recovery pointer on failure, the one
    attempt destroyed the session. This is the field sequence, in order.
    """
    ok_result(call("edit", session_id=session, name="pressure_quotient_max", op="set",
                   selection={"kind": "cells", "args": [[0, 0]]}, value=1.7,
                   intent="edit, then build, then resume"))
    report = ok_result(call(
        "build", session_id=session, revision="RTEST", staging_dir=str(tmp_path),
        reference_bin_path=files["bin_path"], reference_bin_sha256=files["bin_sha256"],
        source_bin_path=files["bin_path"], source_bin_sha256=files["bin_sha256"],
    ))["report"]
    assert report["verified"] is True

    edited = ok_result(
        call("table_detail", session_id=session, name="pressure_quotient_max")
    )["table"]["values"]
    record = ok_result(call("session_serialize", session_id=session))["record"]

    bridge.reset()  # the app is killed after the build

    recovered = ok_result(call(
        "session_recover", record=record,
        source_bin_path=files["bin_path"], source_bin_sha256=files["bin_sha256"],
        xdf_paths={"base": {"path": files["xdf_path"], "sha256": files["xdf_sha256"]}},
    ))
    rid = recovered["session_id"]
    after = ok_result(
        call("table_detail", session_id=rid, name="pressure_quotient_max")
    )["table"]["values"]
    assert after == edited
    # The undo stack has to come back with it, not just the bytes.
    assert recovered["can_undo"] is True
    assert ok_result(call("undo", session_id=rid))["done"] is True


def test_recover_rejects_a_changed_source_bin(session: str, files: dict):
    record = ok_result(call("session_serialize", session_id=session))["record"]
    bridge.reset()
    env = call("session_recover", record=record,
               source_bin_path=files["bin_path"], source_bin_sha256="00" * 32)
    assert err_code(env) == ErrorCode.HASH_MISMATCH.value


def test_recover_requires_verified_xdf_paths(session: str, files: dict):
    record = ok_result(call("session_serialize", session_id=session))["record"]
    bridge.reset()
    env = call(
        "session_recover", record=record,
        source_bin_path=files["bin_path"], source_bin_sha256=files["bin_sha256"],
    )
    assert err_code(env) == ErrorCode.BAD_PARAMS.value


def test_recover_requires_a_record():
    assert err_code(call("session_recover")) == ErrorCode.BAD_PARAMS.value


# --------------------------------------------------------------------------- #
# determinism, concurrency, and exception mapping
# --------------------------------------------------------------------------- #
def test_identical_requests_serialize_byte_identically(files: dict):
    req = json.dumps({"bridge_version": BRIDGE_VERSION, "op": "preflight", "params": files})
    assert dispatch(req) == dispatch(req)


def test_a_concurrent_request_is_rejected_busy():
    # hold the engine lock, then a dispatch cannot take it and must not block
    assert bridge._LOCK.acquire(blocking=False)
    try:
        assert err_code(call("bridge_info")) == ErrorCode.BUSY.value
    finally:
        bridge._LOCK.release()


def test_the_lock_is_released_after_a_failed_op(session: str):
    # an op that errors must still release the lock, or the engine wedges
    call("table_detail", session_id=session, name="nope")
    assert bridge._LOCK.acquire(blocking=False)
    bridge._LOCK.release()


def test_two_threads_never_run_an_op_at_once():
    """A real race: N threads dispatch at once; the lock must serialize them."""
    active = {"n": 0, "max": 0}
    guard = threading.Lock()

    def slow(_params):
        with guard:
            active["n"] += 1
            active["max"] = max(active["max"], active["n"])
        # a busy no-op; the point is overlap, so keep the window open briefly
        for _ in range(20000):
            pass
        with guard:
            active["n"] -= 1
        return {"ok": True}

    bridge.OPS["_slow_test"] = slow
    try:
        results = []

        def worker():
            results.append(call("_slow_test"))

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        del bridge.OPS["_slow_test"]

    # some calls got BUSY, none ever overlapped inside the engine
    assert active["max"] == 1
    codes = {e.get("error", {}).get("code") for e in results if not e["ok"]}
    assert codes <= {ErrorCode.BUSY.value}


def test_an_unexpected_exception_maps_to_internal_without_a_traceback():
    def boom(_params):
        raise RuntimeError("secret internal detail")

    bridge.OPS["_boom_test"] = boom
    try:
        env = call("_boom_test")
    finally:
        del bridge.OPS["_boom_test"]

    assert err_code(env) == ErrorCode.INTERNAL.value
    # the message is generic and the advanced field names the type, but no
    # traceback text ("Traceback", file paths, line numbers) leaks to the UI
    blob = json.dumps(env)
    assert "Traceback" not in blob
    assert "bridge.py" not in blob
    # the raised message may appear in advanced, but the point is: no stack
    assert env["error"]["message"] == "the engine hit an unexpected error"


def test_dispatch_obj_and_dispatch_agree(files: dict):
    """The object path and the string path produce the same envelope."""
    req = {"bridge_version": BRIDGE_VERSION, "op": "preflight", "params": files}
    from_obj = dispatch_obj(dict(req))
    from_str = json.loads(dispatch(json.dumps(req)))
    assert from_obj == from_str
