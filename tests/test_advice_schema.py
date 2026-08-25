"""The recommendations schema: what a malformed answer must not get past.

Two of these are product requirements rather than type checks, and are called
out as such because losing either quietly would be losing the feature's point:
**evidence is mandatory** (D6/AE3 — "no evidence, no flag" is structural, not a
reviewer's discipline) and the **risk tier is a closed set** (a tier that
arrived unannounced would render unstyled, so a safety-relevant item could be
thumbed past like any other).

The rest guard the property the whole module exists for: that a caller learns
*everything* wrong with a file in one pass, each problem naming the record and
the field, so an answering model can fix the file in one round trip.

Pure module, so no fixtures and no bin: everything here runs anywhere.
"""

from __future__ import annotations

import json

import pytest

from simoscal.advice import (
    AdviceRejected,
    CONFIDENCE_LEVELS,
    OPERATIONS,
    RISK_TIERS,
    SCHEMA_VERSION,
    SELECTION_ARITY,
    dumps,
    parse,
    to_obj,
    validate,
)

BIN_SHA = "a" * 64
XDF_SHA = "b" * 64


def _rec(**over) -> dict:
    rec = {
        "id": "rec-1",
        "table": {
            "name": "slot_put_max_1",
            "id": "IP_FAC_BPA_SP[1]",
            "description": "Map for boost pressure actuator setpoint",
        },
        "change": {
            "space": "patch",
            "operation": "set",
            "selection": {"kind": "cells", "args": [[0, 7]]},
            "value": 57.5,
        },
        "intent": "pull wastegate duty where the pull knocked",
        "evidence": "pull #3, rows 188-204, knock count 3, IAT 48 C",
        "risk": "safety-relevant",
        "confidence": "medium",
        "prediction": "knock count returns to 0 across 5000-6000 rpm at similar IAT",
    }
    rec.update(over)
    return rec


def _file(records=None, **over) -> dict:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "provenance": {
            "profile": "SC8S50",
            "bin_sha256": BIN_SHA,
            "xdf_sha256": XDF_SHA,
        },
        "summary": "one knock-driven change",
        "recommendations": [_rec()] if records is None else records,
    }
    payload.update(over)
    return payload


def _reject(payload) -> list:
    """Validate a payload expected to fail, returning its problems."""
    parsed, problems = validate(payload)
    assert parsed is None, "expected the file to be rejected"
    assert problems
    return problems


def _fields(problems) -> set:
    return {p.field for p in problems}


# --------------------------------------------------------------------------- #
# happy path
# --------------------------------------------------------------------------- #
def test_three_recommendations_validate_and_round_trip():
    records = [
        _rec(id="rec-1"),
        _rec(id="rec-2", change={
            "space": "base",
            "operation": "mul",
            "selection": {"kind": "region", "args": [0, 2, 3, 5]},
            "value": 0.95,
        }),
        _rec(id="rec-3", risk="performance", confidence="low", change={
            "space": "base",
            "operation": "paste",
            "selection": {"kind": "row", "args": [4]},
            "array": [[1.0, 2.0], [3.0, 4.0]],
        }),
    ]
    parsed = parse(json.dumps(_file(records)))

    assert parsed.schema_version == SCHEMA_VERSION
    assert parsed.provenance.bin_sha256 == BIN_SHA
    assert [r.id for r in parsed.recommendations] == ["rec-1", "rec-2", "rec-3"]

    first = parsed.recommendations[0]
    assert first.table.name == "slot_put_max_1"
    assert first.table.label == "`IP_FAC_BPA_SP[1]` — Map for boost pressure actuator setpoint"
    assert first.change.selection.kind == "cells"
    assert first.change.selection.args == ((0, 7),)
    assert first.change.value == pytest.approx(57.5)
    assert first.change.array is None
    assert first.risk == "safety-relevant"
    assert first.prediction.startswith("knock count returns")

    assert parsed.recommendations[1].change.selection.args == (0, 2, 3, 5)
    assert parsed.recommendations[2].change.array == ((1.0, 2.0), (3.0, 4.0))

    # every field survives a render/read cycle, byte-for-byte on the second pass
    again = parse(dumps(parsed))
    assert again == parsed
    assert dumps(again) == dumps(parsed)
    assert to_obj(parsed)["recommendations"][0]["table"]["id"] == "IP_FAC_BPA_SP[1]"


