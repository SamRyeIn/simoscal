"""Replay an imported recommendations file against a live session — and change nothing.

This is the gate between "a model said so" and "a person is shown it". Every
recommendation that survives schema validation is replayed here through the
library's **real** edit path with ``dry_run=True``: the same guards, the same
encode, the same :class:`EditRejected` with the same words. A recommendation the
guards refuse never reaches the queue and is never rendered as a suggestion — it
is dropped, counted, and its refusal reason kept so whoever is improving the
answering side can read it.

Three outcomes, counted separately because they mean different things:

* **queued** — the guards accepted it. Carries the *dry-run preview* — what the
  bin would really hold, re-decoded — so the UI draws the real effect rather
  than the claimed one.
* **dropped** — the guards refused it, or there is no write path to the table it
  names. The reason is the engine's own sentence, not a restatement.
* **malformed** — it failed the schema. Separate from *refused*: a malformed
  record means the answering side is producing bad files, a refused one means it
  is producing bad advice.

**Nothing here mutates the session.** The journal, the undo history and the
bytes are all exactly as they were when the review started. That is the whole
safety claim, and it rests on :meth:`Tune.dry_run` rather than on care taken
here.

Two rules about how a recommendation is turned back into an edit:

*Routing follows the table, not a field in the file.* A recommendation names a
table and the values it should end at; which call performs that write is a
property of the table (its ``owner``), never something the answering side gets
to choose. So the file has one way to say "change this", and the tables that
carry invariants a grid write cannot see are still written by the call that
knows about them.

*A domain-owned table may only be given a value, never an arithmetic op.* The
generic editor is what performs arithmetic over a selection; an adapter's job is
placement — take the table's current values, put the stated ones in, hand the
result to the owning call. ``add``/``mul``/``interpolate`` on such a table are
dropped rather than reimplemented here, because a second implementation of the
engine's arithmetic is exactly the drift the dry run exists to avoid.

Where an adapter would have to *convert* between the grid's units and the domain
call's, there is deliberately none: see :data:`NOT_ADAPTED`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any, Callable, Optional, Sequence

import numpy as np

from ..tune import EditRejected, Tune, TuneError
from ..tune.units import psi_from_hpa
from ..tune.editing import EditOp, Selection, apply_op
from ..tune.journal import EditEntry
from .schema import (
    AdviceRejected,
    MalformedRecord,
    Problem,
    Recommendation,
    RecommendationFile,
    parse_partial,
)

__all__ = [
    "Dropped",
    "NOT_ADAPTED",
    "Preview",
    "ProvenanceMismatch",
    "Queued",
    "ReviewResult",
    "Staging",
    "review",
]

#: The operations an owner-locked table's adapter accepts. Each states the value
#: the table should end at, so the adapter only has to place it. Anything else
#: would mean performing the engine's arithmetic in a second place.
_PLACEMENT_OPS = frozenset({EditOp.SET.value, EditOp.FILL.value, EditOp.PASTE.value})

#: Owner-locked tables that have a write path and still get **no** adapter, with
#: the reason. These are the ones where the owning call takes a different
#: quantity than the grid holds, so "the value the table should end at" and "the
#: value the call takes" are different numbers.
#:
#: ``airmass_setpoint_max`` is the project's canonical trap: the XDF labels it
#: identity mg/stk and the ECU stores kg/stk, so a recommendation saying "set it
#: to 2000" is ambiguous between the two by exactly the factor that removes the
#: limiter. Refusing to guess is the only safe reading — the recommendation is
#: dropped with this sentence, and a person can make the change by hand on the
#: screen that knows the units.
NOT_ADAPTED: dict[str, str] = {
    "airmass_setpoint_max": (
        "this table stores kg/stk although it is labelled mg/stk, so a stated "
        "grid value is ambiguous between the two by a factor of a million — the "
        "courier will not guess which was meant. Make this change on the screen "
        "that owns it, where the units are explicit."
    ),
}

_SLOT_CURVE = re.compile(r"^slot(?P<slot>[1-9])_put_setpoint$")
_SLOT_FLAG = re.compile(r"^slot(?P<slot>[1-9])_(?P<key>.+)$")
_REV_TRIO = re.compile(r"^rev_limit_(?P<which>soft|medium|hard)$")


# --------------------------------------------------------------------------- #
# results
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Staging:
    """How the owning editor should pre-load this change, in **its** units.

    The client does no unit arithmetic. There is none anywhere in it, and this
    field is what keeps it that way: a slot boost cap is stored in hPa absolute
    and edited in psi gauge, so somebody has to convert, and the side that owns
    :func:`~simoscal.tune.units.psi_from_hpa` is the side that should. A second
    implementation of that conversion on a tablet is the same class of drift the
    dry run exists to avoid.

    :attr:`editor` names one screen's draft and is the only string the client
    switches on:

    ``boost``
        A per-slot cap curve. :attr:`values` is one row of psi gauge, one per
        rpm breakpoint, and :attr:`slot` says which slot it belongs to.
    ``lambda``
        One time-row of the full-load enrichment map, with :attr:`row` naming it.
    ``limiters``
        One scalar, with :attr:`key` naming which — ``soft``/``medium``/``hard``
        for the cylinder-cut trio, ``static`` for the standstill cap, ``speed``
        for the road-speed quartet.
    ``slots``
        The per-slot switchboard.
    ``table``
        The generic grid editor: :attr:`values` is the whole table, in the
        table's own units, and the recommendation already names the table.

    :attr:`editor` names the screen even when nothing can be pre-loaded onto it,
    so the client never has to infer a destination from :attr:`Queued.routed_via`
    — routing stays a fact this side owns. A non-empty :attr:`reason` is what
    says the pre-load is absent, and why, in words a person can act on: a flag
    that is one press, an axis with no draft to put values in. Such a
    recommendation is still queued and still shown in full.

    :attr:`values` is always nested rows, the same shape discipline
    :class:`Preview` uses, so both read the same way round on the client.
    """

    editor: str = ""
    values: tuple = ()
    units: str = ""
    #: The switch-patch slot this stages onto; 0 when the editor has no slots.
    slot: int = 0
    #: The row of a per-row editor; -1 when the editor edits the whole thing.
    row: int = -1
    #: Which member of a multi-field editor — a limiter's name, a flag's key.
    key: str = ""
    #: Why there is nothing to stage. Empty whenever :attr:`editor` is set.
    reason: str = ""


@dataclass(frozen=True)
class Preview:
    """What the replay actually computed — not what the recommendation claimed.

    ``encoded`` is read back off the buffer after the write, before the buffer
    was put back, so it is what the bin would really hold. A cell that cannot be
    represented exactly shows up here as :attr:`quantized`, with the worst error
    in :attr:`max_abs_quantization`.
    """

    before: tuple
    requested: tuple
    encoded: tuple
    quantized: bool
    max_abs_quantization: float
    warning: str = ""
    #: What the three arrays above are in — the **table's** own units, which are
    #: not always the units the owning editor works in (a slot cap is stored in
    #: hPa absolute and edited in psi gauge). Carried so a client can label these
    #: numbers without guessing, and without reusing
    #: :attr:`Staging.units`, which describes a different set of numbers.
    units: str = ""


@dataclass(frozen=True)
class Queued:
    """A recommendation the guards accepted, with the effect it would really have."""

    recommendation: Recommendation
    routed_via: str
    preview: Preview
    #: Every ``(space, table, row, col)`` the replay actually changed — measured
    #: by diffing the table across the write, not inferred from the selection.
    footprint: frozenset
    #: The ids of other queued recommendations touching at least one of the same
    #: cells. Both are still queued; the reviewer is told rather than left to
    #: find out at Apply time.
    overlaps: tuple[str, ...] = ()
    #: Anything true about the replay a reviewer should read before accepting.
    note: str = ""
    #: How the owning editor pre-loads this change if it is accepted.
    staging: Staging = Staging()


@dataclass(frozen=True)
class Dropped:
    """A recommendation the guards refused, or one with no write path.

    ``reason`` is the refusal verbatim wherever the engine produced one. Never
    rendered as a suggestion; counted so the answering side can be improved.
    """

    recommendation: Recommendation
    reason: str
    routed_via: str = ""


@dataclass(frozen=True)
class ReviewResult:
    """The three lists, plus the file's own envelope fields."""

    queued: tuple[Queued, ...]
    dropped: tuple[Dropped, ...]
    malformed: tuple[MalformedRecord, ...]
    summary: str = ""
    schema_version: int = 0

    @property
    def counts(self) -> dict:
        """One count per outcome. They sum to the number of records in the file."""
        return {
            "queued": len(self.queued),
            "dropped": len(self.dropped),
            "malformed": len(self.malformed),
            "total": len(self.queued) + len(self.dropped) + len(self.malformed),
        }


