"""Limiters — the ceilings that quietly cap a tune that is otherwise correct.

Most of this domain is the basics guide's "move it out of the way" list: raise
a monitoring ceiling so it stops binding before the calibration does. They are
individually boring and collectively the reason a boost curve does not deliver.

One is not boring. ``C_M_AIR_CYL_SP_MAX`` — Maximum allowed M_AIR_CYL_SP is
labelled mg/stk by every XDF in circulation and **stores kg/stk**. Writing the
2000 the guide prints does not set a 2000 mg/stk ceiling; it sets 2,000,000
mg/stk, about 1.44 million times stock, which is the limiter removed. No guard
catches it, because 2000 is a perfectly ordinary number for that field.

So this module does not offer a way to write that table's raw value. It offers
:meth:`Limits.airmass_cap_mg`, which takes mg/stk and does the conversion — the
mistake is not guarded against, it is unavailable.

Two limiters here are not single tables at all, and their methods exist so that
fact cannot be edited away one scalar at a time:

* :meth:`Limits.speed_limiter` — four tables holding one number. The ECU selects
  among them, so a partial write leaves the car limited by an un-written level,
  which reads as the edit having silently failed.
* :meth:`Limits.rev_limits` — the switch patch's progressive cylinder-cut trio,
  which must escalate (``soft <= medium <= hard``) to mean anything at all.

Both refuse the incoherent state outright and write nothing when they do.
"""

from __future__ import annotations

from typing import Optional, Sequence

import numpy as np

from ..profile import TAG_KG_PER_STROKE
from ..journal import EditEntry
from ..profiles.sc8s50 import SPEED_LIMITER
from ..profiles.switchpatch_2933 import REV_LIMIT_TRIO
from ._common import Domain, float_bug_write, guarded_ceiling

__all__ = ["Limits", "MG_PER_KG", "REV_LIMIT_TRIO", "SPEED_LIMITER"]

#: Milligrams per kilogram — the scale hiding inside the airmass cap's label.
MG_PER_KG = 1_000_000.0


