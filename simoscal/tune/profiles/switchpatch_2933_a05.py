"""A05's address book for BinToolz' 5-slot map switch, patch 29.33.

The second car this patch is mapped for, and the cheap half of the port: the
patch is one BinToolz build cut for several file structures, so *what* the 92
tables are was already settled in :mod:`.switchpatch_2933` and only *where* they
sit had to be established here. This module is that — 92 uniqueids and the
evidence for them — and it imports every description, unit, shape and owner from
the shared module rather than restating any of it.

**How these addresses were established.** By role, table for table, against
``A05 Switch Patch.29.33.V2.xdf`` — never by offsetting S50's. The two
definitions are the same generated file with the addresses moved: 185 tables
each, and index for index they agree exactly on title, A2L symbol and category
path (the ``Switch Patch | Map Switching | Map Slot N`` path is what tells the
five identically-titled ``PUT setpoint`` grids apart). That ordered
correspondence *is* the role mapping, and ``test_f8`` re-derives it from both
XDFs and compares it to the constants below, so a typo here fails a test rather
than reaching a bin.

**Why arithmetic would not have worked.** The A05 offsets are not one delta from
S50's. Across the 92 they fall into three: ``+0x12F60`` for the RAL and LC
scalars, ``+0x13000`` for the map-switching block, ``+0x13020`` for the gauge
bitmasks (67, 20 and 5 tables of the 92). Deriving the book by adding S50's most
common delta would have placed 25 of the 92 in the wrong table while resolving
perfectly, which is the failure this library exists to make impossible.

**What is the same, and what is not.** All 92 tables exist on A05 with identical
shapes, so nothing is declared unavailable and no per-car shape override is
needed. That is *not* true of the patch XDF as a whole — six of its 185 tables
are (16, 18) here against S50's (16, 16), the same grid difference the base
profile carries (see :mod:`.scga05`) — but none of the six is one of ours. If a
later revision maps the flex-fuel ``Spark modifier`` grids, they need per-car
shapes.

.. note::

   Unlike ``SCGa05_cal.xdf``, this file declares ``BASEOFFSET 0x220000`` and its
   addresses are counted from the start of the whole bin — the same convention
   ``SC8S50.V1.0.xdf`` uses, at A05's CAL offset. So the patch space needs no
   rebase, while the base space it merges into does. The two declared offsets
   differ (``0`` and ``0x220000``) and the two *effective* offsets are equal,
   which is why :func:`~simoscal.tune.project._open_shared_space` compares the
   effective ones.
"""

from __future__ import annotations

from .switchpatch_2933 import build_switch_patch_profile

# --------------------------------------------------------------------------- #
# The address book — every value read off A05's own patch XDF
# --------------------------------------------------------------------------- #
#: A05 — the seven tables that are not per-slot, by logical name.
A05_STANDALONE_UIDS = {
    "slot_put_rpm_axis":        "0x907dc",
    "slot_put_rpm_axis_header": "0x907da",
    "rev_limit_soft":           "0x8fa78",
    "rev_limit_medium":         "0x8fa7a",
    "rev_limit_hard":           "0x8fa7c",
    "lc_limiter_timing":        "0x8fa91",
    "lc_release_speed":         "0x8fa9c",
}

#: A05 — the five slot PUT setpoint grids.
A05_PUT_GRID_UIDS = {
    1: "0x9041a", 2: "0x904da", 3: "0x9059a", 4: "0x9065a", 5: "0x9071a",
}

#: A05 — the sixteen per-slot scalars, ``setting key -> {slot: uniqueid}``.
A05_SLOT_SETTING_UIDS = {
    "enable_sl_tc":     {1: "0x9083f", 2: "0x90840", 3: "0x90841", 4: "0x90842", 5: "0x90843"},
    "disable_oem_tc":   {1: "0x9083a", 2: "0x9083b", 3: "0x9083c", 4: "0x9083d", 5: "0x9083e"},
    "enable_lc":        {1: "0x90835", 2: "0x90836", 3: "0x90837", 4: "0x90838", 5: "0x90839"},
    "enable_nls":       {1: "0x90830", 2: "0x90831", 3: "0x90832", 4: "0x90833", 5: "0x90834"},
    "enable_ral":       {1: "0x9081c", 2: "0x9081d", 3: "0x9081e", 4: "0x9081f", 5: "0x90820"},
    "pops_enable":      {1: "0x8fab4", 2: "0x8fab5", 3: "0x8fab6", 4: "0x8fab7", 5: "0x8fab8"},
    "enable_ff_spark":  {1: "0x907f4", 2: "0x907fa", 3: "0x90800", 4: "0x90806", 5: "0x9080c"},
    "enable_ff_put":    {1: "0x907f5", 2: "0x907fb", 3: "0x90801", 4: "0x90807", 5: "0x9080d"},
    "enable_ff_lambda": {1: "0x907f6", 2: "0x907fc", 3: "0x90802", 4: "0x90808", 5: "0x9080e"},
    "enable_ff_tq":     {1: "0x907f7", 2: "0x907fd", 3: "0x90803", 4: "0x90809", 5: "0x9080f"},
    "enable_ff_iat":    {1: "0x907f8", 2: "0x907fe", 3: "0x90804", 4: "0x9080a", 5: "0x90810"},
    "enable_ff_mpi":    {1: "0x907f9", 2: "0x907ff", 3: "0x90805", 4: "0x9080b", 5: "0x90811"},
    "rpm_limiter":      {1: "0x8faa0", 2: "0x8faa2", 3: "0x8faa4", 4: "0x8faa6", 5: "0x8faa8"},
    "speed_limiter":    {1: "0x8faaa", 2: "0x8faac", 3: "0x8faae", 4: "0x8fab0", 5: "0x8fab2"},
    "manual_afu":       {1: "0x9085a", 2: "0x9085b", 3: "0x9085c", 4: "0x9085d", 5: "0x9085e"},
    "gauge_settings":   {1: "0x924b0", 2: "0x924b1", 3: "0x924b2", 4: "0x924b3", 5: "0x924b4"},
}


SWITCH_PATCH_2933_A05 = build_switch_patch_profile(
    name="SwitchPatch2933_A05",
    xdf="A05 Switch Patch.29.33.V2.xdf",
    standalone_uids=A05_STANDALONE_UIDS,
    put_grid_uids=A05_PUT_GRID_UIDS,
    slot_setting_uids=A05_SLOT_SETTING_UIDS,
)

__all__ = [
    "SWITCH_PATCH_2933_A05",
    "A05_PUT_GRID_UIDS",
    "A05_SLOT_SETTING_UIDS",
    "A05_STANDALONE_UIDS",
]