class ProvenanceMismatch(AdviceRejected):
    """The file answers a different calibration than this session holds.

    Refused wholesale, before any replay. A reply aimed at another bin is not a
    set of weak recommendations — its cells are not the cells it thinks they
    are, so replaying it would produce previews that look reasonable and are
    about the wrong bytes.
    """


# --------------------------------------------------------------------------- #
# adapters — table -> the call that owns writing it
# --------------------------------------------------------------------------- #
#: What an adapter returns: the dry-run entries, and how the screen that owns
#: the table would pre-load the same change. Returned together, from one
#: function, so the call that knows which domain wrote it is also the one that
#: says which editor stages it — the pairing cannot drift.
Adapted = tuple[Sequence[EditEntry], Staging]


def _slot_curve(tune: Tune, name: str, space: str, target: np.ndarray) -> Adapted:
    """A per-slot boost grid: one curve, tiled across the meaningless Y rows."""
    slot = int(_SLOT_CURVE.match(name)["slot"])
    curve = _one_curve(tune, name, space, target)
    entry = tune.switchpatch.slot_curve(slot, hpa=curve, dry_run=True)
    # The *requested* curve, converted — not the encoded one. Staging is "type
    # this in for me", so the editor's own guards and the psi floor apply to it
    # exactly as they would to a number somebody typed, and the encoded figure
    # stays where it belongs: in the preview, saying what it will really become.
    # Unrounded, too — the editor snaps its own nudges and would only ever round
    # a staged cap *up*, which is the wrong direction for a boost ceiling.
    return (entry,), Staging(
        editor="boost",
        slot=slot,
        units="psi gauge",
        values=_as_tuple([psi_from_hpa(float(v)) for v in curve]),
    )


