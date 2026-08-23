"""Logical table names → XDF tables, resolved exactly or not at all.

A :class:`Profile` is a map file in Python: one :class:`TableSpec` per logical
name, binding it to an XDF symbol (or uniqueid, for patch-added tables that
have no symbol), a plain-English description, units, and any guard tags the
library must honour when writing it.

Resolution order, per logical name, is deliberately short:

1. the profile's own entry, resolved **exactly** against the loaded XDF;
2. failing that (name absent from the profile), the name treated as an exact
   symbol / uniqueid / title in the XDF;
3. otherwise it is a **miss**.

There is no step that guesses. A near-miss produces suggestions in the error
text for a human to act on — never an automatic substitution. A wrong table is
a wrong byte in an ECU, and the whole point of the map file is that the binding
was decided once, by a person, and recorded.

:func:`resolve` collects *every* miss before raising, so pointing a revision at
an XDF that is missing four tables tells you all four at once, before any bin
is opened for editing (requirements AE3).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Iterable, Iterator, Mapping, Optional, Union

from ..calfile import CalFile, TableView
from ..model import AmbiguousTableError, SimosCalError

__all__ = [
    "TAG_FLOAT_BUG",
    "TAG_AXIS",
    "TAG_KG_PER_STROKE",
    "TAG_NO_SYMBOL",
    "GROUPS",
    "GROUP_AIRFLOW",
    "GROUP_BOOST",
    "GROUP_FUELING",
    "GROUP_LAUNCH_TRACTION",
    "GROUP_LIMITERS",
    "GROUP_PEDAL_TORQUE",
    "GROUP_TIMING",
    "GROUP_TURBO_THERMAL",
    "Profile",
    "ProfileResolutionError",
    "ResolutionMiss",
    "ResolvedProfile",
    "ResolvedTable",
    "TableSpec",
]

# ---- guard tags ------------------------------------------------------------ #
# Tags are declarative facts about a table that a *writer* must honour. They
# live on the spec (data), not in the domain method (code), so a second XDF
# inherits them by writing one map file.

#: The XDF's declared display max is a TunerPro editor artifact, not an ECU
#: limit — a write above it is legitimate and goes through ``set_raw``.
TAG_FLOAT_BUG = "float_bug"

#: The store is genuinely kg/stk despite an identity mg/stk XDF label. Any API
#: taking a mg/stk value must divide by 1e6 before writing. See the
#: ``air-cyl-sp-max-kg-not-mg`` note in ``Code/README.md`` § Safety: writing a
#: raw 2000 here removes the limiter rather than setting it to 2000 mg/stk.
TAG_KG_PER_STROKE = "kg_per_stroke"

#: Patch-added table with no A2L symbol — addressable only by uniqueid.
TAG_NO_SYMBOL = "no_symbol"

#: Breakpoint vector. Generic writes must preserve a strictly increasing axis;
#: a non-monotonic axis makes every table sharing it ambiguous or unreachable.
TAG_AXIS = "axis"


# ---- domain groups --------------------------------------------------------- #
# What a table is *for*, in the tuner's own vocabulary. A spec's group is the
# only grouping an editing client is offered, and it is curated here rather than
# taken from the XDF for two reasons the XDF's own categories demonstrate:
#
#   * they classify by shape as often as by domain — 15 of the 58 generically
#     editable tables sit in a category called "Axis", which files the boost
#     setpoint's rpm breakpoints away from the boost setpoint;
#   * and where they do classify by domain they disagree with the tuner —
#     `IP_PUT_SP` — Pressure up throttle setpoint is filed under "Airflow", and
#     `ID_PV_AV_FL` — Pedal value threshold for the determination of LV_FL_RAW
#     under "Fuel".
#
# An axis therefore takes its parent table's group: a breakpoint is edited in
# service of the map it indexes, and is browsed for beside it.

#: Charge-pressure request and its actuation — setpoint grids and their axes,
#: pressure quotients, overboost thresholds, wastegate feedforward.
GROUP_BOOST = "Boost"

#: Ignition angle: base maps and their corrections.
GROUP_TIMING = "Timing"

#: Lambda setpoints, full-load enrichment, and the thresholds that arm them.
GROUP_FUELING = "Fueling"

#: What the engine is allowed to ingest — per-stroke airmass ceilings.
GROUP_AIRFLOW = "Airflow"

#: Where the engine is made to stop: rev, road-speed, and torque ceilings.
GROUP_LIMITERS = "Limiters"

#: Hardware-protection ceilings — turbocharger speed and temperature, cylinder
#: head temperature control.
GROUP_TURBO_THERMAL = "Turbo & thermal"

#: How pedal travel becomes a torque request.
GROUP_PEDAL_TORQUE = "Pedal & torque request"

#: Launch control, traction control, and no-lift shift.
GROUP_LAUNCH_TRACTION = "Launch & traction"

#: Every group, in the order an editing client lists them. Membership is closed:
#: :class:`TableSpec` refuses a group that is not in this tuple, so a typo fails
#: at construction rather than surfacing as a ninth heading on a tablet.
GROUPS = (
    GROUP_BOOST,
    GROUP_TIMING,
    GROUP_FUELING,
    GROUP_AIRFLOW,
    GROUP_LIMITERS,
    GROUP_TURBO_THERMAL,
    GROUP_PEDAL_TORQUE,
    GROUP_LAUNCH_TRACTION,
)


@dataclass(frozen=True)
class TableSpec:
    """One logical name's binding to a table in a particular XDF.

    ``key`` is whatever :meth:`CalFile.get` resolves exactly: a symbol
    (``"IP_PUT_SP"``), a hex uniqueid string (``"0x7d41a"``), or an int
    uniqueid. ``description`` is the plain-English meaning — normally the XDF
    ``title``, or a clearer phrasing when the title is terse or, as with the
    five identically-titled ``PUT setpoint`` slot grids, non-unique.

    ``owner`` names the domain call that is the **only** legitimate way to write
    this table (see :attr:`owner`). Empty means the generic editor may write it.
    """

    name: str
    key: Union[str, int]
    description: str
    units: str = ""
    shape: Optional[tuple[int, int]] = None  # asserted at resolve time when set
    tags: frozenset[str] = frozenset()
    #: The domain call that owns writes to this table, phrased for an error
    #: message (e.g. ``"tune.switchpatch.slot_curve() (bridge op boost_edit)"``).
    #: Some tables carry structural invariants no generic grid write can honour —
    #: the switch patch's eight-row tiling, its below-base-ceiling rule, its
    #: separate axis-length header — and those invariants live in the domain
    #: method, not in the byte writer. Declaring the owner here rather than in
    #: the domain method means the *generic* path can refuse the table without
    #: knowing anything about the patch: :func:`~simoscal.tune.editing.apply_op`
    #: rejects an owned table, and the catalog stops offering it (CR-20260813-01).
    owner: str = ""
    #: Which of :data:`GROUPS` this table belongs to — the domain heading an
    #: editing client files it under.
    #:
    #: Required in practice for any spec with an empty :attr:`owner`, since those
    #: are exactly the tables the generic catalog offers and a browser cannot file
    #: one with no heading. An owner-locked table may leave it empty: it is
    #: reached through its domain call's screen, never browsed. Each profile
    #: enforces its own version of that rule at import — see
    #: :meth:`Profile.ungrouped`.
    group: str = ""

    def __post_init__(self) -> None:
        if self.group and self.group not in GROUPS:
            raise ValueError(
                f"table spec {self.name!r}: unknown group {self.group!r}; "
                f"known groups: {', '.join(GROUPS)}"
            )

    @property
    def label(self) -> str:
        """The ``` `ID` — Description ``` form every report and message uses."""
        return f"`{self.key}` — {self.description}"

    def has(self, tag: str) -> bool:
        return tag in self.tags

    @property
    def domain_owned(self) -> bool:
        """Whether writes to this table must go through :attr:`owner`."""
        return bool(self.owner)


@dataclass(frozen=True)
class Profile:
    """A named set of :class:`TableSpec`s authored against one XDF.

    Profiles compose: a patched-bin tune resolves the base calibration through
    the SC8S50 profile and the patch-added tables through the switch-patch
    profile, so :meth:`merged_with` builds the union. Merging is strict — a
    logical name defined by both profiles is a map-authoring bug, not a
    precedence question.
    """

    name: str
    xdf: str  # the XDF filename this map was authored against (documentation)
    specs: Mapping[str, TableSpec] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name, spec in self.specs.items():
            if name != spec.name:
                raise ValueError(
                    f"profile {self.name!r}: key {name!r} does not match "
                    f"spec.name {spec.name!r}"
                )

    def __contains__(self, name: object) -> bool:
        return name in self.specs

    def __getitem__(self, name: str) -> TableSpec:
        try:
            return self.specs[name]
        except KeyError:
            raise KeyError(
                f"profile {self.name!r} has no logical name {name!r}; "
                f"known names: {', '.join(self.names())}"
            ) from None

    def __iter__(self) -> Iterator[str]:
        return iter(self.specs)

    def __len__(self) -> int:
        return len(self.specs)

    def names(self) -> list[str]:
        return sorted(self.specs)

    def ungrouped(self) -> list[str]:
        """Logical names whose spec declares no :attr:`TableSpec.group`.

        A generically editable table with no group is one a browser cannot file:
        it either vanishes from the list or lands under a heading that means
        "nobody decided", and the first is calibration someone cannot find.

        What counts as acceptable here is per-profile, which is why this reports
        rather than raises. The SC8S50 base map groups everything, so its tests
        assert this is empty; the switch-patch map deliberately leaves its
        owner-locked slot tables unfiled and asserts only that nothing
        generically editable is (see its ``_ungrouped_is_deliberate``).
        """
        return sorted(n for n, spec in self.specs.items() if not spec.group)

    def merged_with(self, other: "Profile", *, name: str = "") -> "Profile":
        """Union of two profiles; overlapping logical names raise."""
        clash = sorted(set(self.specs) & set(other.specs))
        if clash:
            raise ValueError(
                f"cannot merge profiles {self.name!r} and {other.name!r}: "
                f"both define {', '.join(clash)}"
            )
        return Profile(
            name=name or f"{self.name}+{other.name}",
            xdf=f"{self.xdf}, {other.xdf}",
            specs={**self.specs, **other.specs},
        )


@dataclass(frozen=True)
class ResolvedTable:
    """A :class:`TableSpec` bound to a live :class:`TableView`."""

    spec: TableSpec
    view: TableView

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def label(self) -> str:
        return self.spec.label

    @property
    def units(self) -> str:
        return self.spec.units or (self.view.units or "")

    def has(self, tag: str) -> bool:
        return self.spec.has(tag)

    @property
    def owner(self) -> str:
        """The domain call that owns writes to this table; empty if generic."""
        return self.spec.owner

    @property
    def group(self) -> str:
        """The domain heading this table is filed under; one of :data:`GROUPS`."""
        return self.spec.group

    @property
    def domain_owned(self) -> bool:
        return self.spec.domain_owned


@dataclass(frozen=True)
class ResolutionMiss:
    """One logical name that did not resolve, and why."""

    name: str
    key: Union[str, int, None]
    reason: str
    suggestions: tuple[str, ...] = ()

    def format(self) -> str:
        line = f"  - {self.name!r}"
        if self.key is not None and self.key != self.name:
            line += f" (key {self.key!r})"
        line += f": {self.reason}"
        if self.suggestions:
            line += "\n      did you mean: " + "; ".join(self.suggestions)
        return line


class ProfileResolutionError(SimosCalError):
    """One or more logical names did not resolve — raised before any edit.

    Carries every miss, not just the first, so a script pointed at the wrong
    (or an older) XDF reports the full gap in one run.
    """

    def __init__(self, profile: Profile, xdf: str, misses: Iterable[ResolutionMiss]):
        self.profile = profile
        self.xdf = xdf
        self.misses = tuple(misses)
        detail = "\n".join(m.format() for m in self.misses)
        super().__init__(
            f"profile {profile.name!r} could not resolve {len(self.misses)} "
            f"logical name(s) against {xdf}:\n{detail}\n"
            "Nothing was edited. Fix the map file (or point at the intended "
            "XDF) — names are never resolved by guessing."
        )


class ResolvedProfile:
    """Every logical name of a profile, bound to tables in one open ``CalFile``.

    Behaves as a read-only mapping of logical name → :class:`ResolvedTable`.
    Domain modules take one of these; they never call :meth:`CalFile.get`
    directly, so every table a revision touches came through the map.
    """

    def __init__(
        self,
        profile: Profile,
        cal: CalFile,
        tables: Mapping[str, ResolvedTable],
    ) -> None:
        self.profile = profile
        self.cal = cal
        self._tables = dict(tables)

    def __contains__(self, name: object) -> bool:
        return name in self._tables

    def __getitem__(self, name: str) -> ResolvedTable:
        try:
            return self._tables[name]
        except KeyError:
            raise KeyError(
                f"{name!r} was not resolved for profile {self.profile.name!r}; "
                f"resolved names: {', '.join(sorted(self._tables))}"
            ) from None

    def __iter__(self) -> Iterator[str]:
        return iter(self._tables)

    def __len__(self) -> int:
        return len(self._tables)

    def names(self) -> list[str]:
        return sorted(self._tables)

    def view(self, name: str) -> TableView:
        return self[name].view


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #
_MAX_SUGGESTIONS = 5
#: Below this similarity a "suggestion" is just noise, so none is offered.
_MIN_SUGGESTION_RATIO = 0.6


def _suggestions(cal: CalFile, key: Union[str, int]) -> tuple[str, ...]:
    """The closest-named tables, to *show a human* — never to substitute.

    Ranks every table in the XDF by string similarity to the key so a renamed,
    re-cased, or index-shifted variant surfaces first. This similarity is
    presentational only: resolution itself never consults it, so a suggestion
    can be wrong without a wrong byte ever being written.
    """
    if not isinstance(key, str):
        return ()
    scored: list[tuple[float, str]] = []
    for view in cal.unique_tables():
        name = view.symbol or view.title or view.uniqueid_hex
        ratio = SequenceMatcher(None, key.lower(), name.lower()).ratio()
        if ratio >= _MIN_SUGGESTION_RATIO:
            scored.append(
                (ratio, f"{name} — {view.title or '(untitled)'}")
            )
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    return tuple(text for _ratio, text in scored[:_MAX_SUGGESTIONS])


def _resolve_key(cal: CalFile, key: Union[str, int]) -> tuple[Optional[TableView], str]:
    """Resolve one key exactly. Returns ``(view, reason)``; view is None on miss."""
    try:
        return cal.get(key), ""
    except KeyError:
        return None, "no table with this symbol, title, or uniqueid in the XDF"
    except AmbiguousTableError as exc:
        return None, (
            f"ambiguous — {len(exc.candidates)} tables share it "
            f"({', '.join(exc.candidates[:4])}); bind the map entry to a uniqueid"
        )


def resolve(
    profile: Profile,
    cal: CalFile,
    *,
    names: Optional[Iterable[str]] = None,
    xdf_label: str = "",
) -> ResolvedProfile:
    """Bind ``names`` (default: the whole profile) to tables in ``cal``.

    Raises :class:`ProfileResolutionError` listing every miss. A name absent
    from the profile is still attempted as a literal XDF symbol/uniqueid, so a
    revision can reach an unmapped table without editing the map file — it just
    loses the map's description and guard tags, and says so in the report.
    """
    wanted = list(profile.names() if names is None else names)
    label = xdf_label or profile.xdf
    tables: dict[str, ResolvedTable] = {}
    misses: list[ResolutionMiss] = []

    for name in wanted:
        spec = profile.specs.get(name)
        key = spec.key if spec is not None else name
        view, reason = _resolve_key(cal, key)
        if view is None:
            misses.append(
                ResolutionMiss(name, key, reason, _suggestions(cal, key))
            )
            continue
        if spec is None:
            spec = TableSpec(
                name=name,
                key=name,
                description=view.title or "(no XDF title)",
                units=view.units or "",
            )
        elif spec.shape is not None and view.shape != spec.shape:
            misses.append(
                ResolutionMiss(
                    name,
                    key,
                    f"resolved to shape {view.shape}, but the map declares "
                    f"{spec.shape} — refusing to write a differently-shaped table",
                )
            )
            continue
        tables[name] = ResolvedTable(spec=spec, view=view)

    if misses:
        raise ProfileResolutionError(profile, label, misses)
    return ResolvedProfile(profile, cal, tables)
