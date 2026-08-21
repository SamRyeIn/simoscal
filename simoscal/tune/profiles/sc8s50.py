"""Profile map for ``SC8S50.V1.0.xdf`` — the 2017 VW GTI Simos 18.1/18.6 file structure.

Every logical name below is bound to a symbol that the R00–R12 revision lineage
actually touched, so a revision written against this profile can express the
whole existing calibration. Descriptions are the XDF ``title`` verbatim unless
the title is terse enough to mislead, in which case the tuning guide's clearer
phrasing is used and noted.

Shapes are declared so that resolving against a *different* Simos 18 XDF fails
loud if a same-named symbol has a different geometry there — a 4×6 boost
setpoint and an 8×12 boost setpoint are not the same table, whatever they are
called.
"""

from __future__ import annotations

from ..profile import TAG_AXIS, TAG_FLOAT_BUG, TAG_KG_PER_STROKE, Profile, TableSpec


def _spec(name, key, description, units="", shape=None, tags=frozenset(), owner=""):
    return TableSpec(
        name=name, key=key, description=description,
        units=units, shape=shape, tags=tags, owner=owner,
    )


_SPECS = [
    # ---- boost ------------------------------------------------------------ #
    _spec("put_setpoint", "IP_PUT_SP",
          "Pressure up throttle setpoint", "hPa", (4, 6)),
    _spec("put_setpoint_rpm_axis", "ldp_n_ip_put_sp",
          "Pressure up throttle setpoint : x axis (engine speed)", "rpm", (1, 6),
          frozenset({TAG_AXIS})),
    _spec("pressure_quotient_max", "IP_PQ_CHA_MAX",
          "Maximum allowed pressure quotient at turbo charger compressor",
          "-", (8, 8)),
    _spec("overboost_threshold", "IP_PUT_AMP_DIF_MAX_PRS_DIF_THR",
          "Overpressure upstream throttle threshold for turbocharger "
          "overpressure diagnosis (P0234)", "hPa", (1, 6)),
    # C_PRS_IM_SP_MAX is float32 with an identity equation; its XDF display max
    # of 10000 is a TunerPro editor limit the stock value already exceeds 24x.
    _spec("manifold_pressure_max", "C_PRS_IM_SP_MAX",
          "Maximum allowed PRS_IM_SP (maximum requested intake-manifold "
          "pressure setpoint)", "hPa", (1, 1), frozenset({TAG_FLOAT_BUG})),
    # THE kg/stk trap. The XDF labels this identity-scaled mg/stk; the ECU
    # stores kg/stk. Any mg/stk API must divide by 1e6 — writing raw 2000 here
    # raises the ceiling ~1.44 million-fold (stock is 0.001389 kg/stk), i.e.
    # removes the limiter.
    #
    # Domain-owned, and this one is owned for a different reason than the switch
    # patch's tables: not a structural invariant, but a *unit* the display
    # actively contradicts. The generic editor shows "0.002" beside the XDF's
    # "mg/stk" label, and the sane-looking correction — type 2000 — is the
    # catastrophic one. No guard catches it either: the table is float-bug
    # flagged, but its declared max is 20000, so 2000 breaches nothing
    # (CR-20260815-04). The mg/stk entry point is the domain call.
    _spec("airmass_setpoint_max", "C_M_AIR_CYL_SP_MAX",
          "Maximum allowed M_AIR_CYL_SP (maximum allowed airmass setpoint)",
          "mg/stk", (1, 1), frozenset({TAG_KG_PER_STROKE, TAG_FLOAT_BUG}),
          owner="tune.limits.airmass_cap_mg(), which takes mg/stk and stores "
                "kg/stk — the XDF's mg/stk label is wrong"),
    # Same symbol family, same "mg/stk" label, same declared 0..20000 range, and
    # float-bug flagged like its sibling above — but it reads 0.0 in both the
    # stock and every patched bin, so nothing here proves whether it stores
    # kg/stk too. Mapped solely to carry that doubt into the catalog: an
    # unmapped table arrives with no tags and no owner, which is precisely how
    # this one stayed generically writable. No domain call writes it and no
    # revision ever has, so refusing costs nothing today; if a use for it
    # appears, settle the units first and give it a real writer.
    _spec("airmass_full_load", "C_M_AIR_CYL_FL",
          "Airmass per cylinder at full load (units unconfirmed — see owner)",
          "mg/stk", (1, 1), frozenset({TAG_FLOAT_BUG}),
          owner="no verified write path — this table's units are unconfirmed "
                "and may be kg/stk like C_M_AIR_CYL_SP_MAX"),
    _spec("intake_air_max_vvl0", "IP_M_AIR_CYL_MAX_STND_VVL[STND]",
          "Maximum intake air of the engine at standardized ambient pressure, "
          "valve lift STND", "mg/stk", (1, 12)),
    _spec("intake_air_max_vvl1", "IP_M_AIR_CYL_MAX_STND_VVL[LFT_1]",
          "Maximum intake air of the engine at standardized ambient pressure, "
          "valve lift 1", "mg/stk", (1, 12)),

    # ---- wastegate -------------------------------------------------------- #
    # BPA = boost pressure actuator = the wastegate. Cells are actuator
    # position: 1 = closed (all flow through the turbine), 0 = open.
    _spec("wastegate_feedforward_vvl0", "IP_FAC_BPA_SP[0]",
          "Map for boost pressure actuator setpoint, VVL 0 "
          "(wastegate position feedforward)", "-", (10, 16)),
    _spec("wastegate_feedforward_vvl1", "IP_FAC_BPA_SP[1]",
          "Map for boost pressure actuator setpoint, VVL 1 "
          "(wastegate position feedforward)", "-", (10, 16)),
    # Shared by IP_FAC_BPA_SP[0] and [1] and nothing else — editing it
    # re-breakpoints both wastegate maps at once.
    _spec("wastegate_exh_flow_axis", "ldp_fac_1_ip_fac_bpa_sp",
          "Map for boost pressure actuator setpoint : x axis "
          "(exhaust flow factor), shared by VVL 0 and VVL 1", "-", (1, 16),
          frozenset({TAG_AXIS})),

    # ---- fueling ---------------------------------------------------------- #
    _spec("lambda_basic", "IP_LAMB_BAS[1]",
          "Basic lambda setpoint grid", "-", (8, 12)),
    _spec("lambda_basic_hpdi", "IP_LAMB_BAS_HPDI[1]",
          "Basic HPDI lambda setpoint grid (direct injection)", "-", (8, 12)),
    _spec("lambda_basic_mpi", "IP_LAMB_BAS_MPI[1]",
          "Basic MPI lambda setpoint grid (port injection)", "-", (8, 12)),
    # Shared by the three lambda grids above.
    _spec("lambda_rpm_axis", "ldpm_n_32_1_lasp",
          "Basic lambda setpoint : x axis (engine speed), shared by "
          "BAS/HPDI/MPI", "rpm", (1, 12), frozenset({TAG_AXIS})),
    _spec("lambda_load_axis", "ldpm_maf_1_lasp",
          "Basic lambda setpoint : y axis (airmass load), shared by "
          "BAS/HPDI/MPI", "mg/stk", (1, 8), frozenset({TAG_AXIS})),
    _spec("lambda_setpoint_min", "C_LAMB_BAS_COR_MIN",
          "Minimal value for lambda setpoint", "-", (1, 1)),
    _spec("lambda_catalyst_min", "IP_LAMB_COP_MIN",
          "Minimum lambda value for catalyst overheating protection",
          "-", (6, 6)),
    _spec("lambda_turbo_min", "IP_LAMB_TUR_OHP_MIN",
          "Minimum lambda value for turbo charger overheating prevention "
          "based on engine speed", "-", (1, 8)),
    _spec("pedal_threshold_full_load", "ID_PV_AV_FL",
          "Pedal value threshold for the determination of LV_FL_RAW "
          "(heavy-throttle enrichment entry)", "%", (7, 8)),

    # ---- limits ----------------------------------------------------------- #
    _spec("torque_reference_max", "IP_TQI_REF_MAX_MON",
          "Maximum reference indicated engine torque", "Nm", (1, 7)),
]

