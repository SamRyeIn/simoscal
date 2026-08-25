"""The safety brief: what ships with a bundle, and where each fact came from.

The brief exists so whoever answers a bundle starts from the facts that have
already cost somebody a flash cycle. Its one structural rule is that each fact is
stated from the **single place that already owns it**:

* facts about *this car's tables* are rendered from the open profile, never
  written down a second time — a second copy is a copy that drifts, and this one
  would drift into a public repository;
* facts about these ECUs *in general* stay hand-written prose, embedded verbatim.

So the tests here are mostly about sourcing rather than wording: that a profile
declaring a fact renders it, that a profile declaring none renders **no section**
rather than an empty one, and that no car's numbers leak into the public prose.

SC8S50 and SCGA05 are both real registered profiles and between them cover every
branch: SC8S50 declares kg/stk, float-bug and stock facts and numbers its XDF
from the whole bin; SCGA05 declares none of those, numbers from the CAL block,
and declares ten unavailable tables.
"""

from __future__ import annotations

import re

import pytest

from simoscal.advice.brief import AUTHORED_PATH, authored_half, car_facts, safety_brief
from simoscal.tune.profile import TAG_AXIS, TAG_FLOAT_BUG, TAG_KG_PER_STROKE
from simoscal.tune.profiles import PROFILES

SC8S50 = PROFILES["SC8S50"]
SCGA05 = PROFILES["SCGA05"]
PATCH = PROFILES["SwitchPatch2933"]


# --------------------------------------------------------------------------- #
# the generated half
# --------------------------------------------------------------------------- #
def test_the_brief_names_every_kg_per_stroke_table_both_ways():
    text = car_facts(SC8S50)
    tagged = [s for s in SC8S50.specs.values() if s.has(TAG_KG_PER_STROKE)]
    assert tagged, "the fixture profile no longer declares a kg/stk table"
    for spec in tagged:
        assert spec.label in text, f"{spec.name} is tagged kg/stk but not in the brief"
    assert "kg/stk" in text and "0.002" in text


def test_the_brief_names_every_float_bug_table_both_ways():
    text = car_facts(SC8S50)
    tagged = [s for s in SC8S50.specs.values() if s.has(TAG_FLOAT_BUG)]
    assert tagged, "the fixture profile no longer declares a float-bug table"
    for spec in tagged:
        assert spec.label in text
    assert "display artifact" in text


def test_the_brief_names_every_axis_that_must_strictly_increase():
    text = car_facts(SC8S50)
    tagged = [s for s in SC8S50.specs.values() if s.has(TAG_AXIS)]
    assert tagged
    for spec in tagged:
        assert spec.label in text
    assert "strictly increase" in text


def test_the_brief_carries_each_declared_stock_reference():
    text = car_facts(SC8S50)
    assert SC8S50.stock_references, "the fixture profile declares no stock references"
    for key, sentence in SC8S50.stock_references.items():
        assert key in text
        assert sentence in text


def test_the_brief_names_the_cal_layout_and_the_address_convention():
    text = car_facts(SC8S50)
    assert "`SC8S50`" in text
    assert "**whole bin**" in text


def test_a_cal_relative_profile_says_so_explicitly():
    """An address means two different bytes under the two conventions, so a
    reader must not be left to assume the common one."""
    text = car_facts(SCGA05)
    layout = text[text.index("### Which calibration this is"):]
    layout = layout[:layout.index("###", 5)]
    assert "**calibration block**" in layout
    assert "not a file offset" in layout
    assert "**whole bin**" not in layout


def test_a_profile_declaring_no_such_facts_renders_no_section_at_all():
    """Silence, not an empty heading — the rule the SOP guidance already set."""
    assert not SCGA05.stock_references
    assert not [s for s in SCGA05.specs.values() if s.has(TAG_FLOAT_BUG)]
    text = car_facts(SCGA05)
    assert "What stock reads" not in text
    assert "Declared maxima" not in text
    assert "Units that are not what the label says" not in text
    # and nothing that looks like a placeholder
    assert "None" not in text and "N/A" not in text


def test_declared_unavailable_tables_are_listed_with_their_reason():
    text = car_facts(SCGA05)
    assert SCGA05.unavailable, "the fixture profile declares nothing unavailable"
    for name, reason in SCGA05.unavailable.items():
        assert f"`{name}`" in text
        assert reason.split(".")[0][:40] in text


def test_a_profile_with_nothing_declared_still_renders_a_valid_section():
    text = car_facts(PATCH)
    assert text.startswith("## This calibration")
    # the patch profile has no structure of its own; it inherits one
    assert "CAL layout" not in text


def test_the_courier_refuses_to_write_are_listed_from_their_one_source():
    """The list is rendered from review.NOT_ADAPTED, so adding an entry there
    tells the answering side about it without a second edit here."""
    from simoscal.advice.review import NOT_ADAPTED

    text = car_facts(SC8S50)
    for name, reason in NOT_ADAPTED.items():
        if name in SC8S50.specs:
            assert SC8S50.specs[name].label in text
            assert reason[:40] in text