class Limits(Domain):
    """Reached as ``tune.limits``."""

    def airmass_cap_mg(self, mg_per_stroke: float, *, intent: str = "") -> EditEntry:
        """Set the maximum allowed airmass setpoint, in **mg/stk**.

        The value is converted to the kg/stk the ECU actually stores, so 2000
        mg/stk writes 0.002 — the number the tuning guide tells you to type
        when the display "looks wrong". The display is not wrong; the XDF label
        is.

        Refuses a value large enough to be a raw kg/stk figure typed by
        mistake, since nothing downstream would catch it.
        """
        resolved = self._tune.table("airmass_setpoint_max")
        if not resolved.has(TAG_KG_PER_STROKE):
            raise ValueError(
                f"{resolved.label} is not marked as a kg/stk store in the "
                f"{self._tune.space('base').profile.name} profile — this "
                "method would convert a value that needs no conversion"
            )
        if mg_per_stroke <= 0:
            raise ValueError(
                f"limits.airmass_cap_mg: {mg_per_stroke!r} mg/stk is not a "
                "positive airmass"
            )
        if mg_per_stroke < 1.0:
            raise ValueError(
                f"limits.airmass_cap_mg: {mg_per_stroke:g} looks like a raw "
                "kg/stk value, not mg/stk. This method takes mg/stk (e.g. "
                "2000) and converts internally — pass the mg/stk figure."
            )
        kg_per_stroke = mg_per_stroke / MG_PER_KG
        return self._tune.write(
            "airmass_setpoint_max", [[kg_per_stroke]],
            intent=intent or (
                f"raise the airmass setpoint ceiling to {mg_per_stroke:g} mg/stk"
            ),
            detail=(
                f"{mg_per_stroke:g} mg/stk stored as {kg_per_stroke:g} kg/stk. "
                "The XDF labels this table identity-scaled mg/stk, but the ECU "
                "stores kg/stk: writing the mg/stk figure directly would raise "
                "the ceiling a millionfold and remove the limiter."
            ),
        )

    def intake_air_max(
        self, mg_per_stroke: float, *, intent: str = "",
        tables: Sequence[str] = ("intake_air_max_vvl0", "intake_air_max_vvl1"),
    ) -> tuple[EditEntry, ...]:
        """Flatten the maximum-intake-air tables to ``mg_per_stroke``.

        Genuinely mg/stk, unlike the airmass setpoint cap above — these take
        the guide's 2000 as written. Both valve-lift variants are set together,
        since leaving one behind caps the engine on whichever lift it uses.
        """
        entries = []
        for name in tables:
            values = self._values(name)
            entries.append(self._tune.write(
                name, np.full(values.shape, float(mg_per_stroke)),
                intent=intent or (
                    f"raise the maximum intake air to {mg_per_stroke:g} mg/stk "
                    "so it stops binding"
                ),
                detail="genuine mg/stk store — the physical value is written as given",
            ))
        return tuple(entries)

    def torque_reference_max(self, nm: float, *, intent: str = "") -> EditEntry:
        """Flatten the maximum reference indicated engine torque, in Nm."""
        values = self._values("torque_reference_max")
        return self._tune.write(
            "torque_reference_max", np.full(values.shape, float(nm)),
            intent=intent or (
                f"move the reference torque ceiling to {nm:g} Nm so the torque "
                "monitor stops binding"
            ),
        )

    # -- the coherent multi-table limiters ------------------------------------ #
    def rev_limits(
        self,
        *,
        soft: Optional[float] = None,
        medium: Optional[float] = None,
        hard: Optional[float] = None,
        space: str = "patch",
        intent: str = "",
    ) -> tuple[EditEntry, ...]:
        """Set the switch patch's progressive cylinder-cut trio, in rpm.

        These are rpm **offsets above the patch's engagement point**, not
        absolute rev limits — see the spec docstrings in
        :mod:`~simoscal.tune.profiles.switchpatch_2933`, which is also where the
        limits of what is established about them are written down.

        The trio escalates: soft cuts one cylinder every four, medium one every
        three, hard two every four. So ``soft <= medium <= hard`` is not a
        preference, it is what "progressive" means, and a trio ordered any other
        way asks the ECU to escalate backwards. That is checked against the
        *resulting* trio, so passing one value re-validates it against the live
        values of the other two.

        Nothing is written unless every value passes: the check runs over the
        whole trio first, so a rejected member cannot leave one scalar moved and
        the other two behind (the atomicity the screens rely on).
        """
        requested = {"soft": soft, "medium": medium, "hard": hard}
        if all(v is None for v in requested.values()):
            raise ValueError(
                "limits.rev_limits: give at least one of soft=, medium=, hard="
            )

        # Resolve the whole trio — given values where given, live values where
        # not — so the ordering check sees what the ECU would actually hold.
        resulting: dict[str, float] = {}
        for label, name in zip(requested, REV_LIMIT_TRIO):
            given = requested[label]
            if given is None:
                resulting[label] = float(
                    self._values(name, space=space).ravel()[0]
                )
            else:
                value = float(given)
                if not np.isfinite(value):
                    raise ValueError(
                        f"limits.rev_limits: {label} must be a finite rpm offset, "
                        f"got {given!r}"
                    )
                self._require_within_declared(name, value, f"rev_limits({label})",
                                              space=space)
                resulting[label] = value

        order = [resulting["soft"], resulting["medium"], resulting["hard"]]
        if not (order[0] <= order[1] <= order[2]):
            raise ValueError(
                "limits.rev_limits: the cut trio must escalate — soft <= medium "
                f"<= hard, but this would leave soft={order[0]:g}, "
                f"medium={order[1]:g}, hard={order[2]:g} rpm. Soft cuts one "
                "cylinder every four, medium one every three, hard two every "
                "four; ordering them any other way asks the ECU to escalate "
                "backwards. Nothing was written."
            )

        entries = []
        for label, name in zip(requested, REV_LIMIT_TRIO):
            if requested[label] is None:
                continue
            entries.append(self._tune.write(
                name, [[resulting[label]]], space=space,
                intent=intent or (
                    f"set the {label} cylinder-cut offset to "
                    f"{resulting[label]:g} rpm above the engagement point"
                ),
                detail=(
                    f"trio after this write: soft={order[0]:g}, "
                    f"medium={order[1]:g}, hard={order[2]:g} rpm above the "
                    "engagement point"
                ),
            ))
        return tuple(entries)

    def speed_limiter(
        self, kmh: float, *, tables: Sequence[str] = SPEED_LIMITER,
        intent: str = "",
    ) -> tuple[EditEntry, ...]:
        """Set the road-speed limiter, in km/h — all four scalars together.

        The limiter is four tables holding one number: three levels and a
        not-active value, all stock 200 km/h. The ECU selects among them, so
        writing one alone leaves the car limited by whichever of the others it
        picked — an edit that looks like it silently failed. This writes the
        whole quartet or none of it, which is why the four specs name it as
        their owner.

        Unlike a ceiling, this moves in both directions: putting the factory
        200 km/h back is as legitimate as raising it.
        """
        value = float(kmh)
        if not np.isfinite(value) or value <= 0:
            raise ValueError(
                f"limits.speed_limiter: {kmh!r} is not a positive road speed"
            )
        names = tuple(tables)
        for name in names:
            self._require_within_declared(name, value, "speed_limiter")

        entries = []
        for name in names:
            entries.append(self._tune.write(
                name, [[value]],
                intent=intent or f"set the road-speed limiter to {value:g} km/h",
                detail=(
                    f"one of {len(names)} scalars holding the limiter speed, "
                    "all written together — the ECU selects among them, so a "
                    "partial write leaves the car limited by an un-written level"
                ),
            ))
        return tuple(entries)

    def _require_within_declared(
        self, name: str, value: float, what: str, *, space: str = "base"
    ) -> None:
        """Refuse a value outside the table's own declared range.

        A declared max is trustworthy *here* in a way it is not for the float-bug
        tables: these are integer stores whose XDF range is the encodable range
        of the field, so a value past it does not overflow a display convention,
        it overflows the field.
        """
        resolved = self._tune.table(name, space=space)
        z = resolved.view.table.z
        low = getattr(z, "min", None) if z is not None else None
        high = getattr(z, "max", None) if z is not None else None
        tol = 1e-6 * (abs(value) + 1.0)
        if (low is not None and value < low - tol) or (
            high is not None and value > high + tol
        ):
            raise ValueError(
                f"limits.{what}: {value:g} is outside {resolved.label}'s "
                f"declared range {low:g}–{high:g} {resolved.units}. That range "
                "is the encodable range of the stored field, not a display "
                "convention — refusing. Nothing was written."
            )

    # -- generic escapes, still journaled ------------------------------------ #
    def raise_ceiling(self, name: str, target: float, *, intent: str = "") -> EditEntry:
        """Raise any mapped limiter to ``target``, never lowering a higher cell."""
        return guarded_ceiling(self._tune, name, target, intent=intent)

    def float_bug_value(self, name: str, value: float, *, intent: str = "") -> EditEntry:
        """Write a float-bug-tagged table past its display maximum, deliberately."""
        return float_bug_write(self._tune, name, value, intent=intent)
