"""BinToolz' 5-slot map switch, patch 29.33 — what it *is*, and where S50 keeps it.

These tables exist only after the 29.33 ``.btp`` has been applied, and they are
patch-added: **no A2L symbol**, and the five slot grids all carry the identical
title ``PUT setpoint``. Both facts force uniqueid binding — the one case where a
map entry is an address rather than a name. In these XDFs a table's uniqueid
*equals* its XDF address.

That forced binding is also why this module is split the way it is. Everything
down to :func:`build_switch_patch_profile` describes **the patch**: what it does,
table by table, what may be written and what may not, and the structural rules a
writer has to honour. Below it, and only there, sits S50's address book. None of it is a per-car fact — it is one BinToolz build, cut for several
file structures, and the definitions are the same file with the addresses moved.
What *is* per-car is the address book, and only that. So the prose lives here
once, :func:`build_switch_patch_profile` turns an address book into a profile,
and a second car is a second address book (see :mod:`.switchpatch_2933_a05`) —
never a second copy of the descriptions, which would be free to drift from these
while claiming to describe the same patch.

The corollary matters as much: an address book is not derivable from another
car's. The A05 offsets from S50 fall into three different deltas depending on
which part of the patch a table sits in, so adding the most common one would put
25 of the 92 in the wrong table — and, since these bind by address rather than by
name, all 92 would still resolve. Each book is read off its own XDF.

S50 bindings were verified against both switch-patch XDFs on 2026-07-11; see
``knowledge/sc8s50-switchpatch-xdf.md``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from ..profile import (
    GROUP_LAUNCH_TRACTION,
    TAG_AXIS,
    TAG_NO_SYMBOL,
    Profile,
    TableSpec,
)

#: Slot count the 29.33 patch provides.
SLOTS = (1, 2, 3, 4, 5)

#: Geometry of a slot's PUT setpoint grid: 8 uncharacterized Y rows × the 12
#: breakpoints of the shared RPM axis. The lineage tiles one curve across all
#: eight rows, since the Y axis carries no meaning in the patch.
SLOT_GRID_SHAPE = (8, 12)

#: The as-patched, deliberately non-binding default in every slot grid.
SLOT_DEFAULT_HPA = 4000.0

#: The shared axis header is a length marker the patch reads; it must stay 12.
SLOT_AXIS_HEADER_VALUE = 12.0

#: The as-patched, no-op value in every ``Spark modifier`` cell, in °CRK. Note
#: it is a *decoded* zero, not a raw zero: the grid carries the base ignition
#: map's own codec (``0.375 * raw - 35.625``), so neutral is raw 95. That the
#: patch ships every slot neutral at 0.00° is what proves the grid is an
#: **additive** offset rather than a replacement or a multiplier — a
#: replacement would have every slot commanding no timing at all, and a
#: multiplier would have to ship neutral at 1.0. See
#: ``knowledge/sc8s50-switchpatch-xdf.md`` § Per-slot ``Spark modifier``.
SPARK_DEFAULT_DEGREES = 0.0

#: The as-patched, no-op value in every ``Lambda modifier`` cell, as a lambda
#: offset. Neutral is again a *decoded* zero on a signed codec centred at raw
#: 32768 (``raw / 1024 - 32``), which carries the same proof as the spark grid
#: above: a grid that shipped neutral at 0.00 cannot be a replacement setpoint —
#: every slot would be commanding lambda 0.00 — and cannot be a multiplier,
#: which would have to ship neutral at 1.0. It is additive onto the lambda the
#: base grid asks for.
#:
#: What the patch does **not** establish is the sign. The `Spark modifier`
#: grid's sign was measured — R20 wrote +1.125 to +3.750 CRK and the R20 and
#: R22 logs show exactly that arriving in `Ign Avg` — but no revision has ever
#: written a `Lambda modifier` cell, so the direction is inferred from the
#: sibling and not yet observed. A caller must therefore treat a positive offset
#: as *leaner* (the sibling's convention) while designing so that being wrong
#: about it fails rich; :meth:`SwitchPatch.slot_lambda_map` enforces exactly
#: that by refusing any write it cannot bound on both sides.
LAMBDA_DEFAULT_OFFSET = 0.0

# Every table in this profile is domain-owned: the patch's structural rules —
# eight-row tiling, the below-base-ceiling cap, the separate axis-length header —
# live in ``SwitchPatch``, and a generic grid write honours none of them. So each
# spec names its owning call, and the generic editor refuses the table outright
# rather than writing a structurally invalid patch that still passes the byte
# gates (CR-20260813-01).
_OWNER_SLOT_CURVE = "tune.switchpatch.slot_curve() (bridge op `boost_edit`)"
_OWNER_RPM_AXIS = "tune.switchpatch.slot_rpm_axis() (bridge op `boost_rpm_axis`)"
_OWNER_AXIS_HEADER = (
    "no write path at all — tune.switchpatch checks this header and never "
    "writes it"
)
_OWNER_SLOT_SPARK = "tune.switchpatch.slot_spark_map() (bridge op `spark_edit`)"
_OWNER_SLOT_LAMBDA = "tune.switchpatch.slot_lambda_map()"
_OWNER_TRACTION = "tune.switchpatch.traction_control()"
_OWNER_SLOT_FLAG = "tune.switchpatch.set_slot_flag() (bridge op `slot_flag`)"
_OWNER_REV_LIMITS = (
    "tune.limits.rev_limits(), which writes the trio in one call and refuses "
    "unless soft <= medium <= hard (bridge op `limiters_edit`)"
)


# --------------------------------------------------------------------------- #
# The per-slot scalars — the switchboard
# --------------------------------------------------------------------------- #
# Sixteen 1×1 tables differ from slot to slot. They are what map switching is
# *for* on this patch: one shared tune of every feature's internals, and a
# per-slot decision about which features are on.
#
# They are described here rather than as sixteen hand-written specs because the
# app renders them as a table — five slots across, one setting down — and that
# grid needs the same facts the profile needs. One registry, both consumers, so
# a setting cannot be writable in the app and unmapped in the profile.

#: A 0/1 switch. The only kind this library writes.
KIND_FLAG = "flag"
#: A scalar with a unit and a meaningful range (rpm, kph, a fraction).
KIND_NUMBER = "number"
#: A packed value whose individual bits are not documented anywhere we have.
KIND_OPAQUE = "opaque"


@dataclass(frozen=True)
class SlotSetting:
    """One per-slot scalar: what it is, and whether we are willing to write it.

    ``readonly`` is the important field. Empty means writable; anything else is
    the *reason* it is not, carried all the way to the screen so a person sees
    why a row will not toggle instead of finding it inert. The patch exposes
    plenty we can read and describe but have no business writing yet, and
    "shown, explained, and refused" is the honest presentation of that.

    ``caution`` is different: the setting *is* writable, and this is what it
    does to a moving car. It is not a confirmation gate, just the sentence the
    person should have read first.
    """

    key: str                 # logical suffix — slot{N}_{key}
    title: str               # the XDF title, verbatim
    description: str         # what it does, in English
    kind: str
    units: str = ""
    group: str = ""          # how the switchboard clusters rows
    caution: str = ""
    readonly: str = ""

    @property
    def writable(self) -> bool:
        return not self.readonly


_UNVERIFIED_NUMBER = (
    "reads 0 in every slot of the as-patched bin, so the meaning of a non-zero "
    "value — and whether 0 means 'leave the OEM limiter alone' — is inferred, "
    "not established. Writing an override nobody has characterised is how you "
    "get a rev limit you did not intend, so this library reads it and stops."
)

SLOT_SETTINGS = (
    # ---- traction ---------------------------------------------------------- #
    SlotSetting(
        key="enable_sl_tc", title="Enable SL TC",
        description="Enable the switch patch's own slip-based traction control "
                    "(a PID controller intervening through ignition retard and "
                    "the wastegate)",
        kind=KIND_FLAG, group="Traction",
        caution="Its PID weights and slip targets are global, shared by every "
                "slot, and ship at defaults nobody here has reviewed.",
    ),
    SlotSetting(
        key="disable_oem_tc", title="Disable OEM TC",
        description="Disable the factory ECU-side traction-control torque "
                    "intervention",
        kind=KIND_FLAG, group="Traction",
        caution="Turns off a driver-safety system on a road car. Pair it with "
                "Enable SL TC — the two intervene differently and fighting each "
                "other is worse than either alone. The ABS/ESC module's "
                "brake-based intervention is a separate controller this cannot "
                "touch.",
    ),
    # ---- features ---------------------------------------------------------- #
    SlotSetting(
        key="enable_lc", title="Enable LC",
        description="Enable launch control (configured globally in the LC "
                    "category: target rpm, timing during pull-up)",
        kind=KIND_FLAG, group="Features",
        caution="Launch control on a DSG loads the clutches and driveline hard.",
    ),
    SlotSetting(
        key="enable_nls", title="Enable NLS",
        description="Enable no-lift shift",
        kind=KIND_FLAG, group="Features",
        caution="Written for manual gearboxes; what it does on a DSG is not "
                "established here.",
    ),
    SlotSetting(
        key="enable_ral", title="Enable RAL",
        description="Enable the patch's RAL feature — the expansion is not "
                    "recorded in either switch-patch XDF or the knowledge base "
                    "(commonly rolling anti-lag, unverified)",
        kind=KIND_FLAG, group="Features",
        caution="Turning on a feature whose name we cannot expand is a decision "
                "to find out on the road.",
    ),
    SlotSetting(
        key="pops_enable", title="Pops enable",
        description="Enable pops and bangs / impulse combustion on overrun",
        kind=KIND_FLAG, group="Features",
        caution="Puts combustion into the exhaust; hard on the turbine and the "
                "catalyst.",
    ),
    # ---- flex fuel --------------------------------------------------------- #
    # Six independent enables for the flex-fuel corrections, one per quantity
    # the patch can trim against ethanol content.
    SlotSetting(
        key="enable_ff_spark", title="Enable flex fuel spark modifier",
        description="Apply the flex-fuel ignition-timing correction",
        kind=KIND_FLAG, group="Flex fuel",
    ),
    SlotSetting(
        key="enable_ff_put", title="Enable flex fuel PUT modifier",
        description="Apply the flex-fuel boost-target correction",
        kind=KIND_FLAG, group="Flex fuel",
    ),
    SlotSetting(
        key="enable_ff_lambda", title="Enable flex fuel lambda modifier",
        description="Apply the flex-fuel lambda-setpoint correction",
        kind=KIND_FLAG, group="Flex fuel",
    ),
    SlotSetting(
        key="enable_ff_tq", title="Enable flex fuel TQ modifier",
        description="Apply the flex-fuel torque-model correction",
        kind=KIND_FLAG, group="Flex fuel",
    ),
    SlotSetting(
        key="enable_ff_iat", title="Enable flex fuel IAT modifier",
        description="Apply the flex-fuel intake-air-temperature correction",
        kind=KIND_FLAG, group="Flex fuel",
    ),
    SlotSetting(
        key="enable_ff_mpi", title="Enable flex fuel MPI modifier",
        description="Apply the flex-fuel port-injection correction",
        kind=KIND_FLAG, group="Flex fuel",
    ),
    # ---- read-only --------------------------------------------------------- #
    SlotSetting(
        key="rpm_limiter", title="RPM limiter",
        description="Per-slot engine-speed limit override",
        kind=KIND_NUMBER, units="rpm", group="Limits",
        readonly=_UNVERIFIED_NUMBER,
    ),
    SlotSetting(
        key="speed_limiter", title="Speed limiter",
        description="Per-slot road-speed limit override",
        kind=KIND_NUMBER, units="kph", group="Limits",
        readonly=_UNVERIFIED_NUMBER,
    ),
    SlotSetting(
        key="manual_afu", title="Manual AFU",
        description="A 0–1 fraction, stored /128. The XDF says it 'does not set "
                    "manual AFU active, this only adjusts the value'; the "
                    "patch's own logging category refers to 'manual e content', "
                    "so this is most likely the hand-set ethanol fraction — "
                    "likely, not established",
        kind=KIND_NUMBER, group="Flex fuel",
        readonly="what quantity this actually sets is inferred from a category "
                 "name, and a fuel-composition input the engine trusts is not "
                 "something to write on an inference.",
    ),
    SlotSetting(
        key="gauge_settings", title="Gauge settings (bitmask)",
        description="Eight packed display/gauge option bits",
        kind=KIND_OPAQUE, group="Display",
        readonly="no source we have says what any individual bit means, and a "
                 "bitmask written as a whole number sets seven bits you did not "
                 "choose.",
    ),
)

#: Settings by logical suffix, for the domain call and the bridge.
SLOT_SETTINGS_BY_KEY = {s.key: s for s in SLOT_SETTINGS}


#: The logical names of the tables that are not per-slot — the shared RPM
#: axis and its header, the cylinder-cut trio, launch control's two scalars.
#: Every address book must name exactly these, which is what stops a port
#: from quietly shipping a profile that is missing the axis.
STANDALONE_ROLES = (
    "slot_put_rpm_axis",
    "slot_put_rpm_axis_header",
    "rev_limit_soft",
    "rev_limit_medium",
    "rev_limit_hard",
    "lc_limiter_timing",
    "lc_release_speed",
)


def _standalone_specs(uids: Mapping[str, str]) -> list[TableSpec]:
    """The seven non-per-slot specs, bound to one car's address book."""
    return [
        TableSpec(
            name="slot_put_rpm_axis", key=uids["slot_put_rpm_axis"],
            description="PUT SP RPM Axis — engine-speed breakpoints shared by all "
                        "five slot PUT setpoint grids",
            units="rpm", shape=(1, 12), tags=frozenset({TAG_AXIS, TAG_NO_SYMBOL}),
            owner=_OWNER_RPM_AXIS,
        ),
        TableSpec(
            name="slot_put_rpm_axis_header", key=uids["slot_put_rpm_axis_header"],
            description="PUT SP RPM Axis Header — breakpoint count, must remain 12",
            units="", shape=(1, 1), tags=frozenset({TAG_NO_SYMBOL}),
            owner=_OWNER_AXIS_HEADER,
        ),

        # ---- the progressive cylinder-cut trio ---------------------------------- #
        # Three rpm *offsets*, not absolute rev limits, and the distinction is the
        # whole reason these carry this much prose. Each title reads "above
        # engagement point", and all three sit in the patch's **RAL** category
        # beside `Minimum engagement RPM` (2500) and `Maximum engagement RPM` (4500)
        # — so the reference point they are measured from is
        # the patch's own engagement rpm, not redline. Nothing we have states which
        # of that pair is the reference, or whether the offsets apply outside RAL,
        # and this library does not guess: the numbers are read, written, and
        # described exactly as the XDF describes them.
        #
        # What each one *does* is documented, unusually for this patch — the XDF
        # spells out the cut pattern, and it escalates across the three. That
        # escalation is the invariant: soft cuts least, hard cuts most, so a trio
        # ordered any other way asks the ECU to escalate backwards. Hence the
        # domain owner; a generic grid write to one scalar cannot see the other two.
        #
        # As-patched (verified on the R12 bin, 2026-08-20): 0 / 64 / 64 rpm.
        TableSpec(
            name="rev_limit_soft", key=uids["rev_limit_soft"],
            description="Rev soft limit above engagement point — rpm offset at "
                        "which the engine cuts fuel and spark to 1 cylinder every 4",
            units="rpm", shape=(1, 1), tags=frozenset({TAG_NO_SYMBOL}),
            owner=_OWNER_REV_LIMITS,
        ),
        TableSpec(
            name="rev_limit_medium", key=uids["rev_limit_medium"],
            description="Rev medium limit above engagement point — rpm offset at "
                        "which the engine cuts fuel and spark to 1 cylinder every 3",
            units="rpm", shape=(1, 1), tags=frozenset({TAG_NO_SYMBOL}),
            owner=_OWNER_REV_LIMITS,
        ),
        TableSpec(
            name="rev_limit_hard", key=uids["rev_limit_hard"],
            description="Rev hard limit above engagement point — rpm offset at "
                        "which the engine cuts fuel and spark to 2 cylinders every 4",
            units="rpm", shape=(1, 1), tags=frozenset({TAG_NO_SYMBOL}),
            owner=_OWNER_REV_LIMITS,
        ),

        # ---- launch control's limiter behaviour ---------------------------------- #
        # Both are **LC** category, not RAL and not a general rev limiter: they
        # describe how the launch-control rpm limiter behaves and when it lets go.
        # Independent scalars with no cross-table invariant, so they stay generically
        # editable — the coverage brainstorm's rule for patch-space tables (its Key
        # Decision 3), and the same call the pedal maps get in the base space.
        #
        # They are also the only two tables in this profile the generic browser ever
        # shows, so they are the only two that need a ``group``: every other spec
        # here is owner-locked and reached through the Boost or Slots screen, which
        # is domain-shaped already. Filing a per-slot RAL toggle or a gauge bitmask
        # under one of the eight engine-domain headings would be classification for
        # its own sake — see ``_ungrouped_is_deliberate`` below.
        TableSpec(
            name="lc_limiter_timing", key=uids["lc_limiter_timing"],
            description="Timing during RPM limiter and rampout — ignition angle "
                        "held while launch control sits on its limiter",
            units="\N{DEGREE SIGN}CRK", shape=(1, 1), tags=frozenset({TAG_NO_SYMBOL}),
            group=GROUP_LAUNCH_TRACTION,
        ),
        TableSpec(
            name="lc_release_speed", key=uids["lc_release_speed"],
            description="Release RPM limiter speed — road speed at which launch "
                        "control releases its rpm limiter",
            units="km/h", shape=(1, 1), tags=frozenset({TAG_NO_SYMBOL}),
            group=GROUP_LAUNCH_TRACTION,
        ),
    ]


