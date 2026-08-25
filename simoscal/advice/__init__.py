"""simoscal.advice — the courier half of "Tune with Claude".

A session's context leaves the device as one bundle, a person asks Claude
anywhere, and the answer comes back as a *recommendations file*. Nothing in that
file is trusted: it is validated against :mod:`simoscal.advice.schema` and then
replayed through the library's real edit guards with ``dry_run=True`` before a
human is shown anything. Refusals never render as suggestions.

Everything in this package is pure by design — no I/O, no session, no numpy in
the public surface, and nothing imported from :mod:`simoscal.bridge`. The
schema has to be readable by whoever is *answering*, which may be a model with
no session in front of it, and it is versioned independently of
``BRIDGE_VERSION`` because a file authored outside the app will change shape
faster than the app's protocol does.

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
