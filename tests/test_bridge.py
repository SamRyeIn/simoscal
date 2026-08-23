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


def test_the_catalog_wire_form_carries_the_domain_group(session: str):
    """The app groups its browser by this field, so it has to cross the bridge.

    ``group`` rides on the dataclass, which the bridge serializes whole — this
    asserts the field is actually on the wire and populated, so a rename on the
    Python side surfaces here rather than as an empty heading on the tablet.
    """
    from simoscal.tune.profile import GROUPS

    tables = ok_result(call("catalog", session_id=session))["tables"]
    assert all(t.get("group") for t in tables), "every row needs a heading"
    assert {t["group"] for t in tables} <= set(GROUPS)


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

    # The patch space *may* contribute — its non-slot scalars are ordinary
    # independent values and are deliberately generically editable. What it must
    # never contribute is anything per-slot or the shared axis: those carry the
    # cross-slot coherence a grid edit silently breaks (CR-20260813-01).
    patch = [t for t in tables if t["space"] == "patch"]
    assert all(
        not t["name"].startswith("slot") for t in patch
    ), f"a slot-owned table reached the generic catalog: {[t['name'] for t in patch]}"


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


# --------------------------------------------------------------------------- #
# analyze_logs — read-only, sessionless datalog analysis
# --------------------------------------------------------------------------- #
@pytest.fixture
def log_params(tmp_path) -> dict:
    """Two synthetic datalogs in the shape the app passes them: path + hash + name."""
    from tests.faultinject import PullSpec, build_folder

    build_folder(
        tmp_path,
        [PullSpec(put_overshoot=25.0, knock={3: -3.0}), PullSpec()],
        wastegate=True,
        ign_table=True,
    )
    return {
        "logs": [
            {
                "log_path": str(csv),
                "log_sha256": _sha256(csv),
                "display_name": csv.name,
            }
            for csv in sorted(tmp_path.glob("simostools-*.csv"))
        ]
    }


def test_analyze_logs_happy_path(log_params: dict):
    result = ok_result(call("analyze_logs", **log_params))
    assert result["logs"] and result["pulls"]
    assert result["ran"], "some checks must have run"
    # The two calibration-aware checks skip without a bin, exactly as on desktop.
    assert {"boost_cal", "boost_p0234"} <= {s["check_id"] for s in result["skipped"]}
    assert result["cal_resolved"] is False


def test_analyze_logs_needs_no_session():
    """Reading a datalog has nothing to do with editing a bin, so it is not gated on one."""
    assert bridge._SESSIONS == {}
    # (the happy path above already ran with an empty registry; this pins the
    # intent so a future session gate on this op fails a test rather than a user)
    assert "session_id" not in bridge.OPS["analyze_logs"].__doc__


def test_analyze_logs_returns_every_plot_in_the_inventory(log_params: dict):
    from simoscal.analysis import PLOT_SPECS

    result = ok_result(call("analyze_logs", **log_params))
    assert [p["id"] for p in result["plots"]] == [s.id for s in PLOT_SPECS]
    for plot in result["plots"]:
        # The copy the app renders above each plot travels with it.
        assert plot["title"] and plot["description"] and plot["tip"]
        assert plot["panels"]


def test_analyze_logs_plot_series_are_plain_json_numbers(log_params: dict):
    """No numpy scalar may reach the wire — the same rule every other op obeys."""
    result = ok_result(call("analyze_logs", **log_params))
    drawn = [p for p in result["plots"] if p["drawn"]]
    assert drawn, "the synthetic logs should draw something"
    for plot in drawn:
        for panel in plot["panels"]:
            for series in panel["series"]:
                for segment in series["segments"]:
                    assert len(segment["x"]) == len(segment["y"])
                    assert all(isinstance(v, float) for v in segment["x"])
                    assert all(isinstance(v, float) for v in segment["y"])


def test_analyze_logs_rejects_a_changed_file(log_params: dict, tmp_path):
    """A CSV edited after the app hashed it is refused before it is parsed."""
    target = Path(log_params["logs"][0]["log_path"])
    target.write_text(target.read_text() + "\n0,0,0\n")
    assert err_code(call("analyze_logs", **log_params)) == ErrorCode.HASH_MISMATCH.value


def test_analyze_logs_missing_file_is_file_not_found(log_params: dict):
    log_params["logs"][0]["log_path"] = "/nonexistent/simostools-gone.csv"
    assert err_code(call("analyze_logs", **log_params)) == ErrorCode.FILE_NOT_FOUND.value