#: The progressive cylinder-cut trio, in escalation order. The invariant every
#: writer must hold: ``soft <= medium <= hard``.
REV_LIMIT_TRIO = ("rev_limit_soft", "rev_limit_medium", "rev_limit_hard")

#: Launch control's limiter behaviour — generically editable, unlike the trio.
LAUNCH_CONTROL_LIMITER = ("lc_limiter_timing", "lc_release_speed")


def slot_names(kind: str) -> tuple[str, ...]:
    """Logical names for one per-slot table ``kind``, slots 1–5 in order.

    ``kind`` is the suffix: ``"put_setpoint"``, ``"enable_sl_tc"``, or
    ``"disable_oem_tc"``. The vocabulary is a property of the *patch*, not of a
    car, so these are the names every 29.33 profile carries — the S50 profile is
    asked here only because a membership check needs some profile to ask.
    """
    names = tuple(f"slot{s}_{kind}" for s in SLOTS)
    unknown = [n for n in names if n not in SWITCH_PATCH_2933]
    if unknown:
        raise KeyError(f"no per-slot tables of kind {kind!r} in this profile")
    return names


def _ungrouped_is_deliberate(name: str, specs: list[TableSpec]) -> list[TableSpec]:
    """Assert that only owner-locked specs go without a :attr:`TableSpec.group`.

    A group is a heading in the generic table browser, and the browser is offered
    exactly the specs with no ``owner``. So the rule this profile holds itself to
    is not "every table has a group" — it is "every table the browser can show
    has one". The ninety owner-locked slot tables reach the user through the
    Boost and Slots screens, which are already shaped by domain; giving a gauge
    bitmask an engine-domain heading would say something untrue about it.

    The check runs at import, so a future spec added here without an ``owner``
    and without a ``group`` fails before it can appear as an unfiled row.
    """
    orphans = sorted(s.name for s in specs if not s.owner and not s.group)
    if orphans:
        raise ValueError(
            f"{name}: generically editable tables need a group: "
            f"{', '.join(orphans)}"
        )
    return specs


