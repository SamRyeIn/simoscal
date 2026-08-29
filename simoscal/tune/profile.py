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

import hashlib

from dataclasses import dataclass, field, replace
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, Iterable, Iterator, Mapping, Optional, Union

from ..calfile import CalFile, TableView
from ..checksum import StructureSpec
from ..model import AmbiguousTableError, SimosCalError

if TYPE_CHECKING:  # pragma: no cover - annotation only
    from ..model import Axis
from ..codec import unpacked_reason

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
    "layout_digest",
    "pin_layouts",
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
      that wants to compare a guide instruction against it;
    * :attr:`table_sets` — which logical names this car groups into the sets a
      domain call writes together.

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
    #: Named groupings of logical names that a domain call writes as one set.
    #:
    #: The counterpart of :attr:`stock_references` for *which tables*, and it
    #: exists for the same reason. ``tune.limits.speed_limiter()`` writes "the
    #: road-speed limiter quartet"; which four logical names that is, and
    #: whether there are four of them at all, is a fact about the car. Holding
    #: those tuples as module globals in one car's profile and importing them
    #: into the domain code made every domain call quietly assert that car's
    #: table sets about whatever bin was open.
    #:
    #: Keys are short ids the domain code names (``"speed_limiter"``,
    #: ``"ignition_base_vvl0"``); values are the logical names, in the order the
    #: domain should write them. Every member must be a name this profile knows
    #: — mapped or declared unavailable — so a typo fails at import rather than
    #: at the call site of a revision.
    #:
    #: A profile that declares no set under a key the domain asks for gets a
    #: loud :meth:`table_set` failure, never another car's tuple.
    table_sets: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    #: Whether this car's XDF numbers its tables from the start of the
    #: **calibration block** rather than the start of the whole bin.
    #:
    #: Both are legitimate conventions and the file states which it uses in its
    #: ``BASEOFFSET`` header, but that statement is only meaningful next to the
    #: image it was authored for. ``SC8S50.V1.0.xdf`` declares ``0x200000`` and
    #: is written against a full 4 MB bin; ``SCGa05_cal.xdf`` declares ``0`` and
    #: is written against the extracted CAL block alone — the ``_cal`` in its
    #: name. Hand the second one a full bin and every address is short by the
    #: CAL file offset: reads return padding and a write would land outside the
    #: region the checksums cover, so the bin would build clean and flash wrong.
    #:
    #: This is a *declaration*, never an inference. The library could guess —
    #: a CAL-relative file's addresses all fit inside ``cal_block_length``, and
    #: a full-bin file's do not — but guessing where a write lands is the one
    #: place this library must not be clever. Declaring it here states the
    #: convention as a per-car fact next to that car's other per-car facts, and
    #: leaves :func:`~simoscal.preflight.preflight` free to refuse any file whose
    #: header disagrees with the declaration rather than quietly accommodating it.
    xdf_addresses_cal_relative: bool = False
    #: Logical name → the layout this map was authored against, as returned by
    #: :func:`layout_digest`. Generated, not hand-written — regenerate with
    #: ``python -m simoscal.tune.profiles pin <profile> <xdf> <bin>``.
    #:
    #: Resolution matches a spec by symbol and by shape, and neither says
    #: *where* the table is or *how* its bytes decode. An XDF can name every
    #: table this profile wants, declare every shape correctly, and still put
    #: one of them four bytes further along, or read it as int16 where the real
    #: calibration is uint16 — and every downstream gate would agree with it,
    #: because the journal, the readback and the byte audit are all derived from
    #: the same definition file (CR-20260828-02). Pinning the layout is what
    #: makes those gates check the *bin* rather than check the XDF against
    #: itself.
    #:
    #: A name absent from this mapping is simply not pinned, which is how a
    #: profile that has never been pinned behaves exactly as it did before —
    #: so this can be filled in per car as each one's XDF is reviewed, and a
    #: spec added to a pinned profile fails loudly at import rather than
    #: silently arriving unpinned (see :meth:`unpinned`).
    table_layouts: Mapping[str, str] = field(default_factory=dict)

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
        for set_name, members in self.table_sets.items():
            if not members:
                raise ValueError(
                    f"profile {self.name!r}: table set {set_name!r} is empty; a "
                    "set with no members cannot be told apart from one the "
                    "profile forgot to declare — leave it out, or declare the "
                    "gap in `unavailable`"
                )
            unknown = [
                n for n in members
                if n not in self.specs and n not in self.unavailable
            ]
            if unknown:
                raise ValueError(
                    f"profile {self.name!r}: table set {set_name!r} names "
                    f"{', '.join(unknown)}, which this profile neither maps nor "
                    "declares unavailable"
                )
        if self.xdf_addresses_cal_relative and self.structure is None:
            raise ValueError(
                f"profile {self.name!r}: xdf_addresses_cal_relative says addresses "
                "are relative to the CAL block, but the profile declares no "
                "structure, so there is nothing that says where that block starts"
            )

    def structure_mismatch(self, discovered: StructureSpec) -> Optional[str]:
        """Why ``discovered`` is not this car's CAL layout — ``None`` if it is.

        The counterpart to :attr:`expected_xdf_base_offset`, and the other half
        of the same question. That gate holds the *XDF* to the profile; this one
        holds the *bin* to it. Resolution proves only that a definition file
        names this car's tables — it opens no bin at all — so without this a
        file from one car paired with the definition file from another is
        recognised as the second car and edited at the second car's addresses,
        in bytes the first car's checksums do not cover (CR-20260828-01).

        Only the two fields that place a byte are compared. ``cal_block_length``
        is deliberately excluded: a declared spec may carry the official block
        length while a discovered one carries how far that bin's own CAL CRC
        reaches, and the two legitimately differ by ``0x200`` on both cars we
        hold. ``asw_file_offset`` and ``ecm3_addr_locs`` are excluded for the
        same reason — a discovered spec states the ECM3 address location as a
        file offset with the block base left at 0, which is a different (and
        equally correct) way of saying where the same bytes are.
        """
        if self.structure is None:
            return None
        mine = self.structure
        for field_name, label in (
            ("cal_file_offset", "CAL block file offset"),
            ("cal_base_address", "CAL base address"),
        ):
            want = getattr(mine, field_name)
            got = getattr(discovered, field_name)
            if want != got:
                return (
                    f"{label} {got:#x} in this bin, but the {self.name} profile "
                    f"describes a calibration whose {label.lower()} is {want:#x}"
                )
        return None

    @property
    def expected_xdf_base_offset(self) -> Optional[int]:
        """The ``BASEOFFSET`` this profile's XDF must declare, or ``None``.

        ``None`` for a profile with no structure (the switch patch), which is
        checked against whatever base profile it is merged into rather than on
        its own. Otherwise it follows directly from the convention: a
        CAL-relative file declares ``0``, a full-bin file declares the CAL block's
        file offset. A file declaring anything else is not the file this profile
        was authored against, whatever its tables are named.
        """
        if self.structure is None:
            return None
        return 0 if self.xdf_addresses_cal_relative else self.structure.cal_file_offset

    @property
    def xdf_base_offset(self) -> Optional[int]:
        """The base offset to *use* for this XDF's addresses, or ``None``.

        Pass to :meth:`~simoscal.CalFile.open` as ``base_offset``. ``None`` means
        "no override" — the file's own header is already right for a full bin, so
        nothing is imposed on it and behaviour is exactly as before this field
        existed. For a CAL-relative file it is the CAL block's file offset, which
        is what its addresses are counted from.
        """
        if self.structure is None or not self.xdf_addresses_cal_relative:
            return None
        return self.structure.cal_file_offset

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

    def table_set(self, set_name: str) -> tuple[str, ...]:
        """The logical names this car groups under ``set_name``.

        Raises rather than falling back. A domain that asks for a set this
        profile does not declare has reached the edge of what has been measured
        on this car, and the only two honest answers are the car's own tuple or
        a refusal — the third option, another car's tuple, is the defect this
        replaces.
        """
        try:
            return tuple(self.table_sets[set_name])
        except KeyError:
            raise KeyError(
                f"profile {self.name!r} declares no table set {set_name!r}; "
                f"it declares: {', '.join(sorted(self.table_sets)) or 'none'}. "
                "A set is a per-car fact — this profile has to name its own "
                "tables, not inherit another car's."
            ) from None

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

    @property
    def stale_pins(self) -> list[str]:
        """Pinned names this profile does not map — a pin nothing can check.

        Harmless on its own (an unreachable entry writes no byte), so this
        reports rather than raises: profiles are legitimately *derived* — a test
        decoy, a subset — and a derivation that narrows the specs should not have
        to remember to narrow the pins too. It is still worth catching in the
        authored maps, because a renamed spec leaves exactly this trace, and the
        new name shows up in :attr:`unpinned` at the same time.
        """
        return sorted(set(self.table_layouts) - set(self.specs))

    @property
    def unpinned(self) -> list[str]:
        """Logical names with no entry in :attr:`table_layouts`.

        Reports rather than raises, for the same reason :meth:`ungrouped` does:
        what counts as acceptable is per-profile. A base map authored against a
        definition file we hold pins every spec, and its module asserts this is
        empty at import, so a spec added later cannot arrive unauthenticated. A
        patch map whose XDF is third-party is not pinned at all, and a merge of
        the two is legitimately part-pinned — demanding completeness of every
        profile would make that merge impossible while proving nothing.
        """
        return sorted(set(self.specs) - set(self.table_layouts))

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
        set_clash = sorted(
            k for k in set(self.table_sets) & set(other.table_sets)
            if tuple(self.table_sets[k]) != tuple(other.table_sets[k])
        )
        if set_clash:
            raise ValueError(
                f"cannot merge profiles {self.name!r} and {other.name!r}: "
                f"they group different tables under {', '.join(set_clash)}"
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
        # Pins are per spec and the spec sets are disjoint (checked above), so
        # the union cannot conflict. A profile that pins nothing contributes
        # nothing, which leaves each half of the merged profile authenticated
        # exactly as far as its own map file authenticated it — the base
        # calibration pinned, patch-added tables not — and :attr:`unpinned` says
        # which is which rather than the merge averaging the two into a claim
        # neither source made.
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
            table_sets={**self.table_sets, **other.table_sets},
            unavailable=unavailable,
            table_layouts={**self.table_layouts, **other.table_layouts},
            # The convention travels with the structure, because it is only
            # meaningful beside one: whichever profile said where the CAL block
            # is also said how its XDF counts from there.
            xdf_addresses_cal_relative=(
                self.xdf_addresses_cal_relative if self.structure is not None
                else other.xdf_addresses_cal_relative
            ),
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
# Layout pinning
# --------------------------------------------------------------------------- #
#: How much of the fingerprint hash a pin records. Sixteen hex characters is 64
#: bits: far past collision by accident, and short enough that a generated block
#: of them stays readable in a diff. This is not a security boundary — nobody is
#: searching for a preimage — it is a fixture check against a definition file
#: that quietly changed.
_DIGEST_CHARS = 16


def _canonical_axis(axis: Optional["Axis"]) -> tuple:
    """One axis reduced to what decides which bytes it is and how they decode.

    :func:`~simoscal.xdf.table_data_fingerprint` answers a stricter question —
    "are these two XDFTABLE entries literally the same declaration" — and takes
    the stride fields verbatim, which is right for detecting a uniqueid conflict
    inside one file. A pin is comparing two *different* files, and there the
    three packed stride spellings are one layout (see
    :func:`~simoscal.codec.unpacked_reason`), so they are collapsed to one token
    here. A stride the codec would refuse keeps its raw value: it cannot be
    written through at all, and a pin should still notice it changing.
    """
    if axis is None:
        return ()
    emb = axis.embedded
    if emb is None:
        return (None,)
    stride: object = (
        "packed" if unpacked_reason(emb) is None
        else (emb.major_stride_bits, emb.minor_stride_bits)
    )
    scaling = axis.scaling
    return (
        emb.address, emb.rows, emb.cols, emb.elem_bits, stride,
        emb.signed, emb.little_endian, emb.is_float, emb.column_major,
        # Compared on the source expression, as the parser's fingerprint does:
        # the coefficients are floats parsed from it, and NaN != NaN would make a
        # non-linear scaling never match itself.
        None if scaling is None else (scaling.expression, scaling.is_linear),
    )


def layout_digest(view: TableView) -> str:
    """A stable short digest of *where* ``view`` is and *how* it decodes.

    Moves when the address, shape, element width, packing, signedness,
    endianness, float flag, element order, or scaling expression of the table or
    either of its axes moves. Does not move for a retitled or recategorised
    table, or for a definition file that spells a packed stride differently —
    neither changes a byte.

    Stable across runs and machines: a SHA-256 over canonical text, never
    :func:`hash`, whose value is salted per process and would make a pin
    meaningless the moment it was written down.
    """
    table = view.table
    canonical = repr((
        _canonical_axis(table.x), _canonical_axis(table.y), _canonical_axis(table.z),
    ))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:_DIGEST_CHARS]


