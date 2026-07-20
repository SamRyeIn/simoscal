"""The BinToolz 5-slot map switch: per-slot boost caps and traction control.

The switch patch adds five selectable map slots, each with its own ``PUT
setpoint`` grid, plus its own traction-control implementation. R09 established
the semantics that make the whole arrangement work: the effective boost target
is the **minimum** of the base ``IP_PUT_SP`` — Pressure up throttle setpoint
and the selected slot's grid. So the base setpoint can be parked high and
non-binding while each slot's grid is the thing that actually caps boost.

That makes one invariant load-bearing: **every slot's curve must sit below the
base ceiling**, or that slot is capped by the base table instead of its own and
the slot switch stops meaning anything. :meth:`SwitchPatch.slot_curve` checks
it against the live base table rather than a constant, so it stays true even
when the base ceiling is changed in the same revision.

Two more things the patch tables need care with, both encoded here:

* The grids are 8 × 12 with an **uncharacterized Y axis**. The lineage tiles
  one rpm curve across all eight rows, since leaving seven rows at the patch's
  4000 hPa default would make the cap depend on an axis nobody calibrated.
* The tables are patch-added, so they have **no A2L symbol** and all five share
  the title ``PUT setpoint``. They are addressed by uniqueid, which is why they
  live in their own profile and their own table space.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional, Sequence, Union

import numpy as np

from ... import btp
from ..journal import KIND_AXIS, KIND_CHECK, VERDICT_APPLIED, VERDICT_SKIPPED, EditEntry
from ..profiles.switchpatch_2933 import (
    SLOT_AXIS_HEADER_VALUE,
    SLOT_DEFAULT_HPA,
    SLOT_GRID_SHAPE,
    SLOTS,
)
from ..units import AMBIENT_HPA, hpa_from_psi, psi_from_hpa
from ._common import Domain, require_shape

__all__ = ["PATCH_SPACE", "SwitchPatch"]

#: Conventional name for the table space holding the patch-added tables.
PATCH_SPACE = "patch"

_TC_FLAGS = ("enable_sl_tc", "disable_oem_tc")


class SwitchPatch(Domain):
    """Reached as ``tune.switchpatch``."""

    space = PATCH_SPACE

    # -- per-slot boost caps -------------------------------------------------- #
    def slot_curve(
        self,
        slot: int,
        *,
        psi: Optional[Union[float, Sequence[float]]] = None,
        hpa: Optional[Union[float, Sequence[float]]] = None,
        rounding: str = "floor",
        require_as_patched: bool = False,
        intent: str = "",
    ) -> EditEntry:
        """Set map slot ``slot``'s boost cap, tiled across all eight rows.

        Give exactly one of ``psi`` (gauge) or ``hpa`` (absolute), as either a
        single number for a flat cap or one value per breakpoint of the shared
        rpm axis. A psi figure is floored, never rounded up, so a cap asked for
        as "10 psi" cannot encode above 10 psi.

        Set ``require_as_patched`` when building from a freshly patched bin, to
        assert the slot still holds the patch's non-binding default before it
        is overwritten — the R11 check, which catches a revision that silently
        started from the wrong base.
        """
        self._require_slot(slot)
        name = f"slot{slot}_put_setpoint"
        cols = SLOT_GRID_SHAPE[1]
        curve = self._curve_hpa(psi, hpa, cols, rounding)

        current = self._tune.values(name, space=self.space)
        if require_as_patched and not np.allclose(
            current, SLOT_DEFAULT_HPA, atol=1.0
        ):
            raise ValueError(
                f"switchpatch.slot_curve: slot {slot} reads "
                f"{current.min():.1f}–{current.max():.1f} hPa, not the "
                f"as-patched {SLOT_DEFAULT_HPA:g} hPa default — this bin is "
                "not a freshly patched base"
            )
        self._check_below_base_ceiling(slot, curve)

        return self._tune.write(
            name, np.tile(curve, (SLOT_GRID_SHAPE[0], 1)), space=self.space,
            intent=intent or (
                f"cap map slot {slot} at "
                + (f"{psi_from_hpa(curve[0]):.2f} psi gauge"
                   if np.allclose(curve, curve[0]) else "a per-rpm boost curve")
            ),
            detail=(
                "tiled across all 8 uncharacterized Y rows: "
                + ", ".join(f"{v:.0f}" for v in curve)
                + " hPa absolute ("
                + ", ".join(f"{psi_from_hpa(v):.1f}" for v in curve)
                + " psi gauge)"
            ),
        )

    def slot_rpm_axis(
        self, breakpoints: Sequence[float], *, intent: str = ""
    ) -> EditEntry:
        """Re-breakpoint the rpm axis shared by all five slot grids.

        One axis for all five slots, so this reinterprets every slot's curve at
        once. The patch's separate axis-length header must stay at 12; it is
        checked, never written.
        """
        current = self._tune.values("slot_put_rpm_axis", space=self.space)
        new = require_shape(
            np.asarray(breakpoints, dtype=np.float64).reshape(current.shape),
            current.shape, "switchpatch.slot_rpm_axis",
        )
        if not np.all(np.diff(new.ravel()) > 0):
            raise ValueError(
                "switchpatch.slot_rpm_axis: breakpoints must strictly "
                f"increase, got {[float(v) for v in new.ravel()]}"
            )
        self._check_axis_header()
        return self._tune.write(
            "slot_put_rpm_axis", new, space=self.space, kind=KIND_AXIS,
            intent=intent or "re-breakpoint the shared slot rpm axis",
            detail="shared by all five slot PUT setpoint grids",
        )

    # -- traction control ------------------------------------------------------ #
    def traction_control(
        self,
        *,
        slots: Iterable[int] = SLOTS,
        enable: bool = True,
        disable_oem: Optional[bool] = None,
        intent: str = "",
    ) -> tuple[EditEntry, ...]:
        """Turn the patch's own traction control on or off, per slot.

        ``enable`` drives ``Enable SL TC``; ``disable_oem`` drives ``Disable
        OEM TC`` and follows ``enable`` unless given. The pairing matters: the
        patch's TC intervenes through ignition retard and the wastegate while
        the factory system intervenes through torque request, and leaving both
        active has them fighting each other.

        Leaving a slot out is a legitimate choice — a "safe" map with factory
        TC intact — so slots are explicit rather than implied.
        """
        if disable_oem is None:
            disable_oem = enable
        wanted = {"enable_sl_tc": 1.0 if enable else 0.0,
                  "disable_oem_tc": 1.0 if disable_oem else 0.0}

        entries = []
        for slot in slots:
            self._require_slot(slot)
            for flag, value in wanted.items():
                name = f"slot{slot}_{flag}"
                current = float(
                    self._tune.values(name, space=self.space).ravel()[0]
                )
                if current not in (0.0, 1.0):
                    raise ValueError(
                        f"switchpatch.traction_control: {name} reads "
                        f"{current!r}, expected the 0/1 of a flag — refusing "
                        "to write over something that is not a flag"
                    )
                entries.append(self._tune.write(
                    name, [[value]], space=self.space,
                    intent=intent or (
                        f"{'enable' if value else 'disable'} "
                        f"{'the switch patch' if flag == 'enable_sl_tc' else 'the factory'}"
                        f" traction control on slot {slot}"
                    ),
                ))
        return tuple(entries)

    # -- gates ----------------------------------------------------------------- #
    def require_sanity(
        self, *, stock_bin: Optional[Union[str, Path]] = None
    ) -> None:
        """Register a build gate: the patch must still load and decode.

        Runs on the finished file, because that is the only thing that answers
        it. With ``stock_bin``, it additionally checks the result actually
        differs from stock — a patch that "applied" but changed nothing would
        otherwise pass every table-level check.
        """
        def run(bin_path: Path) -> tuple[bool, str]:
            result = btp.switch_patch_sanity(
                bin_path,
                stock_bin_path=Path(stock_bin) if stock_bin else None,
            )
            detail = (
                f"{result.tables_resolved} slot/switch table(s) resolved, "
                f"{result.tables_decoded} decoded, "
                f"{len(result.decode_errors)} decode error(s)"
            )
            if result.differ_from_stock is not None:
                detail += f", {result.differ_from_stock} table(s) differ from stock"
            return result.plausible, detail

        self._tune.post_checks.append(
            self._post_check("switch-patch sanity", run)
        )

    def _post_check(self, name, run):
        from ..project import PostCheck

        return PostCheck(name=name, run=run)

    # -- helpers ---------------------------------------------------------------- #
    def _require_slot(self, slot: int) -> None:
        if slot not in SLOTS:
            raise ValueError(
                f"switchpatch: slot {slot!r} does not exist; the 29.33 patch "
                f"provides slots {', '.join(str(s) for s in SLOTS)}"
            )

    def _curve_hpa(self, psi, hpa, cols: int, rounding: str) -> np.ndarray:
        if (psi is None) == (hpa is None):
            raise ValueError(
                "switchpatch.slot_curve: give exactly one of psi= (gauge) or "
                "hpa= (absolute)"
            )
        if psi is not None:
            values = np.atleast_1d(np.asarray(psi, dtype=np.float64))
            curve = np.array(
                [hpa_from_psi(float(v), rounding=rounding) for v in values]
            )
        else:
            curve = np.atleast_1d(np.asarray(hpa, dtype=np.float64))
        if curve.size == 1:
            curve = np.full(cols, float(curve[0]))
        curve = require_shape(curve, (cols,), "switchpatch.slot_curve")
        if np.any(curve <= AMBIENT_HPA):
            raise ValueError(
                f"switchpatch.slot_curve: every value must be above ambient "
                f"({AMBIENT_HPA:g} hPa absolute = 0 psi gauge); got a minimum "
                f"of {curve.min():.1f} hPa"
            )
        return curve

    def _check_below_base_ceiling(self, slot: int, curve: np.ndarray) -> None:
        """A slot above the base ceiling is capped by the base, not by itself."""
        try:
            base_put = self._tune.values("put_setpoint")
        except Exception:  # noqa: BLE001 - no base space mapped; nothing to check
            return
        ceiling = float(np.max(base_put[-1]))
        if np.any(curve >= ceiling):
            raise ValueError(
                f"switchpatch.slot_curve: slot {slot}'s cap reaches "
                f"{curve.max():.0f} hPa, at or above the base "
                f"`IP_PUT_SP` — Pressure up throttle setpoint full-load "
                f"ceiling of {ceiling:.0f} hPa. Under the min() semantics the "
                "base table would cap this slot instead of its own grid, so "
                "the slot switch would stop meaning anything."
            )

    def _check_axis_header(self) -> EditEntry:
        header = float(
            self._tune.values("slot_put_rpm_axis_header", space=self.space).ravel()[0]
        )
        if not np.isclose(header, SLOT_AXIS_HEADER_VALUE):
            raise ValueError(
                f"switchpatch: the slot rpm axis header reads {header:g}, not "
                f"{SLOT_AXIS_HEADER_VALUE:g} — the patch reads this as the "
                "breakpoint count, so the axis is not the shape this expects"
            )
        return self._tune.note(
            "slot_put_rpm_axis_header", space=self.space, kind=KIND_CHECK,
            verdict=VERDICT_SKIPPED,
            intent="verify the shared axis length header",
            detail=f"reads {header:.0f}, as the patch requires; not written",
        )
