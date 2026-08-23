"""Session recovery persistence — make an open edit session survive a process
kill and restore byte-exactly.

The edit session already exists: a :class:`~simoscal.tune.project.Tune` binds a
bin to its table spaces and its :class:`~simoscal.tune.journal.Journal` is the
ordered record of every edit. This module does the one thing that was missing —
persist enough of a live ``Tune`` to reconstruct it later, and restore it into
an *equivalent* live ``Tune`` — without rebuilding the session model or inventing
a recipe format.

How reconstruction stays byte-exact for every table kind
--------------------------------------------------------
The journal records each edit's ``after`` values as *physical* numbers. Replaying
those through the write path would be wrong for the raw/non-linear tables, whose
only write path is ``set_raw`` on integers — re-encoding a decoded physical value
would not round-trip. So this module never re-encodes. Instead it:

1. Re-opens a fresh ``Tune`` from the **same source bin and the same patches**,
   which deterministically reproduces the *pristine patched buffer* — the state
   before any edit.
2. Applies a **byte-level diff**: the exact bytes the edits left across the
   journaled table extents *and the stored checksums*, written back verbatim.
   This is correct for a linear grid, a raw table, and a restore-to-source write
   alike, because it copies bytes rather than reinterpreting values. The
   checksums are in the diff because a build corrects them into this same buffer
   without journaling a table write (see :func:`_byte_diff`).
3. Verifies the reconstructed buffer against a **full-buffer SHA-256** captured
   at serialize time — so a moved source, a changed patch, or any reconstruction
   discrepancy fails loud instead of silently restoring the wrong bytes.

The source bin is never written; its hash is checked on restore and the recovery
data records it, so restoring against a different bin than the session was built
on is refused. "Reproducible" here means *re-openable and byte-identical within
this engine version* — there is no cross-version replay promise (that is Phase 2).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence, Union

import numpy as np

from .. import __version__, checksum
from .journal import EditEntry, Journal, KIND_PATCH
from .profile import Profile
from .profiles import PROFILES
from .project import BASE_SPACE, PatchSpec, Tune

__all__ = [
    "FORMAT_VERSION",
    "RecoveryError",
    "serialize_session",
    "restore_session",
    "save_session",
    "load_session",
    "SessionHistory",
]

#: Serialized-form version. Bumped when the on-disk shape changes so an old blob
#: cannot be silently misread by a newer loader.
FORMAT_VERSION = 2


class RecoveryError(Exception):
    """A session could not be serialized or restored safely."""


# --------------------------------------------------------------------------- #
# numpy / EditEntry (de)serialization
# --------------------------------------------------------------------------- #
def _arr_to_json(arr: Optional[np.ndarray]) -> Optional[dict]:
    if arr is None:
        return None
    a = np.asarray(arr)
    return {"dtype": str(a.dtype), "shape": list(a.shape), "data": a.ravel().tolist()}


def _arr_from_json(obj: Optional[dict]) -> Optional[np.ndarray]:
    if obj is None:
        return None
    return np.asarray(obj["data"], dtype=np.dtype(obj["dtype"])).reshape(obj["shape"])


def _entry_to_json(entry: EditEntry) -> dict:
    return {
        "space": entry.space,
        "name": entry.name,
        "label": entry.label,
        # A key is a str symbol or an int uniqueid; both round-trip through JSON,
        # but tag the int so restore rebuilds the right type (resolution differs).
        "key": entry.key,
        "key_is_int": isinstance(entry.key, int),
        "kind": entry.kind,
        "verdict": entry.verdict,
        "units": entry.units,
        "intent": entry.intent,
        "before": _arr_to_json(entry.before),
        "after": _arr_to_json(entry.after),
        "offsets": sorted(entry.offsets),
        "declared": sorted(entry.declared),
        "rows_changed": list(entry.rows_changed),
        "detail": entry.detail,
        "warning": entry.warning,
    }


def _entry_from_json(obj: dict) -> EditEntry:
    key = obj["key"]
    if obj.get("key_is_int") and not isinstance(key, int):
        key = int(key)
    return EditEntry(
        space=obj["space"],
        name=obj["name"],
        label=obj["label"],
        key=key,
        kind=obj["kind"],
        verdict=obj["verdict"],
        units=obj.get("units", ""),
        intent=obj.get("intent", ""),
        before=_arr_from_json(obj.get("before")),
        after=_arr_from_json(obj.get("after")),
        offsets=frozenset(obj.get("offsets", ())),
        declared=frozenset(obj.get("declared", ())),
        rows_changed=tuple(obj.get("rows_changed", ())),
        detail=obj.get("detail", ""),
        warning=obj.get("warning", ""),
    )


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _image(tune: Tune):
    """The one :class:`BinImage` every space shares (see project.py)."""
    return tune.space(BASE_SPACE).cal.binimage


def _buffer_bytes(tune: Tune) -> bytes:
    return _image(tune).to_bytes()


def _invalidate_views(tune: Tune) -> None:
    """Drop every cached decode, including profile-held TableView objects."""
    for space in tune.spaces.values():
        for name in space.tables.names():
            space.tables[name].view.invalidate()
        space.cal._views.clear()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _byte_diff(tune: Tune) -> dict[str, int]:
    """The edited bytes over every journaled table extent, as ``{offset: byte}``.

    Keyed on :meth:`Journal.declared_offsets` — the extents of every physical/raw
    table write, including a restore-to-source write that staged nothing — plus
    the stored-checksum bytes. That is exactly the set whose bytes must be pinned
    to reconstruct the session on top of the pristine patched buffer; patch bytes
    are excluded because restore re-applies the patches themselves.

    The stored checksums are in here because a *build* writes them into this same
    live buffer (``CalFile.save(correct_checksums=True)`` applies
    :func:`~simoscal.checksum.correction_patches` in place), and they are not a
    journaled table extent. Leaving them out made a built session unrecoverable
    for good: ``buffer_sha256`` was taken after the correction while the diff
    could not reproduce it, so :func:`restore_session` failed its own whole-buffer
    gate every time, blaming the source bin (CR-20260816-01). They are located by
    :func:`~simoscal.checksum.stored_checksum_ranges`, the same single statement of
    where they live that the build's raw-diff audit allowance uses. Pinning them
    unconditionally is safe: on a session that was never built they still hold the
    source's own values, so restore writes them back unchanged.
    """
    buf = _buffer_bytes(tune)
    offsets = set(tune.journal.declared_offsets()) | _checksum_offsets(buf)
    return {str(off): buf[off] for off in sorted(offsets)}


def _checksum_offsets(data: bytes) -> frozenset[int]:
    """Every byte offset holding a stored checksum value, for ``data``'s layout."""
    offsets: set[int] = set()
    spec = checksum.discover_structure(data)
    for _name, start, length in checksum.stored_checksum_ranges(data, spec):
        offsets.update(range(start, start + length))
    return frozenset(offsets)


