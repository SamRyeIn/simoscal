"""Profile map for ``S50 Switch Patch.29.33.V2.xdf`` — BinToolz' 5-slot map switch.

These tables exist only after ``SL PATCH.29.33 - S50.btp`` has been applied,
and they are patch-added: **no A2L symbol**, and the five slot grids all carry
the identical title ``PUT setpoint``. Both facts force uniqueid binding — the
one case where a map entry is an address rather than a name. In this XDF a
table's uniqueid *equals* its XDF address, and its file offset in the bin is
``0x200000 + address``.

Bindings were verified against both switch-patch XDFs on 2026-07-11; see
``knowledge/sc8s50-switchpatch-xdf.md``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..profile import (
    GROUP_LAUNCH_TRACTION,
    TAG_AXIS,
    TAG_NO_SYMBOL,
    Profile,
    TableSpec,
)

#: Slot count the 29.33 patch provides.
SLOTS = (1, 2, 3, 4, 5)

# Per-slot uniqueids, verified on the as-patched bin.
_PUT_GRID_UIDS = {1: "0x7d41a", 2: "0x7d4da", 3: "0x7d59a", 4: "0x7d65a", 5: "0x7d71a"}

#: Geometry of a slot's PUT setpoint grid: 8 uncharacterized Y rows × the 12
#: breakpoints of the shared RPM axis. The lineage tiles one curve across all
#: eight rows, since the Y axis carries no meaning in the patch.
SLOT_GRID_SHAPE = (8, 12)

#: The as-patched, deliberately non-binding default in every slot grid.
SLOT_DEFAULT_HPA = 4000.0

#: The shared axis header is a length marker the patch reads; it must stay 12.
SLOT_AXIS_HEADER_VALUE = 12.0

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
    uids: dict               # slot -> hex uniqueid string
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
        uids={1: "0x7d83f", 2: "0x7d840", 3: "0x7d841", 4: "0x7d842", 5: "0x7d843"},
        caution="Its PID weights and slip targets are global, shared by every "
                "slot, and ship at defaults nobody here has reviewed.",
    ),
    SlotSetting(
        key="disable_oem_tc", title="Disable OEM TC",
        description="Disable the factory ECU-side traction-control torque "
                    "intervention",
        kind=KIND_FLAG, group="Traction",
        uids={1: "0x7d83a", 2: "0x7d83b", 3: "0x7d83c", 4: "0x7d83d", 5: "0x7d83e"},
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
        uids={1: "0x7d835", 2: "0x7d836", 3: "0x7d837", 4: "0x7d838", 5: "0x7d839"},
        caution="Launch control on a DSG loads the clutches and driveline hard.",
    ),
    SlotSetting(
        key="enable_nls", title="Enable NLS",
        description="Enable no-lift shift",
        kind=KIND_FLAG, group="Features",
        uids={1: "0x7d830", 2: "0x7d831", 3: "0x7d832", 4: "0x7d833", 5: "0x7d834"},
        caution="Written for manual gearboxes; what it does on a DSG is not "
                "established here.",
    ),
    SlotSetting(
        key="enable_ral", title="Enable RAL",
        description="Enable the patch's RAL feature — the expansion is not "
                    "recorded in either switch-patch XDF or the knowledge base "
                    "(commonly rolling anti-lag, unverified)",
        kind=KIND_FLAG, group="Features",
        uids={1: "0x7d81c", 2: "0x7d81d", 3: "0x7d81e", 4: "0x7d81f", 5: "0x7d820"},
        caution="Turning on a feature whose name we cannot expand is a decision "
                "to find out on the road.",
    ),
    SlotSetting(
        key="pops_enable", title="Pops enable",
        description="Enable pops and bangs / impulse combustion on overrun",
        kind=KIND_FLAG, group="Features",
        uids={1: "0x7cb54", 2: "0x7cb55", 3: "0x7cb56", 4: "0x7cb57", 5: "0x7cb58"},
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
        uids={1: "0x7d7f4", 2: "0x7d7fa", 3: "0x7d800", 4: "0x7d806", 5: "0x7d80c"},
    ),
    SlotSetting(
        key="enable_ff_put", title="Enable flex fuel PUT modifier",
        description="Apply the flex-fuel boost-target correction",
        kind=KIND_FLAG, group="Flex fuel",
        uids={1: "0x7d7f5", 2: "0x7d7fb", 3: "0x7d801", 4: "0x7d807", 5: "0x7d80d"},
    ),
    SlotSetting(
        key="enable_ff_lambda", title="Enable flex fuel lambda modifier",
        description="Apply the flex-fuel lambda-setpoint correction",
        kind=KIND_FLAG, group="Flex fuel",
        uids={1: "0x7d7f6", 2: "0x7d7fc", 3: "0x7d802", 4: "0x7d808", 5: "0x7d80e"},
    ),
    SlotSetting(
        key="enable_ff_tq", title="Enable flex fuel TQ modifier",
        description="Apply the flex-fuel torque-model correction",
        kind=KIND_FLAG, group="Flex fuel",
        uids={1: "0x7d7f7", 2: "0x7d7fd", 3: "0x7d803", 4: "0x7d809", 5: "0x7d80f"},
    ),
    SlotSetting(
        key="enable_ff_iat", title="Enable flex fuel IAT modifier",
        description="Apply the flex-fuel intake-air-temperature correction",
        kind=KIND_FLAG, group="Flex fuel",
        uids={1: "0x7d7f8", 2: "0x7d7fe", 3: "0x7d804", 4: "0x7d80a", 5: "0x7d810"},
    ),
    SlotSetting(
        key="enable_ff_mpi", title="Enable flex fuel MPI modifier",
        description="Apply the flex-fuel port-injection correction",
        kind=KIND_FLAG, group="Flex fuel",
        uids={1: "0x7d7f9", 2: "0x7d7ff", 3: "0x7d805", 4: "0x7d80b", 5: "0x7d811"},
    ),
    # ---- read-only --------------------------------------------------------- #
    SlotSetting(
        key="rpm_limiter", title="RPM limiter",
        description="Per-slot engine-speed limit override",
        kind=KIND_NUMBER, units="rpm", group="Limits",
        uids={1: "0x7cb40", 2: "0x7cb42", 3: "0x7cb44", 4: "0x7cb46", 5: "0x7cb48"},
        readonly=_UNVERIFIED_NUMBER,
    ),
    SlotSetting(
        key="speed_limiter", title="Speed limiter",
        description="Per-slot road-speed limit override",
        kind=KIND_NUMBER, units="kph", group="Limits",
        uids={1: "0x7cb4a", 2: "0x7cb4c", 3: "0x7cb4e", 4: "0x7cb50", 5: "0x7cb52"},
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
        uids={1: "0x7d85a", 2: "0x7d85b", 3: "0x7d85c", 4: "0x7d85d", 5: "0x7d85e"},
        readonly="what quantity this actually sets is inferred from a category "
                 "name, and a fuel-composition input the engine trusts is not "
                 "something to write on an inference.",
    ),
    SlotSetting(
        key="gauge_settings", title="Gauge settings (bitmask)",
        description="Eight packed display/gauge option bits",
        kind=KIND_OPAQUE, group="Display",
        uids={1: "0x7f490", 2: "0x7f491", 3: "0x7f492", 4: "0x7f493", 5: "0x7f494"},
        readonly="no source we have says what any individual bit means, and a "
                 "bitmask written as a whole number sets seven bits you did not "
                 "choose.",
    ),
)

#: Settings by logical suffix, for the domain call and the bridge.
SLOT_SETTINGS_BY_KEY = {s.key: s for s in SLOT_SETTINGS}

_specs = [
    TableSpec(
        name="slot_put_rpm_axis", key="0x7d7dc",
        description="PUT SP RPM Axis — engine-speed breakpoints shared by all "
                    "five slot PUT setpoint grids",
        units="rpm", shape=(1, 12), tags=frozenset({TAG_AXIS, TAG_NO_SYMBOL}),
        owner=_OWNER_RPM_AXIS,
    ),
    TableSpec(
        name="slot_put_rpm_axis_header", key="0x7d7da",
        description="PUT SP RPM Axis Header — breakpoint count, must remain 12",
        units="", shape=(1, 1), tags=frozenset({TAG_NO_SYMBOL}),
        owner=_OWNER_AXIS_HEADER,
    ),

    # ---- the progressive cylinder-cut trio ---------------------------------- #
    # Three rpm *offsets*, not absolute rev limits, and the distinction is the
    # whole reason these carry this much prose. Each title reads "above
    # engagement point", and all three sit in the patch's **RAL** category
    # beside `Minimum engagement RPM` (0x7cb12, 2500) and `Maximum engagement
    # RPM` (0x7cb14, 4500) — so the reference point they are measured from is
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
        name="rev_limit_soft", key="0x7cb18",
        description="Rev soft limit above engagement point — rpm offset at "
                    "which the engine cuts fuel and spark to 1 cylinder every 4",
        units="rpm", shape=(1, 1), tags=frozenset({TAG_NO_SYMBOL}),
        owner=_OWNER_REV_LIMITS,
    ),
    TableSpec(
        name="rev_limit_medium", key="0x7cb1a",
        description="Rev medium limit above engagement point — rpm offset at "
                    "which the engine cuts fuel and spark to 1 cylinder every 3",
        units="rpm", shape=(1, 1), tags=frozenset({TAG_NO_SYMBOL}),
        owner=_OWNER_REV_LIMITS,
    ),
    TableSpec(
        name="rev_limit_hard", key="0x7cb1c",
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
        name="lc_limiter_timing", key="0x7cb31",
        description="Timing during RPM limiter and rampout — ignition angle "
                    "held while launch control sits on its limiter",
        units="\N{DEGREE SIGN}CRK", shape=(1, 1), tags=frozenset({TAG_NO_SYMBOL}),
        group=GROUP_LAUNCH_TRACTION,
    ),
    TableSpec(
        name="lc_release_speed", key="0x7cb3c",
        description="Release RPM limiter speed — road speed at which launch "
                    "control releases its rpm limiter",
        units="km/h", shape=(1, 1), tags=frozenset({TAG_NO_SYMBOL}),
        group=GROUP_LAUNCH_TRACTION,
    ),
]

for _slot in SLOTS:
    _specs += [
        TableSpec(
            name=f"slot{_slot}_put_setpoint", key=_PUT_GRID_UIDS[_slot],
            description=f"PUT setpoint — boost target grid for map slot {_slot}",
            units="hPa", shape=SLOT_GRID_SHAPE, tags=frozenset({TAG_NO_SYMBOL}),
            owner=_OWNER_SLOT_CURVE,
        ),
    ]
    # The sixteen per-slot scalars, straight off the registry above. Owned like
    # everything else in this profile: a flag is only written through the domain
    # call that checks it *is* a flag first, and a read-only setting names the
    # reason it has no write path at all.
    for _setting in SLOT_SETTINGS:
        _specs.append(TableSpec(
            name=f"slot{_slot}_{_setting.key}", key=_setting.uids[_slot],
            description=f"{_setting.title} — {_setting.description}, "
                        f"map slot {_slot}",
            units=_setting.units, shape=(1, 1), tags=frozenset({TAG_NO_SYMBOL}),
            owner=(
                _OWNER_SLOT_FLAG if _setting.writable
                else f"no write path — {_setting.readonly}"
            ),
        ))


#: The progressive cylinder-cut trio, in escalation order. The invariant every
#: writer must hold: ``soft <= medium <= hard``.
REV_LIMIT_TRIO = ("rev_limit_soft", "rev_limit_medium", "rev_limit_hard")

#: Launch control's limiter behaviour — generically editable, unlike the trio.
LAUNCH_CONTROL_LIMITER = ("lc_limiter_timing", "lc_release_speed")


def slot_names(kind: str) -> tuple[str, ...]:
    """Logical names for one per-slot table ``kind``, slots 1–5 in order.

    ``kind`` is the suffix: ``"put_setpoint"``, ``"enable_sl_tc"``, or
    ``"disable_oem_tc"``.
    """
    names = tuple(f"slot{s}_{kind}" for s in SLOTS)
    unknown = [n for n in names if n not in SWITCH_PATCH_2933]
    if unknown:
        raise KeyError(f"no per-slot tables of kind {kind!r} in this profile")
    return names



def _ungrouped_is_deliberate(specs: list[TableSpec]) -> list[TableSpec]:
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
            f"SwitchPatch2933: generically editable tables need a group: "
            f"{', '.join(orphans)}"
        )
    return specs


_specs = _ungrouped_is_deliberate(_specs)


SWITCH_PATCH_2933 = Profile(
    name="SwitchPatch2933",
    xdf="S50 Switch Patch.29.33.V2.xdf",
    specs={s.name: s for s in _specs},
)

__all__ = [
    "SWITCH_PATCH_2933",
    "LAUNCH_CONTROL_LIMITER",
    "REV_LIMIT_TRIO",
    "SLOTS",
    "SLOT_GRID_SHAPE",
    "SLOT_DEFAULT_HPA",
    "SLOT_AXIS_HEADER_VALUE",
    "SLOT_SETTINGS",
    "SLOT_SETTINGS_BY_KEY",
    "SlotSetting",
    "KIND_FLAG",
    "KIND_NUMBER",
    "KIND_OPAQUE",
    "slot_names",
]