def _slot_rpm_axis(tune: Tune, name: str, space: str, target: np.ndarray) -> Adapted:
    return (tune.switchpatch.slot_rpm_axis(target.ravel(), dry_run=True),), Staging(
        editor="boost",
        reason=(
            "the rpm axis is shared by all five slots, so it is re-breakpointed "
            "in one advanced action rather than staged as a draft. Open Boost "
            "and use the axis editor there."
        ),
    )


def _slot_flag(tune: Tune, name: str, space: str, target: np.ndarray) -> Adapted:
    key = _SLOT_FLAG.match(name)["key"]
    slot = int(_SLOT_FLAG.match(name)["slot"])
    value = float(target.ravel()[0])
    if value not in (0.0, 1.0):
        raise EditRejected(
            f"{name} is a 0/1 flag; {value:g} is neither on nor off"
        )
    entries = tune.switchpatch.set_slot_flag(
        key, slots=(slot,), on=bool(value), dry_run=True
    )
    # A flag has two states and no in-between, so the Slots screen sends one
    # straight through rather than staging it. There is no draft to pre-load.
    return entries, Staging(
        editor="slots",
        key=key,
        slot=slot,
        reason=(
            f"a per-slot flag is one press on the Slots screen, not a staged "
            f"draft. Set {key} on slot {slot} "
            f"{'on' if value else 'off'} there."
        ),
    )


def _lambda_full_load(tune: Tune, name: str, space: str, target: np.ndarray) -> Adapted:
    row = _one_changed_row(tune, name, space, target)
    entry = tune.fueling.full_load_enrichment(target[row], row=row, dry_run=True)
    return (entry,), Staging(
        editor="lambda", row=row, units="lambda", values=_as_tuple(target[row]),
    )


def _rev_limit(tune: Tune, name: str, space: str, target: np.ndarray) -> Adapted:
    which = _REV_TRIO.match(name)["which"]
    value = float(target.ravel()[0])
    return tune.limits.rev_limits(
        space=space, dry_run=True, **{which: value}
    ), Staging(editor="limiters", key=which, units="rpm", values=_as_tuple(value))


def _static_rev_limit(tune: Tune, name: str, space: str, target: np.ndarray) -> Adapted:
    value = float(target.ravel()[0])
    return tune.limits.static_rev_limit(value, dry_run=True), Staging(
        editor="limiters", key="static", units="rpm", values=_as_tuple(value),
    )