def _equal_but_for_checksums(candidate: bytes, target: bytes) -> bool:
    """Whether ``candidate`` equals ``target`` once the stored checksums are ignored.

    Used where two buffers are compared across a build: the build corrects the
    checksums in place, and a checksum is derived from the calibration rather
    than part of it, so a difference confined to those bytes is not a difference
    in the session.
    """
    if candidate == target:
        return True
    if len(candidate) != len(target):
        return False
    patched = bytearray(candidate)
    spec = checksum.discover_structure(target)
    for _name, start, length in checksum.stored_checksum_ranges(target, spec):
        patched[start:start + length] = target[start:start + length]
    return bytes(patched) == target


def _contiguous_runs(diff: Mapping[str, int]):
    """Yield ``(start_offset, bytes)`` runs from a ``{offset_str: byte}`` diff.

    Consecutive offsets coalesce into one run so a table extent is written in a
    single call. Sorting is by integer offset, so the string keys order correctly.
    """
    offsets = sorted((int(k) for k in diff), )
    i = 0
    n = len(offsets)
    while i < n:
        start = offsets[i]
        run = bytearray([diff[str(start)]])
        j = i + 1
        while j < n and offsets[j] == offsets[j - 1] + 1:
            run.append(diff[str(offsets[j])])
            j += 1
        yield start, bytes(run)
        i = j