def test_analyze_logs_requires_a_non_empty_list():
    assert err_code(call("analyze_logs", logs=[])) == ErrorCode.BAD_PARAMS.value
    assert err_code(call("analyze_logs", logs="nope")) == ErrorCode.BAD_PARAMS.value
    assert err_code(call("analyze_logs", logs=["nope"])) == ErrorCode.BAD_PARAMS.value
    assert err_code(call("analyze_logs")) == ErrorCode.BAD_PARAMS.value


def test_analyze_logs_unparseable_csv_is_an_analysis_error(tmp_path):
    junk = tmp_path / "simostools-junk.csv"
    junk.write_text("")                       # no header row at all
    env = call("analyze_logs", logs=[{"log_path": str(junk), "log_sha256": _sha256(junk)}])
    assert err_code(env) == ErrorCode.ANALYSIS_ERROR.value
    assert "Traceback" not in json.dumps(env)


def test_analyze_logs_display_name_labels_the_pulls(log_params: dict):
    """The app's copy is content-addressed, so the name a person sees must travel."""
    for entry in log_params["logs"]:
        entry["display_name"] = "my drive.csv" if entry is log_params["logs"][0] else "second.csv"
    result = ok_result(call("analyze_logs", **log_params))
    names = {log["name"] for log in result["logs"]}
    assert "my drive.csv" in names
    assert {pull["file"] for pull in result["pulls"]} <= names


def test_analyze_logs_runs_cal_checks_when_a_bin_is_supplied(log_params: dict, files: dict):
    """With a bin + XDF the two calibration-aware checks run instead of skipping."""
    result = ok_result(call("analyze_logs", **log_params, **files))
    assert result["cal_resolved"] is True
    assert {"boost_cal", "boost_p0234"} <= set(result["ran"])


def test_analyze_logs_is_deterministic(log_params: dict):
    first = dispatch(json.dumps(
        {"bridge_version": BRIDGE_VERSION, "op": "analyze_logs", "params": log_params}
    ))
    second = dispatch(json.dumps(
        {"bridge_version": BRIDGE_VERSION, "op": "analyze_logs", "params": log_params}
    ))
    assert first == second


# --------------------------------------------------------------------------- #
# log_overlay — read-only, sessionless, and provably inert (U3 / AE1-AE3)
# --------------------------------------------------------------------------- #
def test_log_overlay_returns_pulls_with_both_boost_traces(log_params: dict):
    result = ok_result(call("log_overlay", **log_params))

    assert result["available"] is True
    assert result["pulls"], "the synthetic logs contain pulls"
    drawn = [p for p in result["pulls"] if p["drawn"]]
    assert drawn, "at least one pull should draw"

    sources = {s["source"] for s in drawn[0]["series"]}
    assert sources == {"boost", "boost_sp"}, "actual and setpoint, both"
    for series in drawn[0]["series"]:
        assert series["segments"]
        for seg in series["segments"]:
            assert len(seg["x"]) == len(seg["y"])
            assert all(isinstance(v, float) for v in seg["y"])


def test_log_overlay_carries_the_pull_chooser_fields(log_params: dict):
    """Gear, rpm span and duration — what the pull list shows, engine-formatted."""
    result = ok_result(call("log_overlay", **log_params))

    pull = result["pulls"][0]
    assert pull["gear"] == 3 and pull["gear_resolved"] is True
    assert pull["rpm_min"] < pull["rpm_max"]
    assert pull["duration_s"] > 0
    assert pull["file"]


def test_log_overlay_needs_no_session(log_params: dict):
    """Reading a datalog has nothing to do with holding an open edit session."""
    bridge.reset()   # no sessions exist at all
    assert ok_result(call("log_overlay", **log_params))["pulls"]

    env = call("log_overlay", logs=[])
    assert err_code(env) == ErrorCode.BAD_PARAMS.value


