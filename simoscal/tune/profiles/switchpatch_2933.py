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

from ..profile import TAG_AXIS, TAG_NO_SYMBOL, Profile, TableSpec

#: Slot count the 29.33 patch provides.
SLOTS = (1, 2, 3, 4, 5)

# Per-slot uniqueids, verified on the as-patched bin.
_PUT_GRID_UIDS = {1: "0x7d41a", 2: "0x7d4da", 3: "0x7d59a", 4: "0x7d65a", 5: "0x7d71a"}
_ENABLE_SL_TC_UIDS = {1: "0x7d83f", 2: "0x7d840", 3: "0x7d841", 4: "0x7d842", 5: "0x7d843"}
_DISABLE_OEM_TC_UIDS = {1: "0x7d83a", 2: "0x7d83b", 3: "0x7d83c", 4: "0x7d83d", 5: "0x7d83e"}

#: Geometry of a slot's PUT setpoint grid: 8 uncharacterized Y rows × the 12
#: breakpoints of the shared RPM axis. The lineage tiles one curve across all
#: eight rows, since the Y axis carries no meaning in the patch.
SLOT_GRID_SHAPE = (8, 12)

#: The as-patched, deliberately non-binding default in every slot grid.
SLOT_DEFAULT_HPA = 4000.0

#: The shared axis header is a length marker the patch reads; it must stay 12.
SLOT_AXIS_HEADER_VALUE = 12.0

_specs = [
    TableSpec(
        name="slot_put_rpm_axis", key="0x7d7dc",
        description="PUT SP RPM Axis — engine-speed breakpoints shared by all "
                    "five slot PUT setpoint grids",
        units="rpm", shape=(1, 12), tags=frozenset({TAG_AXIS, TAG_NO_SYMBOL}),
    ),
    TableSpec(
        name="slot_put_rpm_axis_header", key="0x7d7da",
        description="PUT SP RPM Axis Header — breakpoint count, must remain 12",
        units="", shape=(1, 1), tags=frozenset({TAG_NO_SYMBOL}),
    ),
]

for _slot in SLOTS:
    _specs += [
        TableSpec(
            name=f"slot{_slot}_put_setpoint", key=_PUT_GRID_UIDS[_slot],
            description=f"PUT setpoint — boost target grid for map slot {_slot}",
            units="hPa", shape=SLOT_GRID_SHAPE, tags=frozenset({TAG_NO_SYMBOL}),
        ),
        TableSpec(
            name=f"slot{_slot}_enable_sl_tc", key=_ENABLE_SL_TC_UIDS[_slot],
            description=f"Enable SL TC — enable the switch patch's own "
                        f"slip-based traction control on map slot {_slot}",
            units="", shape=(1, 1), tags=frozenset({TAG_NO_SYMBOL}),
        ),
        TableSpec(
            name=f"slot{_slot}_disable_oem_tc", key=_DISABLE_OEM_TC_UIDS[_slot],
            description=f"Disable OEM TC — disable the factory ECU-side "
                        f"traction-control torque intervention on map slot {_slot}",
            units="", shape=(1, 1), tags=frozenset({TAG_NO_SYMBOL}),
        ),
    ]


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


SWITCH_PATCH_2933 = Profile(
    name="SwitchPatch2933",
    xdf="S50 Switch Patch.29.33.V2.xdf",
    specs={s.name: s for s in _specs},
)

__all__ = [
    "SWITCH_PATCH_2933",
    "SLOTS",
    "SLOT_GRID_SHAPE",
    "SLOT_DEFAULT_HPA",
    "SLOT_AXIS_HEADER_VALUE",
    "slot_names",
]
