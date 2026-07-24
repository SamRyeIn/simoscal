"""Contract tests for the versioned Python↔Kotlin bridge (Quick Edit V6).

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