def _check_address_book(
    name: str,
    standalone_uids: Mapping[str, str],
    put_grid_uids: Mapping[int, str],
    slot_setting_uids: Mapping[str, Mapping[int, str]],
    spark_grid_uids: Mapping[int, str] | None,
    spark_grid_shape: tuple[int, int] | None,
    lambda_grid_uids: Mapping[int, str] | None = None,
    lambda_grid_shape: tuple[int, int] | None = None,
) -> None:
    """Refuse an address book that is incomplete, over-full, or self-colliding.

    Three failures this catches, all of which a port can commit by accident and
    none of which resolution would notice:

    * a **missing** role — resolution only checks the names a profile declares,
      so a book that forgot the RPM axis produces a profile that resolves
      perfectly and has no axis to edit;
    * an **extra** role — a name nothing builds a spec from, which reads like
      coverage and is not;
    * a **repeated uniqueid** — two logical names pointing at one table, the
      copy-a-column typo. Both names would then resolve, and writing one would
      silently move the other.

    Uniqueids are compared as parsed ints, so ``0x7D41A`` and ``0x7d41a`` are the
    same address here as they are in the bin.
    """
    expected_standalone = set(STANDALONE_ROLES)
    expected_settings = {s.key for s in SLOT_SETTINGS}
    for label, got, want in (
        ("standalone roles", set(standalone_uids), expected_standalone),
        ("per-slot settings", set(slot_setting_uids), expected_settings),
    ):
        missing, extra = sorted(want - got), sorted(got - want)
        if missing or extra:
            raise ValueError(
                f"{name}: address book {label} do not match the patch: "
                f"missing {missing or 'none'}, unexpected {extra or 'none'}"
            )

    # The spark grids are optional — a car gains them when someone reads their
    # uniqueids off that car's own patch XDF — but they are all-or-nothing, and
    # they never come without a shape. S50's grid is (16, 16) and A05's is
    # (16, 18), so unlike every other table in this patch the geometry is
    # genuinely per-car; a shared constant here would put a book's grids in the
    # profile at the wrong size while resolving perfectly.
    if (spark_grid_uids is None) != (spark_grid_shape is None):
        raise ValueError(
            f"{name}: spark_grid_uids and spark_grid_shape must be given "
            "together — the Spark modifier grid's shape differs between cars "
            "(S50 is 16x16, A05 is 16x18), so it cannot be defaulted"
        )
    # The five ``Lambda modifier`` grids are optional on exactly the same terms,
    # and for the same reason: a car gains them when someone reads their
    # uniqueids off that car's own patch XDF. They sit on the *base lambda*
    # grid's axes rather than the ignition grid's, so their geometry is a
    # second per-car fact and travels with the book the same way.
    if (lambda_grid_uids is None) != (lambda_grid_shape is None):
        raise ValueError(
            f"{name}: lambda_grid_uids and lambda_grid_shape must be given "
            "together — the Lambda modifier grid sits on the base lambda "
            "grid's own axes, whose breakpoint counts are per-car, so its "
            "shape cannot be defaulted"
        )

    for label, slots in (
        ("PUT grids", set(put_grid_uids)),
        *(() if spark_grid_uids is None
          else (("Spark modifier grids", set(spark_grid_uids)),)),
        *(() if lambda_grid_uids is None
          else (("Lambda modifier grids", set(lambda_grid_uids)),)),
        *((f"setting {k!r}", set(v)) for k, v in slot_setting_uids.items()),
    ):
        if slots != set(SLOTS):
            raise ValueError(
                f"{name}: address book {label} covers slots {sorted(slots)}, "
                f"but the patch has {list(SLOTS)}"
            )

    seen: dict[int, str] = {}
    everything = [
        *((role, uid) for role, uid in standalone_uids.items()),
        *((f"slot{s}_put_setpoint", uid) for s, uid in put_grid_uids.items()),
        *((f"slot{s}_spark_modifier", uid)
          for s, uid in (spark_grid_uids or {}).items()),
        *((f"slot{s}_lambda_modifier", uid)
          for s, uid in (lambda_grid_uids or {}).items()),
        *(
            (f"slot{s}_{key}", uid)
            for key, per_slot in slot_setting_uids.items()
            for s, uid in per_slot.items()
        ),
    ]
    for role, uid in everything:
        address = int(uid, 16)
        if address in seen:
            raise ValueError(
                f"{name}: {role} and {seen[address]} are both bound to uniqueid "
                f"{uid} — two logical names cannot share one table, and writing "
                "either would silently move the other"
            )
        seen[address] = role


