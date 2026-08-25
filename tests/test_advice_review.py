"""`advice_review`: what a person is shown, and what the session did while deciding.

Two properties carry this module, and everything else here is in service of one
of them:

1. **A recommendation the guards refuse never reaches the queue.** It is dropped,
   counted, and its refusal reason is the engine's own sentence — so the answering
   side can be improved from words that are true, and a person is never shown a
   suggestion that could not be applied. AE2 is the canonical case.
2. **Reviewing a file changes nothing.** The journal, the undo history and every
   byte are identical before and after a review of a file containing every
   outcome type. That is the whole safety claim of the courier, and it is tested
   against a real bin rather than a fixture.

Real SC8S50 files are gitignored → tests skip (never fail) when absent.
"""

from __future__ import annotations

import hashlib
import json

import numpy as np
import pytest

from pathlib import Path

from simoscal.advice import AdviceRejected
from simoscal.advice.review import ProvenanceMismatch, review
from simoscal.tune import SC8S50, SessionHistory, Tune
from simoscal.tune.domains.switchpatch import PATCH_SPACE
from simoscal.tune.profiles.switchpatch_2933 import SWITCH_PATCH_2933

CODE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = CODE_ROOT.parent

STOCK_BIN = CODE_ROOT / "bin" / "5G0906259L__0002.bin"
XDF = CODE_ROOT / "xdf" / "SC8S50.V1.0.xdf"
SWITCH_XDF = REPO_ROOT / "BinToolz-main" / "definitions" / "S50 Switch Patch.29.33.V2.xdf"
PATCHED_BIN = (
    REPO_ROOT / "Tunes" / "TuningBasicsGuide" / "BinToolz-patched"
    / "CB_HSL_SP2933_5G0906259L_0002_BasicsGuide_R04.bin"
)

_have_base = STOCK_BIN.is_file() and XDF.is_file()
_have_patch = PATCHED_BIN.is_file() and SWITCH_XDF.is_file() and XDF.is_file()

#: A plainly generic table: grid cells, reversible, no owner.
GRID = "pressure_quotient_max"


@pytest.fixture
def base_tune() -> Tune:
    if not _have_base:
        pytest.skip("real SC8S50 bin/XDF absent")
    return Tune.open(SC8S50, xdf=XDF, bin=STOCK_BIN)


@pytest.fixture
def patched_tune() -> Tune:
    if not _have_patch:
        pytest.skip("patched bin / switch XDF absent")
    return Tune.open(
        SC8S50, xdf=XDF, bin=PATCHED_BIN,
        extra_spaces={PATCH_SPACE: (SWITCH_PATCH_2933, SWITCH_XDF)},
    )


# --------------------------------------------------------------------------- #
# building files
# --------------------------------------------------------------------------- #
def _ref(tune: Tune, name: str, space: str = "base") -> dict:
    """The table block as the bundle will render it: logical name + `ID` — Description."""
    resolved = tune.table(name, space=space)
    return {
        "name": name,
        "id": str(resolved.spec.key),
        "description": resolved.spec.description,
    }


def _rec(tune, name, *, space="base", operation="set", selection=None,
         value=None, array=None, id="rec", risk="performance", **over) -> dict:
    change = {
        "space": space,
        "operation": operation,
        "selection": selection or {"kind": "all", "args": []},
    }
    if value is not None:
        change["value"] = value
    if array is not None:
        change["array"] = array
    rec = {
        "id": id,
        "table": _ref(tune, name, space),
        "change": change,
        "intent": f"adjust {name}",
        "evidence": "pull #3, rows 188-204, knock count 3, IAT 48 C",
        "risk": risk,
        "confidence": "medium",
        "prediction": "the next pull shows the change and no new knock",
    }
    rec.update(over)
    return rec


def _file(records, **over) -> str:
    payload = {
        "schema_version": 1,
        "provenance": {
            "profile": "SC8S50",
            "bin_sha256": "a" * 64,
            "xdf_sha256": "b" * 64,
        },
        "recommendations": records,
    }
    payload.update(over)
    return json.dumps(payload)


def _fingerprint(tune: Tune) -> tuple:
    """Everything a review must not move: bytes, edit ledgers, journal, history."""
    seen: set[int] = set()
    digests, ledgers = [], []
    for name in sorted(tune.spaces):
        cal = tune.spaces[name].cal
        ledgers.append((name, tuple(cal.edited_ranges)))
        image = cal.binimage
        if id(image) in seen:
            continue
        seen.add(id(image))
        digests.append(hashlib.sha256(image.to_bytes()).hexdigest())
    return (tuple(digests), tuple(ledgers), tune.journal.entries)