# --------------------------------------------------------------------------- #
# serialize
# --------------------------------------------------------------------------- #
def serialize_session(
    tune: Tune,
    *,
    patches: Sequence[PatchSpec] = (),
    history: Optional["SessionHistory"] = None,
) -> dict:
    """Serialize a live ``Tune`` to a recovery record (a JSON-safe ``dict``).

    ``patches`` are the :class:`PatchSpec`\\ s the tune was opened with — they are
    re-applied on restore to reproduce the patched buffer, so they must be passed
    (a ``Tune`` keeps only the patch *results*, not the specs). The base profile,
    the base XDF, and every extra table space are read back off the tune itself.

    Nothing is written; the tune is only read.
    """
    base = tune.space(BASE_SPACE)
    if tune.recipe_report is not None:
        raise RecoveryError(
            "sessions with a bulk SOP recipe report cannot be recovered yet; "
            "refusing to save a session that would silently lose coherence gates"
        )
    extra_spaces = {
        name: {
            "profile": space.profile.name,
            "xdf": str(space.xdf),
            "xdf_sha256": _sha256_file(space.xdf),
        }
        for name, space in tune.spaces.items()
        if name != BASE_SPACE
    }

    post_checks = []
    for check in tune.post_checks:
        if not check.recovery_key:
            raise RecoveryError(
                f"post-build check {check.name!r} has no recovery descriptor; "
                "refusing to save a session that would silently lose a safety gate"
            )
        params = dict(check.recovery_params)
        stock_bin = params.get("stock_bin")
        if stock_bin is not None:
            stock_path = Path(str(stock_bin))
            if not stock_path.is_file():
                raise RecoveryError(
                    f"post-build check {check.name!r} references missing stock bin "
                    f"{stock_path}"
                )
            params["stock_bin_sha256"] = _sha256_file(stock_path)
        post_checks.append({
            "name": check.name,
            "key": check.recovery_key,
            "params": params,
        })

    # The journal minus patch entries: Tune.open re-creates the patch entries
    # when it re-applies the patches, so persisting them would double them.
    journal_entries = [
        _entry_to_json(e) for e in tune.journal.entries if e.kind != KIND_PATCH
    ]

    buffer_sha = _sha256_bytes(_buffer_bytes(tune))

    if history is None:
        history = getattr(tune, "_session_history", None)

    return {
        "format_version": FORMAT_VERSION,
        "engine_version": __version__,
        "source": {
            "bin": str(tune.source_bin),
            "bin_sha256": _sha256_file(Path(tune.source_bin)),
            "base": {
                "profile": base.profile.name,
                "xdf": str(base.xdf),
                "xdf_sha256": _sha256_file(base.xdf),
            },
            "extra_spaces": extra_spaces,
            "patches": [
                {"label": p.label, "path": str(p.path), "description": p.description}
                for p in patches
            ],
        },
        "buffer_sha256": buffer_sha,
        "byte_diff": _byte_diff(tune),
        "journal": journal_entries,
        "post_checks": post_checks,
        "history": history._to_record() if history is not None else None,
    }


# --------------------------------------------------------------------------- #
# restore
# --------------------------------------------------------------------------- #
def _profile_by_name(name: str) -> Profile:
    try:
        return PROFILES[name]
    except KeyError:
        raise RecoveryError(
            f"unknown profile {name!r} in recovery data; known profiles: "
            f"{', '.join(sorted(PROFILES))}"
        ) from None


def _resolve_path(stored: str, override: Optional[Union[str, Path]]) -> Path:
    return Path(override) if override is not None else Path(stored)