# --------------------------------------------------------------------------- #
# across table spaces
# --------------------------------------------------------------------------- #
class _FakeSpace:
    def __init__(self, profile):
        self.profile = profile
        self.tables = _FakeResolved(profile)


class _FakeResolved:
    def __init__(self, profile):
        self._names = sorted(profile.specs)

    def names(self):
        return list(self._names)


class _FakeTune:
    """Enough of a Tune for the brief: table spaces, each with a profile."""

    def __init__(self, **spaces):
        self.spaces = {name: _FakeSpace(p) for name, p in spaces.items()}


def test_a_base_plus_patch_session_renders_both_profiles_facts_once_each():
    """Opening a second space adds that space's facts and repeats none.

    A table may legitimately appear under more than one heading — the airmass
    ceiling is a units fact, a declared-max fact and a courier refusal — so the
    test is that the merge changes no count, not that every count is one.
    """
    base_only = car_facts(_FakeTune(base=SC8S50))
    both = car_facts(_FakeTune(base=SC8S50, patch=PATCH))

    for spec in SC8S50.specs.values():
        if spec.label in base_only:
            assert both.count(spec.label) == base_only.count(spec.label), spec.name

    # the patch profile's own axis, marked with the space it lives in
    patch_axis = next(s for s in PATCH.specs.values() if s.has(TAG_AXIS))
    assert both.count(patch_axis.label) == 1
    assert patch_axis.label not in base_only
    assert "in the `patch` table space" in both


def test_only_tables_this_session_resolved_are_described():
    """A spec that did not resolve is not a table in this bin, and naming it
    would describe something the answering side can neither see nor edit."""
    tune = _FakeTune(base=SC8S50)
    dropped = next(s for s in SC8S50.specs.values() if s.has(TAG_AXIS))
    tune.spaces["base"].tables._names = [
        n for n in sorted(SC8S50.specs) if n != dropped.name
    ]
    text = car_facts(tune)
    assert dropped.label not in text


# --------------------------------------------------------------------------- #
# the authored half
# --------------------------------------------------------------------------- #
def test_the_authored_half_names_the_general_facts():
    """Silently dropping one of these fails here rather than in a wrong answer."""
    text = authored_half()
    assert "IP_PUT_AMP_DIF_MAX_PRS_DIF_THR" in text
    assert "Overpressure upstream throttle threshold" in text
    assert "not even the same order of magnitude" in text
    assert "`Gear ()`" in text and "`Gear (gear)`" in text
    assert "plus one" in text
    assert "Calc HP (hp)" in text and "Calc TQ (nm)" in text
    assert "gear ratio" in text
    assert "display artifact" in text
    assert "**not**, by itself" in text


def test_the_authored_half_says_it_is_not_the_safety_mechanism():
    """A stated non-guardrail: it must say so before anyone relies on it."""
    text = authored_half()
    head = text[:text.index("## Name every table")]
    assert "not the safety mechanism" in head
    assert "replayed" in head


def test_the_authored_half_is_embedded_verbatim():
    """Embedding must not alter a byte, so what shipped can be diffed against
    what is in the repository."""
    authored = AUTHORED_PATH.read_text(encoding="utf-8")
    assert authored_half() == authored
    assert authored.rstrip() in safety_brief(SC8S50)


def test_the_authored_half_comes_before_the_numbers():
    brief = safety_brief(SC8S50)
    assert brief.index("not the safety mechanism") < brief.index("## This calibration")


# --------------------------------------------------------------------------- #
# the regression that keeps car data out of a public repository
# --------------------------------------------------------------------------- #
BOX_CODES = ("5G0906259L", "3CN906259B", "5G0906259", "3CN906259")


def test_no_box_code_appears_in_the_public_prose():
    text = authored_half()
    for code in BOX_CODES:
        assert code not in text, f"{code} is a car identifier and must not be in public prose"


def test_no_car_specific_figure_appears_in_the_public_prose():
    """Any four-or-more-digit number in this file would be an rpm, an hPa, a
    mg/stk or a box code — i.e. a measurement of one particular car."""
    text = authored_half()
    found = re.findall(r"\d{4,}", text)
    assert not found, f"car-specific figures in public prose: {found}"


def test_no_profiles_stock_reference_text_leaks_into_the_public_prose():
    """The generated half is the only place a car's measured values belong."""
    text = authored_half()
    for profile in PROFILES.values():
        for sentence in profile.stock_references.values():
            assert sentence not in text
        for reason in profile.unavailable.values():
            assert reason not in text


def test_the_authored_file_is_shipped_with_the_package():
    """It is read at runtime on a device that pip-installed this library, so it
    must be package data — a doc directory does not survive an install."""
    import tomllib
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    patterns = data["tool"]["setuptools"]["package-data"]["simoscal.advice"]
    assert any(AUTHORED_PATH.match(f"*{p.lstrip('*')}") for p in patterns), (
        f"{AUTHORED_PATH.name} is not covered by package-data {patterns}"
    )