def test_zero_recommendations_is_a_valid_answer():
    """Claude looked and found nothing — distinguishable from a parse failure."""
    parsed = parse(json.dumps(_file([], summary="nothing in these logs justifies a change")))
    assert parsed.recommendations == ()
    assert parsed.summary == "nothing in these logs justifies a change"


def test_operand_free_operations_need_no_value():
    parsed = parse(json.dumps(_file([_rec(change={
        "space": "base",
        "operation": "interpolate",
        "selection": {"kind": "region", "args": [0, 0, 2, 6]},
    })])))
    change = parsed.recommendations[0].change
    assert change.operation == "interpolate"
    assert change.value is None and change.array is None


def test_summary_is_optional():
    payload = _file()
    del payload["summary"]
    assert parse(json.dumps(payload)).summary == ""


# --------------------------------------------------------------------------- #
# the two product rules
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("evidence", ["", "   "], ids=["empty", "whitespace"])
def test_evidence_is_mandatory(evidence):
    """D6/AE3: a recommendation citing nothing is malformed, not merely weak."""
    problems = _reject(_file([_rec(evidence=evidence)]))
    assert [p.where for p in problems] == ["recommendations[0].evidence"]
    assert problems[0].field == "evidence"
    assert "must not be empty" in problems[0].message


def test_evidence_missing_entirely_names_the_record_and_the_field():
    record = _rec()
    del record["evidence"]
    problems = _reject(_file([record]))
    assert [(p.where, p.field) for p in problems] == [
        ("recommendations[0].evidence", "evidence")
    ]


def test_risk_tier_is_a_closed_set():
    problems = _reject(_file([_rec(risk="mild")]))
    assert [p.field for p in problems] == ["risk"]
    assert "unknown risk 'mild'" in problems[0].message
    for tier in RISK_TIERS:
        assert tier in problems[0].message


@pytest.mark.parametrize("tier", RISK_TIERS)
def test_every_declared_risk_tier_is_accepted(tier):
    assert parse(json.dumps(_file([_rec(risk=tier)]))).recommendations[0].risk == tier


@pytest.mark.parametrize("level", CONFIDENCE_LEVELS)
def test_every_declared_confidence_level_is_accepted(level):
    parsed = parse(json.dumps(_file([_rec(confidence=level)])))
    assert parsed.recommendations[0].confidence == level


def test_confidence_is_a_closed_set():
    problems = _reject(_file([_rec(confidence="pretty sure")]))
    assert [p.field for p in problems] == ["confidence"]


def test_prediction_is_mandatory():
    """G6: an accepted recommendation must be gradeable by the next log review."""
    record = _rec()
    del record["prediction"]
    problems = _reject(_file([record]))
    assert [p.field for p in problems] == ["prediction"]


# --------------------------------------------------------------------------- #
# versioning
# --------------------------------------------------------------------------- #
def test_a_newer_schema_version_is_one_explained_rejection():
    """Not a field-by-field failure: the fields are written to a schema we do not know."""
    payload = _file([_rec(evidence="", risk="mild")], schema_version=SCHEMA_VERSION + 7)
    problems = _reject(payload)
    assert len(problems) == 1
    assert problems[0].field == "schema_version"
    assert "newer than" in problems[0].message
    assert str(SCHEMA_VERSION + 7) in problems[0].message
    # the malformed records underneath were not reported at all
    assert "evidence" not in problems[0].message


