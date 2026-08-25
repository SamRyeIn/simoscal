"""Shared machinery for the domain modules.

A domain module is a thin, named face on :meth:`Tune.write`. It exists to turn
"what a tuner means" into "which cells, in which units, with which guard" —
and to make the safety-relevant decisions once, in the library, where they can
be tested, rather than once per revision script where they can be mistyped.

Nothing here writes bytes itself; everything routes through ``Tune.write``, so
every domain call is journaled by construction.
"""

from __future__ import annotations

import functools
import inspect
from typing import TYPE_CHECKING, Callable, Optional, TypeVar

import numpy as np

from ..journal import (
    KIND_GUARDED_CEILING,
    KIND_RAW,
    VERDICT_BLOCKED,
    VERDICT_GUARDED_SKIP,
    VERDICT_UNCHANGED,
    EditEntry,
)
from ..profile import TAG_FLOAT_BUG

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..project import Tune

__all__ = ["Domain", "dry_runnable", "float_bug_write", "guarded_ceiling"]

F = TypeVar("F", bound=Callable)


def dry_runnable(method: F) -> F:
    """Give a domain edit call a ``dry_run=`` keyword.

    With ``dry_run=True`` the call runs unchanged inside
    :meth:`~simoscal.tune.project.Tune.dry_run`: every guard it has, the same
    encode, the same exception and message on a refusal, the same returned
    :class:`~simoscal.tune.journal.EditEntry` — and then the session is rewound
    so nothing was journaled and no byte moved.

    A decorator rather than a parameter each method threads by hand, because
    the property being claimed is *equivalence*: the dry path and the real path
    must disagree about nothing except whether state moved. Twenty-eight
    hand-written branches would be twenty-eight chances to diverge; here the
    body is literally the same code either way, and the only thing that varies
    is what happens around it.

    The entry it hands back describes an edit that did **not** happen. A caller
    showing it to a person is showing a prediction — an accurate one, since it
    came off the real buffer before the rewind — not a record.
    """
    @functools.wraps(method)
    def wrapper(self, *args, dry_run: bool = False, **kwargs):
        if not dry_run:
            return method(self, *args, **kwargs)
        with self._tune.dry_run():
            return method(self, *args, **kwargs)

    wrapper.__signature__ = _signature_with_dry_run(method)
    return wrapper  # type: ignore[return-value]


def _signature_with_dry_run(method: Callable) -> inspect.Signature:
    """``method``'s signature plus a keyword-only ``dry_run: bool = False``."""
    sig = inspect.signature(method)
    params = list(sig.parameters.values())
    extra = inspect.Parameter(
        "dry_run", inspect.Parameter.KEYWORD_ONLY, default=False, annotation=bool,
    )
    at = next(
        (i for i, prm in enumerate(params) if prm.kind is prm.VAR_KEYWORD),
        len(params),
    )
    params.insert(at, extra)
    return sig.replace(parameters=params)


class Domain:
    """Base for the per-area facades reached as ``tune.<domain>``."""

    def __init__(self, tune: "Tune") -> None:
        self._tune = tune

    def _values(self, name: str, space: str = "base") -> np.ndarray:
        return self._tune.values(name, space=space)

    def _table_set(self, set_name: str, space: str = "base") -> tuple[str, ...]:
        """The open bin's own names for a set this domain writes together.

        Required by construction: a domain call whose whole meaning is "write
        these together" cannot proceed on a car that has not said which tables
        those are, and the tuple it would otherwise reach for belongs to a
        different engine. :meth:`~simoscal.tune.profile.Profile.table_set`
        raises with the profile named.
        """
        return self._tune.space(space).profile.table_set(set_name)

    def _optional_table_set(
        self, set_name: str, space: str = "base"
    ) -> tuple[str, ...]:
        """As :meth:`_table_set`, but ``()`` when this car declares none.

        For the sets that only sharpen what a journal entry *says* — which of
        four written variants is the live one, say. Absence there is a car
        nobody has established that for, and the honest output is the sentence
        without the clause, exactly as
        :attr:`~simoscal.tune.profile.Profile.stock_references` handles the
        numbers. Never use this for a set that decides what gets written.
        """
        return tuple(self._tune.space(space).profile.table_sets.get(set_name, ()))

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"<{type(self).__name__} of {self._tune!r}>"