def pin_layouts(profile: Profile, cal: CalFile) -> dict[str, str]:
    """Every mapped name in ``profile`` → its layout in ``cal``, for pinning.

    The generator behind :attr:`Profile.table_layouts`. Run it against the
    definition file the map was authored against — the reviewed one, named in
    :attr:`Profile.xdf` — and paste the result into the profile module.
    ``python -m simoscal.tune.profiles pin`` does exactly this and formats the
    block.

    Deliberately not called at import to pin a profile automatically: a pin
    computed from whatever file is in front of you authenticates nothing, and
    the whole value of the mapping is that a human put those numbers there once,
    from a file they had reason to trust.
    """
    resolved = resolve(profile, cal, xdf_label=profile.xdf)
    return {
        name: layout_digest(resolved[name].view)
        for name in sorted(resolved.names())
    }


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
        # The pin, where the profile has one. Symbol and shape say this is the
        # right *table*; the digest says this is the right *definition of* it —
        # same address, same element type and strides, same scaling. Nothing
        # downstream can make this check for us: the journal, the readback and
        # the byte audit are all computed through whatever this XDF says, so
        # they agree with a moved table as readily as with a correct one
        # (CR-20260828-02).
        pinned = profile.table_layouts.get(name)
        if pinned is not None:
            actual = layout_digest(view)
            if actual != pinned:
                misses.append(
                    ResolutionMiss(
                        name,
                        key,
                        f"layout {actual} does not match the {pinned} this map "
                        f"was authored against — this XDF places or decodes the "
                        f"table differently from {profile.xdf}, so writing "
                        f"through it would put bytes somewhere nobody reviewed",
                    )
                )
                continue
        tables[name] = ResolvedTable(spec=spec, view=view)

    if misses:
        raise ProfileResolutionError(profile, label, misses)
    return ResolvedProfile(profile, cal, tables)
