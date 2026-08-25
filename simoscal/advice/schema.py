"""The recommendations file — what an off-device answer must look like to be read.

A person exports a context bundle, asks Claude anywhere, and brings back a
*recommendations file*. This module is the gate that file passes before anything
in it is replayed against a session. It is the schema and nothing else: no I/O,
no session, no numpy, no bridge. Give it text, get back typed records or a list
of everything wrong with them.

Three properties are worth stating, because each is a product requirement rather
than a type check:

* **Evidence is mandatory** (D6). A recommendation with nothing cited is
  *malformed*, not merely weak — it is rejected here and never reaches the
  review queue, so "no evidence, no flag" is structural rather than a
  reviewer's discipline.
* **The closed sets are closed.** ``risk``, ``confidence``, ``operation`` and
  the selection ``kind`` each accept a fixed vocabulary. A tier nobody has seen
  before is refused rather than passed through to render unstyled, and an
  operation this library cannot perform is refused before a replay discovers it.
* **Validation is a rejection list, not a boolean.** A caller gets every problem
  with every record at once, each naming the record and the field, so whoever is
  answering can fix a whole file in one pass instead of one error per round trip.

**Versioned independently of the bridge** (D5): the file is authored *outside*
the app by a model and will change shape faster than ``BRIDGE_VERSION``. A
version this library does not understand is one clean, explained rejection —
never a field-by-field failure that buries the real reason.

**The change is addressed exactly the way an op addresses it.** ``space``,
``operation``, ``selection`` and ``value``/``array`` are the same fields, with
the same meanings and the same ``{"kind": ..., "args": [...]}`` selection
encoding, that ``bridge.dispatch``'s ``edit`` op already takes — so a replay
passes them through rather than translating them. Selection args map onto
:class:`~simoscal.tune.editing.Selection` as::

    {"kind": "all"}                                -> Selection.all()
    {"kind": "row",    "args": [3]}                -> Selection.row(3)
    {"kind": "col",    "args": [7]}                -> Selection.col(7)
    {"kind": "region", "args": [0, 2, 3, 5]}       -> Selection.region(0, 2, 3, 5)
    {"kind": "cells",  "args": [[3, 7], [3, 8]]}   -> Selection.cells([(3, 7), (3, 8)])

Which *op* replays a given record is not a field in the file: it follows from
which table the record names (D3). A recommendation is not a new invariant, so
it gets no new write path — it gets the ones that already exist.

What this module does **not** check, deliberately: whether the table exists in
this profile, whether the selection fits its shape, whether the value is one the
guards accept. Those are questions only a live session can answer, and the
dry-run replay answers them with the real guards' own words. Passing validation
here means the file is *readable*, never that its advice is *safe*.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Optional, Sequence

__all__ = [
    "AdviceRejected",
    "Change",
    "CONFIDENCE_LEVELS",
    "OPERATIONS",
    "Problem",
    "Provenance",
    "Recommendation",
    "RecommendationFile",
    "RISK_TIERS",
    "SCHEMA_VERSION",
    "SELECTION_ARITY",
    "SELECTION_KINDS",
    "SUPPORTED_SCHEMA_VERSIONS",
    "SelectionSpec",
    "TableRef",
    "dumps",
    "parse",
    "to_obj",
    "validate",
]

#: What this library writes and prefers to read.
SCHEMA_VERSION = 1

#: Every version this library can read. A file outside the set is rejected with
#: one message that says so — see :func:`parse`.
SUPPORTED_SCHEMA_VERSIONS = frozenset({1})

#: The risk tiers, closed. The UI styles safety-relevant items distinctly, so a
#: tier that arrived unannounced would render as though it were ordinary.
RISK_TIERS = ("cosmetic", "performance", "safety-relevant")

#: Stated confidence, closed. A queue is read comparatively; free text ("fairly
#: sure?") cannot be compared, sorted, or back-tested against outcomes.
CONFIDENCE_LEVELS = ("low", "medium", "high")

#: The generic operations, mirroring :class:`~simoscal.tune.editing.EditOp`.
#: Restated rather than imported so this module stays free of the session layer;
#: ``tests/test_advice_schema.py`` asserts the two never drift apart.
OPERATIONS = ("set", "add", "sub", "mul", "div", "fill", "interpolate", "paste", "restore")

#: Selection kind -> how many args it takes. ``cells`` is variadic: its args are
#: the ``[row, col]`` pairs themselves. Mirrors
#: :meth:`~simoscal.tune.editing.Selection.mask`; the same drift test pins it.
SELECTION_ARITY: dict[str, Optional[int]] = {
    "all": 0,
    "row": 1,
    "col": 1,
    "region": 4,
    "cells": None,
}

#: The selection kinds, closed.
SELECTION_KINDS = tuple(SELECTION_ARITY)

#: Operations that must carry an operand, and those that must not. ``paste``
#: needs an array specifically; ``interpolate`` and ``restore`` derive their
#: result from the table itself, so an operand on either means the author
#: misunderstood the operation rather than mistyped a number.
_NEEDS_OPERAND = ("set", "add", "sub", "mul", "div", "fill")
_NEEDS_ARRAY = ("paste",)
_TAKES_NO_OPERAND = ("interpolate", "restore")

_ENVELOPE_KEYS = {"schema_version", "provenance", "summary", "recommendations"}
_PROVENANCE_KEYS = {"profile", "bin_sha256", "xdf_sha256"}
_RECORD_KEYS = {
    "id", "table", "change", "intent", "evidence", "risk", "confidence", "prediction",
}
_TABLE_KEYS = {"name", "id", "description"}
_CHANGE_KEYS = {"space", "operation", "selection", "value", "array"}
_SELECTION_KEYS = {"kind", "args"}

_HEX = frozenset("0123456789abcdef")


# --------------------------------------------------------------------------- #
# problems
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Problem:
    """One reason a file was rejected, addressed precisely enough to fix.

    ``where`` is a path into the document (``recommendations[2].change.value``)
    and ``field`` the leaf it names, so a caller can both print the problem and
    key off the field programmatically.
    """

    where: str
    field: str
    message: str

    def __str__(self) -> str:
        return f"{self.where}: {self.message}"


class AdviceRejected(Exception):
    """A recommendations file that could not be read, with every reason at once.

    :attr:`problems` is the whole list — never the first failure only. Reporting
    one problem per round trip is what turns fixing a file into a conversation.
    """

    def __init__(self, problems: Sequence[Problem]):
        self.problems: tuple[Problem, ...] = tuple(problems)
        count = len(self.problems)
        noun = "problem" if count == 1 else "problems"
        detail = "\n".join(f"  - {p}" for p in self.problems)
        super().__init__(f"{count} {noun} in the recommendations file:\n{detail}")


# --------------------------------------------------------------------------- #
# records
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Provenance:
    """Which calibration the answer was written against.

    Carried so a reply can be matched to the bundle that prompted it. A reply
    aimed at a different bin is not a set of weak recommendations — it is a set
    of recommendations about cells that are not the cells it thinks they are.
    Checking that match needs a session, so it belongs to the replay; this
    module only guarantees the fields are present and well-formed.
    """

    profile: str
    bin_sha256: str
    xdf_sha256: str


@dataclass(frozen=True)
class TableRef:
    """Which table, named both ways.

    ``name`` is the logical (profile) name a replay resolves and edits through.
    ``id`` and ``description`` are the ``` `ID` — Description ``` halves the
    project names tables by everywhere a human reads them. Both halves are
    required: a recommendation that names one without the other is rejected, so
    a reviewer never has to look up what they are being asked to change.
    """

    name: str
    id: str
    description: str

    @property
    def label(self) -> str:
        """The project's naming form: ``` `ID` — Description ```."""
        return f"`{self.id}` — {self.description}"


