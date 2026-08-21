"""Plain-English names for the quantities a table's axes measure.

An XDF axis carries breakpoints and a unit string and nothing else. That is
enough to *decode* it and nowhere near enough to *label* it: "rpm" down the top
of a grid does not say whether the row is engine speed or turbocharger speed,
and a bare "-" says nothing at all. The name of the quantity lives only in the
axis's own A2L symbol — ``ldp_n_ip_cha_max``, ``ldp_tia_cha_up_ip_pq_cha_max`` —
which is precise but is not English.

So this module is a curated symbol → English map, in the same spirit as the
profile: every entry was read off the decoded breakpoints and checked against
the knowledge base before it was written down, and an axis that is not in the
map falls back to its symbol rather than to a guess. A wrong axis label is a
tuning mistake waiting to happen — someone edits the 4000-rpm column believing
it is the 4000-kg/h column — so "I don't know, here is the symbol" is the right
answer when the map has no entry.

To add an axis: decode its breakpoints, confirm the range matches the quantity
you think it is, and add the symbol here with a comment naming the evidence.
"""

from __future__ import annotations

from typing import Optional

__all__ = [
    "AXIS_QUANTITIES",
    "DIMENSIONLESS",
    "axis_label",
    "table_signature",
    "units_label",
]

#: How a dimensionless quantity is spelled out. The XDF writes ``-``, which
#: reads as a missing value rather than as "this is a ratio".
DIMENSIONLESS = "dimensionless"

#: Axis A2L symbol → the quantity it measures, in English.
#:
#: Verified against the stock ``5G0906259L__0002`` bin: each entry's decoded
#: breakpoints were printed and checked to be the stated quantity in the stated
#: unit before being added.
AXIS_QUANTITIES: dict[str, str] = {
    # -- engine speed ------------------------------------------------------- #
    # Every one of these decodes to a monotonic rpm ladder over the engine's
    # operating range (400–7000).
    "ldp_n_ip_cha_max": "Engine speed",
    "ldp_n_ip_put_sp": "Engine speed",
    "ldp_n_ip_lamb_tur_ohp_min": "Engine speed",
    "ldp_n_32_ip_lamb_cop_min": "Engine speed",
    "ldp_n_32_pv_av_fl": "Engine speed",
    "ldp_n_32_mon_ip_tqi_max_mon": "Engine speed",
    "ldpm_n_1_insy": "Engine speed",
    "ldpm_n_32_1_lasp": "Engine speed",
    "ldpm_n_ip_iga_bas_igsp": "Engine speed",
    "ldpm_n_32_5_igsp": "Engine speed",
    # 1504–4992 rpm, the CAP_H overboost-diagnosis y axis.
    "ldpm_n_ip_put_cap_diag": "Engine speed",
    # 1000–5520, declared in the XDF as `U/min` — the German spelling of rpm,
    # not a different quantity. Decoded and checked before being written here.
    "DATA_ThmMng.CoTE_nEng_A_VW": "Engine speed",

    # -- airmass / flow ----------------------------------------------------- #
    # mg/stk ladders (70–1400): airmass per stroke, the load axis the ignition
    # and lambda grids are indexed on.
    "ldpm_maf_1_lasp": "Airmass per stroke",
    "ldpm_maf_ip_iga_bas_igsp": "Airmass per stroke",
    # kg/h through the compressor (100–1500) — the overboost diagnosis axis.
    "ldp_maf_kgh_tcha_put_amp_dif": "Turbocharger air mass flow",

    # -- pressure / temperature --------------------------------------------- #
    # hPa absolute, 590–2500: the requested manifold pressure IP_PUT_SP is
    # scheduled against.
    "ldp_map_sp_ip_put_sp": "Manifold pressure setpoint",
    # hPa absolute, 1000–2400: the *requested* PUT the CAP_H diagnosis cap is
    # scheduled against. Distinct from the axis above — same unit, and both are
    # a requested pressure, but this one indexes the diagnosis rather than the
    # setpoint grid.
    "ldpm_put_sp_ip_put_cap_diag": "Pressure up throttle setpoint",
    # °C, -20.25 to 50.25. `tia_cha_up` = intake air temperature upstream of the
    # charger, i.e. what the compressor inlet sees.
    "ldp_tia_cha_up_ip_pq_cha_max": "Compressor-inlet air temperature",
    # °C, -30 to 80.25. `tia` with no station qualifier: the sensed intake air
    # temperature the IGA temperature corrections are selected on. Distinct from
    # the compressor-inlet axis above — same unit, different station.
    "ldpm_tia_iga_cor_sel": "Intake air temperature",

    # -- wastegate feedforward ---------------------------------------------- #
    # The two flow factors IP_FAC_BPA_SP[0]/[1] are indexed on; both 0–1.5.
    # x = exhaust, y = intake (knowledge/ecu-tuning-basics.md § Wastegate).
    "ldp_fac_1_ip_fac_bpa_sp": "Exhaust flow factor",
    "ldp_fac_2_ip_fac_bpa_sp": "Intake flow factor",

    # -- charge / load ------------------------------------------------------- #
    # 10.0–60.0 %, the relative cylinder charge (Füllung) the cylinder-head
    # temperature setpoint is scheduled against. A percentage of the cylinder's
    # capacity, not a percentage of anything the driver commands.
    "DATA_ThmMng.CoTE_rChRel_A_VW": "Relative cylinder charge",

    # -- misc --------------------------------------------------------------- #
    # 0–6 integers: gear, indexed from 0 (neutral) as the ECU counts it.
    "ldp_gear_pv_av_fl": "Gear",
    # 0.4–1.0: the efficiency of the actual ignition angle against optimum.
    "ldp_eff_iga_av_ip_lamb_cop_min": "Ignition-angle efficiency",
}


