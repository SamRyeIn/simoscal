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
"""

from __future__ import annotations

from typing import Sequence

import numpy as np

from ..profile import TAG_KG_PER_STROKE
from ..journal import EditEntry
from ._common import Domain, float_bug_write, guarded_ceiling

__all__ = ["Limits", "MG_PER_KG"]

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

    # -- generic escapes, still journaled ------------------------------------ #
    def raise_ceiling(self, name: str, target: float, *, intent: str = "") -> EditEntry:
        """Raise any mapped limiter to ``target``, never lowering a higher cell."""
        return guarded_ceiling(self._tune, name, target, intent=intent)

    def float_bug_value(self, name: str, value: float, *, intent: str = "") -> EditEntry:
        """Write a float-bug-tagged table past its display maximum, deliberately."""
        return float_bug_write(self._tune, name, value, intent=intent)
