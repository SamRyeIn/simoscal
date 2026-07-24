"""simoscal.bridge — the one versioned boundary the Android app calls (Quick Edit V6).

Everything the phone does to a bin goes through :func:`dispatch`. Kotlin owns the
UI, the lifecycle, and the files; this module owns every byte decision. The
contract is deliberately narrow and deliberately dumb at the edges:

* **JSON in, JSON out.** ``dispatch`` takes one request string and returns one
  response string. No ``PyObject``, no numpy array, and no Python exception ever
  crosses into Kotlin — a value that reached the UI un-serialized would be a
  value the cross-runtime golden gate never compared. The result is assembled
  into plain JSON *inside Python*, the same discipline the V0 parity payload uses.

* **Files by verified path + hash, never bytes.** Every op that names a file
  takes ``<name>_path`` and ``<name>_sha256``. The bridge stats the path and
  hashes the bytes before it opens anything; a missing file or a hash that does
  not match the one Kotlin recorded when it copied the file into app-private
  storage fails loud (``FILE_NOT_FOUND`` / ``HASH_MISMATCH``) rather than editing
  a file that changed underfoot. Base64-in-JSON is never used — a multi-megabyte
  bin stays a private file.

* **A versioned envelope.** Request and response both carry ``bridge_version``. A
  request from a newer or older app than this engine understands is rejected with
  ``VERSION_MISMATCH`` — it never gets far enough to act on a field it might read
  differently.

* **Stable error codes, private tracebacks.** Every failure maps to one of
  :class:`ErrorCode`'s stable strings plus a plain-language ``message`` and an
  ``advanced`` detail for a "show details" pane. A traceback is logged to the
  private ``simoscal.bridge`` logger and **never** placed in the response.

* **One session at a time touches bytes.** A live Quick Edit session is a
  ``Tune`` held in a process-global registry keyed by an opaque ``session_id``;
  Kotlin holds only the string. ``dispatch`` takes a non-blocking lock for the
  duration of a call, so a second request that races the first is rejected with
  ``BUSY`` rather than mutating a session mid-op. Kotlin also serializes calls on
  a single dispatcher; this guard is the last line, not the only one.

Why *report* is not its own op: a report is only ever the atomic product of the
``build`` op's gate run. Re-deriving a report from the live journal after the
gates ran is exactly the drift CR-20260724-02 closed, so the bridge never offers
a path to it — ``build`` returns the verified report or nothing does.

Everything the app is allowed to do to a bin is in :data:`OPS`. If it is not
there, it is not a thing the phone can do.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import uuid
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np

from . import __version__
from .preflight import Verdict, preflight
from .tune import (
    SC8S50,
    EditOp,
    EditRejected,
    ProfileResolutionError,
    Selection,
    SessionHistory,
    Tune,
    TuneError,
    apply_op,
    boost_curve_model,
    build_revision,
    catalog,
    restore_session,
    serialize_session,
    slot_curve_result,
    table_detail,
)
from .tune.domains.switchpatch import PATCH_SPACE
from .tune.profiles import PROFILES, SWITCH_PATCH_2933
from .tune.recovery import RecoveryError

#: Bump when the request/response contract changes shape. A request carrying a
#: different version is rejected rather than misread.
BRIDGE_VERSION = 1

_log = logging.getLogger("simoscal.bridge")


# --------------------------------------------------------------------------- #
# stable error codes
# --------------------------------------------------------------------------- #
class ErrorCode(str, Enum):
    """The closed set of failure codes Kotlin switches on.

    Stable strings, not messages: a UI maps a code to a localized string and a
    recovery action, so the wording of ``message`` can change without breaking
    the app. Every raised :class:`BridgeError` carries one of these.
    """

    BAD_REQUEST = "BAD_REQUEST"          # not JSON, or missing op/params
    VERSION_MISMATCH = "VERSION_MISMATCH"  # request bridge_version != ours
    UNKNOWN_OP = "UNKNOWN_OP"            # op name not in OPS
    BAD_PARAMS = "BAD_PARAMS"            # a required param is missing/ill-typed
    FILE_NOT_FOUND = "FILE_NOT_FOUND"    # a path+hash file does not exist
    HASH_MISMATCH = "HASH_MISMATCH"      # file changed since Kotlin hashed it
    PREFLIGHT_BLOCKED = "PREFLIGHT_BLOCKED"  # (reserved) not used to gate here
    UNKNOWN_SESSION = "UNKNOWN_SESSION"  # session_id not in the registry
    EDIT_REJECTED = "EDIT_REJECTED"      # a guard/selection refused the edit
    RECOVERY_ERROR = "RECOVERY_ERROR"    # a recovery record could not be restored
    PROFILE_ERROR = "PROFILE_ERROR"      # the XDF does not resolve the profile
    TUNE_ERROR = "TUNE_ERROR"            # opening/editing the tune failed loudly
    BUSY = "BUSY"                        # another call holds the engine lock
    INTERNAL = "INTERNAL"                # an unexpected exception (logged)


class BridgeError(Exception):
    """A failure that maps to a stable :class:`ErrorCode` and a UI-safe message."""

    def __init__(self, code: ErrorCode, message: str, advanced: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.advanced = advanced


# --------------------------------------------------------------------------- #
# JSON safety — nothing numpy or dataclass reaches the wire un-normalized
# --------------------------------------------------------------------------- #
def _jsonify(obj: Any) -> Any:
    """Recursively convert a value into JSON-safe primitives.

    Dataclasses become dicts, numpy scalars/arrays become Python numbers/lists,
    Enums become their value, Paths become strings, and tuples become lists.
    Every number that crosses the boundary is a plain ``float``/``int`` so Kotlin
    never receives a numpy object. Raises rather than silently dropping a type it
    does not recognize — an un-serializable value is a contract bug, not a value
    to hide.
    """
    if obj is None or isinstance(obj, (str, bool)):
        return obj
    if isinstance(obj, (int, np.integer)):
        return int(obj)
    if isinstance(obj, (float, np.floating)):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return _jsonify(obj.tolist())
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, Path):
        return str(obj)
    if is_dataclass(obj) and not isinstance(obj, type):
        return {k: _jsonify(v) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {str(k): _jsonify(v) for k, v in obj.items()}
    if isinstance(obj, (set, frozenset)):
        return [_jsonify(v) for v in sorted(obj, key=repr)]
    if isinstance(obj, (list, tuple)):
        return [_jsonify(v) for v in obj]
    raise TypeError(f"bridge cannot serialize {type(obj).__name__}")


# --------------------------------------------------------------------------- #
# param + file helpers
# --------------------------------------------------------------------------- #
def _require(params: dict, key: str) -> Any:
    if key not in params:
        raise BridgeError(ErrorCode.BAD_PARAMS, f"missing required parameter {key!r}")
    return params[key]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verified_path(params: dict, name: str, *, required: bool = True) -> Optional[Path]:
    """Resolve ``<name>_path`` and verify it against ``<name>_sha256``.

    The whole point of the path+hash contract: Kotlin copied a user file into
    app-private storage and recorded its hash; the bridge refuses to open the
    file unless the bytes still hash to that value. A missing hash is itself a
    bad request — we never open an unverified path.
    """
    path_key, sha_key = f"{name}_path", f"{name}_sha256"
    if path_key not in params:
        if required:
            raise BridgeError(ErrorCode.BAD_PARAMS, f"missing required parameter {path_key!r}")
        return None
    raw_path = _require(params, path_key)
    expected = _require(params, sha_key)
    if not isinstance(raw_path, str) or not isinstance(expected, str):
        raise BridgeError(
            ErrorCode.BAD_PARAMS,
            f"{path_key!r} and {sha_key!r} must be strings",
        )
    path = Path(raw_path)
    if not path.is_file():
        raise BridgeError(
            ErrorCode.FILE_NOT_FOUND,
            f"the {name} file is not where the app said it was",
            advanced=f"no file at {path}",
        )
    actual = _sha256_file(path)
    if actual != expected:
        raise BridgeError(
            ErrorCode.HASH_MISMATCH,
            f"the {name} file changed since it was imported",
            advanced=f"{path}: expected {expected[:12]}…, found {actual[:12]}…",
        )
    return path


def _profile(name: str):
    if not isinstance(name, str):
        raise BridgeError(ErrorCode.BAD_PARAMS, "profile must be a string")
    try:
        return PROFILES[name]
    except KeyError:
        raise BridgeError(
            ErrorCode.PROFILE_ERROR,
            f"unknown profile {name!r}",
            advanced=f"known profiles: {', '.join(sorted(PROFILES))}",
        )


# --------------------------------------------------------------------------- #
# session registry
# --------------------------------------------------------------------------- #
class _Session:
    """One live Quick Edit session: the tune, its undo history, and provenance."""

    __slots__ = ("id", "tune", "history", "provenance")

    def __init__(self, session_id: str, tune: Tune, provenance: dict) -> None:
        self.id = session_id
        self.tune = tune
        self.history = SessionHistory(tune)
        self.provenance = provenance


_SESSIONS: dict[str, _Session] = {}
#: Non-blocking: a call that cannot take it immediately is rejected BUSY rather
#: than queued, so a second request can never mutate a session mid-op. Kotlin
#: serializes calls too; this is the engine-side backstop.
_LOCK = threading.Lock()


def _session(params: dict) -> _Session:
    sid = _require(params, "session_id")
    if not isinstance(sid, str):
        raise BridgeError(ErrorCode.BAD_PARAMS, "session_id must be a string")
    sess = _SESSIONS.get(sid)
    if sess is None:
        raise BridgeError(
            ErrorCode.UNKNOWN_SESSION,
            "that edit session is no longer open",
            advanced=f"no session {sid!r}; it may have been closed or the app restarted",
        )
    return sess


def _history_state(sess: _Session) -> dict:
    return {"can_undo": sess.history.can_undo, "can_redo": sess.history.can_redo}


def _entry_summary(entry) -> dict:
    """A journal entry as flat UI text — never the numpy before/after arrays."""
    return {
        "label": entry.label,
        "name": entry.name,
        "kind": entry.kind,
        "verdict": entry.verdict,
        "units": getattr(entry, "units", "") or "",
        "intent": entry.intent,
        "detail": entry.detail or "",
        "warning": entry.warning or "",
    }


# --------------------------------------------------------------------------- #
# ops
# --------------------------------------------------------------------------- #
def _op_bridge_info(params: dict) -> dict:
    """Handshake: the app checks it speaks this engine's version before anything."""
    return {
        "bridge_version": BRIDGE_VERSION,
        "engine_version": __version__,
        "ops": sorted(OPS),
    }


