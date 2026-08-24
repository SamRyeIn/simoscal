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

from ...checksum import SC8S50_STRUCTURE
from ..profile import (
    GROUP_AIRFLOW,
    GROUP_BOOST,
    GROUP_FUELING,
    GROUP_LIMITERS,
    GROUP_PEDAL_TORQUE,
    GROUP_TIMING,
    GROUP_TURBO_THERMAL,
    TAG_AXIS,
    TAG_FLOAT_BUG,
    TAG_KG_PER_STROKE,
    Profile,
    TableSpec,
    apply_groups,
)


def _spec(name, key, description, units="", shape=None, tags=frozenset(), owner=""):
    return TableSpec(
        name=name, key=key, description=description,
        units=units, shape=shape, tags=tags, owner=owner,
    )


# Owners for the two base-space table sets whose writes carry an invariant a
# generic grid edit cannot honour: the four-scalar speed-limiter coherence, and
# the lambda full-load lean bound (refuse any setpoint ≥ 1.00).
_OWNER_SPEED_LIMITER = (
    "tune.limits.speed_limiter(), which writes all four quartet scalars as one "
    "coherent set (bridge op `limiters_edit`)"
)
_OWNER_LAMBDA_FL = (
    "tune.fueling.full_load_enrichment(), which refuses any setpoint at or "
    "above lambda 1.00 (bridge op `lambda_fl_edit`)"
)
_OWNER_STATIC_REV = (
    "tune.limits.static_rev_limit(), which writes all four transmission "
    "variants as one set and refuses a target above the engine's own rev limit"
)
#: The engine's actual rev limiter. Readable — a person and a guard both need to
#: know where it sits — but with no write path at all: raising the speed at which
#: this engine stops is a different decision from letting it reach that speed
#: while stationary, and it should not be reachable by tapping a grid cell on a
#: tablet. If a revision ever wants it, it gets a considered writer of its own.
_OWNER_REV_LIMIT = (
    "no write path — this is the engine's rev limiter itself. Raising it is a "
    "separate decision from the standstill cap and needs its own writer"
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
    # The table an early revision of the shared recipe mistook for the overboost
    # limit. It is a manifold-setpoint limitation offset, not the P0234 threshold
    # (that is `IP_PUT_AMP_DIF_MAX_PRS_DIF_THR`), and writing 2700 here does not
    # do what the guide asks. Mapped with no write path so the mistake cannot be
    # repeated through the generic editor, and so the float-bug flag it needs is
    # declared in the same place as every other one: its XDF range is
    # -10000..10000 while stock reads 271695.84 hPa, 27x the declared maximum.
    _spec("manifold_pressure_limit_offset", "C_PRS_IM_SP_LIM",
          "Offset to the pressure behind the air cleaner for the limitation of "
          "the manifold setpoint", "hPa", (1, 1), frozenset({TAG_FLOAT_BUG}),
          owner="no write path — this is a manifold-setpoint limitation offset, "
                "not the overboost threshold. The P0234 threshold is "
                "`IP_PUT_AMP_DIF_MAX_PRS_DIF_THR`, written by "
                "tune.boost.overboost_threshold()"),
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
# Almost none of them is domain-owned. These are ordinary calibration values
# with no unit the display contradicts and no structural invariant a partial
# write would break, which is what `owner` exists for (see `airmass_setpoint_max`
# above and the switch patch's slot grids). A limiter being *raised* by the SOP
# is not a reason to hide it: the guide raises it, a person editing by hand may
# want to put it back, and a ceiling you cannot see is a ceiling you cannot check.
#
# The exception is the road-speed limiter quartet, which *does* carry a
# structural invariant — four tables holding one number, where a partial write
# leaves the car limited by whichever un-written level the ECU happens to
# select. That is exactly the coherence `owner` exists to protect, so the four
# are owned by ``tune.limits.speed_limiter()``, which writes them as one set.
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
    # change to one alone is almost certainly a mistake — moving the limiter
    # means writing all four. That coherence is why they are domain-owned: a
    # generic grid write to one leaves the car limited by whichever of the
    # others the ECU selects, which looks like the edit silently failing.
    # Quartet membership was confirmed against the XDF on 2026-08-20: these
    # four are the only ``LMVLim_vMax_vLim_C_VW.*`` symbols it defines (the
    # only other ``LMVLim`` entries are the P15A4 error-class scalars for
    # ``LMVLim_bTrckAuth_VW``); there is no hysteresis sibling. Stored /128,
    # so the real encodable ceiling is 511.99 km/h.
    _spec("speed_limiter_level1", "LMVLim_vMax_vLim_C_VW.VehSpdl2Lvl1",
          "Overall maximal velocity, limiter level 1", "km/h", (1, 1),
          owner=_OWNER_SPEED_LIMITER),
    _spec("speed_limiter_level2", "LMVLim_vMax_vLim_C_VW.VehSpdl2Lvl2",
          "Overall maximal velocity, limiter level 2", "km/h", (1, 1),
          owner=_OWNER_SPEED_LIMITER),
    _spec("speed_limiter_level3", "LMVLim_vMax_vLim_C_VW.VehSpdl2Lvl3",
          "Overall maximal velocity, limiter level 3", "km/h", (1, 1),
          owner=_OWNER_SPEED_LIMITER),
    _spec("speed_limiter_inactive", "LMVLim_vMax_vLim_C_VW.VehSpdl2NotAcv",
          "Overall maximal velocity, limiter not active", "km/h", (1, 1),
          owner=_OWNER_SPEED_LIMITER),

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

# The pedal-feel and lambda full-load enrichment surfaces (app domain screens,
# 2026-08-20 plan U1). Every entry was decoded off the stock ``5G0906259L__0002``
# bin before being written down; none is float32 (no FLOAT_BUG candidates), and
# every unit label was checked against its stored scale — torque factors are
# /32768 fractions, lambda is /1024, both honestly labelled dimensionless.
_SPECS.extend([
    # ---- pedal feel: driver-interpretation maps ----------------------------- #
    # Pedal % + engine speed → fraction of maximum torque requested (0..2,
    # stock tops out at 1.0). This DSG car reads the DCT family; the MT/AT
    # variants exist in the XDF but are dead tables for this transmission and
    # are deliberately left unmapped — offering an editor for a map the car
    # never reads invites editing the wrong one. Grid: rows = pedal value
    # (y axis, %), columns = engine speed (x axis, rpm).
    #
    # None is domain-owned (dual-path by design): no unit lies and no
    # cross-table invariant binds them — high/low-speed variants are *often*
    # set identical, but that is a tuning choice, not a structural rule.
    _spec("pedal_dct_high", "IP_FAC_TQ_REQ_DRIV_H_VS_DCT",
          "Driver interpretation map for high vehicle speed (DCT)",
          "-", (12, 12)),
    _spec("pedal_dct_low", "IP_FAC_TQ_REQ_DRIV_L_VS_DCT",
          "Driver interpretation map for low vehicle speed (DCT)",
          "-", (12, 12)),
    _spec("pedal_dct_sport_high", "IP_FAC_TQ_REQ_DRIV_H_VS_DCT_S",
          "Driver interpretation map for high vehicle speed "
          "(DCT, gear shift program = S)", "-", (12, 12)),
    _spec("pedal_dct_sport_low", "IP_FAC_TQ_REQ_DRIV_L_VS_DCT_S",
          "Driver interpretation map for low vehicle speed "
          "(DCT, gear shift program = S)", "-", (12, 12)),
    _spec("pedal_dct_offroad_high", "IP_FAC_TQ_REQ_DRIV_H_OFRD_DCT",
          "Driver interpretation map for high vehicle speed (DCT) "
          "at off-road mode", "-", (12, 12)),
    # The XDF title for this one repeats the sport-program wording; it is the
    # low-speed off-road DCT map (the symbol is the authority).
    _spec("pedal_dct_offroad_low", "IP_FAC_TQ_REQ_DRIV_L_OFRD_DCT",
          "Driver interpretation map for low vehicle speed (DCT) "
          "at off-road mode", "-", (12, 12)),
    _spec("pedal_drive_off", "IP_FAC_TQ_REQ_DRIV_DROF",
          "Driver torque request factor at drive off situation",
          "-", (8, 8)),

    # ---- fueling: lambda full-load enrichment ------------------------------- #
    # The time-based full-load enrichment map: columns = engine speed, rows =
    # time at full load (0–60 s). Stock is flat 1.00 — this car's stock
    # calibration does all its enrichment through the basic lambda grids, so
    # any value written here below 1.00 is *added* enrichment as time at full
    # load accumulates. Leaner is hotter: the danger direction is up, which is
    # why the main map is domain-owned with a hard refusal at ≥ 1.00 (decided
    # by Sam, 2026-08-20) rather than left to a grid cell edit.
    _spec("lambda_full_load", "IP_LAMB_FL_SP",
          "Lambda Full Load Enrichment depending on N_32 and time T_FL",
          "-", (8, 12), owner=_OWNER_LAMBDA_FL),
    # The IAT-conditional variant: same shape and axes, selected instead of the
    # main map when intake air temperature exceeds the threshold below (with
    # the hysteresis below that). Mapped but grid-editable — the screen edits
    # the main map; this one is context a tuner reads and, rarely, edits by
    # hand with the same care as any grid.
    _spec("lambda_full_load_iat", "IP_LAMB_FL_SP_TIA",
          "Lambda Full Load Enrichment map used in dependency of intake air "
          "temperature", "-", (8, 12)),
    _spec("lambda_full_load_iat_threshold", "C_TIA_THD_LAMB_FL_SP",
          "Intake air temperature threshold for lambda full load enrichment",
          "\N{DEGREE SIGN}C", (1, 1)),
    _spec("lambda_full_load_iat_hysteresis", "C_TIA_HYS_LAMB_FL_SP",
          "Intake air temperature hysteresis for lambda full load enrichment",
          "\N{DEGREE SIGN}C", (1, 1)),
])

# The standstill rev cap, and the rev limiter it sits under.
#
# Stock holds this engine to 3808 rpm whenever the vehicle is stopped — the
# familiar "won't rev past about 3800 in park". It is a separate, lower cap than
# the rev limiter proper: `ID_N_MAX_STAT_VVL_L`/`_H` stop the engine at 6816 rpm
# whether moving or not, with the P0219 overspeed diagnosis a further 384 rpm
# clear at 7200. So raising the standstill cap toward 6816 does not raise the
# speed this engine will reach; it lets the *existing* limiter be the thing that
# catches you in park, exactly as it already is in gear.
#
# All four transmission variants hold the same 3808 and only one applies to a
# given car (this one is DCT). They are grouped and owned for the same reason as
# the road-speed quartet: the ECU selects among them, so writing one alone can
# leave the car capped by an un-written sibling, which reads as the edit having
# silently failed — after a flash, which is not a cheap way to find out.
#
# All four are 8-bit scaled x32, so they quantize to 32 rpm steps: 3808 is
# 119 x 32 and 6816 is exactly 213 x 32.
_SPECS.extend([
    _spec("static_rev_limit_dct", "C_N_MAX_DCT",
          "Engine speed threshold for engine speed limitation for stopped DCT "
          "vehicle (the standstill rev cap — this car's variant)",
          "rpm", (1, 1), owner=_OWNER_STATIC_REV),
    _spec("static_rev_limit_at", "C_N_MAX_AT",
          "Engine speed threshold for engine speed limitation for stopped AT "
          "vehicle", "rpm", (1, 1), owner=_OWNER_STATIC_REV),
    _spec("static_rev_limit_mt", "C_N_MAX_MT",
          "Engine speed threshold for engine speed limitation for stopped MT "
          "vehicle", "rpm", (1, 1), owner=_OWNER_STATIC_REV),
    _spec("static_rev_limit_cvt", "C_N_MAX_CVT",
          "Engine speed threshold for engine speed limitation for stopped CVT "
          "vehicle", "rpm", (1, 1), owner=_OWNER_STATIC_REV),
    # How far above the standstill cap fuel is cut to *all* cylinders. Stock 100
    # rpm, matching `C_N_MAX_FCUT_OFS` for the moving case. Ordinary independent
    # scalar, so it stays generically editable — it is the soft-to-hard distance,
    # not the cap itself.
    _spec("static_rev_fuel_cut_offset", "C_N_MAX_FCUT_OFS_VST",
          "Engine speed offset for activation fuel cut-off at all cylinders in "
          "case of stopped vehicle", "rpm", (1, 1)),
    # The rev limiter proper, per valve-lift mode. Mapped so it is readable — the
    # standstill guard checks against it, and a person deserves to see what the
    # real ceiling is — but deliberately given no write path (see the owner).
    _spec("engine_speed_limit_vvl0", "ID_N_MAX_STAT_VVL_L",
          "Static engine speed limit for VVL system, low (the engine's rev "
          "limiter, per gear)", "rpm", (1, 8), owner=_OWNER_REV_LIMIT),
    _spec("engine_speed_limit_vvl1", "ID_N_MAX_STAT_VVL_H",
          "Static engine speed limit for VVL system, high (the engine's rev "
          "limiter, per gear)", "rpm", (1, 8), owner=_OWNER_REV_LIMIT),
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
#: failing — which is why all four are owned by ``tune.limits.speed_limiter()``.
SPEED_LIMITER = (
    "speed_limiter_level1", "speed_limiter_level2",
    "speed_limiter_level3", "speed_limiter_inactive",
)

#: The four standstill rev caps, one per transmission variant.
#:
#: Grouped for the same reason as ``SPEED_LIMITER``: four tables holding one
#: number, of which the ECU reads whichever matches the car. Only the DCT entry
#: applies here; the other three are inert for this transmission and are written
#: alongside it so the change cannot be defeated by a wrong assumption about
#: which one the ECU resolves.
STATIC_REV_LIMIT = (
    "static_rev_limit_dct", "static_rev_limit_at",
    "static_rev_limit_mt", "static_rev_limit_cvt",
)

#: The engine's own rev limiter, per valve-lift mode — read-only context for the
#: standstill cap, which must never be set above it.
ENGINE_SPEED_LIMIT = ("engine_speed_limit_vvl0", "engine_speed_limit_vvl1")

#: The DCT (DSG) driver-interpretation pedal maps, primary pair first, plus the
#: drive-off factor. The MT/AT families are deliberately unmapped — dead tables
#: for this transmission.
PEDAL_MAPS = (
    "pedal_dct_high", "pedal_dct_low",
    "pedal_dct_sport_high", "pedal_dct_sport_low",
    "pedal_dct_offroad_high", "pedal_dct_offroad_low",
    "pedal_drive_off",
)

#: The lambda full-load enrichment set: the owned main map, then the
#: grid-editable IAT variant and its threshold/hysteresis pair.
LAMBDA_FULL_LOAD = (
    "lambda_full_load",
    "lambda_full_load_iat",
    "lambda_full_load_iat_threshold",
    "lambda_full_load_iat_hysteresis",
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


# --------------------------------------------------------------------------- #
# Domain groups — what each table is *for*
# --------------------------------------------------------------------------- #
# The heading an editing client files a table under. Declared here in one block
# rather than as a keyword on each spec so the whole classification is reviewable
# at once: the question "is anything filed in the wrong place?" is answered by
# reading this, not by scanning 69 call sites.
#
# An axis is filed with the map it indexes, never in a bucket of its own. A
# breakpoint is edited in service of the table it breakpoints and is looked for
# beside it — see the note on the group constants in ``..profile``.
_GROUPS: dict[str, tuple[str, ...]] = {
    # Charge-pressure request and its actuation, from the setpoint grid through
    # the wastegate feedforward to the ceilings and diagnoses that cap it.
    # `IP_PQ_CHA_MAX` — Maximum allowed pressure quotient at turbo charger
    # compressor is filed here rather than under turbo protection: it is a
    # pressure ratio, and it is raised or lowered to change how much boost is
    # allowed, which is the question someone browsing "Boost" is asking.
    GROUP_BOOST: (
        "put_setpoint",
        "put_setpoint_rpm_axis",
        "put_setpoint_map_axis",
        "pressure_quotient_max",
        "overboost_threshold",
        "manifold_pressure_max",
        "manifold_pressure_limit_offset",
        "put_from_ambient_enable",
        "wastegate_feedforward_vvl0",
        "wastegate_feedforward_vvl1",
        "wastegate_exh_flow_axis",
        "charge_air_pressure_max_diag",
        "charge_air_diag_put_axis",
        "charge_air_diag_rpm_axis",
    ),
    # Ignition angle: the nine base cam-position grids and the two IAT
    # corrections applied on top of them, with their shared axes.
    GROUP_TIMING: IGNITION_BASE_VVL0 + IGNITION_TEMP_CORRECTION,
    # Lambda, everywhere it is set: the three basic grids and their shared axes,
    # the three minimum-value floors, the full-load enrichment set, and
    # `ID_PV_AV_FL` — Pedal value threshold for the determination of LV_FL_RAW,
    # which is what arms full-load enrichment (the XDF files it under "Fuel"
    # too, and here that is the right call).
    GROUP_FUELING: LAMBDA_FAMILY + (
        "lambda_rpm_axis",
        "lambda_load_axis",
    ) + LAMBDA_FLOORS + LAMBDA_FULL_LOAD + (
        "pedal_threshold_full_load",
    ),
    # What the engine is allowed to ingest, per stroke.
    GROUP_AIRFLOW: (
        "airmass_setpoint_max",
        "airmass_full_load",
        "intake_air_max_vvl0",
        "intake_air_max_vvl1",
    ),
    # Where the engine is made to stop — engine speed, road speed, and torque.
    GROUP_LIMITERS: (
        "torque_reference_max",
        "static_rev_fuel_cut_offset",
    ) + STATIC_REV_LIMIT + ENGINE_SPEED_LIMIT + SPEED_LIMITER,
    # Hardware-protection ceilings. Unlike the limiters above, these exist to
    # keep a component alive rather than to cap what the car will do.
    GROUP_TURBO_THERMAL: TURBO_PROTECTION + CYLINDER_HEAD_TEMP,
    # How pedal travel becomes a torque request.
    GROUP_PEDAL_TORQUE: PEDAL_MAPS,
}


_SPECS = apply_groups("SC8S50", _SPECS, _GROUPS)


#: What stock reads on this car, for guidance text that compares a guide
#: instruction against it. Each value is measured off the stock
#: ``5G0906259L__0002`` bin, and each is a whole sentence rather than a number,
#: because the comparison the guidance wants to draw is the part that is
#: car-specific — not just the figure. A profile for another car that has not
#: been measured declares none of these, and the guidance renders without the
#: comparison rather than quoting this car's numbers at that car.
STOCK_REFERENCES = {
    "lambda_floors": (
        "On 5G0906259L stock is 0.72-0.75 — already richer than 0.80 — so "
        "writing 0.80 would RAISE these floors (leaner) under raised boost."
    ),
    "full_load_lambda": (
        "Stock already reads all 1.0 on 5G0906259L — already compliant, "
        "nothing to write."
    ),
}


#: The table sets a domain call writes as one, keyed by the id the domain names.
#:
#: The tuples above are the declaration; this dict is what makes them reachable
#: as a fact about *this car* rather than as an import from this module. A domain
#: asking for ``"speed_limiter"`` gets whichever quartet the open bin's profile
#: names — see :attr:`~simoscal.tune.profile.Profile.table_sets`.
TABLE_SETS: dict[str, tuple[str, ...]] = {
    "ignition_base_vvl0": IGNITION_BASE_VVL0,
    "ignition_temp_correction": IGNITION_TEMP_CORRECTION,
    "lambda_family": LAMBDA_FAMILY,
    "lambda_floors": LAMBDA_FLOORS,
    "lambda_full_load": LAMBDA_FULL_LOAD,
    "wastegate_maps": WASTEGATE_MAPS,
    "speed_limiter": SPEED_LIMITER,
    "static_rev_limit": STATIC_REV_LIMIT,
    # Which of the four standstill caps this car's ECU actually resolves. All
    # four are written; only this one does anything, and saying so in the
    # journal is a claim about the car in front of you, not about the method.
    "static_rev_limit_active": ("static_rev_limit_dct",),
    "engine_speed_limit": ENGINE_SPEED_LIMIT,
    "turbo_protection": TURBO_PROTECTION,
    "pedal_maps": PEDAL_MAPS,
    "charge_air_diag": CHARGE_AIR_DIAG,
    "cylinder_head_temp": CYLINDER_HEAD_TEMP,
}


SC8S50 = Profile(
    name="SC8S50",
    xdf="SC8S50.V1.0.xdf",
    specs={s.name: s for s in _SPECS},
    structure=SC8S50_STRUCTURE,
    stock_references=STOCK_REFERENCES,
    table_sets=TABLE_SETS,
)

__all__ = [
    "SC8S50",
    "STOCK_REFERENCES",
    "TABLE_SETS",
    "CHARGE_AIR_DIAG",
    "CYLINDER_HEAD_TEMP",
    "ENGINE_SPEED_LIMIT",
    "STATIC_REV_LIMIT",
    "IGNITION_BASE_VVL0",
    "IGNITION_TEMP_CORRECTION",
    "LAMBDA_FAMILY",
    "LAMBDA_FLOORS",
    "LAMBDA_FULL_LOAD",
    "PEDAL_MAPS",
    "SPEED_LIMITER",
    "TURBO_PROTECTION",
    "WASTEGATE_MAPS",
]
