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
    "TAG_KG_PER_STROKE",
    "TAG_NO_SYMBOL",
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


@dataclass(frozen=True)
class TableSpec:
    """One logical name's binding to a table in a particular XDF.

    ``key`` is whatever :meth:`CalFile.get` resolves exactly: a symbol
    (``"IP_PUT_SP"``), a hex uniqueid string (``"0x7d41a"``), or an int
    uniqueid. ``description`` is the plain-English meaning — normally the XDF
    ``title``, or a clearer phrasing when the title is terse or, as with the
    five identically-titled ``PUT setpoint`` slot grids, non-unique.
    """

    name: str
    key: Union[str, int]
    description: str
    units: str = ""
    shape: Optional[tuple[int, int]] = None  # asserted at resolve time when set
    tags: frozenset[str] = frozenset()

    @property
    def label(self) -> str:
        """The ``` `ID` — Description ``` form every report and message uses."""
        return f"`{self.key}` — {self.description}"

    def has(self, tag: str) -> bool:
        return tag in self.tags


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