def restore_session(
    data: Mapping,
    *,
    source_bin: Optional[Union[str, Path]] = None,
    xdf_paths: Optional[Mapping[str, Union[str, Path]]] = None,
) -> Tune:
    """Reconstruct a live ``Tune`` from a recovery record, byte-exactly.

    ``source_bin`` / ``xdf_paths`` override the stored paths when files moved
    between app sessions (``xdf_paths`` is keyed by space name, ``"base"`` for the
    primary XDF). The source bin's SHA-256 is verified against the record, and the
    fully reconstructed buffer is verified against the record's ``buffer_sha256`` —
    either mismatch raises :class:`RecoveryError` rather than restoring wrong bytes.
    """
    fmt = data.get("format_version")
    if fmt != FORMAT_VERSION:
        raise RecoveryError(
            f"recovery format_version {fmt!r} is not supported by this build "
            f"(expected {FORMAT_VERSION})."
        )
    engine_version = data.get("engine_version")
    if engine_version != __version__:
        raise RecoveryError(
            f"recovery engine_version {engine_version!r} does not match this "
            f"engine ({__version__!r}); recovery is supported only within one "
            "engine version"
        )

    src = data["source"]
    bin_path = _resolve_path(src["bin"], source_bin)
    if not bin_path.is_file():
        raise RecoveryError(f"source bin not found for restore: {bin_path}")
    actual_sha = _sha256_file(bin_path)
    if actual_sha != src["bin_sha256"]:
        raise RecoveryError(
            "source bin has changed since the session was saved "
            f"({bin_path}): recorded {src['bin_sha256'][:12]}…, "
            f"found {actual_sha[:12]}…. Refusing to restore onto a different bin."
        )

    xdf_paths = dict(xdf_paths or {})
    base_profile = _profile_by_name(src["base"]["profile"])
    base_xdf = _resolve_path(src["base"]["xdf"], xdf_paths.get("base"))

    extra_spaces = {
        name: (
            _profile_by_name(spec["profile"]),
            _resolve_path(spec["xdf"], xdf_paths.get(name)),
        )
        for name, spec in src.get("extra_spaces", {}).items()
    }
    xdf_specs = {"base": (base_xdf, src["base"]), **{
        name: (extra_spaces[name][1], spec)
        for name, spec in src.get("extra_spaces", {}).items()
    }}
    for name, (path, spec) in xdf_specs.items():
        if not path.is_file():
            raise RecoveryError(f"{name} XDF not found for restore: {path}")
        expected = spec.get("xdf_sha256")
        actual = _sha256_file(path)
        if actual != expected:
            raise RecoveryError(
                f"{name} XDF has changed since the session was saved ({path}): "
                f"recorded {str(expected)[:12]}…, found {actual[:12]}…. "
                "Refusing to restore with different table definitions."
            )
    patches = tuple(
        PatchSpec(label=p["label"], path=Path(p["path"]),
                  description=p.get("description", ""))
        for p in src.get("patches", [])
    )

    # 1. Reproduce the pristine patched buffer from source + patches.
    tune = Tune.open(
        base_profile, xdf=base_xdf, bin=bin_path,
        patches=patches, extra_spaces=extra_spaces,
    )

    # 2. Apply the byte-level diff — the edited bytes, verbatim. Offsets are
    #    coalesced into contiguous runs so each table extent is one region-checked
    #    write through the public BinImage API rather than a byte at a time.
    image = _image(tune)
    diff = data.get("byte_diff", {})
    for start, run in _contiguous_runs(diff):
        try:
            image.write(start, run)
        except Exception as exc:  # noqa: BLE001 - region bound / bad offset
            raise RecoveryError(
                f"recovery byte run at {start:#x} could not be applied: {exc}"
            ) from exc

    # 3. Verify the whole reconstructed buffer against the recorded hash.
    got = _sha256_bytes(_buffer_bytes(tune))
    if got != data["buffer_sha256"]:
        raise RecoveryError(
            "restored buffer does not match the saved session "
            f"(recorded {data['buffer_sha256'][:12]}…, got {got[:12]}…). "
            "The source bin or a patch may differ from when it was saved."
        )

    # 4. Rebuild the journal: patch entries from Tune.open (already present),
    #    then the persisted non-patch edits, preserving order.
    for obj in data.get("journal", []):
        tune.journal.record(_entry_from_json(obj))

    for check in data.get("post_checks", []):
        key = check.get("key")
        params = check.get("params") or {}
        if key == "switch_patch_sanity":
            stock_bin = params.get("stock_bin")
            if stock_bin is not None:
                stock_path = Path(str(stock_bin))
                if (
                    not stock_path.is_file()
                    or _sha256_file(stock_path) != params.get("stock_bin_sha256")
                ):
                    raise RecoveryError(
                        "switch-patch sanity reference bin changed since the "
                        "session was saved"
                    )
            tune.switchpatch.require_sanity(stock_bin=stock_bin)
        else:
            raise RecoveryError(
                f"unknown post-build recovery check {key!r}; refusing to "
                "restore without a registered safety gate"
            )

    if data.get("history") is not None:
        tune._recovered_history = data["history"]

    # Invalidate any cached decodes so table reads reflect the applied diff.
    _invalidate_views(tune)

    return tune