def build_switch_patch_profile(
    *,
    name: str,
    xdf: str,
    standalone_uids: Mapping[str, str],
    put_grid_uids: Mapping[int, str],
    slot_setting_uids: Mapping[str, Mapping[int, str]],
    spark_grid_uids: Mapping[int, str] | None = None,
    spark_grid_shape: tuple[int, int] | None = None,
    lambda_grid_uids: Mapping[int, str] | None = None,
    lambda_grid_shape: tuple[int, int] | None = None,
) -> Profile:
    """Bind this patch's tables — 92, or 97 with the spark grids — to one car.

    The descriptions, units and owners come from the patch — they are the same on
    every car it is cut for. Only ``*_uids`` change, and they are read off that
    car's own patch XDF rather than offset from another car's (see the module
    docstring for why arithmetic does not work here). Shapes are the same on
    every car too, with exactly one exception, which is why it is the one shape
    this signature asks for: see ``spark_grid_shape`` below.

    ``spark_grid_uids`` is the one optional part, and the only place a shape is
    asked for. The five per-slot ``Spark modifier`` grids came later than the
    rest of this book, and a car keeps working without them — so a book that has
    not had them read off its own XDF yet simply omits them and gets the other
    92. Supplying them requires ``spark_grid_shape`` in the same call, because
    that geometry is per-car (see :func:`_check_address_book`).
    """
    _check_address_book(
        name, standalone_uids, put_grid_uids, slot_setting_uids,
        spark_grid_uids, spark_grid_shape,
        lambda_grid_uids, lambda_grid_shape,
    )

    specs = _standalone_specs(standalone_uids)
    for slot in SLOTS:
        specs.append(TableSpec(
            name=f"slot{slot}_put_setpoint", key=put_grid_uids[slot],
            description=f"PUT setpoint — boost target grid for map slot {slot}",
            units="hPa", shape=SLOT_GRID_SHAPE, tags=frozenset({TAG_NO_SYMBOL}),
            owner=_OWNER_SLOT_CURVE,
        ))
        if spark_grid_uids is not None:
            specs.append(TableSpec(
                name=f"slot{slot}_spark_modifier", key=spark_grid_uids[slot],
                description=(
                    "Spark modifier — additive ignition-angle offset for map "
                    f"slot {slot}, on the shared base-timing rpm × airmass grid"
                ),
                units="\N{DEGREE SIGN}CRK", shape=spark_grid_shape,
                tags=frozenset({TAG_NO_SYMBOL}),
                owner=_OWNER_SLOT_SPARK,
            ))
        if lambda_grid_uids is not None:
            specs.append(TableSpec(
                name=f"slot{slot}_lambda_modifier", key=lambda_grid_uids[slot],
                description=(
                    "Lambda modifier — additive lambda offset for map slot "
                    f"{slot}, on the base lambda setpoint grid's own "
                    "rpm x airmass axes"
                ),
                units="lambda", shape=lambda_grid_shape,
                tags=frozenset({TAG_NO_SYMBOL}),
                owner=_OWNER_SLOT_LAMBDA,
            ))
        # The sixteen per-slot scalars, straight off the registry above. Owned
        # like everything else in this profile: a flag is only written through
        # the domain call that checks it *is* a flag first, and a read-only
        # setting names the reason it has no write path at all.
        for setting in SLOT_SETTINGS:
            specs.append(TableSpec(
                name=f"slot{slot}_{setting.key}",
                key=slot_setting_uids[setting.key][slot],
                description=f"{setting.title} — {setting.description}, "
                            f"map slot {slot}",
                units=setting.units, shape=(1, 1),
                tags=frozenset({TAG_NO_SYMBOL}),
                owner=(
                    _OWNER_SLOT_FLAG if setting.writable
                    else f"no write path — {setting.readonly}"
                ),
            ))

    specs = _ungrouped_is_deliberate(name, specs)
    return Profile(
        name=name, xdf=xdf, specs={s.name: s for s in specs},
        # Unlike a base profile's sets, these are the same on every car: they
        # name tables the *patch* adds, so the grouping travels with the patch
        # and reaches a tune through `Profile.merged_with`.
        table_sets={
            "rev_limit_trio": REV_LIMIT_TRIO,
            "launch_control_limiter": LAUNCH_CONTROL_LIMITER,
        },
    )