def _speed_limiter(tune: Tune, name: str, space: str, target: np.ndarray) -> Adapted:
    value = float(target.ravel()[0])
    return tune.limits.speed_limiter(value, dry_run=True), Staging(
        editor="limiters", key="speed", units="km/h", values=_as_tuple(value),
    )


#: Logical name (or pattern) -> the label of the call that owns it, and the
#: adapter that calls it. Keyed by the profile's own logical names: a profile
#: that names its tables differently gets no adapter and its owner-locked tables
#: drop with their own ``owner`` sentence, which is the safe direction to fail.
_EXACT: dict[str, tuple[str, Callable]] = {
    "slot_put_rpm_axis": ("tune.switchpatch.slot_rpm_axis()", _slot_rpm_axis),
    "lambda_full_load": ("tune.fueling.full_load_enrichment()", _lambda_full_load),
    "static_rev_limit_at": ("tune.limits.static_rev_limit()", _static_rev_limit),
    "static_rev_limit_cvt": ("tune.limits.static_rev_limit()", _static_rev_limit),
    "static_rev_limit_dct": ("tune.limits.static_rev_limit()", _static_rev_limit),
    "static_rev_limit_mt": ("tune.limits.static_rev_limit()", _static_rev_limit),
    "speed_limiter_inactive": ("tune.limits.speed_limiter()", _speed_limiter),
    "speed_limiter_level1": ("tune.limits.speed_limiter()", _speed_limiter),
    "speed_limiter_level2": ("tune.limits.speed_limiter()", _speed_limiter),
    "speed_limiter_level3": ("tune.limits.speed_limiter()", _speed_limiter),
}

_PATTERNS: tuple[tuple[re.Pattern, str, Callable], ...] = (
    (_SLOT_CURVE, "tune.switchpatch.slot_curve()", _slot_curve),
    (_REV_TRIO, "tune.limits.rev_limits()", _rev_limit),
    # Last: the slot-flag pattern is the broadest and would otherwise swallow
    # the per-slot boost grid.
    (_SLOT_FLAG, "tune.switchpatch.set_slot_flag()", _slot_flag),
)


def _adapter(name: str) -> Optional[tuple[str, Callable]]:
    if name in _EXACT:
        return _EXACT[name]
    for pattern, label, fn in _PATTERNS:
        if pattern.match(name):
            return label, fn
    return None


def _one_curve(tune: Tune, name: str, space: str, target: np.ndarray) -> np.ndarray:
    """The single row a tiled grid should end at, or a refusal."""
    current = tune.values(name, space=space)
    changed = sorted({int(r) for r in np.nonzero(~np.isclose(target, current))[0]})
    if not changed:
        raise EditRejected(f"{name} already holds these values; nothing to change")
    rows = target[changed]
    if not np.allclose(rows, rows[0]):
        raise EditRejected(
            f"{name} is written as one curve tiled across every row, but this "
            f"change asks for different values in rows {changed} — state one curve"
        )
    return rows[0]


def _one_changed_row(tune: Tune, name: str, space: str, target: np.ndarray) -> int:
    current = tune.values(name, space=space)
    changed = sorted({int(r) for r in np.nonzero(~np.isclose(target, current))[0]})
    if not changed:
        raise EditRejected(f"{name} already holds these values; nothing to change")
    if len(changed) > 1:
        raise EditRejected(
            f"{name} is written one row at a time; this change spans rows "
            f"{changed} — split it into one recommendation per row"
        )
    return changed[0]


# --------------------------------------------------------------------------- #
# replay
# --------------------------------------------------------------------------- #
def _selection(rec: Recommendation) -> Selection:
    spec = rec.change.selection
    if spec.kind == "cells":
        return Selection.cells(spec.args)
    return Selection(spec.kind, tuple(spec.args))


def _placed(tune: Tune, name: str, space: str, rec: Recommendation) -> np.ndarray:
    """Current values with the recommendation's stated values put in.

    Placement only — the mask comes from the engine's own :class:`Selection`, so
    which cells are addressed is decided by the same code the real edit path
    uses.
    """
    current = tune.values(name, space=space)
    mask = _selection(rec).mask(current.shape)
    target = current.copy()
    if rec.change.value is not None:
        target[mask] = float(rec.change.value)
    else:
        operand = np.asarray(rec.change.array, dtype=np.float64)
        count = int(mask.sum())
        if operand.size == count:
            target[mask] = operand.ravel()
        elif operand.shape == current.shape:
            target[mask] = operand[mask]
        else:
            raise EditRejected(
                f"the array does not match the selection: {operand.size} "
                f"value(s) for {count} cell(s) of {name}"
            )
    return target


