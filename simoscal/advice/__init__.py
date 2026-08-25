"""simoscal.advice — the courier half of "Tune with Claude".

A session's context leaves the device as one bundle, a person asks Claude
anywhere, and the answer comes back as a *recommendations file*. Nothing in that
file is trusted: it is validated against :mod:`simoscal.advice.schema` and then
replayed through the library's real edit guards with ``dry_run=True`` before a
human is shown anything. Refusals never render as suggestions.

Nothing in this package imports :mod:`simoscal.bridge`. What this module
re-exports is the schema alone — pure, with no I/O, no session and no numpy —
because it has to be readable by whoever is *answering*, which may be a model
with no session and no scientific stack in front of it. It is versioned
independently of ``BRIDGE_VERSION`` because a file authored outside the app will
change shape faster than the app's protocol does.

The heavier halves are imported from their own modules rather than from here, so
that stays true: :mod:`~simoscal.advice.bundle` and :mod:`~simoscal.advice.brief`
describe an open :class:`~simoscal.tune.project.Tune`, and
:mod:`~simoscal.advice.review` replays against one.

Passing validation means a file is *readable*. It never means its advice is
safe — the dry-run replay is what means that.
"""

from __future__ import annotations

from .schema import (
    AdviceRejected,
    CONFIDENCE_LEVELS,
    Change,
    OPERATIONS,
    MalformedRecord,
    Problem,
    Provenance,
    RISK_TIERS,
    Recommendation,
    RecommendationFile,
    SCHEMA_VERSION,
    SELECTION_ARITY,
    SELECTION_KINDS,
    SUPPORTED_SCHEMA_VERSIONS,
    SelectionSpec,
    TableRef,
    dumps,
    parse,
    parse_partial,
    to_obj,
    validate,
    validate_partial,
)

__all__ = [
    "AdviceRejected",
    "CONFIDENCE_LEVELS",
    "Change",
    "OPERATIONS",
    "MalformedRecord",
    "Problem",
    "Provenance",
    "RISK_TIERS",
    "Recommendation",
    "RecommendationFile",
    "SCHEMA_VERSION",
    "SELECTION_ARITY",
    "SELECTION_KINDS",
    "SUPPORTED_SCHEMA_VERSIONS",
    "SelectionSpec",
    "TableRef",
    "dumps",
    "parse",
    "parse_partial",
    "to_obj",
    "validate",
    "validate_partial",
]