# --------------------------------------------------------------------------- #
# S50's address book — the only per-car part of this module
# --------------------------------------------------------------------------- #
#: S50 — the seven tables that are not per-slot, by logical name.
S50_STANDALONE_UIDS = {
    "slot_put_rpm_axis":        "0x7d7dc",
    "slot_put_rpm_axis_header": "0x7d7da",
    "rev_limit_soft":           "0x7cb18",
    "rev_limit_medium":         "0x7cb1a",
    "rev_limit_hard":           "0x7cb1c",
    "lc_limiter_timing":        "0x7cb31",
    "lc_release_speed":         "0x7cb3c",
}

#: S50 — the five slot PUT setpoint grids, verified on the as-patched bin.
S50_PUT_GRID_UIDS = {
    1: "0x7d41a", 2: "0x7d4da", 3: "0x7d59a", 4: "0x7d65a", 5: "0x7d71a",
}

#: S50 — the five per-slot ``Spark modifier`` grids.
#:
#: Slot attribution is from each table's third ``CATEGORYMEM`` in
#: ``S50 Switch Patch.29.33.V2.xdf`` (``category="248"`` down to ``"244"``,
#: i.e. ``CATEGORY`` indices ``0xF7``–``0xF3`` = Map Slot 1–5), not from address
#: order: the five sit at a tidy ``0x100`` stride and reading the slot off that
#: stride would be a guess that happened to be right.
S50_SPARK_GRID_UIDS = {
    1: "0x7cf1a", 2: "0x7d01a", 3: "0x7d11a", 4: "0x7d21a", 5: "0x7d31a",
}