def _footprint(entries: Sequence[EditEntry]) -> frozenset:
    """The cells a replay really changed, measured across the write."""
    cells = set()
    for entry in entries:
        if entry.before is None or entry.after is None:
            continue
        before = np.atleast_2d(np.asarray(entry.before, dtype=np.float64))
        after = np.atleast_2d(np.asarray(entry.after, dtype=np.float64))
        if before.shape != after.shape:
            continue
        for r, c in zip(*np.nonzero(~np.isclose(before, after))):
            cells.add((entry.space, entry.name, int(r), int(c)))
    return frozenset(cells)


def _as_tuple(values) -> tuple:
    arr = np.atleast_2d(np.asarray(values, dtype=np.float64))
    return tuple(tuple(float(v) for v in row) for row in arr)


def _preview_from_entries(
    tune: Tune, name: str, space: str, target: np.ndarray,
    entries: Sequence[EditEntry], units: str = "",
) -> Preview:
    named = next(
        (e for e in entries if e.name == name and e.after is not None), None
    )
    encoded = target if named is None else np.asarray(named.after, dtype=np.float64)
    before = (
        tune.values(name, space=space) if named is None or named.before is None
        else np.asarray(named.before, dtype=np.float64)
    )
    requested = np.asarray(target, dtype=np.float64)
    enc = np.asarray(encoded, dtype=np.float64)
    quantized = requested.shape != enc.shape or not np.array_equal(requested, enc)
    worst = (
        float(np.max(np.abs(requested - enc)))
        if requested.shape == enc.shape else float("nan")
    )
    return Preview(
        _as_tuple(before), _as_tuple(requested), _as_tuple(enc),
        bool(quantized), worst,
        " ".join(e.warning for e in entries if e.warning).strip(),
        units,
    )


def _replay(
    tune: Tune, rec: Recommendation
) -> tuple[str, Preview, frozenset, str, Staging]:
    """Replay one recommendation. Raises :class:`EditRejected` on a refusal."""
    name, space = rec.table.name, rec.change.space
    try:
        resolved = tune.table(name, space=space)
    except (TuneError, KeyError) as exc:
        raise EditRejected(
            f"this calibration has no table {name!r} in the {space!r} space "
            f"({exc})"
        ) from None

    if resolved.spec.key is not None and str(resolved.spec.key) != rec.table.id:
        raise EditRejected(
            f"the file calls {name!r} `{rec.table.id}`, but this calibration "
            f"resolves it as {resolved.label} — the recommendation and the "
            "table it names do not agree"
        )

    if not resolved.domain_owned:
        result = apply_op(
            tune, name, rec.change.operation, space=space,
            selection=_selection(rec), value=rec.change.value,
            array=rec.change.array,
            intent=rec.intent, dry_run=True,
        )
        entry = result.entry
        preview = Preview(
            _as_tuple(entry.before) if entry.before is not None else (),
            _as_tuple(result.requested), _as_tuple(result.encoded),
            bool(result.quantized), result.max_abs_quantization(),
            result.warning or "",
            resolved.units or "",
        )
        # The generic editor already works in the table's own units, so the
        # requested grid is staged verbatim — this is the one editor that needs
        # no conversion, which is exactly why it is the generic one.
        staging = Staging(
            editor="table",
            units=resolved.units or "",
            values=_as_tuple(result.requested),
        )
        return "bridge op `edit`", preview, _footprint((entry,)), "", staging

    if name in NOT_ADAPTED:
        raise EditRejected(f"{resolved.label} has no courier write path: {NOT_ADAPTED[name]}")

    routed = _adapter(name)
    if routed is None:
        raise EditRejected(
            f"{resolved.label} is owned by {resolved.owner}. The courier has no "
            "adapter for it, so it cannot be replayed here."
        )
    label, adapt = routed

    if rec.change.operation not in _PLACEMENT_OPS:
        raise EditRejected(
            f"{resolved.label} is written by {label}, which is given the values "
            f"the table should end at. Operation {rec.change.operation!r} asks "
            "for arithmetic the owning call cannot perform — state the resulting "
            "values instead."
        )

    target = _placed(tune, name, space, rec)
    # Belt and braces: every adapter already asks its domain call for a dry run,
    # and nesting is safe, so this block guarantees the rewind even if an adapter
    # ever forgets — or if a multi-call adapter refuses partway through. This is
    # the path the whole safety claim rests on; it does not rely on care taken in
    # the adapters.
    with tune.dry_run():
        try:
            entries, staging = adapt(tune, name, space, target)
        except (ValueError, TypeError, KeyError) as exc:
            # The domain calls raise a loud ValueError for their guard refusals —
            # the boost/limiter analog of apply_op's EditRejected. Either way the
            # words are the engine's, not ours.
            if isinstance(exc, EditRejected):
                raise
            raise EditRejected(str(exc)) from None
        preview = _preview_from_entries(
            tune, name, space, target, entries, resolved.units or ""
        )
        footprint = _footprint(entries)
    note = ""
    others = sorted({e.name for e in entries} - {name})
    if others:
        note = (
            f"{label} writes {', '.join(others)} in the same call — accepting "
            "this changes those too."
        )
    return label, preview, footprint, note, staging