def test_log_overlay_attributes_the_same_gear_under_both_headers(tmp_path):
    """AE2: `Gear ()` and `Gear (gear)` logs of one pull overlay identically.

    The two header forms differ by a fixed offset, and the offset is the log
    layer's job — so the same physical pull logged either way must attribute the
    same gear *and* survive the gear trim to the same samples. If the offset ever
    leaked into the overlay path, this is where it would show.
    """
    from tests.faultinject import PullSpec, build_folder

    def overlay_for(folder, header, logged_gear):
        build_folder(
            folder, [PullSpec(gear=logged_gear)],
            gear_header=header, wastegate=True, ign_table=True,
        )
        csvs = sorted(folder.glob("simostools-*.csv"))
        return ok_result(call("log_overlay", logs=[
            {"log_path": str(c), "log_sha256": _sha256(c)} for c in csvs
        ]))

    actual = overlay_for(tmp_path / "actual", "Gear (gear)", 3.0)
    zero_indexed = overlay_for(tmp_path / "zero", "Gear ()", 2.0)

    for result in (actual, zero_indexed):
        assert result["pulls"][0]["gear"] == 3, "both resolve to real 3rd gear"

    def traces(result):
        return {
            s["source"]: [seg["y"] for seg in s["segments"]]
            for s in result["pulls"][0]["series"]
        }

    assert traces(actual) == traces(zero_indexed)


def test_log_overlay_trims_samples_from_a_different_gear(tmp_path):
    """The gear trim drops the samples the DSG's early flip mislabels."""
    from tests.faultinject import PullSpec, build_folder

    folder = tmp_path / "flip"
    build_folder(folder, [PullSpec(gear=3.0)], wastegate=True, ign_table=True)
    csv_path = sorted(folder.glob("simostools-*.csv"))[0]

    rows = csv_path.read_text().splitlines()
    header = rows[0].split(",")
    gear_col = header.index("Gear (gear)")
    # Flip the last quarter of the file to 4th, as the gear channel does before
    # the shift actually lands.
    flipped = rows[: 1 + int(len(rows[1:]) * 0.75)]
    for row in rows[1 + int(len(rows[1:]) * 0.75):]:
        cells = row.split(",")
        cells[gear_col] = "4"
        flipped.append(",".join(cells))
    csv_path.write_text("\n".join(flipped) + "\n")

    result = ok_result(call("log_overlay", logs=[
        {"log_path": str(csv_path), "log_sha256": _sha256(csv_path)}
    ]))
    drawn = [p for p in result["pulls"] if p["drawn"]]
    assert drawn, "the 3rd-gear part of the pull still draws"
    for pull in drawn:
        assert pull["gear"] == 3
        samples = sum(len(seg["x"]) for s in pull["series"] for seg in s["segments"])
        assert samples < pull["n_samples"] * len(pull["series"]), (
            "the 4th-gear tail must not reach the overlay"
        )


def test_log_overlay_unreadable_csv_is_an_analysis_error(tmp_path):
    """A CSV the log layer cannot parse fails loud, with a stable code."""
    empty = tmp_path / "simostools-empty.csv"
    empty.write_text("")
    env = call("log_overlay", logs=[
        {"log_path": str(empty), "log_sha256": _sha256(empty)}
    ])
    assert err_code(env) == ErrorCode.ANALYSIS_ERROR.value


def test_log_overlay_says_why_a_readable_log_cannot_draw(tmp_path):
    """A parseable log missing the boost channels reports *which* are missing.

    Not an error: the file was read fine, it simply has nothing to draw with.
    Without ambient pressure there is no honest baseline to zero gauge boost
    against, and the app needs to say that rather than show an empty canvas.
    """
    thin = tmp_path / "simostools-thin.csv"
    thin.write_text("Time,Engine Speed (rpm)\n0,1000\n0.05,1100\n")
    result = ok_result(call("log_overlay", logs=[
        {"log_path": str(thin), "log_sha256": _sha256(thin)}
    ]))

    assert result["available"] is False
    assert set(result["missing_channels"]) == {"put", "put_sp", "ambient_press"}
    assert result["pulls"] == []


def test_log_overlay_rejects_a_changed_file(log_params: dict):
    target = Path(log_params["logs"][0]["log_path"])
    target.write_text(target.read_text() + "0,0,0\n")
    assert err_code(call("log_overlay", **log_params)) == ErrorCode.HASH_MISMATCH.value


# --------------------------------------------------------------------------- #
# limiters / lambda_fl — the domain-screen read+edit pairs (U3)
# --------------------------------------------------------------------------- #
def test_limiters_reads_the_speed_quartet_on_a_base_session(session: str):
    result = ok_result(call("limiters", session_id=session))

    assert len(result["speed_limiter"]) == 4
    for scalar in result["speed_limiter"]:
        assert scalar["value"] == pytest.approx(200.0)
        assert scalar["units"] == "km/h"
        assert scalar["owner"], "the quartet is domain-owned"
    # No switch patch on a base-only session, so no trio to show.
    assert result["rev_limits"] is None
    assert result["launch_control"] is None