@dataclass(frozen=True)
class SelectionSpec:
    """Which cells, in the encoding the ``edit`` op already takes."""

    kind: str
    args: tuple = ()


@dataclass(frozen=True)
class Change:
    """The proposed write, addressed the way an op addresses one.

    ``array`` is nested tuples (a tuple of floats for a vector, a tuple of
    tuples for a grid) so a record is immutable and hashable; :func:`dumps`
    renders it back to JSON lists.
    """

    space: str
    operation: str
    selection: SelectionSpec
    value: Optional[float] = None
    array: Optional[tuple] = None


@dataclass(frozen=True)
class Recommendation:
    """One proposed change, with everything that makes it reviewable.

    Every field is required. The three that are requirements rather than data —
    ``evidence`` (D6/AE3), ``risk`` (styled distinctly), ``prediction``
    (G6: an accepted recommendation must be gradeable by the next log review) —
    are enforced here rather than left to the reviewer to notice missing.
    """

    id: str
    table: TableRef
    change: Change
    intent: str
    evidence: str
    risk: str
    confidence: str
    prediction: str


@dataclass(frozen=True)
class RecommendationFile:
    """The whole envelope: which schema, which calibration, what was found.

    Zero recommendations is a valid, meaningful answer — "I looked and found
    nothing to change" — and is distinguishable from a file that failed to
    parse, because that raises :class:`AdviceRejected` instead.
    """

    schema_version: int
    provenance: Provenance
    recommendations: tuple[Recommendation, ...]
    summary: str = ""