# --------------------------------------------------------------------------- #
# happy path
# --------------------------------------------------------------------------- #
def test_three_valid_recommendations_are_all_queued_with_previews(base_tune):
    current = base_tune.values(GRID)
    records = [
        _rec(base_tune, GRID, id="a", operation="set",
             selection={"kind": "cells", "args": [[0, 0]]},
             value=float(current[0][0]) + 0.02),
        _rec(base_tune, GRID, id="b", operation="mul",
             selection={"kind": "row", "args": [1]}, value=1.01),
        _rec(base_tune, "put_setpoint", id="c", operation="set",
             selection={"kind": "cells", "args": [[0, 0]]},
             value=float(base_tune.values("put_setpoint")[0][0]) + 10.0),
    ]
    result = review(base_tune, _file(records))

    assert result.counts == {"queued": 3, "dropped": 0, "malformed": 0, "total": 3}
    assert [q.recommendation.id for q in result.queued] == ["a", "b", "c"]
    for item in result.queued:
        assert item.routed_via == "bridge op `edit`"
        assert item.preview.requested and item.preview.encoded
        assert item.footprint, "a queued item must know which cells it moves"
        # the preview is the real effect, re-decoded off the buffer
        assert np.asarray(item.preview.encoded).shape == np.asarray(item.preview.requested).shape


def test_the_preview_is_what_the_bin_would_hold_not_what_was_asked(base_tune):
    """A value the table cannot represent exactly comes back quantized."""
    current = float(base_tune.values(GRID)[0][0])
    result = review(base_tune, _file([
        _rec(base_tune, GRID, id="q", selection={"kind": "cells", "args": [[0, 0]]},
             value=current + 0.0001),
    ]))
    (item,) = result.queued
    assert item.preview.quantized
    assert item.preview.max_abs_quantization > 0
    assert item.preview.encoded[0][0] != item.preview.requested[0][0]


def test_zero_recommendations_reviews_to_three_empty_lists(base_tune):
    result = review(base_tune, _file([], summary="nothing to change"))
    assert result.counts["total"] == 0
    assert result.summary == "nothing to change"


# --------------------------------------------------------------------------- #
# AE2 — the kg/stk trap
# --------------------------------------------------------------------------- #
def test_writing_2000_to_the_airmass_ceiling_is_dropped_not_queued(base_tune):
    """AE2. `C_M_AIR_CYL_SP_MAX` — Maximum allowed airmass setpoint stores kg/stk.

    A stated grid value of 2000 is ambiguous between the label's mg/stk and the
    store's kg/stk by the factor that removes the limiter, so the courier
    refuses to guess. The recommendation never appears in the queue.
    """
    result = review(base_tune, _file([
        _rec(base_tune, "airmass_setpoint_max", id="trap", value=2000.0,
             risk="safety-relevant"),
    ]))
    assert result.counts == {"queued": 0, "dropped": 1, "malformed": 0, "total": 1}
    assert result.queued == ()
    (drop,) = result.dropped
    assert drop.recommendation.id == "trap"
    assert "kg/stk" in drop.reason
    assert "C_M_AIR_CYL_SP_MAX" in drop.reason


# --------------------------------------------------------------------------- #
# routing
# --------------------------------------------------------------------------- #
def test_an_owner_locked_table_is_routed_to_its_domain_call(patched_tune):
    """The boost grid is written by slot_curve(), never by the generic editor."""
    ceiling = float(patched_tune.values("put_setpoint").max())
    curve = [ceiling - 400.0] * 12
    result = review(patched_tune, _file([
        _rec(patched_tune, "slot1_put_setpoint", space=PATCH_SPACE, id="boost",
             operation="paste", selection={"kind": "row", "args": [0]},
             array=curve, risk="safety-relevant"),
    ]))
    assert result.counts["queued"] == 1, result.dropped
    (item,) = result.queued
    assert item.routed_via == "tune.switchpatch.slot_curve()"
    # slot_curve tiles one curve across all eight rows — more than the row the
    # recommendation named, and the footprint says so rather than hiding it
    rows = {r for _, _, r, _ in item.footprint}
    assert rows == set(range(8))


