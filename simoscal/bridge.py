"""simoscal.bridge — the one versioned boundary an embedded client calls (protocol V6).

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

* **One session at a time touches bytes.** A live edit session is a
  ``Tune`` held in a process-global registry keyed by an opaque ``session_id``;
  Kotlin holds only the string. ``dispatch`` takes a non-blocking lock for the
  duration of a call, so a second request that races the first is rejected with
  ``BUSY`` rather than mutating a session mid-op. Kotlin also serializes calls on
  a single dispatcher; this guard is the last line, not the only one.

Why some screens get their own ops and others do not: an op exists here when the
write it performs carries an invariant no single-table grid edit can see. The
per-slot boost curves, the road-speed quartet, the cylinder-cut trio and the
lambda lean bound each qualify, so each has a domain-routed op. The pedal maps
do not — they are ordinary independent grids — so the Pedal screen is drawn over
``catalog``/``table_detail``/``edit`` and gets no op of its own. A screen is not
a reason for an op; an invariant is.

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
from .tune.journal import VERDICT_SUPERSEDED
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
    ANALYSIS_ERROR = "ANALYSIS_ERROR"    # a datalog could not be parsed or analyzed
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
    """One live edit session: the tune, its undo history, and provenance."""

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
    """A journal entry as flat UI text — never the numpy before/after arrays.

    ``before``/``after`` are :meth:`~simoscal.tune.journal.EditEntry.before_text`
    and ``after_text``, which narrow to the rows that actually moved: a whole-grid
    ``min..max`` hides a one-row edit completely, and one row is exactly what the
    boost editor writes. ``scope`` is ``scope_text()`` for the same reason — the
    kind alone does not say *which* rows.

    ``touched`` reports whether bytes measurably moved, so the app can tell an
    edit that changed the buffer from one that met its target already. It is the
    entry's own measurement, never inferred from the verdict.
    """
    return {
        "space": entry.space,
        "label": entry.label,
        "name": entry.name,
        "kind": entry.kind,
        "scope": entry.scope_text(),
        "verdict": entry.verdict,
        "units": getattr(entry, "units", "") or "",
        "intent": entry.intent,
        "detail": entry.detail or "",
        "warning": entry.warning or "",
        "before": entry.before_text(),
        "after": entry.after_text(),
        "cells_changed": entry.cells_changed,
        "touched": entry.touched_bytes,
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
    an embedded client's import flow do. Supply ``switch_patch_xdf_*`` to get the boost
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
    if patch_xdf is not None and verdict.switch_patch_present is None:
        # Not a verdict about the bin at all. ``None`` means preflight could not
        # open the switch-patch XDF, so it never got as far as looking for the
        # patch bytes — it distinguishes that from ``False`` precisely so this
        # message can. Collapsing the two sends a person to re-patch a bin that
        # is already patched, while the actual cause sits unread in the verdict
        # (CR-20260815-02).
        detail = str(verdict.advanced.get("switch_patch_error", "")).strip()
        raise BridgeError(
            ErrorCode.PREFLIGHT_BLOCKED,
            "The switch-patch XDF could not be read.",
            advanced=(
                "Compatibility preflight could not open the switch-patch "
                "definition, so it could not check whether this bin carries the "
                "patch. This says nothing about the bin — choose a different "
                "switch-patch XDF."
                + (f" {detail}" if detail else "")
            ),
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


def _op_boost_rpm_axis(params: dict) -> dict:
    """Re-breakpoint the rpm axis shared by all five slot grids (Advanced only).

    Routed through ``switchpatch.slot_rpm_axis`` rather than reached with the
    generic ``edit`` op on purpose. The domain call is the only thing that
    enforces strictly-increasing breakpoints and checks the patch's separate
    axis-length header; a generic grid write to the same table would satisfy
    neither. That matters more here than for a normal table because one axis is
    shared by all five slots, so a bad breakpoint reinterprets every slot curve
    at once — silently, since the stored grids do not change.
    """
    sess = _session(params)
    breakpoints = _require(params, "breakpoints")
    intent = params.get("intent", "")
    if PATCH_SPACE not in sess.tune.spaces:
        raise BridgeError(
            ErrorCode.TUNE_ERROR,
            "this session has no switch-patch boost tables",
            advanced="the tune was opened without the switch-patch profile",
        )

    try:
        entry = sess.tune.switchpatch.slot_rpm_axis(breakpoints, intent=intent)
    except (EditRejected, ValueError) as exc:
        raise BridgeError(ErrorCode.EDIT_REJECTED, str(exc))
    except (TuneError, KeyError, TypeError) as exc:
        raise BridgeError(
            ErrorCode.TUNE_ERROR,
            "the shared slot rpm axis could not be written",
            advanced=str(exc),
        )

    sess.history.commit()
    axis = np.asarray(
        sess.tune.values("slot_put_rpm_axis", space=PATCH_SPACE), dtype=np.float64
    ).ravel()
    return {
        "rpm_axis": _jsonify(axis),
        "entry": _entry_summary(entry),
        **_history_state(sess),
    }


def _op_slot_settings(params: dict) -> dict:
    """The whole per-slot switchboard: every scalar, against all five slots.

    One read rather than sixteen-times-five, because the question is comparative
    and answering it a table at a time is how a slot goes unchecked.
    """
    sess = _session(params)
    _require_patch_space(sess)
    return {"settings": _jsonify(sess.tune.switchpatch.slot_settings())}


def _op_slot_flag(params: dict) -> dict:
    """Set one 0/1 per-slot flag on one or more slots.

    Routed through ``switchpatch.set_slot_flag`` rather than the generic ``edit``
    op for the same reason the boost curve is: the domain call is what checks the
    setting is a flag this profile is willing to write, and that the byte it is
    about to overwrite actually reads 0 or 1. A generic write to the same address
    checks neither, and half of these tables sit within a few bytes of each other.
    """
    sess = _session(params)
    key = _require(params, "key")
    on = _require(params, "on")
    intent = params.get("intent", "")
    _require_patch_space(sess)

    slots = params.get("slots")
    if slots is None:
        raise BridgeError(ErrorCode.BAD_PARAMS, "missing required parameter 'slots'")
    try:
        slots = tuple(int(s) for s in slots)
    except (TypeError, ValueError):
        raise BridgeError(ErrorCode.BAD_PARAMS, "slots must be a list of integers")
    if not slots:
        raise BridgeError(ErrorCode.BAD_PARAMS, "slots must name at least one slot")

    try:
        entries = sess.tune.switchpatch.set_slot_flag(
            str(key), slots=slots, on=bool(on), intent=intent
        )
    except (EditRejected, ValueError) as exc:
        raise BridgeError(ErrorCode.EDIT_REJECTED, str(exc))
    except (TuneError, KeyError) as exc:
        raise BridgeError(
            ErrorCode.TUNE_ERROR,
            f"the flag {key!r} could not be written",
            advanced=str(exc),
        )

    sess.history.commit()
    return {
        "settings": _jsonify(sess.tune.switchpatch.slot_settings()),
        "entries": [_entry_summary(e) for e in entries],
        **_history_state(sess),
    }


def _require_patch_space(sess: _Session) -> None:
    if PATCH_SPACE not in sess.tune.spaces:
        raise BridgeError(
            ErrorCode.TUNE_ERROR,
            "this session has no switch-patch tables",
            advanced="the tune was opened without the switch-patch profile",
        )


def _op_journal(params: dict) -> dict:
    """The session's whole edit journal, in the order the calls were made.

    Read-only, and deliberately **not** a report. It carries no gate verdict, no
    checksum state, no ``verified`` flag and no share path — only what the live
    session has recorded so far. That separation is the point: a *report* is the
    atomic product of the ``build`` op's gate run and re-deriving one from the
    live journal is exactly the drift CR-20260724-02 closed. This op re-derives
    nothing; it hands over the journal as the unverified running list it is, and
    the app is responsible for never painting it as a flash gate.

    Re-reading it is how the changes screen stays current. Undo and redo restore
    the journal wholesale (:meth:`~simoscal.tune.recovery.History._restore`
    replaces the entry list from the snapshot), so the engine's copy is the only
    one that can be right — an app-side tally accumulated from edit replies would
    drift the first time someone stepped back.

    ``superseded_by`` marks a bulk-recipe skip that a later applied write in this
    same session covers, so a ``skipped`` row and an ``applied`` row for the one
    table cannot read as a contradiction (the same substitution ``report.md``
    and the HTML report make).
    """
    sess = _session(params)
    journal = sess.tune.journal
    superseded = journal.superseded()

    entries = []
    for index, entry in enumerate(journal):
        summary = _entry_summary(entry)
        writers = superseded.get(index)
        if writers:
            summary["verdict"] = VERDICT_SUPERSEDED
            summary["superseded_by"] = ", ".join(dict.fromkeys(w.name for w in writers))
        entries.append(summary)

    return {
        "entries": entries,
        # `summary_counts` rather than `counts`: a skip a later write superseded
        # was not held back, and must not be tallied among the ones that were.
        "counts": journal.summary_counts(),
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


#: Name :meth:`SwitchPatch.require_sanity` registers its post-check under.
_SWITCH_PATCH_GATE = "switch-patch sanity"


def _require_switch_patch_gate(sess: _Session) -> None:
    """Make the switch-patch sanity gate unskippable on a patched session.

    ``session_create`` registers it, and ``session_recover`` restores it from the
    record — but neither is the *build*, and a gate that depends on how the
    session happened to be opened is a gate that can go missing. Registering it
    here means every build of a patched bin re-checks that the patch still loads
    and decodes on the finished file, whatever route the session took to get
    here. Idempotent: registering twice would run (and journal) the same check
    twice, so an existing gate is left alone (CR-20260813-01, defense in depth).
    """
    if PATCH_SPACE not in sess.tune.spaces:
        return
    if any(check.name == _SWITCH_PATCH_GATE for check in sess.tune.post_checks):
        return
    sess.tune.switchpatch.require_sanity()


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
            "A build must run against the bin imported for this session.",
            advanced=(
                f"session source {imported_sha[:12]}…; "
                f"reference {params['reference_bin_sha256'][:12]}…; "
                f"source {params['source_bin_sha256'][:12]}…"
            ),
        )

    _require_switch_patch_gate(sess)

    try:
        report = build_revision(
            sess.tune, revision,
            staging_dir=staging_dir,
            reference_bin=reference_bin,
            bin_name=bin_name,
            source_bin=source_bin,
        )
    except ValueError as exc:
        # An unusable revision label or candidate name — e.g. one carrying a path
        # separator, which would place the candidate outside the staging tree the
        # FileProvider shares (CR-20260813-05). Loud, and never a written file.
        raise BridgeError(
            ErrorCode.BAD_PARAMS,
            "that revision name cannot be used as a file name",
            advanced=str(exc),
        )
    return {"report": report.to_dict()}


# --------------------------------------------------------------------------- #
# log analysis — read-only, sessionless
# --------------------------------------------------------------------------- #
def _verified_logs(params: dict) -> tuple[list[Path], dict[str, str]]:
    """Resolve the ``logs`` list into verified paths plus their display names.

    Each element is ``{"log_path", "log_sha256", "display_name"?}`` — the same
    ``<name>_path`` / ``<name>_sha256`` suffix contract every other file-naming
    op uses, so one element goes straight through :func:`_verified_path` and
    inherits its ``FILE_NOT_FOUND`` / ``HASH_MISMATCH`` behaviour unchanged.

    The display name is carried separately because the app's copy of a picked
    CSV is content-addressed: the filename on disk is a hash, and the name a
    person recognises — the one that ends up labelling a pull — is what the
    picker showed them.
    """
    raw = _require(params, "logs")
    if not isinstance(raw, list) or not raw:
        raise BridgeError(ErrorCode.BAD_PARAMS, "'logs' must be a non-empty list")
    paths: list[Path] = []
    names: dict[str, str] = {}
    for element in raw:
        if not isinstance(element, dict):
            raise BridgeError(ErrorCode.BAD_PARAMS, "each entry in 'logs' must be an object")
        path = _verified_path(element, "log")
        paths.append(path)
        display = element.get("display_name")
        if isinstance(display, str) and display:
            names[str(path)] = display
    return paths, names


def _op_analyze_logs(params: dict) -> dict:
    """Run the analysis battery over a set of verified datalog CSVs. Read-only.

    Nothing here writes a file, opens a session, or touches a bin's bytes: it
    parses logs, detects pulls, runs the same battery
    ``python -m simoscal.analysis`` runs, and hands back the findings plus the
    plot series. The op is deliberately sessionless — reading a datalog has
    nothing to do with editing a calibration, and requiring an open session to
    look at a log would be a gate with no safety behind it.

    A bin + XDF may be supplied (both, or neither); with them the two
    calibration-aware checks run, and without them they land in SKIPPED exactly
    as they do on the desktop when no bin resolves. The desktop's *autolocation*
    of a bin deliberately does not happen here — there is no project tree on a
    phone, and a check that silently found some other bin would be worse than
    one that skipped.

    **Plots are series, not images.** matplotlib is outside the mobile
    dependency closure on purpose, so the engine sends the same masked, sorted
    samples its own PNGs are drawn from (:func:`simoscal.analysis.plot_payload`)
    and the app draws them. The inventory — which channel belongs on which
    panel, and in which role — is the engine's either way.
    """
    from .analysis import (
        AnalysisError,
        CheckContext,
        default_battery,
        detect_pulls,
        findings_to_dict,
        load_logset_files,
        plot_payload,
        run_battery,
    )

    paths, names = _verified_logs(params)
    bin_path = _verified_path(params, "bin", required=False)
    xdf_path = _verified_path(params, "xdf", required=False)

    try:
        logset = load_logset_files(paths, names=names)
        pulls = detect_pulls(logset)
    except AnalysisError as exc:
        raise BridgeError(
            ErrorCode.ANALYSIS_ERROR,
            "those datalogs could not be read",
            advanced=str(exc),
        ) from exc

    cal = None
    cal_note = ""
    if bin_path is not None and xdf_path is not None:
        try:
            from .calfile import CalFile

            cal = CalFile.open(str(xdf_path), str(bin_path))
        except Exception as exc:
            # A calibration that will not open must not sink the whole analysis:
            # every channel-based finding is still valid without it. It is
            # reported rather than swallowed, and the two cal-aware checks skip.
            cal_note = f"could not open the calibration: {exc}"

    ctx = CheckContext(logset=logset, pulls=pulls, cal=cal)
    try:
        result = run_battery(default_battery(), ctx)
        document = findings_to_dict(result)
        document["plots"] = plot_payload(ctx)
    except AnalysisError as exc:
        raise BridgeError(
            ErrorCode.ANALYSIS_ERROR,
            "those datalogs could not be analysed",
            advanced=str(exc),
        ) from exc

    if cal_note:
        document["cal_notes"] = [cal_note]
    return _jsonify(document)


def _op_log_overlay(params: dict) -> dict:
    """Detected pulls plus their boost traces, for the boost editor's overlay.

    Sessionless and read-only, like ``analyze_logs`` — and deliberately *not*
    ``analyze_logs``. The overlay needs pulls and two series; the battery is
    seconds of work it does not need, and routing the overlay through the
    analyze flow would make one screen's lifecycle a dependency of another's.

    It carries no findings, no verdicts, and no calibration: the overlay draws
    data behind the curves and every judgement stays with the person editing.
    That is also why it takes no ``session_id`` — reading a datalog has nothing
    to do with holding an open edit session, and this op could not touch one if
    it wanted to.
    """
    from .analysis import (
        AnalysisError,
        CheckContext,
        detect_pulls,
        load_logset_files,
        overlay_payload,
    )

    paths, names = _verified_logs(params)
    try:
        logset = load_logset_files(paths, names=names)
        pulls = detect_pulls(logset)
        payload = overlay_payload(CheckContext(logset=logset, pulls=pulls, cal=None))
    except AnalysisError as exc:
        raise BridgeError(
            ErrorCode.ANALYSIS_ERROR,
            "that datalog could not be read",
            advanced=str(exc),
        ) from exc
    return _jsonify(payload)


def _op_limiters(params: dict) -> dict:
    """Every limiter the Limiters screen shows, read in one call.

    One read rather than one per scalar, for the same reason ``slot_settings``
    is one call: the question is comparative — the cut trio only means anything
    read together — and answering it a table at a time is how one value goes
    unchecked.

    The rev trio lives in the switch patch, so a base-only session reports
    ``rev_limits: null`` and the screen shows the road-speed limiter alone. That
    is a degraded screen, not an error: a bin without the patch genuinely has no
    trio.
    """
    from .tune.profiles.sc8s50 import SPEED_LIMITER
    from .tune.profiles.switchpatch_2933 import (
        LAUNCH_CONTROL_LIMITER,
        REV_LIMIT_TRIO,
    )

    sess = _session(params)

    def scalar(name: str, space: str = "base") -> dict:
        resolved = sess.tune.table(name, space=space)
        return {
            "name": name,
            "label": resolved.label,
            "description": resolved.spec.description,
            "units": resolved.units,
            "value": float(sess.tune.values(name, space=space).ravel()[0]),
            "owner": resolved.owner,
        }

    result: dict = {
        "speed_limiter": [scalar(name) for name in SPEED_LIMITER],
        "rev_limits": None,
        "launch_control": None,
    }
    if PATCH_SPACE in sess.tune.spaces:
        result["rev_limits"] = [
            scalar(name, PATCH_SPACE) for name in REV_LIMIT_TRIO
        ]
        result["launch_control"] = [
            scalar(name, PATCH_SPACE) for name in LAUNCH_CONTROL_LIMITER
        ]
    return result


def _op_limiters_edit(params: dict) -> dict:
    """Apply a limiters change — the rev trio, the speed limiter, or both.

    Routed to the domain ops rather than the generic ``edit`` op because both
    writes are multi-table and neither invariant is visible from a single
    table: a grid write to one quartet scalar leaves the car limited by an
    un-written level, and one to a cut-trio scalar cannot see the other two.
    A refusal is ``EDIT_REJECTED`` with the engine's own reason, and leaves
    every table untouched.
    """
    sess = _session(params)
    intent = params.get("intent", "")
    rev = params.get("rev_limits")
    speed = params.get("speed_limiter_kmh")
    if rev is None and speed is None:
        raise BridgeError(
            ErrorCode.BAD_PARAMS,
            "name at least one of 'rev_limits' or 'speed_limiter_kmh'",
        )

    entries = []
    try:
        if rev is not None:
            if not isinstance(rev, dict):
                raise BridgeError(ErrorCode.BAD_PARAMS, "'rev_limits' must be an object")
            _require_patch_space(sess)
            entries.extend(sess.tune.limits.rev_limits(
                soft=rev.get("soft"), medium=rev.get("medium"),
                hard=rev.get("hard"), intent=intent,
            ))
        if speed is not None:
            entries.extend(sess.tune.limits.speed_limiter(speed, intent=intent))
    except (EditRejected, ValueError) as exc:
        raise BridgeError(ErrorCode.EDIT_REJECTED, str(exc))
    except (TuneError, KeyError, TypeError) as exc:
        raise BridgeError(
            ErrorCode.TUNE_ERROR,
            "the limiter change could not be applied",
            advanced=str(exc),
        )

    sess.history.commit()
    return {
        "limiters": _op_limiters({"session_id": sess.id}),
        "entries": [_entry_summary(e) for e in entries],
        **_history_state(sess),
    }


def _op_lambda_fl(params: dict) -> dict:
    """The full-load enrichment map as the lambda screen draws it. Read-only.

    Rows are time at full load and columns engine speed, so the screen shows one
    time-row as the editable curve with the others ghosted. Both axes are sent
    decoded, and the lean bound comes from the engine rather than a UI constant —
    the number the screen draws its danger band against must be the number the
    engine refuses on.
    """
    from .tune.domains.fueling import LAMBDA_FL_LEAN_MAX, LAMBDA_FL_RICH_MIN

    sess = _session(params)
    try:
        info = table_detail(sess.tune, "lambda_full_load")
    except (TuneError, KeyError) as exc:
        raise BridgeError(
            ErrorCode.TUNE_ERROR,
            "this session has no full-load enrichment map",
            advanced=str(exc),
        )
    return {
        "table": _jsonify(info),
        "lean_max": LAMBDA_FL_LEAN_MAX,
        "rich_min": LAMBDA_FL_RICH_MIN,
    }


def _op_lambda_fl_edit(params: dict) -> dict:
    """Write one time-row of the full-load enrichment map.

    ``row`` is an index or ``seconds`` picks the row by its own breakpoint;
    ``values`` is one lambda per rpm breakpoint or a scalar for a flat row. A
    value at or above the lean bound is ``EDIT_REJECTED`` carrying the engine's
    reason — never clamped, so a lean setpoint cannot be quietly corrected into
    one nobody asked for.
    """
    sess = _session(params)
    values = _require(params, "values")
    intent = params.get("intent", "")
    row = params.get("row")
    seconds = params.get("seconds")

    try:
        entry = sess.tune.fueling.full_load_enrichment(
            values, row=row, seconds=seconds, intent=intent,
        )
    except (EditRejected, ValueError) as exc:
        raise BridgeError(ErrorCode.EDIT_REJECTED, str(exc))
    except (TuneError, KeyError, TypeError) as exc:
        raise BridgeError(
            ErrorCode.TUNE_ERROR,
            "the enrichment row could not be written",
            advanced=str(exc),
        )

    sess.history.commit()
    requested = np.atleast_1d(np.asarray(values, dtype=np.float64)).ravel()
    encoded = np.asarray(
        sess.tune.values("lambda_full_load"), dtype=np.float64
    )[entry.rows_changed[0] if entry.rows_changed else 0]
    if requested.size == 1:
        requested = np.full(encoded.size, float(requested[0]))
    return {
        "requested": _jsonify(requested),
        "encoded": _jsonify(encoded),
        "entry": _entry_summary(entry),
        **_history_state(sess),
    }


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
    # Added for V8. Adding an op does not bump BRIDGE_VERSION: an older app never
    # names it, and a newer app against an older engine gets a clean UNKNOWN_OP
    # rather than a field read two different ways — which is what the version
    # gate exists to prevent.
    "boost_rpm_axis": _op_boost_rpm_axis,
    # The per-slot switchboard. Same versioning reasoning as the ops above.
    "slot_settings": _op_slot_settings,
    "slot_flag": _op_slot_flag,
    # Read-only, and not a report — see `_op_journal`. Same additive versioning
    # as the ops above: an older app never names it.
    "journal": _op_journal,
    "undo": _op_undo,
    "redo": _op_redo,
    "build": _op_build,
    # Read-only and sessionless: it analyses datalogs and never touches a bin.
    # Additive, so BRIDGE_VERSION is unchanged for the same reason the V8 ops
    # left it alone — an older app never names it, and a newer app against an
    # older engine gets a clean UNKNOWN_OP.
    "analyze_logs": _op_analyze_logs,
    # The domain-screen ops. Additive on the same reasoning again.
    #
    # `log_overlay` is read-only and sessionless like `analyze_logs`; the other
    # four are the read/edit pairs behind the Limiters and Lambda screens, each
    # routed to a domain call because its invariant spans more than one table
    # (the quartet, the cut trio) or has a direction the grid cannot express
    # (the lambda lean bound). The Pedal screen deliberately has no op here: its
    # tables are dual-path, so it rides on `catalog`/`table_detail`/`edit`.
    "log_overlay": _op_log_overlay,
    "limiters": _op_limiters,
    "limiters_edit": _op_limiters_edit,
    "lambda_fl": _op_lambda_fl,
    "lambda_fl_edit": _op_lambda_fl_edit,
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