#: S50 — the five per-slot ``Lambda modifier`` grids.
#:
#: Slot attribution is read the same way as the spark grids above — each table's
#: third ``CATEGORYMEM`` in ``S50 Switch Patch.29.33.V2.xdf``, ``category="248"``
#: down to ``"244"`` for Map Slot 1-5 — and not off the tidy ``0xC0`` address
#: stride, which would be a guess that happened to agree.
S50_LAMBDA_GRID_UIDS = {
    1: "0x7cb5a", 2: "0x7cc1a", 3: "0x7ccda", 4: "0x7cd9a", 5: "0x7ce5a",
}

#: S50 — the ``Lambda modifier`` geometry: the 12 rpm x 8 mg/stk breakpoints of
#: the base lambda setpoint grid, whose axis tables these reuse. Note this is
#: the *re-breakpointed* axis this lineage has run since R00, not the factory
#: one — the grids point at the axis tables themselves, so they follow.
S50_LAMBDA_GRID_SHAPE = (8, 12)

#: S50 — the ``Spark modifier`` geometry: the 16 rpm × 16 mg/stk breakpoints of
#: the base ignition maps' own axes (``0x3ce5a`` and ``0x3cdbc``, which these
#: grids reuse byte for byte). A05's is (16, 18), which is why this travels with
#: the address book instead of being a constant of the patch.
S50_SPARK_GRID_SHAPE = (16, 16)