def _op_preflight(params: dict) -> dict:
    """Decide whether a bin + XDF is a safely-editable SC8S50 bin. Read-only."""
    bin_path = _verified_path(params, "bin")
    xdf_path = _verified_path(params, "xdf")
    patch_xdf = _verified_path(params, "switch_patch_xdf", required=False)
    verdict: Verdict = preflight(bin_path, xdf_path, switch_patch_xdf=patch_xdf)
    return _jsonify(verdict)


def _op_session_create(params: dict) -> dict:
    """Open a live edit session over a preflight-passed bin.

    v1 receives an already-patched bin, so the switch-patch space is opened via
    ``extra_spaces`` (no BinToolz re-apply) exactly as the V0 parity payload and
    Quick Edit's import flow do. Supply ``switch_patch_xdf_*`` to get the boost
    editor's ``patch`` space; omit it for a base-only session.
    """
    profile = _profile(params.get("profile", "SC8S50"))
    bin_path = _verified_path(params, "bin")
    xdf_path = _verified_path(params, "xdf")
    patch_xdf = _verified_path(params, "switch_patch_xdf", required=False)

    verdict = preflight(bin_path, xdf_path, switch_patch_xdf=patch_xdf)
    if not verdict.ok_to_edit:
        raise BridgeError(
            ErrorCode.PREFLIGHT_BLOCKED,
            verdict.summary,
            advanced="; ".join(verdict.reasons),
        )
    if patch_xdf is not None and verdict.switch_patch_present is not True:
        raise BridgeError(
            ErrorCode.PREFLIGHT_BLOCKED,
            "The switch-patch tables are not present in this bin.",
            advanced=(
                "A switch-patch XDF was supplied, but compatibility preflight "
                "did not find the patch bytes. The boost editor cannot open "
                "those addresses on an unpatched bin."
            ),
        )

    extra_spaces = {}
    if patch_xdf is not None:
        extra_spaces[PATCH_SPACE] = (SWITCH_PATCH_2933, patch_xdf)

    try:
        tune = Tune.open(profile, xdf=xdf_path, bin=bin_path, extra_spaces=extra_spaces)
    except ProfileResolutionError as exc:
        raise BridgeError(
            ErrorCode.PROFILE_ERROR,
            "this XDF does not define every table the profile needs",
            advanced=str(exc),
        )
    except TuneError as exc:
        raise BridgeError(ErrorCode.TUNE_ERROR, "the tune could not be opened", advanced=str(exc))
    if patch_xdf is not None:
        tune.switchpatch.require_sanity()

    sid = uuid.uuid4().hex
    provenance = {
        "profile": profile.name,
        "bin_path": str(bin_path),
        "bin_sha256": params["bin_sha256"],
        "xdf_path": str(xdf_path),
        "has_switch_patch": patch_xdf is not None,
    }
    _SESSIONS[sid] = _Session(sid, tune, provenance)
    return {"session_id": sid, "provenance": provenance, **_history_state(_SESSIONS[sid])}