# --------------------------------------------------------------------------- #
# public surface
# --------------------------------------------------------------------------- #
def review(
    tune: Tune,
    text: str,
    *,
    provenance: Optional[dict] = None,
) -> ReviewResult:
    """Validate a recommendations file and replay it against ``tune``, dry.

    ``text`` is the file's contents; reading and hash-verifying the file itself
    belongs to the caller that owns paths (the bridge op). ``provenance`` is the
    session's own — when given, the file's must match it or the whole file is
    refused with :class:`ProvenanceMismatch` before any replay.

    Raises :class:`~simoscal.advice.schema.AdviceRejected` when the *envelope*
    cannot be read; a single malformed record is counted, never fatal.

    The session is not modified. Every replay runs inside
    :meth:`Tune.dry_run`, so the journal, the undo history and the bytes are as
    they were when this returned.
    """
    parsed, malformed = parse_partial(text)
    if provenance is not None:
        _check_provenance(parsed, provenance)

    queued: list[Queued] = []
    dropped: list[Dropped] = []
    for rec in parsed.recommendations:
        try:
            label, preview, footprint, note, staging = _replay(tune, rec)
        except EditRejected as exc:
            dropped.append(Dropped(rec, str(exc), _routed_label(tune, rec)))
            continue
        queued.append(Queued(rec, label, preview, footprint, (), note, staging))

    queued = _flag_overlaps(queued)
    return ReviewResult(
        tuple(queued), tuple(dropped), malformed,
        parsed.summary, parsed.schema_version,
    )


def _routed_label(tune: Tune, rec: Recommendation) -> str:
    """Best-effort "which call would have written this", for a dropped record."""
    try:
        resolved = tune.table(rec.table.name, space=rec.change.space)
    except (TuneError, KeyError):
        return ""
    if not resolved.domain_owned:
        return "bridge op `edit`"
    routed = _adapter(rec.table.name)
    return routed[0] if routed else ""


def _check_provenance(parsed: RecommendationFile, session: dict) -> None:
    problems = []
    for field, key in (
        ("profile", "profile"),
        ("bin_sha256", "bin_sha256"),
        ("xdf_sha256", "xdf_sha256"),
    ):
        expected = session.get(key)
        if expected is None:
            continue
        actual = getattr(parsed.provenance, field)
        if str(expected).lower() != str(actual).lower():
            problems.append(Problem(
                f"provenance.{field}", field,
                f"this file answers {field} {actual!r}, but the open session is "
                f"{expected!r} — the recommendations are about a different "
                "calibration and were not replayed",
            ))
    if problems:
        raise ProvenanceMismatch(problems)


def _flag_overlaps(queued: list[Queued]) -> list[Queued]:
    """Mark queued items sharing a cell.

    Recommendations are validated **independently against current session
    state**, not cumulatively, so two that each pass alone can still conflict
    when both are accepted. Saying so here is what stops that being discovered
    at Apply time.
    """
    out = []
    for i, item in enumerate(queued):
        others = tuple(
            other.recommendation.id
            for j, other in enumerate(queued)
            if j != i and item.footprint & other.footprint
        )
        # `replace` rather than a positional rebuild: this function only has an
        # opinion about `overlaps`, and a rebuild silently drops whatever field
        # is added to Queued next — as it already would have dropped `staging`.
        out.append(replace(item, overlaps=others))
    return out