#: S50 — the sixteen per-slot scalars, ``setting key -> {slot: uniqueid}``.
S50_SLOT_SETTING_UIDS = {
    "enable_sl_tc":     {1: "0x7d83f", 2: "0x7d840", 3: "0x7d841", 4: "0x7d842", 5: "0x7d843"},
    "disable_oem_tc":   {1: "0x7d83a", 2: "0x7d83b", 3: "0x7d83c", 4: "0x7d83d", 5: "0x7d83e"},
    "enable_lc":        {1: "0x7d835", 2: "0x7d836", 3: "0x7d837", 4: "0x7d838", 5: "0x7d839"},
    "enable_nls":       {1: "0x7d830", 2: "0x7d831", 3: "0x7d832", 4: "0x7d833", 5: "0x7d834"},
    "enable_ral":       {1: "0x7d81c", 2: "0x7d81d", 3: "0x7d81e", 4: "0x7d81f", 5: "0x7d820"},
    "pops_enable":      {1: "0x7cb54", 2: "0x7cb55", 3: "0x7cb56", 4: "0x7cb57", 5: "0x7cb58"},
    "enable_ff_spark":  {1: "0x7d7f4", 2: "0x7d7fa", 3: "0x7d800", 4: "0x7d806", 5: "0x7d80c"},
    "enable_ff_put":    {1: "0x7d7f5", 2: "0x7d7fb", 3: "0x7d801", 4: "0x7d807", 5: "0x7d80d"},
    "enable_ff_lambda": {1: "0x7d7f6", 2: "0x7d7fc", 3: "0x7d802", 4: "0x7d808", 5: "0x7d80e"},
    "enable_ff_tq":     {1: "0x7d7f7", 2: "0x7d7fd", 3: "0x7d803", 4: "0x7d809", 5: "0x7d80f"},
    "enable_ff_iat":    {1: "0x7d7f8", 2: "0x7d7fe", 3: "0x7d804", 4: "0x7d80a", 5: "0x7d810"},
    "enable_ff_mpi":    {1: "0x7d7f9", 2: "0x7d7ff", 3: "0x7d805", 4: "0x7d80b", 5: "0x7d811"},
    "rpm_limiter":      {1: "0x7cb40", 2: "0x7cb42", 3: "0x7cb44", 4: "0x7cb46", 5: "0x7cb48"},
    "speed_limiter":    {1: "0x7cb4a", 2: "0x7cb4c", 3: "0x7cb4e", 4: "0x7cb50", 5: "0x7cb52"},
    "manual_afu":       {1: "0x7d85a", 2: "0x7d85b", 3: "0x7d85c", 4: "0x7d85d", 5: "0x7d85e"},
    "gauge_settings":   {1: "0x7f490", 2: "0x7f491", 3: "0x7f492", 4: "0x7f493", 5: "0x7f494"},
}