def _op_session_recover(params: dict) -> dict:
    """Restore a session from a recovery record after a process kill.

    ``record`` is the JSON-safe dict :func:`simoscal.tune.serialize_session`
    produced (the app persisted it). The source bin and any XDFs are passed again
    as verified path+hash overrides — ``restore_session`` re-verifies the source
    bin's own hash against the record and rejects a mismatch itself, so a bin
    that changed since the session was saved cannot be restored onto.
    """
    record = _require(params, "record")
    if not isinstance(record, dict):
        raise BridgeError(ErrorCode.BAD_PARAMS, "record must be an object")

    source_bin = _verified_path(params, "source_bin")
    # xdf_paths: {space_name: {path, sha256}} — each verified before use.
    xdf_paths: dict[str, Path] = {}
    raw_xdfs = _require(params, "xdf_paths")
    if not isinstance(raw_xdfs, dict):
        raise BridgeError(ErrorCode.BAD_PARAMS, "xdf_paths must be an object")
    for space, spec in raw_xdfs.items():
        if not isinstance(spec, dict):
            raise BridgeError(ErrorCode.BAD_PARAMS, f"xdf_paths[{space!r}] must be an object")
        p = Path(_require(spec, "path"))
        if not p.is_file():
            raise BridgeError(
                ErrorCode.FILE_NOT_FOUND, f"the {space} XDF is missing", advanced=f"no file at {p}"
            )
        if _sha256_file(p) != _require(spec, "sha256"):
            raise BridgeError(
                ErrorCode.HASH_MISMATCH, f"the {space} XDF changed since import", advanced=str(p)
            )
        xdf_paths[space] = p

    source = record.get("source")
    if not isinstance(source, dict):
        raise BridgeError(ErrorCode.RECOVERY_ERROR, "the saved session has no source provenance")
    required_spaces = {"base", *(source.get("extra_spaces") or {}).keys()}
    missing_spaces = sorted(required_spaces - set(xdf_paths))
    if missing_spaces:
        raise BridgeError(
            ErrorCode.BAD_PARAMS,
            "verified XDF paths are required to recover a session",
            advanced=f"missing spaces: {', '.join(missing_spaces)}",
        )

    patch_xdf = xdf_paths.get(PATCH_SPACE)
    verdict = preflight(source_bin, xdf_paths["base"], switch_patch_xdf=patch_xdf)
    if not verdict.ok_to_edit or (
        PATCH_SPACE in required_spaces and verdict.switch_patch_present is not True
    ):
        raise BridgeError(
            ErrorCode.PREFLIGHT_BLOCKED,
            "The saved session's source files no longer pass compatibility preflight.",
            advanced=verdict.summary + " " + "; ".join(verdict.reasons),
        )

    try:
        tune = restore_session(record, source_bin=source_bin, xdf_paths=xdf_paths or None)
    except (RecoveryError, KeyError, TypeError, ValueError) as exc:
        raise BridgeError(ErrorCode.RECOVERY_ERROR, "the saved session could not be restored", advanced=str(exc))
    except (TuneError, ProfileResolutionError) as exc:
        raise BridgeError(ErrorCode.TUNE_ERROR, "the restored tune could not be opened", advanced=str(exc))

    sid = uuid.uuid4().hex
    provenance = {
        "profile": tune.space("base").profile.name,
        "bin_path": str(tune.source_bin),
        "bin_sha256": source["bin_sha256"],
        "recovered": True,
        "has_switch_patch": PATCH_SPACE in tune.spaces,
    }
    _SESSIONS[sid] = _Session(sid, tune, provenance)
    return {"session_id": sid, "provenance": provenance, **_history_state(_SESSIONS[sid])}