def test_the_domain_calls_own_guard_is_what_refuses(patched_tune):
    """A cap at or above the base ceiling is refused in slot_curve's own words."""
    ceiling = float(patched_tune.values("put_setpoint").max())
    result = review(patched_tune, _file([
        _rec(patched_tune, "slot1_put_setpoint", space=PATCH_SPACE, id="over",
             operation="paste", selection={"kind": "row", "args": [0]},
             array=[ceiling + 500.0] * 12),
    ]))
    (drop,) = result.dropped
    assert drop.routed_via == "tune.switchpatch.slot_curve()"
    assert "switchpatch.slot_curve:" in drop.reason
    assert "base" in drop.reason and "ceiling" in drop.reason


def test_arithmetic_on_a_domain_owned_table_is_refused_not_reimplemented(patched_tune):
    """An adapter places values; it does not perform the engine's arithmetic."""
    result = review(patched_tune, _file([
        _rec(patched_tune, "slot1_put_setpoint", space=PATCH_SPACE, id="mul",
             operation="mul", selection={"kind": "row", "args": [0]}, value=0.95),
    ]))
    (drop,) = result.dropped
    assert "'mul'" in drop.reason
    assert "state the resulting" in drop.reason


def test_a_flag_table_routes_to_set_slot_flag(patched_tune):
    name = "slot1_enable_lc"
    current = float(patched_tune.values(name, space=PATCH_SPACE).ravel()[0])
    result = review(patched_tune, _file([
        _rec(patched_tune, name, space=PATCH_SPACE, id="flag",
             value=0.0 if current else 1.0),
    ]))
    assert result.counts["queued"] == 1, result.dropped
    assert result.queued[0].routed_via == "tune.switchpatch.set_slot_flag()"


def test_a_multi_table_call_says_what_else_it_writes(base_tune):
    """The speed limiter is four scalars written as one coherent set."""
    result = review(base_tune, _file([
        _rec(base_tune, "speed_limiter_level1", id="speed", value=250.0),
    ]))
    assert result.counts["queued"] == 1, result.dropped
    (item,) = result.queued
    assert item.routed_via == "tune.limits.speed_limiter()"
    assert "speed_limiter_level2" in item.note
    tables = {name for _, name, _, _ in item.footprint}
    assert len(tables) == 4


# --------------------------------------------------------------------------- #
# errors that are drops, not crashes
# --------------------------------------------------------------------------- #
def test_a_table_this_calibration_does_not_have_is_dropped_with_a_reason(base_tune):
    payload = json.loads(_file([_rec(base_tune, GRID, id="ghost", value=1.0)]))
    payload["recommendations"][0]["table"]["name"] = "no_such_table"
    result = review(base_tune, json.dumps(payload))
    (drop,) = result.dropped
    assert "no table 'no_such_table'" in drop.reason
    assert result.queued == ()


def test_a_table_named_with_the_wrong_id_is_dropped(base_tune):
    """`ID` — Description and the logical name must agree, or the queue would
    show a person one table's name over another table's change."""
    payload = json.loads(_file([_rec(base_tune, GRID, id="mismatch", value=1.0)]))
    payload["recommendations"][0]["table"]["id"] = "C_M_AIR_CYL_SP_MAX"
    result = review(base_tune, json.dumps(payload))
    (drop,) = result.dropped
    assert "do not agree" in drop.reason


def test_a_selection_off_the_end_of_the_table_is_dropped(base_tune):
    result = review(base_tune, _file([
        _rec(base_tune, GRID, id="oob", selection={"kind": "row", "args": [99]},
             value=1.0),
    ]))
    (drop,) = result.dropped
    assert "out of range" in drop.reason


# --------------------------------------------------------------------------- #
# the three counts
# --------------------------------------------------------------------------- #
def test_a_mixed_file_reports_all_three_counts_and_they_sum(base_tune):
    good = _rec(base_tune, GRID, id="ok",
                selection={"kind": "cells", "args": [[0, 0]]},
                value=float(base_tune.values(GRID)[0][0]) + 0.02)
    refused = _rec(base_tune, "airmass_setpoint_max", id="refused", value=2000.0)
    malformed = _rec(base_tune, GRID, id="bad", value=1.0)
    malformed["evidence"] = ""
    also_malformed = _rec(base_tune, GRID, id="worse", value=1.0)
    also_malformed["risk"] = "mild"

    result = review(base_tune, _file([good, refused, malformed, also_malformed]))
    assert result.counts == {"queued": 1, "dropped": 1, "malformed": 2, "total": 4}
    assert [m.id for m in result.malformed] == ["bad", "worse"]
    assert [m.index for m in result.malformed] == [2, 3]
    assert result.malformed[0].problems[0].field == "evidence"