def units_label(units: Optional[str]) -> str:
    """``units`` as something worth printing — never blank, never a bare ``-``.

    The XDF spells dimensionless as ``-``, which on screen is indistinguishable
    from "no unit was recorded". Both become :data:`DIMENSIONLESS`, so a unitless
    ratio reads as a deliberate fact rather than as missing metadata.
    """
    text = (units or "").strip()
    if text in ("", "-", "–", "—"):
        return DIMENSIONLESS
    return text


def axis_label(symbol: Optional[str], units: Optional[str]) -> str:
    """``Quantity [unit]`` for one axis — the form the editor labels axes with.

    Falls back to the axis symbol when :data:`AXIS_QUANTITIES` has no entry, and
    to ``Axis`` when there is not even a symbol. Both fallbacks are honest
    placeholders: they say what is known and never invent a quantity from the
    unit, because the same unit serves several quantities in this ECU (rpm is
    engine speed on one axis and turbocharger speed on another).
    """
    name = quantity(symbol) or symbol or "Axis"
    return f"{name} [{units_label(units)}]"


def quantity(symbol: Optional[str]) -> Optional[str]:
    """The English quantity for an axis symbol, or ``None`` when uncurated."""
    if not symbol:
        return None
    return AXIS_QUANTITIES.get(symbol)


def table_signature(
    units: Optional[str],
    x_label: Optional[str] = None,
    y_label: Optional[str] = None,
    *,
    count: int = 1,
    is_axis: bool = False,
) -> str:
    """One line saying what a table *is*: its cell unit against its axes.

    ``"hPa vs. Engine speed [rpm] and Manifold pressure setpoint [hPa]"``. This
    is the sentence that turns a grid of numbers into a readable calibration:
    the title names the table, this names its dimensions.

    A table with no breakpoint axes of its own — a scalar constant, or a
    standalone axis table, which *is* a set of breakpoints rather than being
    indexed by one — gets a count and its unit instead, because "vs." with
    nothing after it would be worse than saying less.
    """
    cell = units_label(units)
    axes = [label for label in (x_label, y_label) if label]
    if axes:
        return f"{cell} vs. " + " and ".join(axes)

    noun = "breakpoints" if is_axis else "values"
    if count <= 1:
        return (
            f"A single {DIMENSIONLESS} value"
            if cell == DIMENSIONLESS
            else f"A single value in {cell}"
        )
    return (
        f"{count} {DIMENSIONLESS} {noun}"
        if cell == DIMENSIONLESS
        else f"{count} {noun} in {cell}"
    )
