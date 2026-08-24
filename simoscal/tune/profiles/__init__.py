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
from .scga05 import SCGA05
from .switchpatch_2933 import SWITCH_PATCH_2933
from .switchpatch_2933_a05 import SWITCH_PATCH_2933_A05

#: Every shipped profile, by name — for lookup and for the docs to enumerate.
PROFILES: dict[str, Profile] = {
    SC8S50.name: SC8S50,
    SCGA05.name: SCGA05,
    SWITCH_PATCH_2933.name: SWITCH_PATCH_2933,
    SWITCH_PATCH_2933_A05.name: SWITCH_PATCH_2933_A05,
}

#: Which switch-patch map belongs to which car, keyed by base profile name.
#:
#: A patch profile cannot be chosen by resolution the way a base profile is. Its
#: tables are patch-added and bound by uniqueid, so *every* patch map resolves
#: against *any* patch XDF that happens to contain those addresses — pointing
#: S50's map at A05's file would not miss, it would read 92 wrong tables. The
#: base calibration is what identifies the car, so the patch map follows the
#: base profile that preflight already matched, and a car with no entry here
#: gets a refusal that says so rather than a wrong answer.
PATCH_PROFILES: dict[str, Profile] = {
    SC8S50.name: SWITCH_PATCH_2933,
    SCGA05.name: SWITCH_PATCH_2933_A05,
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


def patch_profile_for(base: Profile) -> Profile:
    """The 29.33 switch-patch map for ``base``'s car.

    Raises :class:`KeyError` when the car has no patch map. That is the correct
    outcome and not a gap to paper over with a default: substituting another
    car's map would resolve cleanly and address the wrong bytes.
    """
    try:
        return PATCH_PROFILES[base.name]
    except KeyError:
        raise KeyError(
            f"no switch-patch map is registered for {base.name}. The patch "
            "tables are bound by address, so another car's map cannot stand in "
            "for it — add one in simoscal/tune/profiles/."
        ) from None


__all__ = [
    "PROFILES",
    "BASE_PROFILES",
    "PATCH_PROFILES",
    "SC8S50",
    "SCGA05",
    "SWITCH_PATCH_2933",
    "SWITCH_PATCH_2933_A05",
    "patch_profile_for",
]