def test_a_malformed_record_does_not_disqualify_the_readable_ones(base_tune):
    """One bad record out of two would otherwise throw away a usable answer."""
    good = _rec(base_tune, GRID, id="ok",
                selection={"kind": "cells", "args": [[0, 0]]},
                value=float(base_tune.values(GRID)[0][0]) + 0.02)
    bad = _rec(base_tune, GRID, id="bad", value=1.0)
    del bad["prediction"]
    result = review(base_tune, _file([good, bad]))
    assert result.counts["queued"] == 1
    assert result.counts["malformed"] == 1


def test_an_unreadable_envelope_is_still_fatal(base_tune):
    with pytest.raises(AdviceRejected) as excinfo:
        review(base_tune, _file([], schema_version=99))
    assert "newer than" in str(excinfo.value)


def test_malformed_json_raises_rather_than_reviewing_nothing(base_tune):
    with pytest.raises(AdviceRejected) as excinfo:
        review(base_tune, "{not json")
    assert "not valid JSON" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# overlap
# --------------------------------------------------------------------------- #
def test_two_recommendations_touching_the_same_cells_are_both_queued_and_flagged(base_tune):
    current = base_tune.values(GRID)
    records = [
        _rec(base_tune, GRID, id="first", operation="mul",
             selection={"kind": "row", "args": [0]}, value=1.01),
        _rec(base_tune, GRID, id="second", operation="set",
             selection={"kind": "cells", "args": [[0, 1]]},
             value=float(current[0][1]) + 0.05),
    ]
    result = review(base_tune, _file(records))
    assert result.counts["queued"] == 2, result.dropped
    by_id = {q.recommendation.id: q for q in result.queued}
    assert by_id["first"].overlaps == ("second",)
    assert by_id["second"].overlaps == ("first",)


def test_recommendations_on_different_tables_do_not_overlap(base_tune):
    records = [
        _rec(base_tune, GRID, id="one", operation="mul",
             selection={"kind": "row", "args": [0]}, value=1.01),
        _rec(base_tune, "put_setpoint", id="two", operation="mul",
             selection={"kind": "row", "args": [0]}, value=1.01),
    ]
    result = review(base_tune, _file(records))
    assert result.counts["queued"] == 2, result.dropped
    assert all(q.overlaps == () for q in result.queued)


def test_each_recommendation_is_replayed_against_current_state_not_cumulatively(base_tune):
    """Two 'set the same cell' items each preview against what the bin holds now."""
    current = float(base_tune.values(GRID)[0][0])
    records = [
        _rec(base_tune, GRID, id="a", selection={"kind": "cells", "args": [[0, 0]]},
             value=current + 0.02),
        _rec(base_tune, GRID, id="b", selection={"kind": "cells", "args": [[0, 0]]},
             value=current + 0.04),
    ]
    result = review(base_tune, _file(records))
    by_id = {q.recommendation.id: q for q in result.queued}
    assert by_id["a"].preview.before[0][0] == pytest.approx(current)
    assert by_id["b"].preview.before[0][0] == pytest.approx(current)
    assert by_id["a"].overlaps == ("b",)


# --------------------------------------------------------------------------- #
# provenance
# --------------------------------------------------------------------------- #
def test_a_file_answering_a_different_bin_is_refused_wholesale(base_tune):
    session = {"profile": "SC8S50", "bin_sha256": "c" * 64, "xdf_sha256": "b" * 64}
    with pytest.raises(ProvenanceMismatch) as excinfo:
        review(base_tune, _file([
            _rec(base_tune, GRID, id="x", selection={"kind": "cells", "args": [[0, 0]]},
                 value=1.0),
        ]), provenance=session)
    assert "different" in str(excinfo.value)
    assert excinfo.value.problems[0].field == "bin_sha256"


def test_a_matching_provenance_reviews_normally(base_tune):
    session = {"profile": "SC8S50", "bin_sha256": "A" * 64, "xdf_sha256": "b" * 64}
    result = review(base_tune, _file([
        _rec(base_tune, GRID, id="x", selection={"kind": "cells", "args": [[0, 0]]},
             value=float(base_tune.values(GRID)[0][0]) + 0.02),
    ]), provenance=session)
    assert result.counts["queued"] == 1


