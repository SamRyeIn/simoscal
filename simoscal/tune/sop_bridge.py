"""Run the basics-guide SOP recipe and fold its outcomes into the journal.

``sop_recipe`` predates this package and stays as it is: it applies the whole
``ecu-tuning-basics`` guide in one pass and reports a
:class:`~simoscal.sop_recipe.TableOutcome` per table. That is the right shape
for what it does, and rewriting it onto the journal types would risk changing
numbers that R00–R12 were validated against.

The problem is that it writes through ``TableView`` directly, so its bytes are
invisible to the journal — and a build whose audit allowance comes from the
journal would flag every one of them as unexplained.

So this bridge does the attribution honestly rather than waiving it: it
snapshots the whole buffer, runs the recipe, diffs, and assigns each changed
byte to the outcome whose table contains it. Any changed byte that lands in no
reported table's extent raises, because that is the recipe having written
somewhere it did not report — exactly the condition the audit exists to catch.
"""

from __future__ import annotations

import warnings
from typing import TYPE_CHECKING, Optional, Sequence

import numpy as np

from .. import sop_recipe
from ..binimage import BinImage
from ..calfile import CalFile
from ..model import SimosCalError
from ..safety import EditRangeWarning
from ..sop_recipe import TableOutcome
from . import audit
from .journal import (
    KIND_SOP,
    VERDICT_APPLIED,
    VERDICT_BLOCKED,
    VERDICT_GUARDED_SKIP,
    VERDICT_SKIPPED,
    VERDICT_UNCHANGED,
    EditEntry,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .project import Tune

__all__ = ["SopBridgeError", "apply_basics_sop", "positional_axis_match"]

#: Re-exported so the domain modules apply the recipe's proven axis-match
#: tolerance rather than inventing a second one.
positional_axis_match = sop_recipe.positional_axis_match

_VERDICTS = {
    sop_recipe.OUTCOME_APPLIED: VERDICT_APPLIED,
    sop_recipe.OUTCOME_APPLIED_BUILDOUT: VERDICT_APPLIED,
    sop_recipe.OUTCOME_ALREADY_SATISFIED: VERDICT_UNCHANGED,
    sop_recipe.OUTCOME_GUARDED_SKIP: VERDICT_GUARDED_SKIP,
    sop_recipe.OUTCOME_GUARD_BLOCKED: VERDICT_BLOCKED,
    sop_recipe.OUTCOME_AXIS_MISMATCH: VERDICT_SKIPPED,
    sop_recipe.OUTCOME_POOR_FIT: VERDICT_SKIPPED,
    sop_recipe.OUTCOME_UNRESOLVED: VERDICT_SKIPPED,
    sop_recipe.OUTCOME_SKIPPED: VERDICT_SKIPPED,
}


class SopBridgeError(SimosCalError):
    """The recipe changed bytes that none of its reported outcomes explains."""


def apply_basics_sop(
    tune: "Tune",
    *,
    space: str = "base",
    symbol_map: Optional[Sequence] = None,
    intent: str = "",
) -> tuple[EditEntry, ...]:
    """Apply the whole basics-guide SOP, journaling one entry per outcome.

    Returns the journaled entries. The recipe's own
    :class:`~simoscal.sop_recipe.RecipeReport` is kept on the tune as
    :attr:`Tune.recipe_report`, so ``build()`` can run its coherence rules —
    the ones that mark a boost change without matching fuelling as **DO NOT
    FLASH** — over what actually happened.
    """
    table_space = tune.space(space)
    cal = table_space.cal
    before = np.frombuffer(cal.binimage.to_bytes(), dtype=np.uint8)

    with warnings.catch_warnings():
        # The recipe deliberately writes past several XDF display ranges; those
        # warnings are captured into its own outcomes, not lost.
        warnings.simplefilter("ignore", EditRangeWarning)
        report = (
            sop_recipe.apply_basics_sop(cal)
            if symbol_map is None
            else sop_recipe.apply_basics_sop(cal, tuple(symbol_map))
        )

    after = np.frombuffer(cal.binimage.to_bytes(), dtype=np.uint8)
    unattributed = set(np.flatnonzero(before != after).tolist())

    # A read-only view of the pre-recipe bytes, so each entry can carry the
    # table's real before/after values rather than the recipe's report-only
    # scalars — which for a multi-cell guarded ceiling are a min and a target,
    # not a table, and would fail the readback gate if taken as one.
    prior = CalFile(
        cal.model,
        BinImage(
            before.tobytes(),
            region_start=cal.binimage.region_start,
            region_size=cal.binimage.region_size,
        ),
        structure=cal.structure,
    )

    entries: list[EditEntry] = []
    for outcome in report.outcomes:
        offsets = _attribute(cal, outcome, unattributed)
        unattributed -= offsets
        entries.append(tune.journal.record(_entry(
            tune, space, outcome, offsets, prior=prior, intent=intent
        )))

    tune.recipe_report = report

    if unattributed:
        sample = ", ".join(hex(o) for o in sorted(unattributed)[:12])
        raise SopBridgeError(
            f"the basics SOP changed {len(unattributed)} byte(s) that none of "
            f"its {len(report.outcomes)} reported outcomes accounts for: "
            f"{sample}. Refusing to journal an unexplained write."
        )
    return tuple(entries)


def _attribute(cal, outcome: TableOutcome, changed: set[int]) -> frozenset[int]:
    """The changed bytes lying inside this outcome's table."""
    if not outcome.symbol or not changed:
        return frozenset()
    try:
        view = cal.get(outcome.symbol)
        extent = audit.table_byte_offsets(view)
    except Exception:  # noqa: BLE001 - an unresolvable symbol simply owns nothing
        return frozenset()
    return frozenset(extent & changed)


def _entry(
    tune: "Tune", space: str, outcome: TableOutcome,
    offsets: frozenset[int], *, prior: CalFile, intent: str,
) -> EditEntry:
    label = f"`{outcome.symbol or '—'}`"
    units = ""
    before = after = None
    key = outcome.symbol or ""
    try:
        resolved = tune.space(space).cal.get(key)
        if resolved.title:
            label = f"`{key}` — {resolved.title}"
        units = resolved.units or ""
        after = np.asarray(resolved.values, dtype=np.float64)
        before = np.asarray(prior.get(key).values, dtype=np.float64)
    except Exception:  # noqa: BLE001 - a skip row's joined symbol owns no table
        key = ""

    detail = outcome.detail
    if outcome.outcome not in (
        sop_recipe.OUTCOME_APPLIED, sop_recipe.OUTCOME_APPLIED_BUILDOUT
    ):
        detail = f"recipe outcome `{outcome.outcome}`" + (f" — {detail}" if detail else "")
    return EditEntry(
        space=space,
        name=outcome.symbol or outcome.guide_section,
        label=label,
        key=key,
        kind=KIND_SOP,
        verdict=_VERDICTS.get(outcome.outcome, VERDICT_SKIPPED),
        units=units,
        intent=intent or outcome.guide_section,
        before=before,
        after=after,
        offsets=offsets,
        rows_changed=_rows_changed(before, after),
        detail=detail,
        warning=outcome.warning,
    )


def _rows_changed(before, after) -> tuple[int, ...]:
    """Which rows the recipe moved, for the report's row-scoped summary."""
    if before is None or after is None or before.shape != after.shape:
        return ()
    if before.ndim != 2:
        return ()
    diff = ~np.isclose(before, after, rtol=0, atol=1e-12)
    return tuple(int(r) for r in np.flatnonzero(diff.any(axis=1)))