SWITCH_PATCH_2933 = build_switch_patch_profile(
    name="SwitchPatch2933",
    xdf="S50 Switch Patch.29.33.V2.xdf",
    standalone_uids=S50_STANDALONE_UIDS,
    put_grid_uids=S50_PUT_GRID_UIDS,
    slot_setting_uids=S50_SLOT_SETTING_UIDS,
    spark_grid_uids=S50_SPARK_GRID_UIDS,
    spark_grid_shape=S50_SPARK_GRID_SHAPE,
    lambda_grid_uids=S50_LAMBDA_GRID_UIDS,
    lambda_grid_shape=S50_LAMBDA_GRID_SHAPE,
)

__all__ = [
    "SWITCH_PATCH_2933",
    "STANDALONE_ROLES",
    "S50_PUT_GRID_UIDS",
    "S50_SPARK_GRID_SHAPE",
    "S50_SPARK_GRID_UIDS",
    "S50_SLOT_SETTING_UIDS",
    "S50_STANDALONE_UIDS",
    "build_switch_patch_profile",
    "LAUNCH_CONTROL_LIMITER",
    "REV_LIMIT_TRIO",
    "SLOTS",
    "SLOT_GRID_SHAPE",
    "SLOT_DEFAULT_HPA",
    "SPARK_DEFAULT_DEGREES",
    "LAMBDA_DEFAULT_OFFSET",
    "S50_LAMBDA_GRID_SHAPE",
    "S50_LAMBDA_GRID_UIDS",
    "SLOT_AXIS_HEADER_VALUE",
    "SLOT_SETTINGS",
    "SLOT_SETTINGS_BY_KEY",
    "SlotSetting",
    "KIND_FLAG",
    "KIND_NUMBER",
    "KIND_OPAQUE",
    "slot_names",
]