def _op_session_serialize(params: dict) -> dict:
    """Return a JSON-safe recovery record the app persists to survive a kill.

    This is the producer side of recovery: Kotlin stores the returned ``record``
    durably (Room/DataStore) after each committed edit, and hands it back to
    :func:`_op_session_recover` on restart. The record carries the source bin's
    hash and the ordered journal, not the bin bytes — recovery re-verifies the
    bin and replays the journal to reproduce the buffer exactly.
    """
    sess = _session(params)
    try:
        record = serialize_session(sess.tune, history=sess.history)
    except RecoveryError as exc:
        raise BridgeError(
            ErrorCode.RECOVERY_ERROR,
            "the session could not be saved for recovery",
            advanced=str(exc),
        )
    return {"record": record}


def _op_session_close(params: dict) -> dict:
    """Drop a session from the registry. Idempotent — closing twice is not an error."""
    sid = _require(params, "session_id")
    existed = _SESSIONS.pop(sid, None) is not None
    return {"closed": True, "existed": existed}


def _op_catalog(params: dict) -> dict:
    """Every editable table as read-only info, optionally for one space."""
    sess = _session(params)
    space = params.get("space")
    tables = catalog(sess.tune, space=space)
    return {"tables": _jsonify(tables)}


def _op_table_detail(params: dict) -> dict:
    """One table's full info, including its current decoded values."""
    sess = _session(params)
    name = _require(params, "name")
    space = params.get("space", "base")
    try:
        info = table_detail(sess.tune, name, space=space)
    except (TuneError, KeyError) as exc:
        raise BridgeError(ErrorCode.TUNE_ERROR, f"no such table {name!r}", advanced=str(exc))
    return {"table": _jsonify(info)}