def test_an_older_schema_version_says_so():
    problems = _reject(_file(schema_version=0))
    assert len(problems) == 1
    assert "older than" in problems[0].message


def test_a_missing_schema_version_is_rejected():
    payload = _file()
    del payload["schema_version"]
    problems = _reject(payload)
    assert [p.field for p in problems] == ["schema_version"]


# --------------------------------------------------------------------------- #
# table identity — both halves, always
# --------------------------------------------------------------------------- #
def test_a_named_table_without_a_description_is_rejected():
    """The project rule is `ID` — Description; one half alone is malformed."""
    record = _rec()
    del record["table"]["description"]
    problems = _reject(_file([record]))
    assert [(p.where, p.field) for p in problems] == [
        ("recommendations[0].table.description", "description")
    ]


def test_a_described_table_without_an_id_is_rejected():
    record = _rec()
    del record["table"]["id"]
    problems = _reject(_file([record]))
    assert [p.field for p in problems] == ["id"]


def test_a_table_without_a_logical_name_is_rejected():
    """The logical name is what a replay resolves and edits through."""
    record = _rec()
    del record["table"]["name"]
    problems = _reject(_file([record]))
    assert [p.field for p in problems] == ["name"]


# --------------------------------------------------------------------------- #
# the change block
# --------------------------------------------------------------------------- #
def test_an_unknown_operation_is_rejected():
    problems = _reject(_file([_rec(change={
        "space": "base",
        "operation": "nudge",
        "selection": {"kind": "all", "args": []},
        "value": 1.0,
    })]))
    assert [p.field for p in problems] == ["operation"]


def test_an_unknown_selection_kind_is_rejected():
    problems = _reject(_file([_rec(change={
        "space": "base",
        "operation": "set",
        "selection": {"kind": "diagonal", "args": [1]},
        "value": 1.0,
    })]))
    assert [p.field for p in problems] == ["kind"]


@pytest.mark.parametrize(
    "kind,args",
    [("row", []), ("row", [1, 2]), ("region", [0, 1]), ("col", [0, 0])],
)
def test_selection_arity_is_enforced(kind, args):
    problems = _reject(_file([_rec(change={
        "space": "base",
        "operation": "set",
        "selection": {"kind": kind, "args": args},
        "value": 1.0,
    })]))
    assert [p.field for p in problems] == ["args"]


def test_a_cells_selection_wants_row_col_pairs():
    problems = _reject(_file([_rec(change={
        "space": "base",
        "operation": "set",
        "selection": {"kind": "cells", "args": [[0, 1], [2]]},
        "value": 1.0,
    })]))
    assert [p.field for p in problems] == ["args"]
    assert "recommendations[0].change.selection.args[1]" == problems[0].where


def test_an_empty_cells_selection_is_rejected():
    problems = _reject(_file([_rec(change={
        "space": "base",
        "operation": "set",
        "selection": {"kind": "cells", "args": []},
        "value": 1.0,
    })]))
    assert [p.field for p in problems] == ["args"]


def test_set_without_an_operand_is_rejected():
    problems = _reject(_file([_rec(change={
        "space": "base",
        "operation": "set",
        "selection": {"kind": "all", "args": []},
    })]))
    assert [p.field for p in problems] == ["value"]
    assert "requires 'value' or 'array'" in problems[0].message


def test_paste_requires_an_array_not_a_scalar():
    problems = _reject(_file([_rec(change={
        "space": "base",
        "operation": "paste",
        "selection": {"kind": "all", "args": []},
        "value": 3.0,
    })]))
    assert [p.field for p in problems] == ["array"]


def test_restore_takes_no_operand():
    problems = _reject(_file([_rec(change={
        "space": "base",
        "operation": "restore",
        "selection": {"kind": "all", "args": []},
        "value": 3.0,
    })]))
    assert [p.field for p in problems] == ["operation"]