# --------------------------------------------------------------------------- #
# file helpers
# --------------------------------------------------------------------------- #
def save_session(
    tune: Tune,
    path: Union[str, Path],
    *,
    patches: Sequence[PatchSpec] = (),
) -> Path:
    """Serialize ``tune`` and write it to ``path`` as deterministic JSON."""
    data = serialize_session(tune, patches=patches)
    path = Path(path)
    path.write_text(
        json.dumps(data, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    return path


def load_session(
    path: Union[str, Path],
    *,
    source_bin: Optional[Union[str, Path]] = None,
    xdf_paths: Optional[Mapping[str, Union[str, Path]]] = None,
) -> Tune:
    """Load a recovery record from ``path`` and restore the live ``Tune``."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return restore_session(data, source_bin=source_bin, xdf_paths=xdf_paths)


# --------------------------------------------------------------------------- #
# undo / redo
# --------------------------------------------------------------------------- #
class SessionHistory:
    """In-session undo/redo over a live ``Tune``, driven off the journal.

    Each :meth:`commit` snapshots the shared buffer and the journal after a
    committed edit. :meth:`undo` / :meth:`redo` move a cursor over those snapshots
    and restore the buffer and journal to that point — by copying bytes, so it is
    exact for every table kind, the same discipline the persistence path uses.

    This is deliberately in-memory: it makes an open session's undo stack work.
    The compact snapshot stack is included in recovery data, so a restored
    session keeps its undo/redo cursor rather than merely reopening the current
    bytes.
    """

    def __init__(self, tune: Tune) -> None:
        self._tune = tune
        self._region = (
            tune.space(BASE_SPACE).cal.binimage.region_start,
            tune.space(BASE_SPACE).cal.binimage.region_end,
        )
        recovered = getattr(tune, "_recovered_history", None)
        if recovered is None:
            self._stack = [self._snapshot()]
            self._cursor = 0
        else:
            self._stack = self._from_record(recovered)
            self._cursor = int(recovered["cursor"])
            if not (0 <= self._cursor < len(self._stack)):
                raise RecoveryError("recovered undo cursor is outside its snapshot stack")
            # Checksums are excluded because a build corrects them into the live
            # buffer without committing an undo point, so the top snapshot of a
            # built session legitimately holds the pre-build checksum bytes. This
            # is the same root cause as CR-20260816-01 and would otherwise reject
            # every built session a step after the byte diff restored it.
            if not _equal_but_for_checksums(
                self._stack[self._cursor][0], _buffer_bytes(tune)
            ):
                raise RecoveryError(
                    "recovered undo cursor does not match the restored session buffer"
                )
            delattr(tune, "_recovered_history")
        tune._session_history = self

    def _snapshot(self) -> tuple[bytes, tuple[EditEntry, ...]]:
        return (_buffer_bytes(self._tune), self._tune.journal.entries)

    def _to_record(self) -> dict:
        """Compact undo snapshots as diffs from the immutable session source."""
        base = self._tune.source_snapshot
        snapshots = []
        for buf, entries in self._stack:
            diff = {
                str(i): value
                for i, (source, value) in enumerate(zip(base, buf))
                if source != value
            }
            snapshots.append({
                "buffer_sha256": _sha256_bytes(buf),
                "byte_diff": diff,
                "journal": [_entry_to_json(entry) for entry in entries],
            })
        return {"cursor": self._cursor, "snapshots": snapshots}

    def _from_record(self, record: Mapping) -> list[tuple[bytes, tuple[EditEntry, ...]]]:
        base = self._tune.source_snapshot
        stack: list[tuple[bytes, tuple[EditEntry, ...]]] = []
        for obj in record.get("snapshots", []):
            buf = bytearray(base)
            for start, run in _contiguous_runs(obj.get("byte_diff", {})):
                end = start + len(run)
                if start < 0 or end > len(buf):
                    raise RecoveryError(
                        f"undo snapshot byte run [{start:#x}, {end:#x}) is outside the bin"
                    )
                buf[start:end] = run
            frozen = bytes(buf)
            if _sha256_bytes(frozen) != obj.get("buffer_sha256"):
                raise RecoveryError("recovered undo snapshot failed its buffer hash")
            entries = tuple(_entry_from_json(entry) for entry in obj.get("journal", []))
            stack.append((frozen, entries))
        if not stack:
            raise RecoveryError("recovered undo history has no snapshots")
        return stack

    def _restore(self, snapshot: tuple[bytes, tuple[EditEntry, ...]]) -> None:
        buf_bytes, entries = snapshot
        image = self._tune.space(BASE_SPACE).cal.binimage
        start, end = self._region
        # Only the in-region bytes ever change under edits; restore that slice.
        image.write(start, buf_bytes[start:end])
        self._tune.journal._entries = list(entries)
        _invalidate_views(self._tune)

    @property
    def can_undo(self) -> bool:
        return self._cursor > 0

    @property
    def can_redo(self) -> bool:
        return self._cursor < len(self._stack) - 1

    def commit(self) -> None:
        """Record the current state as a new undo point.

        Call after each committed edit. A commit after an undo discards the
        redo tail, the conventional undo-stack behaviour.
        """
        del self._stack[self._cursor + 1:]
        self._stack.append(self._snapshot())
        self._cursor += 1

    def undo(self) -> bool:
        if not self.can_undo:
            return False
        self._cursor -= 1
        self._restore(self._stack[self._cursor])
        return True

    def redo(self) -> bool:
        if not self.can_redo:
            return False
        self._cursor += 1
        self._restore(self._stack[self._cursor])
        return True
