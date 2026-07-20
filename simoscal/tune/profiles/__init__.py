"""Shipped profile map files — one module per XDF.

Adding support for another Simos 18 XDF is writing one module here: a dict of
logical name → :class:`~simoscal.tune.profile.TableSpec`, wrapped in a
:class:`~simoscal.tune.profile.Profile`. Nothing else in the package needs to
change, because every table reference in every domain module goes through a
logical name.

See ``Code/README.md`` § Tune API for the how-to.
"""

from __future__ import annotations

from ..profile import Profile
from .sc8s50 import SC8S50
from .switchpatch_2933 import SWITCH_PATCH_2933

#: Every shipped profile, by name — for lookup and for the docs to enumerate.
PROFILES: dict[str, Profile] = {
    SC8S50.name: SC8S50,
    SWITCH_PATCH_2933.name: SWITCH_PATCH_2933,
}

__all__ = ["PROFILES", "SC8S50", "SWITCH_PATCH_2933"]