def test_a_mismatched_file_is_refused_before_any_replay(base_tune):
    """Nothing is replayed, so nothing about the bin can have been read wrong."""
    before = _fingerprint(base_tune)
    session = {"profile": "SCGA05", "bin_sha256": "a" * 64, "xdf_sha256": "b" * 64}
    with pytest.raises(ProvenanceMismatch):
        review(base_tune, _file([
            _rec(base_tune, GRID, id="x", selection={"kind": "cells", "args": [[0, 0]]},
                 value=1.0),
        ]), provenance=session)
    assert _fingerprint(base_tune) == before


# --------------------------------------------------------------------------- #
# the safety claim
# --------------------------------------------------------------------------- #
def test_reviewing_a_file_with_every_outcome_leaves_the_session_untouched(patched_tune):
    """The property the whole courier rests on, over a file that exercises
    the generic path, a domain adapter, a guard refusal and a schema failure."""
    tune = patched_tune
    ceiling = float(tune.values("put_setpoint").max())
    malformed = _rec(tune, GRID, id="bad", value=1.0)
    malformed["evidence"] = ""
    records = [
        _rec(tune, GRID, id="generic", operation="mul",
             selection={"kind": "row", "args": [0]}, value=1.01),
        _rec(tune, "slot1_put_setpoint", space=PATCH_SPACE, id="boost",
             operation="paste", selection={"kind": "row", "args": [0]},
             array=[ceiling - 400.0] * 12),
        _rec(tune, "slot1_put_setpoint", space=PATCH_SPACE, id="over",
             operation="paste", selection={"kind": "row", "args": [0]},
             array=[ceiling + 500.0] * 12),
        _rec(tune, "airmass_setpoint_max", id="trap", value=2000.0),
        malformed,
    ]

    history = SessionHistory(tune)
    before = _fingerprint(patched_tune)
    undo_redo = (history.can_undo, history.can_redo)

    result = review(tune, _file(records))
    assert result.counts == {"queued": 2, "dropped": 2, "malformed": 1, "total": 5}

    assert _fingerprint(patched_tune) == before
    assert (history.can_undo, history.can_redo) == undo_redo
    assert len(tune.journal) == 0


def test_a_real_edit_after_a_review_behaves_as_though_the_review_never_ran(base_tune):
    from simoscal.tune.editing import EditOp, Selection, apply_op

    review(base_tune, _file([
        _rec(base_tune, GRID, id="x", operation="mul",
             selection={"kind": "row", "args": [0]}, value=1.01),
    ]))
    result = apply_op(
        base_tune, GRID, EditOp.MUL, selection=Selection.row(0), value=1.01,
        intent="the real one",
    )
    assert len(base_tune.journal) == 1
    assert result.entry.intent == "the real one"


# --------------------------------------------------------------------------- #
# routing coverage — the guard against a table quietly losing its route
# --------------------------------------------------------------------------- #
def test_every_domain_owned_table_is_accounted_for(patched_tune):
    """A new owner-locked table must be routed, refused by name, or declared
    unwritable — never silently unreachable.

    Without this, adding a table with a real write path and no adapter would
    show up much later as "the courier ignores that table", with nothing saying
    why.
    """
    from simoscal.advice.review import NOT_ADAPTED, _adapter
    from simoscal.tune import catalog

    unaccounted = []
    for info in catalog(patched_tune, include_domain_owned=True):
        if not info.owner:
            continue
        if _adapter(info.name) or info.name in NOT_ADAPTED:
            continue
        if info.owner.startswith(("no write path", "no verified write path")):
            continue
        unaccounted.append(f"{info.name} :: {info.owner[:70]}")
    assert not unaccounted, "domain-owned tables with no route and no reason:\n" + "\n".join(unaccounted)


def test_a_setting_the_profile_will_not_write_is_dropped_in_its_own_words(patched_tune):
    """A per-slot scalar that is read-only, or is not a flag at all, is refused
    by set_slot_flag rather than written as though it were a toggle."""
    result = review(patched_tune, _file([
        _rec(patched_tune, "slot1_manual_afu", space=PATCH_SPACE, id="afu", value=1.0),
    ]))
    (drop,) = result.dropped
    assert "switchpatch.set_slot_flag:" in drop.reason