def _selection_from(spec: Optional[dict]) -> Optional[Selection]:
    """Build a :class:`Selection` from ``{"kind": ..., "args": [...]}``.

    Mirrors the classmethods so the wire form names the same selections the
    editor offers: all / row / col / region / cells. An unknown kind is a bad
    request, not a silent whole-table edit.
    """
    if spec is None:
        return None
    if not isinstance(spec, dict):
        raise BridgeError(ErrorCode.BAD_PARAMS, "selection must be an object")
    kind = spec.get("kind")
    args = spec.get("args", [])
    try:
        if kind == "all":
            return Selection.all()
        if kind == "row":
            return Selection.row(int(args[0]))
        if kind == "col":
            return Selection.col(int(args[0]))
        if kind == "region":
            r0, r1, c0, c1 = args
            return Selection.region(int(r0), int(r1), int(c0), int(c1))
        if kind == "cells":
            return Selection.cells([(int(r), int(c)) for r, c in args])
    except (TypeError, ValueError, IndexError) as exc:
        raise BridgeError(ErrorCode.BAD_PARAMS, f"malformed selection args for kind {kind!r}", advanced=str(exc))
    raise BridgeError(ErrorCode.BAD_PARAMS, f"unknown selection kind {kind!r}")


def _op_edit(params: dict) -> dict:
    """Apply one generic edit op to a table, atomically, and record an undo point.

    On rejection (bad selection, non-reversible table, guard refusal) the table
    and journal are left untouched by ``apply_op`` and no undo point is
    committed — the failure is ``EDIT_REJECTED``, never a partial write.
    """
    sess = _session(params)
    name = _require(params, "name")
    op = _require(params, "op")
    space = params.get("space", "base")
    selection = _selection_from(params.get("selection"))
    value = params.get("value")
    array = params.get("array")
    intent = params.get("intent", "")

    try:
        op_enum = EditOp(op)
    except (TypeError, ValueError):
        raise BridgeError(ErrorCode.BAD_PARAMS, f"unknown edit op {op!r}")

    try:
        result = apply_op(
            sess.tune, name, op_enum, space=space,
            selection=selection, value=value, array=array, intent=intent,
        )
    except EditRejected as exc:
        raise BridgeError(ErrorCode.EDIT_REJECTED, str(exc))
    except (TypeError, ValueError) as exc:
        raise BridgeError(ErrorCode.BAD_PARAMS, "the edit parameters are malformed", advanced=str(exc))
    except (TuneError, KeyError) as exc:
        raise BridgeError(ErrorCode.TUNE_ERROR, f"the edit to {name!r} could not be applied", advanced=str(exc))

    sess.history.commit()
    return {
        "table": name,
        "space": space,
        "requested": _jsonify(result.requested),
        "encoded": _jsonify(result.encoded),
        "quantized": bool(result.quantized),
        "max_abs_quantization": result.max_abs_quantization(),
        "warning": result.warning or "",
        "entry": _entry_summary(result.entry),
        **_history_state(sess),
    }