# ---- ignition --------------------------------------------------------------#
# Nine base-timing grids: VVL 0, port flap low, intake cam position 0-2 x
# exhaust cam position 0-2. The lineage edits all nine identically, because the
# ECU interpolates between cam positions and a pull applied to only some of
# them would leave knock-prone cells reachable.
for _intake in range(3):
    for _exhaust in range(3):
        _SPECS.append(_spec(
            f"ignition_base_vvl0_i{_intake}_e{_exhaust}",
            f"IP_IGA_BAS_IVVT_VVL_PORT_L[STND][{_intake}][{_exhaust}]",
            f"Basic ignition angle, VVL 0 port flap low, intake cam "
            f"{_intake} exhaust cam {_exhaust}",
            "°CRK", (16, 16),
        ))

# ---- ignition: intake-air-temperature correction --------------------------- #
# The spark-vs-IAT tables. Stock pulls timing above 30 degC and adds it when
# very cold; the basics guide's author does neither on an upgraded intercooler
# (see knowledge/ecu-tuning-basics.md, "Spark IAT correction").
#
# Basic and Reference are separate corrections applied to separate angles, but
# they share BOTH breakpoint axes and nothing in the XDF says so at the point of
# edit. They are mapped as a pair so that fact is visible in one place.
_SPECS.extend([
    _spec("ignition_temp_correction_basic", "IP_IGA_BAS_TEMP_N_32",
          "Basis for temperature correction of Basic IGA versus N_32, TIA "
          "(timing offset vs engine speed and intake air temperature)",
          "\N{DEGREE SIGN}CRK", (10, 10)),
    _spec("ignition_temp_correction_reference", "IP_IGA_REF_TEMP_N_32",
          "Basis for temperature correction of Reference IGA versus N_32, TIA "
          "(timing offset vs engine speed and intake air temperature)",
          "\N{DEGREE SIGN}CRK", (10, 10)),
    # Shared axes. The x axis is the wider hazard of the two: it breakpoints ten
    # ignition-correction tables (IP_IGA_BAS_TEMP_*, IP_IGA_REF_TEMP_*, and the
    # coolant-temperature TCO variants), only two of which are mapped here, so a
    # re-breakpoint moves eight tables this profile cannot show you. The y axis
    # is shared by exactly the two tables above and nothing else.
    _spec("ignition_temp_rpm_axis", "ldpm_n_32_5_igsp",
          "Basis for temperature correction of IGA versus N_32, TIA : x axis "
          "(engine speed), shared by ten IGA temperature-correction tables",
          "rpm", (1, 10), frozenset({TAG_AXIS})),
    _spec("ignition_temp_iat_axis", "ldpm_tia_iga_cor_sel",
          "Basis for temperature correction of IGA versus N_32, TIA : y axis "
          "(intake air temperature), shared by Basic and Reference",
          "\N{DEGREE SIGN}C", (1, 10), frozenset({TAG_AXIS})),
])