def test_limiters_edit_writes_the_whole_quartet(session: str):
    result = ok_result(call(
        "limiters_edit", session_id=session, speed_limiter_kmh=250.0,
        intent="raise the road-speed limiter",
    ))

    assert len(result["entries"]) == 4
    for scalar in result["limiters"]["speed_limiter"]:
        assert scalar["value"] == pytest.approx(250.0)
    assert result["can_undo"] is True


def test_limiters_edit_undo_restores_every_quartet_scalar(session: str):
    ok_result(call("limiters_edit", session_id=session, speed_limiter_kmh=250.0))
    ok_result(call("undo", session_id=session))

    for scalar in ok_result(call("limiters", session_id=session))["speed_limiter"]:
        assert scalar["value"] == pytest.approx(200.0)


def test_limiters_edit_refuses_an_unencodable_speed(session: str):
    env = call("limiters_edit", session_id=session, speed_limiter_kmh=600.0)
    assert err_code(env) == ErrorCode.EDIT_REJECTED.value

    for scalar in ok_result(call("limiters", session_id=session))["speed_limiter"]:
        assert scalar["value"] == pytest.approx(200.0), "a refusal writes nothing"


def test_limiters_edit_needs_something_to_change(session: str):
    assert err_code(call("limiters_edit", session_id=session)) == ErrorCode.BAD_PARAMS.value


def test_rev_limits_need_the_patch_space(session: str):
    env = call("limiters_edit", session_id=session, rev_limits={"soft": 100})
    assert err_code(env) == ErrorCode.TUNE_ERROR.value


def test_limiters_reads_and_writes_the_trio_on_a_patched_session(boost_session: str):
    before = ok_result(call("limiters", session_id=boost_session))
    assert [s["name"] for s in before["rev_limits"]] == [
        "rev_limit_soft", "rev_limit_medium", "rev_limit_hard",
    ]
    assert [s["name"] for s in before["launch_control"]] == [
        "lc_limiter_timing", "lc_release_speed",
    ]

    result = ok_result(call(
        "limiters_edit", session_id=boost_session,
        rev_limits={"soft": 100, "medium": 200, "hard": 300},
    ))
    assert len(result["entries"]) == 3
    assert [s["value"] for s in result["limiters"]["rev_limits"]] == [100.0, 200.0, 300.0]


def test_a_backwards_trio_is_rejected_and_writes_nothing(boost_session: str):
    ok_result(call(
        "limiters_edit", session_id=boost_session,
        rev_limits={"soft": 100, "medium": 200, "hard": 300},
    ))

    env = call("limiters_edit", session_id=boost_session, rev_limits={"soft": 250})
    assert err_code(env) == ErrorCode.EDIT_REJECTED.value
    assert "escalate" in env["error"]["message"]

    after = ok_result(call("limiters", session_id=boost_session))
    assert [s["value"] for s in after["rev_limits"]] == [100.0, 200.0, 300.0]


def test_lambda_fl_reads_the_map_with_the_engine_s_own_bound(session: str):
    result = ok_result(call("lambda_fl", session_id=session))

    table = result["table"]
    assert table["shape"] == [8, 12]
    assert table["y_axis"]["values"][0] == 0.0, "rows are time at full load"
    assert table["x_axis"]["units"] == "rpm"
    # The band the screen draws must be the bound the engine refuses on.
    assert result["lean_max"] == 1.0
    assert result["rich_min"] == 0.5


def test_lambda_fl_edit_writes_one_row_and_reports_encoding(session: str):
    result = ok_result(call(
        "lambda_fl_edit", session_id=session, values=0.85, row=4,
        intent="add full-load enrichment 30 s in",
    ))

    assert len(result["encoded"]) == 12
    assert all(abs(v - 0.85) < 1e-3 for v in result["encoded"])
    assert result["requested"] == [0.85] * 12
    assert result["entry"]["cells_changed"] == 12

    grid = ok_result(call("lambda_fl", session_id=session))["table"]["values"]
    assert all(abs(v - 0.85) < 1e-3 for v in grid[4])
    assert all(v == 1.0 for row in grid[:4] + grid[5:] for v in row)


def test_lambda_fl_edit_selects_a_row_by_seconds(session: str):
    ok_result(call("lambda_fl_edit", session_id=session, values=0.82, seconds=30))

    detail = ok_result(call("lambda_fl", session_id=session))["table"]
    row = detail["y_axis"]["values"].index(30.0)
    assert all(abs(v - 0.82) < 1e-3 for v in detail["values"][row])