def guarded_ceiling(
    tune: "Tune", name: str, target: float, *, intent: str, space: str = "base"
) -> EditEntry:
    """Raise every cell of a limiter to ``target`` — never lower a higher one.

    The guide's rule, and the library's since ``sop_recipe``: a limiter you are
    trying to get *out of the way* should only ever move up. A cell already
    above the target is left alone and the outcome says so, because silently
    lowering a ceiling is how a tune gets quietly more restrictive than its
    author believes.

    Refuses outright to write above the table's declared upper limit, unless
    the table is a known float-bug table whose XDF limit is a TunerPro editor
    artifact rather than an ECU one.
    """
    resolved = tune.table(name, space=space)
    current = np.asarray(resolved.view.values, dtype=np.float64)
    tol = 1e-6 * (abs(target) + 1.0)

    zmax = resolved.view.table.z.max if resolved.view.table.z is not None else None
    if zmax is not None and target > zmax + tol and not resolved.has(TAG_FLOAT_BUG):
        return tune.note(
            name, space=space, verdict=VERDICT_BLOCKED,
            kind=KIND_GUARDED_CEILING, intent=intent,
            detail=(f"target {target:.6g} exceeds the table's declared upper "
                    f"limit {zmax:.6g} — refusing to overflow a limiter "
                    "ceiling; table left byte-identical"),
        )

    below = current < target - tol
    if not below.any():
        already_equal = float(np.abs(current - target).max()) <= tol
        return tune.note(
            name, space=space, intent=intent, kind=KIND_GUARDED_CEILING,
            verdict=VERDICT_UNCHANGED if already_equal else VERDICT_GUARDED_SKIP,
            detail=(
                f"already at the {target:.6g} target"
                if already_equal
                else f"all {current.size} cell(s) already at or above "
                     f"{target:.6g} (max {current.max():.6g}) — left unchanged, "
                     "never lowered"
            ),
        )

    staged = current.copy()
    staged[below] = target
    raised = int(below.sum())
    detail = (
        f"raised {raised} of {current.size} cell(s) that were below "
        f"{target:.6g} (lowest was {current[below].min():.6g}); any cell "
        "already at or above the target was left unchanged"
    ) if current.size > 1 else ""
    entry = tune.write(
        name, staged, intent=intent, space=space,
        kind=KIND_GUARDED_CEILING, detail=detail,
    )
    return entry


def float_bug_write(
    tune: "Tune", name: str, value: float, *, intent: str, space: str = "base"
) -> EditEntry:
    """Write a float-bug table past its declared display maximum, deliberately.

    Some Simos constants are float32 with an identity equation and an XDF
    ``max`` that the *stock* value already exceeds — the limit is a TunerPro
    editor artifact, not an ECU one. Writing through ``set`` trips the
    FloatBugGuard, so this routes to a raw element write instead.

    That is only sound while the equation really is identity, so this checks it
    on the live table rather than trusting the tag, and fails loud otherwise —
    writing a physical value into a scaled store would be off by the scaling
    factor, silently.
    """
    resolved = tune.table(name, space=space)
    if not resolved.has(TAG_FLOAT_BUG):
        raise ValueError(
            f"{resolved.label} is not marked as a float-bug table in the "
            f"{tune.space(space).profile.name} profile — write it normally"
        )
    raw = np.asarray(resolved.view.raw, dtype=np.float64)
    physical = np.asarray(resolved.view.values, dtype=np.float64)
    if not np.allclose(raw, physical, rtol=1e-9, atol=0):
        raise ValueError(
            f"{resolved.label}: raw {raw.ravel()[0]:.6g} and physical "
            f"{physical.ravel()[0]:.6g} differ, so its equation is not "
            "identity — a raw write would be off by the scaling factor. "
            "Refusing (fail loud)."
        )
    return tune.write(
        name, np.full(physical.shape, float(value)), intent=intent, space=space,
        kind=KIND_RAW, raw=True,
        detail=("written as a raw element value: the XDF display maximum is a "
                "TunerPro editor artifact, not an ECU limit, and the equation "
                "is identity so raw and physical are the same number"),
    )


def require_shape(
    values: np.ndarray, expected: tuple[int, ...], what: str
) -> np.ndarray:
    """Fail loud on a wrong-sized declaration before it reaches a bin."""
    arr = np.asarray(values, dtype=np.float64)
    if arr.shape != expected:
        raise ValueError(
            f"{what}: expected shape {expected}, got {arr.shape}"
        )
    if not np.all(np.isfinite(arr)):
        raise ValueError(f"{what}: every value must be finite")
    return arr


def nearest_index(axis: Optional[np.ndarray], target: float, what: str) -> int:
    """Index of the breakpoint nearest ``target`` on a decoded axis."""
    if axis is None:
        raise ValueError(f"{what}: table has no decodable axis to index into")
    flat = np.asarray(axis, dtype=np.float64).ravel()
    return int(np.argmin(np.abs(flat - target)))