def _op_boost_curve(params: dict) -> dict:
    """The whole per-slot boost editor model: rpm axis, five slot curves, base ceiling."""
    sess = _session(params)
    try:
        model = boost_curve_model(sess.tune)
    except (TuneError, KeyError, ValueError) as exc:
        raise BridgeError(
            ErrorCode.TUNE_ERROR,
            "this session has no switch-patch boost tables",
            advanced=str(exc),
        )
    return {"boost_curve": _jsonify(model)}


def _op_boost_edit(params: dict) -> dict:
    """Set one slot's boost cap (per-rpm list or a flat scalar), atomically.

    ``psi`` is a scalar for a flat cap or a per-rpm list. The below-base-ceiling
    guard and the psi floor live in the domain call; a guard refusal surfaces as
    ``EDIT_REJECTED`` and the reported ``encoded_psi`` shows where the floor bit.
    """
    sess = _session(params)
    slot = _require(params, "slot")
    psi = _require(params, "psi")
    intent = params.get("intent", "")
    try:
        slot = int(slot)
    except (TypeError, ValueError):
        raise BridgeError(ErrorCode.BAD_PARAMS, "slot must be an integer")

    try:
        result = slot_curve_result(sess.tune, slot, psi=psi, intent=intent)
    except (EditRejected, ValueError) as exc:
        # slot_curve raises a loud ValueError for its guard refusals (a cap at or
        # above the base ceiling, a mis-shaped rpm axis) — those are edits the
        # engine refused, the boost analog of apply_op's EditRejected.
        raise BridgeError(ErrorCode.EDIT_REJECTED, str(exc))
    except (TuneError, KeyError) as exc:
        raise BridgeError(ErrorCode.TUNE_ERROR, f"the boost edit to slot {slot} could not be applied", advanced=str(exc))

    sess.history.commit()
    return {
        "slot": result.slot,
        "requested_psi": _jsonify(result.requested_psi),
        "encoded_psi": _jsonify(result.encoded_psi),
        "floored": bool(result.floored),
        "entry": _entry_summary(result.entry),
        **_history_state(sess),
    }


def _op_undo(params: dict) -> dict:
    """Step back one committed edit. ``done`` is false when there is nothing to undo."""
    sess = _session(params)
    done = False
    if sess.history.can_undo:
        sess.history.undo()
        done = True
    return {"done": done, **_history_state(sess)}


def _op_redo(params: dict) -> dict:
    """Step forward one undone edit. ``done`` is false when there is nothing to redo."""
    sess = _session(params)
    done = False
    if sess.history.can_redo:
        sess.history.redo()
        done = True
    return {"done": done, **_history_state(sess)}


def _op_build(params: dict) -> dict:
    """Run the full gate chain and return the verified report (or the failed one).

    For v1 the imported source bin is both the byte-audit reference and the
    baseline, so ``reference_bin`` is verified path+hash here. A failed build is
    a report with ``verified`` false and ``share_path`` null — never an
    exception. This is also the *only* place a report comes from: there is no op
    that re-derives a report from the live journal after the gates ran.
    """
    sess = _session(params)
    revision = _require(params, "revision")
    staging_dir = Path(_require(params, "staging_dir"))
    reference_bin = _verified_path(params, "reference_bin")
    bin_name = params.get("bin_name", "")
    source_bin = _verified_path(params, "source_bin")

    imported_sha = sess.provenance["bin_sha256"]
    if params["reference_bin_sha256"] != imported_sha or params["source_bin_sha256"] != imported_sha:
        raise BridgeError(
            ErrorCode.HASH_MISMATCH,
            "Quick Edit must build against the bin imported for this session.",
            advanced=(
                f"session source {imported_sha[:12]}…; "
                f"reference {params['reference_bin_sha256'][:12]}…; "
                f"source {params['source_bin_sha256'][:12]}…"
            ),
        )

    report = build_revision(
        sess.tune, revision,
        staging_dir=staging_dir,
        reference_bin=reference_bin,
        bin_name=bin_name,
        source_bin=source_bin,
    )
    return {"report": report.to_dict()}