# The remaining Tuning Basics SOP write targets, plus the one table the revision
# lineage has edited that this profile did not map.
#
# The recipe in `simoscal.sop_recipe` reaches these by resolving ECU symbols
# directly, so the Python side has always been able to write them; the app's
# table browser only ever offers what this profile declares, which is why they
# were unreachable there. Every entry below was decoded off the stock
# `5G0906259L__0002` bin before being written down — the standard the existing
# entries were added under.
#
# None of them is domain-owned. These are ordinary calibration values with no
# unit the display contradicts and no structural invariant a partial write would
# break, which is what `owner` exists for (see `airmass_setpoint_max` above and
# the switch patch's slot grids). A limiter being *raised* by the SOP is not a
# reason to hide it: the guide raises it, a person editing by hand may want to
# put it back, and a ceiling you cannot see is a ceiling you cannot check.
_SPECS.extend([
    # ---- limiters: turbocharger protection --------------------------------- #
    # Stock 189000 / 179000 rpm. The pair is the hard ceiling and the setpoint
    # the closed loop targets below it; the basics guide raises both to 220000.
    # The analysis battery's own turbo watch line (190k) sits between them.
    _spec("turbo_speed_max", "C_N_TCHA_MAX",
          "Maximum turbo charger speed", "rpm", (1, 1)),
    _spec("turbo_speed_max_setpoint", "C_N_TCHA_MAX_SP",
          "Maximum turbo charger speed setpoint for turbo charger protection",
          "rpm", (1, 1)),
    # Stock 185 / 175 degC, compressor-outlet air. Same ceiling/setpoint pairing.
    _spec("compressor_air_temp_max", "C_TIA_THR_TCHA_MAX",
          "Constant to define the maximum air temperature",
          "\N{DEGREE SIGN}C", (1, 1)),
    _spec("compressor_air_temp_max_setpoint", "C_TIA_THR_TCHA_MAX_SP",
          "Maximum air temperature setpoint that could be controlled using the "
          "torque setpoint reduction", "\N{DEGREE SIGN}C", (1, 1)),

    # ---- limiters: overboost diagnosis and road speed ---------------------- #
    # The CAP_H (charge air pressure too high) diagnosis cap, stock 1600-2650
    # hPa. Distinct from `overboost_threshold` (P0234): that one is a
    # pressure *difference* against ambient, this is an absolute cap scheduled
    # against requested PUT and engine speed.
    _spec("charge_air_pressure_max_diag", "IP_PUT_MAX_CAP_H_DIAG",
          "Maximum charge air pressure quotient for charge air pressure too "
          "high (CAP_H) diagnosis", "hPa", (6, 6)),
    # Four scalars, all stock 200 km/h: the three speed-limiter levels and the
    # not-active value. They are separate tables holding the same number, so a
    # change to one alone is almost certainly a mistake — raising the limiter
    # means writing all four.
    _spec("speed_limiter_level1", "LMVLim_vMax_vLim_C_VW.VehSpdl2Lvl1",
          "Overall maximal velocity, limiter level 1", "km/h", (1, 1)),
    _spec("speed_limiter_level2", "LMVLim_vMax_vLim_C_VW.VehSpdl2Lvl2",
          "Overall maximal velocity, limiter level 2", "km/h", (1, 1)),
    _spec("speed_limiter_level3", "LMVLim_vMax_vLim_C_VW.VehSpdl2Lvl3",
          "Overall maximal velocity, limiter level 3", "km/h", (1, 1)),
    _spec("speed_limiter_inactive", "LMVLim_vMax_vLim_C_VW.VehSpdl2NotAcv",
          "Overall maximal velocity, limiter not active", "km/h", (1, 1)),

    # ---- cooling ----------------------------------------------------------- #
    # Stock 80.25-107.2 degC against engine speed and relative charge. The guide
    # lowers the hot end; the SOP treats it as a read-modify-write rule rather
    # than a literal grid, which is why it needs the real table here.
    _spec("cylinder_head_temp_setpoint", "CoTE_tHdCtlSp_M_VW",
          "Cylinder head temperature control setpoint",
          "\N{DEGREE SIGN}C", (6, 6)),

    # ---- boost ------------------------------------------------------------- #
    # Selects whether PUT is computed from ambient pressure (AMP) or from
    # pressure upstream of the charger. Stock 0. A one-cell switch, not a curve.
    _spec("put_from_ambient_enable", "LC_PUT_SP_TOL_ENA_AMP",
          "Use AMP for calculation of PUT out of pressure ratio "
          "(instead of PRS_CHA_UP)", "-", (1, 1)),
    # The other half of IP_PUT_SP's axis pair. `put_setpoint_rpm_axis` (x) was
    # already mapped; this is the y axis the revision lineage's axis-write
    # actually moves, and it was the one table the lineage has edited that this
    # profile could not show. Used by IP_PUT_SP and nothing else, so a
    # re-breakpoint here moves no table the profile hides.
    _spec("put_setpoint_map_axis", "ldp_map_sp_ip_put_sp",
          "Pressure up throttle setpoint : y axis (manifold pressure setpoint)",
          "hPa", (1, 4), frozenset({TAG_AXIS})),

    # ---- axes for the two grids above -------------------------------------- #
    # Both CoTE axes are used by CoTE_tHdCtlSp_M_VW alone, so re-breakpointing
    # either moves nothing else.
    _spec("cylinder_head_temp_rpm_axis", "DATA_ThmMng.CoTE_nEng_A_VW",
          "Cylinder head temperature control setpoint : x axis (engine speed)",
          "rpm", (1, 6), frozenset({TAG_AXIS})),
    _spec("cylinder_head_temp_charge_axis", "DATA_ThmMng.CoTE_rChRel_A_VW",
          "Cylinder head temperature control setpoint : y axis "
          "(relative cylinder charge)", "%", (1, 6), frozenset({TAG_AXIS})),
    # The CAP diagnosis axes are shared with IP_PUT_MIN_CAP_L_DIAG — the
    # pressure-too-*low* counterpart, which this profile does not map. A
    # re-breakpoint therefore moves one table the browser cannot show, which is
    # a decision for a revision to make deliberately rather than a side effect.
    _spec("charge_air_diag_put_axis", "ldpm_put_sp_ip_put_cap_diag",
          "Maximum charge air pressure for CAP_H diagnosis : x axis "
          "(pressure up throttle setpoint), shared with IP_PUT_MIN_CAP_L_DIAG",
          "hPa", (1, 6), frozenset({TAG_AXIS})),
    _spec("charge_air_diag_rpm_axis", "ldpm_n_ip_put_cap_diag",
          "Maximum charge air pressure for CAP_H diagnosis : y axis "
          "(engine speed), shared with IP_PUT_MIN_CAP_L_DIAG",
          "rpm", (1, 6), frozenset({TAG_AXIS})),
])