def test_lambda_fl_edit_refuses_a_lean_setpoint(session: str):
    """AE7 at the boundary: 1.00 is refused end to end, and nothing is written."""
    env = call("lambda_fl_edit", session_id=session, values=1.0, row=4)
    assert err_code(env) == ErrorCode.EDIT_REJECTED.value
    assert "at or above lambda" in env["error"]["message"]

    grid = ok_result(call("lambda_fl", session_id=session))["table"]["values"]
    assert all(v == 1.0 for row in grid for v in row), "the map is untouched"
    assert ok_result(call("journal", session_id=session))["entries"] == []


def test_lambda_fl_edit_refusal_leaves_no_undo_point(session: str):
    call("lambda_fl_edit", session_id=session, values=1.2, row=0)
    assert ok_result(call("undo", session_id=session))["done"] is False


def test_the_owned_tables_are_not_offered_by_the_generic_catalog(session: str):
    """The generic grid must not offer what only a domain call may write."""
    names = {t["name"] for t in ok_result(call("catalog", session_id=session))["tables"]}

    assert "lambda_full_load" not in names
    assert not {
        "speed_limiter_level1", "speed_limiter_level2",
        "speed_limiter_level3", "speed_limiter_inactive",
    } & names
    # The dual-path pedal maps and the FL context tables stay browsable.
    assert "pedal_dct_high" in names
    assert "lambda_full_load_iat" in names


def test_a_generic_edit_to_an_owned_table_is_refused(session: str):
    env = call(
        "edit", session_id=session, name="lambda_full_load", op="set",
        selection={"kind": "row", "args": [4]}, value=0.85,
    )
    assert err_code(env) == ErrorCode.EDIT_REJECTED.value


def test_the_new_ops_did_not_bump_the_bridge_version():
    """Additive ops keep the version: an older app simply never names them."""
    info = ok_result(call("bridge_info"))
    assert info["bridge_version"] == 1
    for op in ("log_overlay", "limiters", "limiters_edit", "lambda_fl", "lambda_fl_edit"):
        assert op in info["ops"]


# --------------------------------------------------------------------------- #
# the standstill rev cap, over the bridge
# --------------------------------------------------------------------------- #
def test_limiters_reports_the_standstill_cap_with_the_limiter_it_sits_under(
    session: str,
):
    """The cap alone is unreadable: 3808 means nothing without the 6816."""
    result = ok_result(call("limiters", session_id=session))

    assert len(result["static_rev_limit"]) == 4
    for scalar in result["static_rev_limit"]:
        assert scalar["value"] == pytest.approx(3808.0)
        assert scalar["units"] == "rpm"
        assert "static_rev_limit" in scalar["owner"]
    assert result["engine_rev_limit"] == pytest.approx(6816.0)


def test_limiters_edit_raises_the_standstill_cap_to_the_limiter(session: str):
    result = ok_result(call(
        "limiters_edit", session_id=session, static_rev_limit_rpm=6816.0,
        intent="rev to the limiter while stopped",
    ))

    assert len(result["entries"]) == 4
    for scalar in result["limiters"]["static_rev_limit"]:
        assert scalar["value"] == pytest.approx(6816.0)
    # The engine's own limiter is untouched — the whole point.
    assert result["limiters"]["engine_rev_limit"] == pytest.approx(6816.0)


def test_a_standstill_cap_above_the_limiter_is_rejected(session: str):
    env = call("limiters_edit", session_id=session, static_rev_limit_rpm=7200.0)
    assert err_code(env) == ErrorCode.EDIT_REJECTED.value
    assert "rev limiter" in env["error"]["message"]

    for scalar in ok_result(call("limiters", session_id=session))["static_rev_limit"]:
        assert scalar["value"] == pytest.approx(3808.0), "a refusal writes nothing"


def test_the_engines_rev_limiter_has_no_write_path_at_all(session: str):
    """Readable, browsable, and refused by the generic editor."""
    names = {t["name"] for t in ok_result(call("catalog", session_id=session))["tables"]}
    assert "engine_speed_limit_vvl0" not in names

    detail = ok_result(call(
        "table_detail", session_id=session, name="engine_speed_limit_vvl0",
    ))["table"]
    assert "no write path" in detail["owner"]

    env = call(
        "edit", session_id=session, name="engine_speed_limit_vvl0", op="set",
        selection={"kind": "all"}, value=7200.0,
    )
    assert err_code(env) == ErrorCode.EDIT_REJECTED.value
