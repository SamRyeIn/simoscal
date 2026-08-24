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

from dataclasses import dataclass, field, replace
from difflib import SequenceMatcher
from typing import Iterable, Iterator, Mapping, Optional, Union

from ..calfile import CalFile, TableView
from ..checksum import StructureSpec
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
    "TableUnavailableError",
    "apply_groups",
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


class TableUnavailableError(SimosCalError, KeyError):
    """A logical name this profile explicitly declares this car does not have.

    Distinct from a plain :class:`KeyError` on purpose. "I have never heard of
    that name" and "I looked for that table on this car and it is not there"
    are different answers, and only the second one is *evidence*: it tells a
    caller the gap is known rather than a typo, and carries the reason so the
    caller can decide whether to skip the step or refuse the whole edit.

    Subclasses :class:`KeyError` as well so existing ``except KeyError`` paths —
    the mapping protocol callers already write — keep working unchanged.
    """

    def __init__(self, profile: str, name: str, reason: str) -> None:
        self.profile = profile
        self.name = name
        self.reason = reason
        super().__init__(
            f"profile {profile!r} declares {name!r} unavailable on this car: "
            f"{reason}"
        )

    def __str__(self) -> str:  # KeyError would otherwise repr() the message
        return self.args[0]


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

    A profile is also where the rest of this car's *facts* live — the ones the
    library used to hold as module globals, which is what made it structurally
    single-car:

    * :attr:`structure` — where this car's CAL block sits in its bin and what
      address the ECU maps it to (a :class:`~simoscal.checksum.StructureSpec`);
    * :attr:`float_bug_symbols` — derived from the specs, not declared twice;
    * :attr:`stock_references` — what stock reads on this car, for guidance text
      that wants to compare a guide instruction against it.

    Profiles compose: a patched-bin tune resolves the base calibration through
    the SC8S50 profile and the patch-added tables through the switch-patch
    profile, so :meth:`merged_with` builds the union. Merging is strict — a
    logical name defined by both profiles is a map-authoring bug, not a
    precedence question.
    """

    name: str
    xdf: str  # the XDF filename this map was authored against (documentation)
    specs: Mapping[str, TableSpec] = field(default_factory=dict)
    #: This car's CAL layout. ``None`` for a profile that only adds tables to
    #: another profile's space (the switch patch), which inherits the base
    #: profile's structure through :meth:`merged_with`.
    structure: Optional[StructureSpec] = None
    #: Named facts about what *stock* reads on this car, for guidance text.
    #: Keys are short ids a guidance string names; values are the sentence to
    #: render. A profile that declares none renders no comparison at all —
    #: silence is the correct output for a car nobody has measured, and
    #: inventing another car's numbers is the failure this replaces.
    stock_references: Mapping[str, str] = field(default_factory=dict)
    #: Logical names this car does **not** have, each mapped to why — a
    #: *declaration*, not an omission.
    #:
    #: The two are only the same to a reader who trusts the author's memory. A
    #: name simply left out of :attr:`specs` cannot be told apart from one
    #: forgotten during the port, and the failure it produces ("no logical name
    #: X") says nothing about whether X was investigated. Declaring it says the
    #: gap was looked at, records what was looked for, and turns the lookup into
    #: a :class:`TableUnavailableError` carrying the reason.
    #:
    #: Two distinct causes both belong here, and the reason string must say
    #: which: the table does not exist in this calibration at all, or it exists
    #: in the bin but this car's XDF declares no editable table for it (an
    #: embedded axis with no standalone entry is the common case).
    unavailable: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name, spec in self.specs.items():
            if name != spec.name:
                raise ValueError(
                    f"profile {self.name!r}: key {name!r} does not match "
                    f"spec.name {spec.name!r}"
                )
        both = sorted(set(self.specs) & set(self.unavailable))
        if both:
            raise ValueError(
                f"profile {self.name!r}: {', '.join(both)} is declared both as a "
                "mapped table and as unavailable — one of the two is wrong, and "
                "guessing which would either hide a table or offer a missing one"
            )

    @property
    def float_bug_symbols(self) -> frozenset[str]:
        """Symbols whose XDF display max is an editor artifact, not an ECU limit.

        Derived from the specs rather than declared beside them: the tag on the
        spec is the single place a table is flagged, so the writer-facing set and
        the domain-facing tag cannot drift apart. It used to be a module global in
        :mod:`simoscal.safety`, listing four symbols that a reader had to
        cross-check by hand against three tagged specs.

        Only string keys appear — a uniqueid-keyed spec has no symbol for
        :func:`~simoscal.safety.is_float_bug_table` to match on.
        """
        return frozenset(
            spec.key
            for spec in self.specs.values()
            if spec.has(TAG_FLOAT_BUG) and isinstance(spec.key, str)
        )

    def __contains__(self, name: object) -> bool:
        return name in self.specs

    def __getitem__(self, name: str) -> TableSpec:
        try:
            return self.specs[name]
        except KeyError:
            pass
        if name in self.unavailable:
            raise TableUnavailableError(self.name, name, self.unavailable[name])
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
        """Union of two profiles; overlapping logical names raise.

        The merged profile keeps whichever :attr:`structure` is declared. Two
        *different* structures raise: a patch profile and the base calibration it
        patches describe one bin, and disagreeing about where its CAL block sits
        is a map-authoring bug rather than something to resolve by precedence.
        """
        clash = sorted(set(self.specs) & set(other.specs))
        if clash:
            raise ValueError(
                f"cannot merge profiles {self.name!r} and {other.name!r}: "
                f"both define {', '.join(clash)}"
            )
        ref_clash = sorted(
            k for k in set(self.stock_references) & set(other.stock_references)
            if self.stock_references[k] != other.stock_references[k]
        )
        if ref_clash:
            raise ValueError(
                f"cannot merge profiles {self.name!r} and {other.name!r}: "
                f"they give different stock references for {', '.join(ref_clash)}"
            )
        if (
            self.structure is not None
            and other.structure is not None
            and self.structure != other.structure
        ):
            raise ValueError(
                f"cannot merge profiles {self.name!r} and {other.name!r}: "
                f"they declare different CAL structures "
                f"({self.structure.name!r} vs {other.structure.name!r})"
            )
        specs = {**self.specs, **other.specs}
        # A gap one profile declares can be *filled* by the other — that is what
        # a patch profile is for — so the merged declaration keeps only the gaps
        # nothing in the union supplies. Without this subtraction a patched bin
        # would still report a table as absent while holding a spec for it.
        unavailable = {
            n: why
            for n, why in {**self.unavailable, **other.unavailable}.items()
            if n not in specs
        }
        return Profile(
            name=name or f"{self.name}+{other.name}",
            xdf=f"{self.xdf}, {other.xdf}",
            specs=specs,
            structure=self.structure if self.structure is not None else other.structure,
            stock_references={**self.stock_references, **other.stock_references},
            unavailable=unavailable,
        )


def apply_groups(
    profile_name: str,
    specs: list[TableSpec],
    groups: Mapping[str, tuple[str, ...]],
) -> list[TableSpec]:
    """Stamp each spec with its group, refusing an incomplete classification.

    ``groups`` is the map-file's classification block: heading → the logical
    names filed under it. Declaring it in one block, rather than as a keyword on
    every spec, is what makes "is anything filed in the wrong place?" answerable
    by reading one screen instead of scanning sixty call sites — so this
    function exists to make that block *checkable* rather than decorative.

    All three failures it refuses are otherwise silent:

    * a name in ``groups`` that no spec declares is a stale entry left behind by
      a rename;
    * a spec no group claims would quietly vanish from a grouped browser, which
      is calibration a person cannot find;
    * two headings claiming one table is a bug, not a precedence question.

    Raised at import time, so a mis-filed table cannot reach a tablet.
    """
    by_name: dict[str, str] = {}
    for group, names in groups.items():
        for name in names:
            if name in by_name:
                raise ValueError(
                    f"{profile_name} grouping: {name!r} is claimed by both "
                    f"{by_name[name]!r} and {group!r}"
                )
            by_name[name] = group

    declared = {spec.name for spec in specs}
    stale = sorted(set(by_name) - declared)
    if stale:
        raise ValueError(
            f"{profile_name} grouping names tables the profile does not declare: "
            f"{', '.join(stale)}"
        )
    missing = sorted(declared - set(by_name))
    if missing:
        raise ValueError(
            f"{profile_name} declares tables no group claims: {', '.join(missing)}"
        )
    return [replace(spec, group=by_name[spec.name]) for spec in specs]


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