#: Every base-timing logical name, in the order the ECU's cam grid runs.
IGNITION_BASE_VVL0 = tuple(
    f"ignition_base_vvl0_i{i}_e{e}" for i in range(3) for e in range(3)
)

#: The two IAT timing corrections and the two axes they share, in the order a
#: revision should think about them: cells first, axes last.
IGNITION_TEMP_CORRECTION = (
    "ignition_temp_correction_basic",
    "ignition_temp_correction_reference",
    "ignition_temp_rpm_axis",
    "ignition_temp_iat_axis",
)

#: The three lambda grids that share ``lambda_rpm_axis`` / ``lambda_load_axis``.
LAMBDA_FAMILY = ("lambda_basic", "lambda_basic_hpdi", "lambda_basic_mpi")

#: The two wastegate feedforward maps, which must always be edited together.
WASTEGATE_MAPS = ("wastegate_feedforward_vvl0", "wastegate_feedforward_vvl1")

#: The three lambda minimum-value floors the basics guide sets to 0.80.
LAMBDA_FLOORS = ("lambda_setpoint_min", "lambda_catalyst_min", "lambda_turbo_min")

#: The turbocharger protection ceilings, each as a (hard limit, setpoint) pair.
#: The setpoint is what the closed loop targets; the limit is where protection
#: acts. Raising one without the other narrows or inverts the gap between them.
TURBO_PROTECTION = (
    "turbo_speed_max", "turbo_speed_max_setpoint",
    "compressor_air_temp_max", "compressor_air_temp_max_setpoint",
)

