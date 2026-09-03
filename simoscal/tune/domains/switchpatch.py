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
from typing import Iterable, Mapping, Optional, Sequence, Union

import numpy as np

from ... import btp
from ..journal import KIND_AXIS, KIND_CHECK, VERDICT_SKIPPED, EditEntry
from ..profiles.switchpatch_2933 import (
    KIND_FLAG,
    LAMBDA_DEFAULT_OFFSET,
    SLOT_AXIS_HEADER_VALUE,
    SLOT_DEFAULT_HPA,
    SLOT_GRID_SHAPE,
    SLOT_SETTINGS,
    SLOT_SETTINGS_BY_KEY,
    SLOTS,
    SPARK_DEFAULT_DEGREES,
)
from ..units import AMBIENT_HPA, hpa_from_psi, psi_from_hpa
from ._common import Domain, dry_runnable, require_shape

__all__ = ["PATCH_SPACE", "SwitchPatch"]

#: Conventional name for the table space holding the patch-added tables.
PATCH_SPACE = "patch"

_TC_FLAGS = ("enable_sl_tc", "disable_oem_tc")


class SwitchPatch(Domain):
    """Reached as ``tune.switchpatch``."""

    space = PATCH_SPACE

    # -- per-slot boost caps -------------------------------------------------- #
    @dry_runnable
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

    @dry_runnable
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

    # -- per-slot timing ------------------------------------------------------- #
    @dry_runnable
    def slot_spark_map(
        self,
        slot: int,
        *,
        rpm: Sequence[float],
        rows: Mapping[float, Sequence[float]],
        max_delivered_degrees: float,
        base_map: str = "ignition_base_vvl0_i0_e0",
        require_as_patched: bool = False,
        intent: str = "",
    ) -> EditEntry:
        """Give one slot its own ignition timing, as an offset onto the shared base.

        This is the *only* way to make one slot's timing differ from another's.
        The nine ``IP_IGA_BAS_IVVT_VVL_PORT_L[STND][i][e]`` — Basic ignition
        angle maps are shared by all five slots, so editing them moves every
        slot at once. The patch's per-slot ``Spark modifier`` grid is an
        **additive** offset in °CRK onto whichever of those the ECU is on, laid
        out on their own rpm × airmass axes (see
        ``knowledge/sc8s50-switchpatch-xdf.md`` for the evidence that it is
        additive rather than a replacement or a multiplier).

        The map is given as the cells you actually mean:

        * ``rpm`` — the rpm breakpoints the columns refer to;
        * ``rows`` — ``{airmass mg/stk: one offset per rpm}``.

        Both must name **exact breakpoints** of the grid's own axes. A value
        between breakpoints has no cell to land in, and rounding it to the
        nearest would silently write a different map than the one asked for.
        Every cell not named keeps what it holds, which for an as-patched bin is
        the neutral 0.00°.

        ``max_delivered_degrees`` is required and has no default. The quantity
        worth capping is **delivered** timing — base + modifier — not the offset
        on its own: +4° onto a cell already at +3.38° is a very different engine
        to +4° onto one at −7.50°. There is no safe universal figure for it, so
        the calibration states its own and this refuses anything above it.
        """
        self._require_slot(slot)
        name = f"slot{slot}_spark_modifier"

        current = self._tune.values(name, space=self.space)
        if require_as_patched and not np.allclose(
            current, SPARK_DEFAULT_DEGREES, atol=1e-6
        ):
            raise ValueError(
                f"switchpatch.slot_spark_map: slot {slot} already holds "
                f"{current.min():+.2f}..{current.max():+.2f}°CRK, not the "
                f"as-patched neutral {SPARK_DEFAULT_DEGREES:.2f}° — this bin is "
                "not the untouched base this call assumed"
            )

        rpm_axis = self._axis_or_fail(name, "x", "rpm")
        airmass_axis = self._axis_or_fail(name, "y", "airmass")
        columns = [self._breakpoint_index(rpm_axis, float(v), "rpm", "rpm")
                   for v in rpm]
        cells: dict[tuple[int, int], float] = {}
        for airmass, offsets in rows.items():
            row = self._breakpoint_index(
                airmass_axis, float(airmass), "airmass", "mg/stk"
            )
            offsets = np.asarray(offsets, dtype=np.float64).ravel()
            if offsets.size != len(columns):
                raise ValueError(
                    f"switchpatch.slot_spark_map: the {airmass:g} mg/stk row has "
                    f"{offsets.size} offsets but rpm names {len(columns)} "
                    "breakpoints — one offset per rpm, in the same order"
                )
            for column, offset in zip(columns, offsets):
                cells[(row, column)] = float(offset)

        self._check_representable(cells)
        self._check_delivered_timing(slot, cells, base_map, max_delivered_degrees)
        self._check_top_row_is_flat(slot, cells, current, airmass_axis)

        written = np.array(sorted(cells.values()))
        return self._tune.write_cells(
            name, cells, space=self.space,
            intent=intent or (
                f"give map slot {slot} its own timing: "
                f"{written.min():+.2f} to {written.max():+.2f}°CRK onto the "
                "shared base ignition map"
            ),
            detail=(
                f"{len(cells)} of {current.size} cells, additive °CRK; "
                + "; ".join(
                    f"{airmass:g} mg/stk: "
                    + ", ".join(f"{v:+.2f}" for v in np.asarray(offsets).ravel())
                    for airmass, offsets in rows.items()
                )
                + f"; delivered timing capped at {max_delivered_degrees:+.2f}°CRK"
            ),
        )

    # -- per-slot fuelling ------------------------------------------------- #
    @dry_runnable
    def slot_lambda_map(
        self,
        slot: int,
        *,
        rpm: Sequence[float],
        rows: Mapping[float, Sequence[float]],
        delivered_lambda_range: tuple[float, float],
        base_grid: str = "lambda_basic_hpdi",
        require_as_patched: bool = False,
        intent: str = "",
    ) -> EditEntry:
        """Give one slot its own fuelling, as an offset onto the shared base grid.

        The sibling of :meth:`slot_spark_map`, and it exists for the same
        reason: ``IP_LAMB_BAS_HPDI[1]`` — Basic HPDI lambda setpoint grid is
        shared by all five slots, so editing it moves every slot at once. The
        patch's per-slot ``Lambda modifier`` grid is an **additive** offset onto
        whatever that grid asks for, laid out on its own axes.

        Additivity is established the same way as the spark grid's: the patch
        ships every slot's grid at a decoded 0.00, which a replacement setpoint
        could not be (every slot would command lambda zero) and a multiplier
        could not be (neutral would have to be 1.0). See
        ``LAMBDA_DEFAULT_OFFSET`` in the profile module.

        **The sign is inferred, not measured.** No revision in this lineage has
        ever written one of these grids, so which way a positive offset moves
        the mixture has never been observed on this car. The sibling's
        convention — the offset adds to the quantity named in the title — makes
        a positive offset *leaner*, and that is what this call assumes. Assuming
        is not good enough on fuelling, where the wrong direction at wide-open
        throttle is a lean excursion under boost, so the guard below does not
        rely on the assumption being right:

        ``delivered_lambda_range`` is required and has no default. It bounds
        **delivered** lambda — base grid + offset — on *both* sides, and every
        named cell is checked against both bounds under both sign conventions.
        A write survives only if it is safe whichever way the patch resolves the
        sign, which is the only honest way to write this grid before a log has
        settled the question. The lean bound is the safety one; the rich bound
        is the high-pressure fuel pump, which on this engine already runs to
        100 % effective volume at 3500-4500 rpm, and beyond which enrichment
        stops arriving and the mixture goes lean anyway.

        The map is given as the cells you actually mean, exactly as
        :meth:`slot_spark_map` takes them:

        * ``rpm`` — the rpm breakpoints the columns refer to;
        * ``rows`` — ``{airmass mg/stk: one offset per rpm}``.

        Both must name exact breakpoints; a value between them is refused, not
        snapped. Every cell not named keeps what it holds.
        """
        self._require_slot(slot)
        name = f"slot{slot}_lambda_modifier"

        current = self._tune.values(name, space=self.space)
        if require_as_patched and not np.allclose(
            current, LAMBDA_DEFAULT_OFFSET, atol=1e-6
        ):
            raise ValueError(
                f"switchpatch.slot_lambda_map: slot {slot} already holds "
                f"{current.min():+.4f}..{current.max():+.4f} lambda, not the "
                f"as-patched neutral {LAMBDA_DEFAULT_OFFSET:.2f} — this bin is "
                "not the untouched base this call assumed"
            )

        low, high = (float(v) for v in delivered_lambda_range)
        if not low < high:
            raise ValueError(
                "switchpatch.slot_lambda_map: delivered_lambda_range is "
                f"({low:g}, {high:g}); it must be (rich bound, lean bound) with "
                "the rich bound lower"
            )

        rpm_axis = self._axis_or_fail(name, "x", "rpm")
        airmass_axis = self._axis_or_fail(name, "y", "airmass")
        columns = [
            self._breakpoint_index(rpm_axis, float(v), "rpm", "rpm",
                                   caller="slot_lambda_map")
            for v in rpm
        ]
        cells: dict[tuple[int, int], float] = {}
        for airmass, offsets in rows.items():
            row = self._breakpoint_index(
                airmass_axis, float(airmass), "airmass", "mg/stk",
                caller="slot_lambda_map",
            )
            offsets = np.asarray(offsets, dtype=np.float64).ravel()
            if offsets.size != len(columns):
                raise ValueError(
                    f"switchpatch.slot_lambda_map: the {airmass:g} mg/stk row "
                    f"has {offsets.size} offsets but rpm names {len(columns)} "
                    "breakpoints — one offset per rpm, in the same order"
                )
            for column, offset in zip(columns, offsets):
                cells[(row, column)] = float(offset)

        self._check_representable(
            cells, table=f"slot{slot}_lambda_modifier", caller="slot_lambda_map",
            units="lambda",
            rationale=(
                "a silent round on a lambda offset moves the mixture by an "
                "amount the calibration never declared, and the lean half of "
                "every rounding is the unsafe half"
            ),
        )
        self._check_delivered_lambda(slot, cells, base_grid, low, high)
        self._check_top_row_is_flat(slot, cells, current, airmass_axis,
                                    caller="slot_lambda_map")

        written = np.array(sorted(cells.values()))
        return self._tune.write_cells(
            name, cells, space=self.space,
            intent=intent or (
                f"give map slot {slot} its own fuelling: "
                f"{written.min():+.4f} to {written.max():+.4f} lambda onto the "
                "shared base lambda setpoint grid"
            ),
            detail=(
                f"{len(cells)} of {current.size} cells, additive lambda; "
                + "; ".join(
                    f"{airmass:g} mg/stk: "
                    + ", ".join(f"{v:+.4f}" for v in np.asarray(offsets).ravel())
                    for airmass, offsets in rows.items()
                )
                + f"; delivered lambda bounded to [{low:.4f}, {high:.4f}] under "
                "both sign conventions"
            ),
        )

    def _check_delivered_lambda(
        self,
        slot: int,
        cells: Mapping[tuple[int, int], float],
        base_grid: str,
        low: float,
        high: float,
    ) -> None:
        """Bound base + offset on both sides, under **both** sign conventions.

        The modifier's sign has never been observed on this car (see
        :meth:`slot_lambda_map`), so a cell is only allowed if both
        ``base + offset`` and ``base - offset`` land inside the declared range.
        That is deliberately strict: it means a revision cannot use this grid to
        reach a lambda that would be unsafe if the patch turns out to add the
        offset the other way, which is exactly the mistake that a first use of
        an unproven table is most likely to make.

        As with delivered timing, the base grid is read live rather than assumed,
        so the check stays true when a revision moves base fuelling in the same
        script — which is the normal case here, since holding one slot at its
        prior lambda while the base grid goes richer is what this grid is for.
        """
        try:
            base = self._tune.values(base_grid)
        except Exception as exc:  # noqa: BLE001 - re-raised with the reason
            raise ValueError(
                f"switchpatch.slot_lambda_map: cannot read the base lambda grid "
                f"{base_grid!r}, so delivered lambda cannot be checked. The "
                "modifier is additive; bounding it alone would say nothing "
                f"about what the engine is fuelled with. ({exc})"
            ) from exc

        worst: Optional[tuple[tuple[int, int], float, float, int]] = None
        for cell, offset in cells.items():
            for sign in (+1, -1):
                delivered = float(base[cell]) + sign * offset
                if low <= delivered <= high:
                    continue
                excess = max(low - delivered, delivered - high)
                if worst is None or excess > worst[3]:
                    worst = (cell, float(base[cell]), delivered, excess)
        if worst is not None:
            (row, column), base_value, delivered, _ = worst
            raise ValueError(
                f"switchpatch.slot_lambda_map: slot {slot} would deliver lambda "
                f"{delivered:.4f} at row {row}, column {column} "
                f"({base_value:.4f} base {delivered - base_value:+.4f} modifier), "
                f"outside the declared range [{low:.4f}, {high:.4f}]. Both signs "
                "are checked because the patch's sign has never been observed on "
                "this car: a write is only allowed if it is safe whichever way "
                "the offset is applied."
            )

    # -- traction control ------------------------------------------------------ #
    @dry_runnable
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
        slots = tuple(slots)

        entries: list[EditEntry] = []
        for flag, value in (("enable_sl_tc", enable), ("disable_oem_tc", disable_oem)):
            for slot in slots:
                entries.append(self.set_slot_flag(
                    flag, slots=(slot,), on=value,
                    intent=intent or (
                        f"{'enable' if value else 'disable'} "
                        f"{'the switch patch' if flag == 'enable_sl_tc' else 'the factory'}"
                        f" traction control on slot {slot}"
                    ),
                )[0])
        return tuple(entries)

    # -- the per-slot switchboard --------------------------------------------- #
    def slot_settings(self) -> tuple[dict, ...]:
        """Every per-slot scalar, with its five slot values. Read-only.

        One call for the whole switchboard rather than sixteen reads times five
        slots, because the question a person actually asks is comparative —
        "which slots have launch control on" — and answering it by opening five
        tables in turn is how you end up sure about a slot you never looked at.

        Carries each setting's ``readonly`` reason and ``caution`` verbatim, so
        whatever renders this cannot present a setting as safely toggleable that
        this profile does not consider writable.
        """
        out = []
        for setting in SLOT_SETTINGS:
            out.append({
                "key": setting.key,
                "title": setting.title,
                "description": setting.description,
                "kind": setting.kind,
                "units": setting.units,
                "group": setting.group,
                "caution": setting.caution,
                "readonly": setting.readonly,
                "writable": setting.writable,
                "values": [
                    float(self._tune.values(
                        f"slot{slot}_{setting.key}", space=self.space
                    ).ravel()[0])
                    for slot in SLOTS
                ],
                "slots": list(SLOTS),
            })
        return tuple(out)

    @dry_runnable
    def set_slot_flag(
        self,
        key: str,
        *,
        slots: Iterable[int] = SLOTS,
        on: bool,
        intent: str = "",
    ) -> tuple[EditEntry, ...]:
        """Set one 0/1 per-slot flag on the given slots.

        Three refusals, and each one is a mistake this makes impossible rather
        than a style preference:

        * an unknown ``key`` — the switchboard is a fixed set, and a typo that
          silently wrote nothing would look exactly like a flag that does not
          work;
        * a setting this profile marks read-only, or one that is not a flag at
          all — ``Manual AFU`` is a 0–1 *fraction* stored /128 and a "toggle" of
          it would write 128× what the caller meant;
        * a flag whose stored value is neither 0 nor 1 — that is not the table
          we think it is, and writing a flag over it would destroy whatever it
          really holds.
        """
        setting = SLOT_SETTINGS_BY_KEY.get(key)
        if setting is None:
            known = ", ".join(sorted(SLOT_SETTINGS_BY_KEY))
            raise ValueError(
                f"switchpatch.set_slot_flag: no per-slot setting {key!r}; "
                f"this patch has {known}"
            )
        if not setting.writable:
            raise ValueError(
                f"switchpatch.set_slot_flag: {setting.title!r} is read-only — "
                f"{setting.readonly}"
            )
        if setting.kind != KIND_FLAG:
            raise ValueError(
                f"switchpatch.set_slot_flag: {setting.title!r} is a "
                f"{setting.kind}, not a 0/1 flag — it has no on/off to set"
            )

        value = 1.0 if on else 0.0
        entries = []
        for slot in slots:
            self._require_slot(slot)
            name = f"slot{slot}_{setting.key}"
            current = float(self._tune.values(name, space=self.space).ravel()[0])
            if current not in (0.0, 1.0):
                raise ValueError(
                    f"switchpatch.set_slot_flag: {name} reads {current!r}, "
                    "expected the 0/1 of a flag — refusing to write over "
                    "something that is not a flag"
                )
            entries.append(self._tune.write(
                name, [[value]], space=self.space,
                intent=intent or (
                    f"{'enable' if on else 'disable'} {setting.title} "
                    f"on slot {slot}"
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

        The check runs against **this session's own patch XDF**, not
        :func:`btp.default_switch_patch_xdf`. That default resolves a path inside
        a BinToolz checkout, which exists on a desktop and nowhere on a phone —
        so on Android the gate could only ever raise "switch-patch XDF not
        found" and fail the build (CR-20260815-05). The session XDF is also the
        more honest reference: it is the definition the edits were made through,
        so the gate re-reads the finished file exactly as the editor wrote it.

        Resolved inside ``run`` rather than here, so a session recovered after a
        process kill uses the path it was rehydrated with instead of one
        captured before the kill.
        """
        def run(bin_path: Path) -> tuple[bool, str]:
            result = btp.switch_patch_sanity(
                bin_path,
                xdf_path=self._tune.space(PATCH_SPACE).xdf,
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

        self._tune.post_checks.append(self._post_check(
            "switch-patch sanity",
            run,
            recovery_key="switch_patch_sanity",
            recovery_params={
                "stock_bin": str(Path(stock_bin)) if stock_bin is not None else None,
            },
        ))

    def _post_check(self, name, run, **kwargs):
        from ..project import PostCheck

        return PostCheck(name=name, run=run, **kwargs)

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

    def _axis_or_fail(self, name: str, which: str, label: str) -> np.ndarray:
        axis = self._tune.axis(name, which, space=self.space)
        if axis is None:
            raise ValueError(
                f"switchpatch.slot_spark_map: {name} has no readable {label} "
                f"({which}) axis, so a breakpoint cannot be named"
            )
        return axis

    #: How far a named breakpoint may sit from a real one and still be that
    #: breakpoint. Not zero, because a stored axis is quantised — the airmass
    #: axis is a 16-bit raw divided by 23.5907, so its nominal 1200 mg/stk
    #: breakpoint decodes to 1200.01 and no exact match would ever succeed. Not
    #: loose either: 0.1% admits that rounding while still refusing 4600 rpm
    #: against a 4500 breakpoint by a factor of twenty.
    _BREAKPOINT_RTOL = 1e-3

    @classmethod
    def _breakpoint_index(
        cls, axis: np.ndarray, value: float, label: str, units: str,
        caller: str = "slot_spark_map",
    ) -> int:
        """The index of the named breakpoint, or a refusal naming the real ones.

        Deliberately not a snap-to-nearest. A typo'd 4600 rpm quietly written at
        4500 reads as intentional everywhere downstream — in the report, the
        journal and the diff — so a value that is not a breakpoint is an error,
        not something to round.
        """
        nearest = int(np.argmin(np.abs(axis - value)))
        tolerance = cls._BREAKPOINT_RTOL * max(1.0, abs(value))
        if abs(float(axis[nearest]) - value) > tolerance:
            offered = ", ".join(f"{v:g}" for v in axis)
            raise ValueError(
                f"switchpatch.{caller}: {value:g} {units} is not a "
                f"{label} breakpoint of this grid; it has {offered}"
            )
        return nearest

    def _check_representable(
        self, cells: Mapping[tuple[int, int], float], *,
        table: str = "slot1_spark_modifier",
        caller: str = "slot_spark_map",
        units: str = "\N{DEGREE SIGN}CRK",
        rationale: str = (
            "on ignition advance a silent round-up is the unsafe direction, "
            "and the calibration should say what the ECU will actually hold"
        ),
    ) -> None:
        """Refuse an offset the grid cannot store, rather than rounding it.

        This grid holds 0.375 °CRK per raw step, so most round numbers are not
        on its lattice: 2.00° would encode to 1.875° and 1.00° to **1.125°**.
        The generic editor treats that as a warning, but this is ignition
        advance — half the roundings go the unsafe way, and a calibration that
        says +1.00 while the ECU holds +1.125 is wrong in the direction that
        matters. ``slot_curve`` floors psi for the same reason; here there is a
        better option than flooring, because the caller can simply be told the
        two values the grid can actually hold and pick one.
        """
        step = self._value_step(table)
        if step is None:
            return
        offenders = []
        for cell, offset in sorted(cells.items()):
            steps = offset / step
            if abs(steps - round(steps)) < 1e-6:
                continue
            low = np.floor(steps) * step
            high = np.ceil(steps) * step
            offenders.append(f"{offset:+.3f} (nearest storable {low:+.3f} or {high:+.3f})")
        if offenders:
            raise ValueError(
                f"switchpatch.{caller}: this grid stores {step:g} {units} per "
                f"raw step, and {len(offenders)} requested offset(s) do not land "
                "on it: " + "; ".join(sorted(set(offenders))) + ". Refusing rather "
                f"than rounding — {rationale}."
            )

    def _value_step(self, table: str = "slot1_spark_modifier") -> Optional[float]:
        """Physical units per raw step for one of the slot grids, or None."""
        view = self._tune.table(table, space=self.space).view
        scaling = view.table.z.scaling if view.table.z is not None else None
        if scaling is None or not scaling.is_linear or not scaling.m:
            return None
        return abs(float(scaling.m))

    def _check_delivered_timing(
        self,
        slot: int,
        cells: Mapping[tuple[int, int], float],
        base_map: str,
        ceiling: float,
    ) -> None:
        """KTD3: cap base + modifier, not the modifier on its own.

        Reads the live base map rather than a constant, so the check stays true
        when a revision moves base timing in the same script. If the base space
        is not mapped there is nothing to add to, and the guard cannot run —
        that is a refusal, not a pass, because the whole point is that the
        offset alone says nothing about what the engine will see.
        """
        try:
            base = self._tune.values(base_map)
        except Exception as exc:  # noqa: BLE001 - re-raised with the reason
            raise ValueError(
                f"switchpatch.slot_spark_map: cannot read the base ignition map "
                f"{base_map!r}, so delivered timing cannot be checked. The "
                "modifier is additive; capping it alone would say nothing about "
                f"what the engine sees. ({exc})"
            ) from exc

        worst: Optional[tuple[tuple[int, int], float, float]] = None
        for cell, offset in cells.items():
            delivered = float(base[cell]) + offset
            if delivered > ceiling and (worst is None or delivered > worst[2]):
                worst = (cell, float(base[cell]), delivered)
        if worst is not None:
            (row, column), base_value, delivered = worst
            raise ValueError(
                f"switchpatch.slot_spark_map: slot {slot} would deliver "
                f"{delivered:+.2f}°CRK at row {row}, column {column} "
                f"({base_value:+.2f}° base + {delivered - base_value:+.2f}° "
                f"modifier), above the declared ceiling of {ceiling:+.2f}°CRK. "
                "The offset is additive, so the cell's base value is half the "
                "answer — a modest offset onto an already-advanced cell is not "
                "a modest amount of timing."
            )

    def _check_top_row_is_flat(
        self,
        slot: int,
        cells: Mapping[tuple[int, int], float],
        current: np.ndarray,
        airmass_axis: np.ndarray,
        *,
        caller: str = "slot_spark_map",
    ) -> None:
        """Above the top breakpoint, only a flat map is bounded.

        Measured airmass at WOT reaches ~1600 mg/stk against a top breakpoint of
        1400 (``Logs/BasicsGuide_R19``), so every pull runs off the end of this
        grid. Whether the ECU clamps there or extrapolates along the last slope
        is not established — but it does not have to be, as long as the last two
        rows agree: a flat segment has zero slope, so clamping and extrapolation
        give the same answer. Writing the top row *differently* from the one
        below makes the unresolved question load-bearing, on the advance side,
        at the highest load the engine ever sees.
        """
        top = len(airmass_axis) - 1
        if not any(row == top for row, _ in cells):
            return
        below = top - 1
        result = current.copy()
        for (row, column), value in cells.items():
            result[row, column] = value
        if np.allclose(result[top], result[below], rtol=0, atol=1e-6):
            return
        differing = np.flatnonzero(
            ~np.isclose(result[top], result[below], rtol=0, atol=1e-6)
        )
        raise ValueError(
            f"switchpatch.{caller}: slot {slot}'s top airmass row "
            f"({airmass_axis[top]:g} mg/stk) would differ from the "
            f"{airmass_axis[below]:g} mg/stk row below it at column(s) "
            f"{', '.join(str(int(c)) for c in differing)}. WOT airmass runs past "
            "the top breakpoint, and only a flat last segment is bounded there — "
            "write the two rows identically, or the behaviour above the top "
            "breakpoint depends on whether the ECU clamps or extrapolates, "
            "which is not established."
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