def test_value_and_array_together_are_rejected():
    problems = _reject(_file([_rec(change={
        "space": "base",
        "operation": "set",
        "selection": {"kind": "all", "args": []},
        "value": 3.0,
        "array": [1.0, 2.0],
    })]))
    assert [p.field for p in problems] == ["array"]
    assert "not both" in problems[0].message


@pytest.mark.parametrize("value", [float("nan"), float("inf")], ids=["nan", "inf"])
def test_a_non_finite_value_is_rejected(value):
    payload = _file([_rec(change={
        "space": "base",
        "operation": "set",
        "selection": {"kind": "all", "args": []},
        "value": value,
    })])
    # json.dumps writes NaN/Infinity as bare literals; the point is the value,
    # so validate the object directly rather than testing the JSON dialect.
    problems = _reject(payload)
    assert [p.field for p in problems] == ["value"]


def test_a_boolean_is_not_a_physical_value():
    problems = _reject(_file([_rec(change={
        "space": "base",
        "operation": "set",
        "selection": {"kind": "all", "args": []},
        "value": True,
    })]))
    assert [p.field for p in problems] == ["value"]


def test_a_ragged_array_is_rejected():
    problems = _reject(_file([_rec(change={
        "space": "base",
        "operation": "paste",
        "selection": {"kind": "all", "args": []},
        "array": [[1.0, 2.0], [3.0]],
    })]))
    assert [p.field for p in problems] == ["array"]


def test_a_non_numeric_array_cell_names_its_position():
    problems = _reject(_file([_rec(change={
        "space": "base",
        "operation": "paste",
        "selection": {"kind": "all", "args": []},
        "array": [[1.0, 2.0], [3.0, "4.0"]],
    })]))
    assert problems[0].where == "recommendations[0].change.array[1][1]"


def test_a_missing_space_is_rejected():
    """Which space is not guessable: base and patch hold same-named tables."""
    problems = _reject(_file([_rec(change={
        "operation": "set",
        "selection": {"kind": "all", "args": []},
        "value": 1.0,
    })]))
    assert [p.field for p in problems] == ["space"]


# --------------------------------------------------------------------------- #
# envelope, provenance, and reporting everything at once
# --------------------------------------------------------------------------- #
def test_provenance_is_required_and_hashes_must_look_like_hashes():
    problems = _reject(_file(provenance={
        "profile": "SC8S50",
        "bin_sha256": "not-a-hash",
        "xdf_sha256": XDF_SHA,
    }))
    assert [p.field for p in problems] == ["bin_sha256"]
    assert "64-character hex" in problems[0].message


def test_a_missing_provenance_block_is_rejected():
    payload = _file()
    del payload["provenance"]
    problems = _reject(payload)
    assert [p.field for p in problems] == ["provenance"]


def test_duplicate_recommendation_ids_are_rejected():
    problems = _reject(_file([_rec(id="rec-1"), _rec(id="rec-1")]))
    assert [p.field for p in problems] == ["id"]
    assert "duplicate id 'rec-1'" in problems[0].message


def test_an_unknown_field_is_named_rather_than_ignored():
    """A typo'd field name is the failure mode; silently dropping it hides it."""
    record = _rec()
    record["evidance"] = record.pop("evidence")
    problems = _reject(_file([record]))
    assert {p.field for p in problems} == {"evidance", "evidence"}


def test_every_problem_in_a_file_is_reported_at_once():
    """One pass, not one error per round trip."""
    problems = _reject(_file([
        _rec(id="a", evidence=""),
        _rec(id="b", risk="mild"),
        _rec(id="c", change={
            "space": "base",
            "operation": "set",
            "selection": {"kind": "row", "args": [0, 1]},
        }),
    ]))
    assert {p.where for p in problems} == {
        "recommendations[0].evidence",
        "recommendations[1].risk",
        "recommendations[2].change.selection.args",
        "recommendations[2].change.value",
    }
    # and the exception carries them all, printably
    with pytest.raises(AdviceRejected) as excinfo:
        parse(json.dumps(_file([
            _rec(id="a", evidence=""),
            _rec(id="b", risk="mild"),
        ])))
    assert len(excinfo.value.problems) == 2
    assert "2 problems in the recommendations file" in str(excinfo.value)
    assert "recommendations[1].risk" in str(excinfo.value)


