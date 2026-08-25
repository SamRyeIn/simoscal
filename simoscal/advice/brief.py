"""The safety brief that ships with a bundle — half generated, half authored.

Whoever answers a bundle needs the hard-won facts about these ECUs, and those
facts divide cleanly into two kinds that must be sourced differently.

*Generated, per car.* Which of this calibration's tables store a unit their
label does not admit to, which declare a maximum that is not a limit, which axes
must stay strictly increasing, what stock reads, which tables this car does not
have and why, and which CAL layout applies. Every one of those is a property of
the open profile, so every one is **rendered from it** rather than written down
a second time. A profile that declares none of a given fact renders no sentence
for it — no empty heading, no placeholder. Silence is the correct output for a
car nobody has measured; inventing another car's numbers is the failure this
replaces.

*Authored, general.* Overboost fault routing, the gear-header indexing rule, the
Calc HP gear-flip trim, and the standing rule that an XDF-declared max is a
display artifact. None of these is about any one car's tables and none should be
given a :class:`~simoscal.tune.profile.Profile` home. They live as prose in
:data:`AUTHORED_PATH`, edited by a person, and are embedded **verbatim** — the
bytes that ship are the bytes in the file.

That split is also what keeps this repository free of car data: the public prose
holds no car's numbers, because the car's numbers are rendered on device into a
bundle that never enters a repository.

The brief is a *stated non-guardrail*. It makes recommendations start sensible;
the dry-run replay in :mod:`simoscal.advice.review` is what makes them safe. The
authored half says so in its own first paragraph, so nobody reading it later
mistakes it for the safety mechanism.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional

from ..tune.profile import TAG_AXIS, TAG_FLOAT_BUG, TAG_KG_PER_STROKE, Profile
from .review import NOT_ADAPTED

__all__ = [
    "AUTHORED_PATH",
    "authored_half",
    "car_facts",
    "safety_brief",
]

#: The authored half, as a file a person edits. Inside the package rather than
#: in ``docs/`` because it has to survive a real install: the Android engine
#: pip-installs this library from a checkout, and only declared packages and
#: package data land on the device. A doc the device cannot read would be a doc
#: the brief silently ships without.
AUTHORED_PATH = Path(__file__).resolve().parent / "safety_brief.md"


def authored_half() -> str:
    """The car-independent prose, exactly as written.

    Verbatim by contract: embedding it in a bundle must not alter a byte, so a
    reader can diff what shipped against what is in the repository.
    """
    return AUTHORED_PATH.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# the generated half
# --------------------------------------------------------------------------- #
def _profiles(source) -> list[tuple[str, Profile, Optional[Iterable[str]]]]:
    """``(space, profile, resolved names)`` for whatever was handed in.

    A :class:`~simoscal.tune.project.Tune` carries one profile per table space
    and the names each one actually resolved; a bare :class:`Profile` carries
    neither, so its facts render for every spec it declares.
    """
    spaces = getattr(source, "spaces", None)
    if spaces is None:
        return [("", source, None)]
    return [
        (name, space.profile, set(space.tables.names()))
        for name, space in spaces.items()
    ]


def _tagged(profile: Profile, resolved: Optional[Iterable[str]], tag: str) -> list:
    """Specs carrying ``tag``, restricted to what this session actually resolved.

    The restriction matters: a spec that did not resolve is not a table in this
    bin, and a brief that named it would be describing a table the answering
    side cannot be shown and cannot edit.
    """
    names = None if resolved is None else set(resolved)
    return [
        spec for name, spec in sorted(profile.specs.items())
        if spec.has(tag) and (names is None or name in names)
    ]


def _bullets(lines: Iterable[str]) -> str:
    return "\n".join(f"- {line}" for line in lines)


def _section(heading: str, body: str) -> str:
    """A heading and its body, or nothing at all when the body is empty."""
    return f"### {heading}\n\n{body}\n" if body else ""


def car_facts(source) -> str:
    """The per-car half, rendered from an open :class:`Tune` or a `Profile`.

    Facts are gathered across every table space, so a session with the switch
    patch open describes the patch-added tables too. Logical names are unique
    across spaces (merging refuses a clash), so nothing is said twice.
    """
    entries = _profiles(source)

    kg: list[str] = []
    float_bug: list[str] = []
    axes: list[str] = []
    stock: dict[str, str] = {}
    missing: dict[str, str] = {}
    layout: list[str] = []
    refused: list[str] = []
    seen_structures: set[str] = set()

    for space, profile, resolved in entries:
        where = f" (in the `{space}` table space)" if space and space != "base" else ""
        for spec in _tagged(profile, resolved, TAG_KG_PER_STROKE):
            kg.append(
                f"{spec.label} stores **kg/stk**, although the definition labels "
                f"it identity-scaled mg/stk{where}. A ceiling of 2 g/stk is "
                "written as 0.002, not as 2000 — writing the mg/stk figure "
                "directly raises the ceiling about a millionfold and removes the "
                "limiter entirely."
            )
        for spec in _tagged(profile, resolved, TAG_FLOAT_BUG):
            float_bug.append(
                f"{spec.label} — the maximum this definition declares is a "
                f"display artifact, not an ECU ceiling{where}. Stock already "
                "exceeds it, so it says nothing about what the ECU accepts."
            )
        for spec in _tagged(profile, resolved, TAG_AXIS):
            axes.append(f"{spec.label}{where}")
        names = None if resolved is None else set(resolved)
        for name in sorted(NOT_ADAPTED):
            if name in profile.specs and (names is None or name in names):
                refused.append(f"{profile.specs[name].label}{where} — {NOT_ADAPTED[name]}")
        stock.update(profile.stock_references)
        missing.update(profile.unavailable)

        if profile.structure is not None and profile.structure.name not in seen_structures:
            seen_structures.add(profile.structure.name)
            convention = (
                "counted from the start of the **calibration block**, not the "
                "start of the whole bin — an address here is not a file offset"
                if profile.xdf_addresses_cal_relative else
                "counted from the start of the **whole bin**"
            )
            layout.append(
                f"CAL layout `{profile.structure.name}`; addresses in this "
                f"calibration's definition are {convention}."
            )

    parts = [
        "## This calibration\n\n"
        "Rendered from the profile this session resolved. Everything in this "
        "section is a fact about *this* bin, not a general rule.\n",
        _section("Which calibration this is", _bullets(layout)),
        _section("Units that are not what the label says", _bullets(kg)),
        _section("Declared maxima that are not ECU limits", _bullets(float_bug)),
        _section(
            "Axes — writes must strictly increase",
            _bullets(axes) + (
                "\n\nA write leaving any of these flat or decreasing is refused. "
                "Re-breakpointing an axis reinterprets every map that indexes it, "
                "including maps this change is not about."
                if axes else ""
            ),
        ),
        _section(
            "Tables the courier will not write",
            _bullets(refused) + (
                "\n\nA recommendation naming one of these is dropped. Say in "
                "`summary` what you would have changed and why, and leave the "
                "edit to the screen that owns it."
                if refused else ""
            ),
        ),
        _section(
            "What stock reads on this car",
            _bullets(f"{key}: {value}" for key, value in sorted(stock.items())),
        ),
        _section(
            "Tables this calibration does not have",
            _bullets(f"`{name}` — {reason}" for name, reason in sorted(missing.items())),
        ),
    ]
    return "\n".join(part for part in parts if part).rstrip() + "\n"


def safety_brief(source) -> str:
    """The whole brief: the authored prose, then this car's facts.

    The authored half comes first and appears verbatim, so a reader meets the
    "this is not the safety mechanism" paragraph before any of the numbers.
    """
    return f"{authored_half().rstrip()}\n\n{car_facts(source)}"