#: The closed op table. If an op is not here, it is not something the phone can do.
OPS: dict[str, Callable[[dict], dict]] = {
    "bridge_info": _op_bridge_info,
    "preflight": _op_preflight,
    "session_create": _op_session_create,
    "session_serialize": _op_session_serialize,
    "session_recover": _op_session_recover,
    "session_close": _op_session_close,
    "catalog": _op_catalog,
    "table_detail": _op_table_detail,
    "edit": _op_edit,
    "boost_curve": _op_boost_curve,
    "boost_edit": _op_boost_edit,
    "undo": _op_undo,
    "redo": _op_redo,
    "build": _op_build,
}


# --------------------------------------------------------------------------- #
# envelope
# --------------------------------------------------------------------------- #
def _ok(op: str, result: dict, request_id: Any) -> dict:
    env: dict[str, Any] = {"bridge_version": BRIDGE_VERSION, "ok": True, "op": op, "result": result}
    if request_id is not None:
        env["request_id"] = request_id
    return env


def _err(op: str, code: ErrorCode, message: str, advanced: str, request_id: Any) -> dict:
    env: dict[str, Any] = {
        "bridge_version": BRIDGE_VERSION,
        "ok": False,
        "op": op,
        "error": {"code": code.value, "message": message, "advanced": advanced},
    }
    if request_id is not None:
        env["request_id"] = request_id
    return env


def dispatch_obj(request: dict) -> dict:
    """Dispatch a parsed request dict to a response dict.

    The typed core of :func:`dispatch`. Kept separate so tests (and the host
    contract runner) can work with objects and skip the JSON string round-trip.
    """
    op = request.get("op")
    request_id = request.get("request_id")

    if not isinstance(op, str):
        return _err("", ErrorCode.BAD_REQUEST, "request has no op", "", request_id)

    version = request.get("bridge_version")
    if version != BRIDGE_VERSION:
        return _err(
            op, ErrorCode.VERSION_MISMATCH,
            "the app and the engine speak different bridge versions",
            f"request bridge_version={version!r}, engine={BRIDGE_VERSION}",
            request_id,
        )

    handler = OPS.get(op)
    if handler is None:
        return _err(op, ErrorCode.UNKNOWN_OP, f"unknown operation {op!r}", "", request_id)

    params = request.get("params", {})
    if not isinstance(params, dict):
        return _err(op, ErrorCode.BAD_REQUEST, "params must be an object", "", request_id)

    # One caller in the engine at a time. A racing request is rejected, not
    # queued, so it can never mutate a session mid-op.
    if not _LOCK.acquire(blocking=False):
        return _err(op, ErrorCode.BUSY, "the engine is busy with another request", "", request_id)
    try:
        result = handler(params)
        return _ok(op, result, request_id)
    except BridgeError as exc:
        return _err(op, exc.code, exc.message, exc.advanced, request_id)
    except Exception as exc:  # noqa: BLE001 - the boundary maps everything to a code
        # The traceback is a debugging aid for us, never a payload for the UI.
        _log.exception("unhandled error in bridge op %r", op)
        return _err(
            op, ErrorCode.INTERNAL,
            "the engine hit an unexpected error",
            f"{type(exc).__name__}: {exc}",
            request_id,
        )
    finally:
        _LOCK.release()


def dispatch(request: str) -> str:
    """The one entry point Kotlin calls: JSON request string → JSON response string.

    A response is always a valid JSON envelope, even for a request that was not
    valid JSON — the app can always parse ``ok`` and ``error.code``. Serialized
    with sorted keys and compact separators so identical calls produce identical
    bytes (the cross-runtime golden gate compares these).
    """
    try:
        parsed = json.loads(request)
        if not isinstance(parsed, dict):
            raise ValueError("request must be a JSON object")
    except (ValueError, TypeError) as exc:
        env = _err("", ErrorCode.BAD_REQUEST, "the request was not valid JSON", str(exc), None)
        return json.dumps(env, sort_keys=True, separators=(",", ":"))

    env = dispatch_obj(parsed)
    return json.dumps(env, sort_keys=True, separators=(",", ":"))


def reset() -> None:
    """Drop every open session. For tests and a clean process re-init only."""
    _SESSIONS.clear()