def test_malformed_json_is_one_clear_failure_not_a_stack_trace():
    with pytest.raises(AdviceRejected) as excinfo:
        parse('{"schema_version": 1, "recommendations": [')
    problems = excinfo.value.problems
    assert len(problems) == 1
    assert "not valid JSON" in problems[0].message
    assert "line 1" in problems[0].message


def test_a_non_object_document_is_rejected_cleanly():
    with pytest.raises(AdviceRejected) as excinfo:
        parse("[]")
    assert len(excinfo.value.problems) == 1
    assert "must be a JSON object" in excinfo.value.problems[0].message


def test_recommendations_must_be_a_list():
    problems = _reject(_file(recommendations={"rec-1": {}}))
    assert [p.field for p in problems] == ["recommendations"]


def test_a_recommendation_that_is_not_an_object_is_rejected():
    problems = _reject(_file(["just a sentence"]))
    assert problems[0].where == "recommendations[0]"


# --------------------------------------------------------------------------- #
# drift: the closed sets are restatements, so pin them to their sources
# --------------------------------------------------------------------------- #
def test_operations_match_the_engine_exactly():
    """``advice`` restates EditOp to stay session-free; drift would let a file
    name an operation the replay cannot perform, or refuse one it can."""
    from simoscal.tune.editing import EditOp

    assert set(OPERATIONS) == {op.value for op in EditOp}


def test_selection_kinds_match_the_engine_exactly():
    from simoscal.tune.editing import Selection

    for kind, arity in SELECTION_ARITY.items():
        if kind == "all":
            assert Selection.all().kind == "all"
        elif kind == "cells":
            assert Selection.cells([(0, 1)]).kind == "cells"
        elif kind == "region":
            sel = Selection.region(0, 1, 2, 3)
            assert sel.kind == "region" and len(sel.args) == arity
        else:
            sel = getattr(Selection, kind)(0)
            assert sel.kind == kind and len(sel.args) == arity


def test_the_schema_module_imports_no_session_and_no_bridge():
    """The answering side reads this schema; it must not drag the engine along."""
    import ast
    from pathlib import Path

    for module in ("__init__.py", "schema.py"):
        src = Path(__file__).resolve().parents[1] / "simoscal" / "advice" / module
        tree = ast.parse(src.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                assert "bridge" not in name, f"{module} imports {name}"
                assert "numpy" not in name, f"{module} imports {name}"
                assert not name.startswith("simoscal.tune"), f"{module} imports {name}"
                assert not name.startswith(".tune"), f"{module} imports {name}"


def test_the_reference_docs_example_actually_validates():
    """The doc is what the answering side copies from; a stale example is a bug."""
    import re
    from pathlib import Path

    doc = Path(__file__).resolve().parents[1] / "docs" / "advice-schema.md"
    text = doc.read_text(encoding="utf-8")
    blocks = re.findall(r"```json\n(.*?)```", text, re.DOTALL)
    assert blocks, "the schema reference has no JSON example"
    parsed = parse(blocks[0])
    assert parsed.schema_version == SCHEMA_VERSION
    assert parsed.recommendations[0].table.id == "IP_FAC_BPA_SP[1]"

    # every closed set is documented, by name
    for member in (*RISK_TIERS, *CONFIDENCE_LEVELS, *OPERATIONS):
        assert f"`{member}`" in text, f"the schema reference does not document {member!r}"
    for kind in SELECTION_ARITY:
        assert f'"kind": "{kind}"' in text, f"the schema reference does not show a {kind!r} selection"