#: The four road-speed limiter scalars, all stock 200 km/h.
#:
#: Grouped because they are four tables holding one number: the three levels and
#: the not-active value. Writing one alone leaves the car limited by whichever of
#: the others the ECU happens to select, which looks like the edit silently
#: failing.
SPEED_LIMITER = (
    "speed_limiter_level1", "speed_limiter_level2",
    "speed_limiter_level3", "speed_limiter_inactive",
)

#: The CAP_H overboost-diagnosis cap and the two axes it is scheduled on. Both
#: axes are shared with ``IP_PUT_MIN_CAP_L_DIAG``, which this profile does not
#: map — see the specs.
CHARGE_AIR_DIAG = (
    "charge_air_pressure_max_diag",
    "charge_air_diag_put_axis",
    "charge_air_diag_rpm_axis",
)

#: The cylinder-head temperature setpoint and its two axes, which nothing else
#: uses.
CYLINDER_HEAD_TEMP = (
    "cylinder_head_temp_setpoint",
    "cylinder_head_temp_rpm_axis",
    "cylinder_head_temp_charge_axis",
)


SC8S50 = Profile(
    name="SC8S50",
    xdf="SC8S50.V1.0.xdf",
    specs={s.name: s for s in _SPECS},
)

__all__ = [
    "SC8S50",
    "CHARGE_AIR_DIAG",
    "CYLINDER_HEAD_TEMP",
    "IGNITION_BASE_VVL0",
    "IGNITION_TEMP_CORRECTION",
    "LAMBDA_FAMILY",
    "LAMBDA_FLOORS",
    "SPEED_LIMITER",
    "TURBO_PROTECTION",
    "WASTEGATE_MAPS",
]