# --------------------------------------------------------------------------- #
# validation helpers — each appends to `problems`, none raises
# --------------------------------------------------------------------------- #
def _is_number(v: Any) -> bool:
    # bool is an int in Python; a boolean where a physical value belongs is a
    # mistake worth naming rather than silently reading as 0.0 or 1.0.
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _is_index(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _nonempty_str(problems: list, where: str, obj: dict, key: str) -> str:
    value = obj.get(key)
    if value is None:
        problems.append(Problem(f"{where}.{key}", key, f"missing required field {key!r}"))
        return ""
    if not isinstance(value, str):
        problems.append(Problem(f"{where}.{key}", key, f"{key!r} must be a string, got {type(value).__name__}"))
        return ""
    if not value.strip():
        problems.append(Problem(f"{where}.{key}", key, f"{key!r} must not be empty"))
        return ""
    return value


def _closed(problems: list, where: str, obj: dict, key: str, allowed: Sequence[str]) -> str:
    value = _nonempty_str(problems, where, obj, key)
    if value and value not in allowed:
        problems.append(Problem(
            f"{where}.{key}", key,
            f"unknown {key} {value!r}; expected one of {', '.join(allowed)}",
        ))
        return ""
    return value


def _unknown_keys(problems: list, where: str, obj: dict, allowed: set) -> None:
    for key in sorted(set(obj) - allowed):
        problems.append(Problem(
            f"{where}.{key}" if where else key, key,
            f"unknown field {key!r}; expected one of {', '.join(sorted(allowed))}",
        ))


def _sha256(problems: list, where: str, obj: dict, key: str) -> str:
    value = _nonempty_str(problems, where, obj, key)
    if value and (len(value) != 64 or not set(value.lower()) <= _HEX):
        problems.append(Problem(f"{where}.{key}", key, f"{key!r} must be a 64-character hex sha256"))
        return ""
    return value.lower()


def _array(problems: list, where: str, raw: Any) -> Optional[tuple]:
    """A rectangular 1-D or 2-D array of finite numbers, as nested tuples."""
    if not isinstance(raw, list) or not raw:
        problems.append(Problem(f"{where}.array", "array", "'array' must be a non-empty list"))
        return None
    if all(isinstance(row, list) for row in raw):
        widths = {len(row) for row in raw}
        if widths == {0} or len(widths) != 1:
            problems.append(Problem(f"{where}.array", "array", "'array' rows must all be the same non-zero length"))
            return None
        for r, row in enumerate(raw):
            for c, cell in enumerate(row):
                if not _is_number(cell):
                    problems.append(Problem(
                        f"{where}.array[{r}][{c}]", "array",
                        f"array cell ({r},{c}) must be a finite number, got {cell!r}",
                    ))
                    return None
        return tuple(tuple(float(cell) for cell in row) for row in raw)
    for i, cell in enumerate(raw):
        if not _is_number(cell):
            problems.append(Problem(
                f"{where}.array[{i}]", "array",
                f"array element {i} must be a finite number, got {cell!r}",
            ))
            return None
    return tuple(float(cell) for cell in raw)


def _selection(problems: list, where: str, raw: Any) -> SelectionSpec:
    where = f"{where}.selection"
    if not isinstance(raw, dict):
        problems.append(Problem(where, "selection", "'selection' must be an object with 'kind' and 'args'"))
        return SelectionSpec("", ())
    _unknown_keys(problems, where, raw, _SELECTION_KEYS)
    kind = _closed(problems, where, raw, "kind", SELECTION_KINDS)
    args = raw.get("args", [])
    if not isinstance(args, list):
        problems.append(Problem(f"{where}.args", "args", "'args' must be a list"))
        return SelectionSpec(kind, ())
    if not kind:
        return SelectionSpec(kind, ())

    arity = SELECTION_ARITY[kind]
    if kind == "cells":
        if not args:
            problems.append(Problem(f"{where}.args", "args", "a 'cells' selection must name at least one cell"))
            return SelectionSpec(kind, ())
        cells = []
        for i, pair in enumerate(args):
            if not (isinstance(pair, list) and len(pair) == 2 and all(_is_index(v) for v in pair)):
                problems.append(Problem(
                    f"{where}.args[{i}]", "args",
                    f"cell {i} must be a [row, col] pair of non-negative integers, got {pair!r}",
                ))
                return SelectionSpec(kind, ())
            cells.append((int(pair[0]), int(pair[1])))
        return SelectionSpec(kind, tuple(cells))

    if len(args) != arity:
        problems.append(Problem(
            f"{where}.args", "args",
            f"a {kind!r} selection takes {arity} arg(s), got {len(args)}",
        ))
        return SelectionSpec(kind, ())
    if not all(_is_index(v) for v in args):
        problems.append(Problem(
            f"{where}.args", "args",
            f"a {kind!r} selection's args must be non-negative integers, got {args!r}",
        ))
        return SelectionSpec(kind, ())
    return SelectionSpec(kind, tuple(int(v) for v in args))


def _change(problems: list, where: str, raw: Any) -> Change:
    where = f"{where}.change"
    if not isinstance(raw, dict):
        problems.append(Problem(where, "change", "'change' must be an object"))
        return Change("", "", SelectionSpec("", ()))
    _unknown_keys(problems, where, raw, _CHANGE_KEYS)
    space = _nonempty_str(problems, where, raw, "space")
    operation = _closed(problems, where, raw, "operation", OPERATIONS)
    selection = _selection(problems, where, raw.get("selection"))

    before_operand = len(problems)
    value = raw.get("value")
    if value is not None and not _is_number(value):
        problems.append(Problem(f"{where}.value", "value", f"'value' must be a finite number, got {value!r}"))
        value = None
    else:
        value = None if value is None else float(value)

    array = None if raw.get("array") is None else _array(problems, where, raw["array"])

    # Operand presence is structural: it follows from the operation alone, so a
    # record that gets it wrong is unreadable rather than merely unwise. Skipped
    # when the operand itself was already rejected — an unreadable value would
    # otherwise be reported a second time as a missing one, and a reader fixing
    # the file would chase a problem that is not there.
    if operation and len(problems) == before_operand:
        has_operand = value is not None or array is not None
        if operation in _TAKES_NO_OPERAND and has_operand:
            problems.append(Problem(
                f"{where}.operation", "operation",
                f"operation {operation!r} takes neither 'value' nor 'array'; it derives its result from the table",
            ))
        elif operation in _NEEDS_ARRAY and array is None:
            problems.append(Problem(
                f"{where}.array", "array",
                f"operation {operation!r} requires 'array'",
            ))
        elif operation in _NEEDS_OPERAND and not has_operand:
            problems.append(Problem(
                f"{where}.value", "value",
                f"operation {operation!r} requires 'value' or 'array'",
            ))
        elif value is not None and array is not None:
            problems.append(Problem(
                f"{where}.array", "array",
                "name 'value' or 'array', not both",
            ))
    return Change(space, operation, selection, value, array)


def _table(problems: list, where: str, raw: Any) -> TableRef:
    where = f"{where}.table"
    if not isinstance(raw, dict):
        problems.append(Problem(where, "table", "'table' must be an object with 'name', 'id' and 'description'"))
        return TableRef("", "", "")
    _unknown_keys(problems, where, raw, _TABLE_KEYS)
    # Both halves of `ID` — Description, always: the project's naming rule is a
    # schema requirement here, not a style note.
    return TableRef(
        _nonempty_str(problems, where, raw, "name"),
        _nonempty_str(problems, where, raw, "id"),
        _nonempty_str(problems, where, raw, "description"),
    )


def _record(problems: list, index: int, raw: Any) -> Recommendation:
    where = f"recommendations[{index}]"
    if not isinstance(raw, dict):
        problems.append(Problem(where, "", "each recommendation must be an object"))
        return Recommendation("", TableRef("", "", ""), Change("", "", SelectionSpec("", ())), "", "", "", "", "")
    _unknown_keys(problems, where, raw, _RECORD_KEYS)
    return Recommendation(
        id=_nonempty_str(problems, where, raw, "id"),
        table=_table(problems, where, raw.get("table")),
        change=_change(problems, where, raw.get("change")),
        intent=_nonempty_str(problems, where, raw, "intent"),
        evidence=_nonempty_str(problems, where, raw, "evidence"),
        risk=_closed(problems, where, raw, "risk", RISK_TIERS),
        confidence=_closed(problems, where, raw, "confidence", CONFIDENCE_LEVELS),
        prediction=_nonempty_str(problems, where, raw, "prediction"),
    )


def _provenance(problems: list, raw: Any) -> Provenance:
    where = "provenance"
    if not isinstance(raw, dict):
        problems.append(Problem(where, "provenance", "'provenance' must be an object naming the bundle this answers"))
        return Provenance("", "", "")
    _unknown_keys(problems, where, raw, _PROVENANCE_KEYS)
    return Provenance(
        _nonempty_str(problems, where, raw, "profile"),
        _sha256(problems, where, raw, "bin_sha256"),
        _sha256(problems, where, raw, "xdf_sha256"),
    )


# --------------------------------------------------------------------------- #
# public surface
# --------------------------------------------------------------------------- #
def validate(payload: Any) -> tuple[Optional[RecommendationFile], list[Problem]]:
    """Validate an already-decoded JSON object; never raises.

    Returns the parsed file and an empty problem list, or ``None`` and every
    problem found. :func:`parse` is the usual entry point; this exists for a
    caller that already holds decoded JSON and wants the list rather than an
    exception.

    A version this library does not understand short-circuits: reporting the
    fields of a document written to a schema we do not know would be reporting
    noise, and would bury the one problem that matters.
    """
    problems: list[Problem] = []
    if not isinstance(payload, dict):
        return None, [Problem("", "", "the recommendations file must be a JSON object")]

    version = payload.get("schema_version")
    if version is None:
        return None, [Problem("schema_version", "schema_version", "missing required field 'schema_version'")]
    if not isinstance(version, int) or isinstance(version, bool):
        return None, [Problem("schema_version", "schema_version", f"'schema_version' must be an integer, got {version!r}")]
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        known = ", ".join(str(v) for v in sorted(SUPPORTED_SCHEMA_VERSIONS))
        relation = "newer than" if version > SCHEMA_VERSION else "older than"
        return None, [Problem(
            "schema_version", "schema_version",
            f"schema version {version} is {relation} this library understands "
            f"(it reads version{'s' if len(SUPPORTED_SCHEMA_VERSIONS) > 1 else ''} {known}); "
            "the file was not read further",
        )]

    _unknown_keys(problems, "", payload, _ENVELOPE_KEYS)
    provenance = _provenance(problems, payload.get("provenance"))

    summary = payload.get("summary", "")
    if not isinstance(summary, str):
        problems.append(Problem("summary", "summary", "'summary' must be a string"))
        summary = ""

    raw_records = payload.get("recommendations")
    records: list[Recommendation] = []
    if raw_records is None:
        problems.append(Problem("recommendations", "recommendations", "missing required field 'recommendations'"))
    elif not isinstance(raw_records, list):
        problems.append(Problem("recommendations", "recommendations", "'recommendations' must be a list"))
    else:
        records = [_record(problems, i, raw) for i, raw in enumerate(raw_records)]
        seen: dict[str, int] = {}
        for i, rec in enumerate(records):
            if not rec.id:
                continue
            if rec.id in seen:
                problems.append(Problem(
                    f"recommendations[{i}].id", "id",
                    f"duplicate id {rec.id!r}; recommendations[{seen[rec.id]}] already used it",
                ))
            else:
                seen[rec.id] = i

    if problems:
        return None, problems
    return RecommendationFile(version, provenance, tuple(records), summary), []


def parse(text: str) -> RecommendationFile:
    """Read a recommendations file from JSON text.

    Raises :class:`AdviceRejected` carrying **every** problem found. Malformed
    JSON is one problem with the decoder's own position, not a traceback: the
    file is authored by a model that will be handed this message to fix it.
    """
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AdviceRejected([Problem(
            "", "",
            f"the file is not valid JSON: {exc.msg} (line {exc.lineno}, column {exc.colno})",
        )]) from None
    parsed, problems = validate(payload)
    if parsed is None:
        raise AdviceRejected(problems)
    return parsed


def _change_obj(change: Change) -> dict:
    obj: dict[str, Any] = {
        "space": change.space,
        "operation": change.operation,
        "selection": {
            "kind": change.selection.kind,
            "args": [list(a) if isinstance(a, tuple) else a for a in change.selection.args],
        },
    }
    if change.value is not None:
        obj["value"] = change.value
    if change.array is not None:
        obj["array"] = [
            list(row) if isinstance(row, tuple) else row for row in change.array
        ]
    return obj


def to_obj(parsed: RecommendationFile) -> dict:
    """The JSON-ready object for a parsed file, with stable key order."""
    return {
        "schema_version": parsed.schema_version,
        "provenance": {
            "profile": parsed.provenance.profile,
            "bin_sha256": parsed.provenance.bin_sha256,
            "xdf_sha256": parsed.provenance.xdf_sha256,
        },
        "summary": parsed.summary,
        "recommendations": [
            {
                "id": rec.id,
                "table": {
                    "name": rec.table.name,
                    "id": rec.table.id,
                    "description": rec.table.description,
                },
                "change": _change_obj(rec.change),
                "intent": rec.intent,
                "evidence": rec.evidence,
                "risk": rec.risk,
                "confidence": rec.confidence,
                "prediction": rec.prediction,
            }
            for rec in parsed.recommendations
        ],
    }


def dumps(parsed: RecommendationFile, *, indent: int = 2) -> str:
    """Render a parsed file back to JSON text.

    Key order is declared, not dict-iteration order, so the same file renders to
    the same bytes twice — the property the bundle side depends on (D7) and the
    one that lets two answers be diffed against each other.
    """
    return json.dumps(to_obj(parsed), indent=indent, ensure_ascii=False, sort_keys=False)
