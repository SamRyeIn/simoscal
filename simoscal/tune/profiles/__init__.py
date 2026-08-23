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

#: The profiles :func:`~simoscal.preflight.preflight` tries when it is handed a
#: bin it cannot yet name — the registry that replaced a hardcoded SC8S50 check.
#:
#: Membership is *derived* from whether a profile declares a
#: :attr:`~simoscal.tune.profile.Profile.structure`, rather than listed by hand,
#: so a profile cannot be added to the package and silently left unregistered.
#: The rule is not a convenience: a structure is the statement "I describe a
#: whole calibration and I know where its CAL block sits". A profile without one
#: only *adds* tables to another profile's space — the switch patch is the
#: example — and could never identify a bin on its own, because resolving it
#: says nothing about the base calibration underneath.
BASE_PROFILES: tuple[Profile, ...] = tuple(
    p for p in PROFILES.values() if p.structure is not None
)

__all__ = ["PROFILES", "BASE_PROFILES", "SC8S50", "SWITCH_PATCH_2933"]
