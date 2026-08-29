"""Profile map for ``SCGa05_cal.xdf`` — box code ``3CN906259B``, software ``SCGA05``.

The second file structure this library maps, and the one that turned every
"obviously universal" fact into a per-car one. It is authored against the same
logical-name vocabulary as :mod:`.sc8s50` — ``put_setpoint`` means the same
thing on both cars — but every key, shape, unit and tag below was measured on
this car's XDF and this car's bin, never inherited. That independence is the
point: a map file is data, and two cars sharing a name is not evidence they
share a table.

Three findings drove the differences, and each is noted at the spec:

* the nine VVL0 base ignition grids are **(16, 18)** here where SC8S50's are
  (16, 16), under the *same symbol name* — the shape check is what stops a
  16x16 write landing in a 16x18 table;
* this XDF scales ``C_M_AIR_CYL_SP_MAX`` and the two ``C_PRS_IM_SP_*`` floats
  **correctly**, so the kg/stk trap and the float-bug flags that SC8S50 needs
  are absent here rather than merely unmentioned;
* ten logical names have no table on this car and are declared in
  :data:`UNAVAILABLE` rather than left out, so the gap reads as a decision.

.. warning::

   ``SCGa05_cal.xdf`` numbers its tables from the start of the **calibration
   block**, not the start of the bin: it declares ``BASEOFFSET 0`` for addresses
   relative to ``0x220000``. Read at the declared offset against a full 4 MB bin
   every value is meaningless — mostly padding, and out of bounds past the
   declared region. The profile declares that convention
   (:attr:`Profile.xdf_addresses_cal_relative`) and every read and write through
   it is rebased by the CAL file offset.

   Profile resolution matches on name and shape and is blind to all of this,
   which is why the convention is checked separately:
   :func:`~simoscal.preflight.preflight` holds the file to the profile's
   declaration and refuses any XDF whose header disagrees, rather than inferring
   what a new file must have meant. See ``docs/porting-to-another-xdf.md`` § 7.
"""

from __future__ import annotations

from ...checksum import SCGA05_STRUCTURE
from ..profile import (
    GROUP_AIRFLOW,
    GROUP_BOOST,
    GROUP_FUELING,
    GROUP_LIMITERS,
    GROUP_PEDAL_TORQUE,
    GROUP_TIMING,
    GROUP_TURBO_THERMAL,
    TAG_AXIS,
    Profile,
    TableSpec,
    apply_groups,
)


def _spec(name, key, description, units="", shape=None, tags=frozenset(), owner=""):
    return TableSpec(
        name=name, key=key, description=description,
        units=units, shape=shape, tags=tags, owner=owner,
    )


# --------------------------------------------------------------------------- #
# Owners — writes that carry an invariant no generic grid edit can honour
# --------------------------------------------------------------------------- #
# These are *structural* invariants, which is why they port: "four scalars hold
# one number" and "a full-load lambda at or above 1.00 is lean at full load" are
# arithmetic about the tables in hand, not calibration advice learned on another
# car. Nothing here quotes a target value.
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
_OWNER_REV_LIMIT = (
    "no write path — this is the engine's rev limiter itself. Raising it is a "
    "separate decision from the standstill cap and needs its own writer"
)


_SPECS = [
    # ---- boost ------------------------------------------------------------ #
    _spec("put_setpoint", "IP_PUT_SP",
          "PUT setpoint (pressure up throttle setpoint)", "hPa", (4, 6)),
    _spec("put_setpoint_rpm_axis", "ldp_n_ip_put_sp",
          "PUT setpoint : x axis (engine speed)", "rpm", (1, 6),
          frozenset({TAG_AXIS})),
    _spec("put_setpoint_map_axis", "ldp_map_sp_ip_put_sp",
          "PUT setpoint : y axis (manifold pressure setpoint)", "hPa", (1, 4),
          frozenset({TAG_AXIS})),
    _spec("pressure_quotient_max", "IP_PQ_CHA_MAX",
          "Turbo max pressure ratio (maximum allowed pressure quotient at the "
          "turbocharger compressor)", "-", (8, 8)),
    _spec("overboost_threshold", "IP_PUT_AMP_DIF_MAX_PRS_DIF_THR",
          "Overpressure upstream throttle threshold for turbocharger "
          "overpressure diagnosis (P0234)", "hPa", (1, 6)),
    # Both C_PRS_IM_SP_* floats are scaled correctly by this XDF (m=0.01), and
    # stock reads 2399.96 / 2716.96 hPa against a declared maximum of 10000 —
    # comfortably inside it. SC8S50's XDF gives the same two tables an identity
    # equation, which displays the same bytes as 239996 / 271695.84 and puts
    # them 24x past the declared maximum; that is what TAG_FLOAT_BUG exists for.
    # Neither table is flagged here, and the absence is a measurement, not an
    # oversight: flagging one would *disable* a live guard on this car.
    _spec("manifold_pressure_max", "C_PRS_IM_SP_MAX",
          "Maximum requested intake-manifold pressure setpoint", "hPa", (1, 1)),
    # Not the overboost limit, on any car. The P0234 threshold is
    # `IP_PUT_AMP_DIF_MAX_PRS_DIF_THR`; a shared tuning recipe once routed a
    # 2700 hPa overboost figure here instead. This XDF's title — "Maximum Intake
    # Manifold Pressure" — reads like the ceiling and makes the confusion easier
    # here than on SC8S50, so the description says what the table actually is
    # and the table keeps no write path.
    _spec("manifold_pressure_limit_offset", "C_PRS_IM_SP_LIM",
          "Offset to the pressure behind the air cleaner for the limitation of "
          "the manifold setpoint (NOT the overboost threshold)", "hPa", (1, 1),
          owner="no write path — this is a manifold-setpoint limitation offset, "
                "not the overboost threshold. The P0234 threshold is "
                "`IP_PUT_AMP_DIF_MAX_PRS_DIF_THR`, written by "
                "tune.boost.overboost_threshold()"),
    _spec("put_from_ambient_enable", "LC_PUT_SP_TOL_ENA_AMP",
          "Pressure ratio calculation toggle: 1 = use ambient pressure (AMP), "
          "0 = use pressure upstream of the charger", "-", (1, 1)),
    _spec("charge_air_pressure_max_diag", "IP_PUT_MAX_CAP_H_DIAG",
          "Maximum charge air pressure for charge-air-pressure-too-high "
          "(CAP_H) diagnosis", "hPa", (6, 6)),
    _spec("charge_air_diag_put_axis", "ldpm_put_sp_ip_put_cap_diag",
          "Maximum charge air pressure for CAP_H diagnosis : x axis "
          "(PUT setpoint)", "hPa", (1, 6), frozenset({TAG_AXIS})),
    _spec("charge_air_diag_rpm_axis", "ldpm_n_ip_put_cap_diag",
          "Maximum charge air pressure for CAP_H diagnosis : y axis "
          "(engine speed)", "rpm", (1, 6), frozenset({TAG_AXIS})),

    # ---- wastegate -------------------------------------------------------- #
    # BPA = boost pressure actuator = the wastegate. Cells are actuator
    # position: 1 = closed (all flow through the turbine), 0 = open. This XDF
    # names them plainly where SC8S50's says "Map for boost pressure actuator
    # setpoint", so the title is kept verbatim.
    _spec("wastegate_feedforward_vvl0", "IP_FAC_BPA_SP[0]",
          "Wastegate position feedforward, VVL 0", "-", (10, 16)),
    _spec("wastegate_feedforward_vvl1", "IP_FAC_BPA_SP[1]",
          "Wastegate position feedforward, VVL 1", "-", (10, 16)),
    _spec("wastegate_exh_flow_axis", "ldp_fac_1_ip_fac_bpa_sp",
          "Wastegate position feedforward : x axis (relative exhaust flow), "
          "shared by both valve-lift maps", "-", (1, 16),
          frozenset({TAG_AXIS})),
    _spec("wastegate_intake_flow_axis", "ldp_fac_2_ip_fac_bpa_sp",
          "Wastegate position feedforward : y axis (relative intake flow), "
          "shared by both valve-lift maps", "-", (1, 10),
          frozenset({TAG_AXIS})),

    # ---- airflow ---------------------------------------------------------- #
    # The kg/stk trap does NOT apply on this car. The ECU stores the same
    # 0.001389 kg/stk as SC8S50 does, but this XDF's equation carries the
    # 1e6 factor, so the table reads and writes as 1389 mg/stk exactly as its
    # label says. TAG_KG_PER_STROKE is therefore absent — and deliberately so:
    # tagging it would make tune.limits.airmass_cap_mg() divide a value that is
    # already in mg/stk, i.e. reintroduce on this car the millionfold error the
    # tag exists to prevent on the other one. Left generically editable for the
    # same reason: airmass_cap_mg() refuses an untagged table, so an owner
    # pointing at it would leave the ceiling with no write path at all.
    _spec("airmass_setpoint_max", "C_M_AIR_CYL_SP_MAX",
          "Maximum allowed M_AIR_CYL_SP (maximum allowed airmass setpoint)",
          "mg/stk", (1, 1)),
    _spec("intake_air_max_vvl0", "IP_M_AIR_CYL_MAX_STND_VVL[STND]",
          "Maximum intake air of the engine at standardized ambient pressure, "
          "valve lift STND", "mg/stk", (1, 12)),
    _spec("intake_air_max_vvl1", "IP_M_AIR_CYL_MAX_STND_VVL[LFT_1]",
          "Maximum intake air of the engine at standardized ambient pressure, "
          "valve lift 1", "mg/stk", (1, 12)),

    # ---- fueling ---------------------------------------------------------- #
    _spec("lambda_basic", "IP_LAMB_BAS[1]",
          "Basic lambda setpoint grid", "-", (8, 12)),
    _spec("lambda_basic_hpdi", "IP_LAMB_BAS_HPDI[1]",
          "Basic HPDI lambda setpoint grid (direct injection)", "-", (8, 12)),
    _spec("lambda_basic_mpi", "IP_LAMB_BAS_MPI[1]",
          "Basic MPI lambda setpoint grid (port injection)", "-", (8, 12)),
    _spec("lambda_setpoint_min", "C_LAMB_BAS_COR_MIN",
          "Minimal value for lambda setpoint", "-", (1, 1)),
    _spec("lambda_catalyst_min", "IP_LAMB_COP_MIN",
          "Minimum lambda value for catalyst overheating protection",
          "-", (6, 6)),
    _spec("lambda_turbo_min", "IP_LAMB_TUR_OHP_MIN",
          "Minimum lambda value for turbo charger overheating prevention "
          "based on engine speed", "-", (1, 8)),
    _spec("lambda_full_load", "IP_LAMB_FL_SP",
          "Lambda setpoint during full load", "-", (8, 12),
          owner=_OWNER_LAMBDA_FL),
    _spec("lambda_full_load_iat", "IP_LAMB_FL_SP_TIA",
          "Lambda setpoint during full load, hot intake air temperature",
          "-", (8, 12)),
    _spec("lambda_full_load_iat_threshold", "C_TIA_THD_LAMB_FL_SP",
          "Intake air temperature threshold for hot-IAT full-load lambda",
          "\N{DEGREE SIGN}C", (1, 1)),
    _spec("lambda_full_load_iat_hysteresis", "C_TIA_HYS_LAMB_FL_SP",
          "Intake air temperature hysteresis for lambda full load enrichment",
          "\N{DEGREE SIGN}C", (1, 1)),
    _spec("pedal_threshold_full_load", "ID_PV_AV_FL",
          "Pedal value threshold for the determination of LV_FL_RAW "
          "(heavy-throttle enrichment entry)", "%", (7, 8)),

    # ---- limits ----------------------------------------------------------- #
    _spec("torque_reference_max", "IP_TQI_REF_MAX_MON",
          "Maximum reference indicated engine torque", "Nm", (1, 7)),
    _spec("static_rev_limit_mt", "C_N_MAX_MT",
          "Engine speed threshold for engine speed limitation for stopped MT "
          "vehicle", "rpm", (1, 1), owner=_OWNER_STATIC_REV),
    _spec("static_rev_limit_at", "C_N_MAX_AT",
          "Engine speed threshold for engine speed limitation for stopped AT "
          "vehicle", "rpm", (1, 1), owner=_OWNER_STATIC_REV),
    _spec("static_rev_limit_dct", "C_N_MAX_DCT",
          "Engine speed threshold for engine speed limitation for stopped DCT "
          "vehicle", "rpm", (1, 1), owner=_OWNER_STATIC_REV),
    _spec("static_rev_limit_cvt", "C_N_MAX_CVT",
          "Engine speed threshold for engine speed limitation for stopped CVT "
          "vehicle", "rpm", (1, 1), owner=_OWNER_STATIC_REV),
    _spec("engine_speed_limit_vvl0", "ID_N_MAX_STAT_VVL_L",
          "Static engine speed limit for VVL system, low (the engine's rev "
          "limiter, per gear)", "rpm", (1, 8), owner=_OWNER_REV_LIMIT),
    _spec("engine_speed_limit_vvl1", "ID_N_MAX_STAT_VVL_H",
          "Static engine speed limit for VVL system, high (the engine's rev "
          "limiter, per gear)", "rpm", (1, 8), owner=_OWNER_REV_LIMIT),
    # All four road-speed scalars share the title "overall maximal velocity" in
    # this XDF, so the description is the only thing that tells them apart —
    # which is precisely why the quartet is written as one set rather than by
    # picking whichever row a browser happens to show first.
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

    # ---- turbo & thermal protection --------------------------------------- #
    _spec("turbo_speed_max", "C_N_TCHA_MAX",
          "Turbocharger speed for maximum torque management", "rpm", (1, 1)),
    _spec("turbo_speed_max_setpoint", "C_N_TCHA_MAX_SP",
          "Turbocharger speed to start torque management", "rpm", (1, 1)),
    _spec("compressor_air_temp_max", "C_TIA_THR_TCHA_MAX",
          "Compressor outlet temperature for maximum torque management",
          "\N{DEGREE SIGN}C", (1, 1)),
    _spec("compressor_air_temp_max_setpoint", "C_TIA_THR_TCHA_MAX_SP",
          "Compressor outlet temperature to start torque management",
          "\N{DEGREE SIGN}C", (1, 1)),
    _spec("cylinder_head_temp_setpoint", "CoTE_tHdCtlSp_M_VW",
          "Cylinder head temperature control setpoint",
          "\N{DEGREE SIGN}C", (6, 6)),

    # ---- pedal & torque request ------------------------------------------- #
    # This car's DCT family carries a `_VS_` (vehicle speed) infix SC8S50's does
    # not, and has no off-road pair at all — see UNAVAILABLE.
    _spec("pedal_dct_high", "IP_FAC_TQ_REQ_DRIV_H_VS_DCT",
          "Driver pedal torque request, high speed (DCT)", "-", (12, 12)),
    _spec("pedal_dct_low", "IP_FAC_TQ_REQ_DRIV_L_VS_DCT",
          "Driver pedal torque request, low speed (DCT)", "-", (12, 12)),
    _spec("pedal_dct_sport_high", "IP_FAC_TQ_REQ_DRIV_H_VS_DCT_S",
          "Driver pedal torque request, high speed (DCT, Sport)", "-", (12, 12)),
    _spec("pedal_dct_sport_low", "IP_FAC_TQ_REQ_DRIV_L_VS_DCT_S",
          "Driver pedal torque request, low speed (DCT, Sport)", "-", (12, 12)),
    _spec("pedal_drive_off", "IP_FAC_TQ_REQ_DRIV_DROF",
          "Driver torque request factor at drive off situation", "-", (8, 8)),

    # ---- ignition: intake-air-temperature correction ----------------------- #
    # Only the Basic half of SC8S50's Basic/Reference pair exists here, and the
    # two shared breakpoint axes are not separate tables in this XDF — see
    # UNAVAILABLE for both. The x axis IS a table, and re-breakpointing it still
    # moves every other IGA temperature-correction map scheduled on it, none of
    # which this profile maps.
    _spec("ignition_temp_correction_basic", "IP_IGA_BAS_TEMP_N_32",
          "Spark IAT correction — basis for temperature correction of Basic "
          "IGA versus N_32, TIA (timing offset vs engine speed and intake air "
          "temperature)", "\N{DEGREE SIGN}CRK", (10, 10)),
    _spec("ignition_temp_rpm_axis", "ldpm_n_32_5_igsp",
          "Basis for temperature correction of IGA versus N_32, TIA : x axis "
          "(engine speed), shared by the IGA temperature-correction tables",
          "rpm", (1, 10), frozenset({TAG_AXIS})),

    # Knock fast loop. All three carry the same shapes as SC8S50, and this XDF
    # titles them with the tuning guide's own names ("Initial Knock Correction",
    # "Cyl Strokes Between Knock Decay Steps", "Knock Correction Decay Amount"),
    # which is independent corroboration of the screenshot-to-symbol mapping
    # that was confirmed cell-by-cell on the other car. Sign convention as on
    # SC8S50: the correction is negative, so `knock_recovery_step` is the
    # amount returned per decay step, not extra retard.
    _spec("knock_retard", "IP_IGA_DEC_KNK",
          "Spark retard at recognised knocking, i.e. how much timing one "
          "detected knock event pulls (rpm x airmass). More negative is a "
          "deeper protective cut",
          "\N{DEGREE SIGN}CRK", (4, 8)),
    _spec("knock_recovery_delay", "IP_DLY_INC_FAST_KNK",
          "Number of segments between each increase of fast loop, i.e. cylinder "
          "strokes between knock decay steps (rpm). Lower recovers sooner",
          "segments", (1, 8)),
    _spec("knock_recovery_step", "IP_IGA_INC_KNK",
          "Increasing value of knock integrated correction when knock is "
          "detected, i.e. the timing returned per decay step (rpm x current "
          "correction). Positive; larger recovers faster",
          "\N{DEGREE SIGN}CRK", (4, 8)),
]

# ---- ignition: the nine VVL0 base grids ------------------------------------ #
# THE shape result of this port, and the reason per-car shape declarations exist
# at all. `IP_IGA_BAS_IVVT_VVL_PORT_L[STND][i][e]` is (16, 18) on this car and
# (16, 16) on SC8S50, under the *same symbol name*. Name-only resolution would
# have matched them and written a 16x16 grid into a 16x18 table, corrupting the
# calibration that follows it and producing a bin that flashes and runs wrong
# timing.
#
# So (16, 18) is declared here as a positive claim about this car, exactly the
# way (16, 16) is declared for the other one. It is not a relaxation and must
# never become one: a profile that declares the wrong shape still fails to
# resolve, which is what keeps the check meaningful now that two shapes are
# legitimate.
IGNITION_GRID_SHAPE = (16, 18)

for _i in range(3):
    for _e in range(3):
        _SPECS.append(_spec(
            f"ignition_base_vvl0_i{_i}_e{_e}",
            f"IP_IGA_BAS_IVVT_VVL_PORT_L[STND][{_i}][{_e}]",
            f"Basic ignition angle, VVL 0, intake cam {_i}, exhaust cam {_e}",
            "\N{DEGREE SIGN}CRK", IGNITION_GRID_SHAPE,
        ))


# --------------------------------------------------------------------------- #
# Table sets — the groupings a revision or a domain call thinks in
# --------------------------------------------------------------------------- #
#: Every base-timing logical name, in the order the ECU's cam grid runs.
#: The knock fast loop, in the order it acts. Same three tables and same
#: rationale as SC8S50; `knock_retard_max` is unavailable here.
KNOCK_CONTROL = ("knock_retard", "knock_recovery_delay", "knock_recovery_step")

IGNITION_BASE_VVL0 = tuple(
    f"ignition_base_vvl0_i{i}_e{e}" for i in range(3) for e in range(3)
)

#: The three basic lambda grids. Unlike SC8S50's, they carry no separately
#: editable shared axes on this car — see ``UNAVAILABLE``.
LAMBDA_FAMILY = ("lambda_basic", "lambda_basic_hpdi", "lambda_basic_mpi")

#: The three lambda minimum-value floors.
LAMBDA_FLOORS = ("lambda_setpoint_min", "lambda_catalyst_min", "lambda_turbo_min")

#: The lambda full-load enrichment set: the owned main map, then the
#: grid-editable IAT variant and its threshold/hysteresis pair.
LAMBDA_FULL_LOAD = (
    "lambda_full_load",
    "lambda_full_load_iat",
    "lambda_full_load_iat_threshold",
    "lambda_full_load_iat_hysteresis",
)

#: The two wastegate feedforward maps, which must always be edited together.
WASTEGATE_MAPS = ("wastegate_feedforward_vvl0", "wastegate_feedforward_vvl1")

#: The four standstill rev caps — one per transmission type, written as one set.
STATIC_REV_LIMIT = (
    "static_rev_limit_mt", "static_rev_limit_at",
    "static_rev_limit_dct", "static_rev_limit_cvt",
)

#: The engine's own rev limiter, per valve-lift mode. Readable, never written.
ENGINE_SPEED_LIMIT = ("engine_speed_limit_vvl0", "engine_speed_limit_vvl1")

#: The four road-speed limiter scalars — four tables holding one number.
SPEED_LIMITER = (
    "speed_limiter_level1", "speed_limiter_level2",
    "speed_limiter_level3", "speed_limiter_inactive",
)

#: The turbocharger protection ceilings, each as a (hard limit, setpoint) pair.
TURBO_PROTECTION = (
    "turbo_speed_max", "turbo_speed_max_setpoint",
    "compressor_air_temp_max", "compressor_air_temp_max_setpoint",
)

#: The DCT (DSG) driver-interpretation pedal maps, primary pair first, plus the
#: drive-off factor. Shorter than SC8S50's by the off-road pair, which this car
#: does not have. The MT/AT families are deliberately unmapped — this profile
#: describes a DCT car, and they are dead tables for it.
PEDAL_MAPS = (
    "pedal_dct_high", "pedal_dct_low",
    "pedal_dct_sport_high", "pedal_dct_sport_low",
    "pedal_drive_off",
)


# --------------------------------------------------------------------------- #
# Declared gaps — logical names this car does not have
# --------------------------------------------------------------------------- #
#: Why each is stated rather than simply left out: see :attr:`Profile.unavailable`.
#:
#: Two different causes are represented, and the wording distinguishes them
#: because they have different fixes. Five tables are **absent from the
#: calibration** — nothing addresses them, and no XDF for this car could. Five
#: are **absent from this XDF**: the data is in the bin, embedded in the map
#: that uses it, but ``SCGa05_cal.xdf`` declares no standalone table for it, so
#: there is nothing to bind a spec to. The second group would come back with a
#: better definition file; the first would not.
UNAVAILABLE: dict[str, str] = {
    # -- absent from the calibration -- #
    "airmass_full_load": (
        "no `C_M_AIR_CYL_FL` — Airmass per cylinder at full load, and no "
        "`*_M_AIR_CYL_*_FL` variant of any spelling, exists in this "
        "calibration. SC8S50 maps it with no verified write path anyway "
        "(its units are unconfirmed there), so nothing is lost by its absence"
    ),
    "static_rev_fuel_cut_offset": (
        "no `C_N_MAX_FCUT_OFS_VST` — Engine speed offset for activation of "
        "fuel cut-off at all cylinders in case of stopped vehicle. This car "
        "declares only `C_N_MAX_FCUT_OFS` — the same offset for the *moving* "
        "case, which is a different table and is not substituted for it"
    ),
    "ignition_temp_correction_reference": (
        "no `IP_IGA_REF_TEMP_N_32` — Basis for temperature correction of "
        "Reference IGA versus N_32, TIA. Only the Basic half of the pair "
        "(`IP_IGA_BAS_TEMP_N_32`) exists here, and it is mapped"
    ),
    "knock_retard_max": (
        "no `IP_IGA_MAX_KNK` — Maximum value for spark retard. The other three "
        "knock fast-loop tables are declared by this XDF and are mapped; the "
        "backstop on total accumulated retard is not, so there is nothing to "
        "read it against here"
    ),
    "pedal_dct_offroad_high": (
        "no `IP_FAC_TQ_REQ_DRIV_H_OFRD_DCT` — Driver pedal torque request, "
        "high speed, off-road (DCT). This car's DCT pedal family is normal / "
        "Sport / reverse / drive-off with no off-road pair"
    ),
    "pedal_dct_offroad_low": (
        "no `IP_FAC_TQ_REQ_DRIV_L_OFRD_DCT` — Driver pedal torque request, "
        "low speed, off-road (DCT). Same family, same absence"
    ),
    # -- present in the bin, not declared as a table by this XDF -- #
    "lambda_rpm_axis": (
        "the basic lambda setpoint's engine-speed breakpoints exist in the bin "
        "(embedded in `IP_LAMB_BAS[1]` and its HPDI/MPI siblings) but "
        "`SCGa05_cal.xdf` declares no standalone axis table for them — there "
        "is no symbol, title, or uniqueid to bind, so the axis is readable "
        "through the grids and not separately editable"
    ),
    "lambda_load_axis": (
        "the basic lambda setpoint's airmass-load breakpoints, same as the "
        "rpm axis above: embedded in the three grids, declared as no table"
    ),
    "ignition_temp_iat_axis": (
        "the IGA temperature correction's intake-air-temperature breakpoints "
        "are embedded in `IP_IGA_BAS_TEMP_N_32` and declared as no standalone "
        "table. The x (engine speed) axis of the same map IS declared and is "
        "mapped as `ignition_temp_rpm_axis`"
    ),
    "cylinder_head_temp_rpm_axis": (
        "the cylinder head temperature setpoint's engine-speed breakpoints are "
        "embedded in `CoTE_tHdCtlSp_M_VW` and declared as no standalone table; "
        "SC8S50's XDF exposes them as `DATA_ThmMng.CoTE_nEng_A_VW`"
    ),
    "cylinder_head_temp_charge_axis": (
        "the cylinder head temperature setpoint's relative-charge breakpoints, "
        "same as its rpm axis above; SC8S50's XDF exposes them as "
        "`DATA_ThmMng.CoTE_rChRel_A_VW`"
    ),
}


# --------------------------------------------------------------------------- #
# Domain groups — what each table is *for*
# --------------------------------------------------------------------------- #
# The same headings and the same filing decisions as SC8S50, because what a
# table is *for* is a property of the logical name and not of the car. Written
# out rather than imported: this block is the reviewable statement that nothing
# is mis-filed here, and borrowing the other profile's would make a change to
# its classification silently restate itself as a claim about this car.
# ``tests/test_foreign_structure.py`` pins that the two agree wherever they both
# declare a name, which catches drift without creating the coupling.
_GROUPS: dict[str, tuple[str, ...]] = {
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
        "wastegate_intake_flow_axis",
        "charge_air_pressure_max_diag",
        "charge_air_diag_put_axis",
        "charge_air_diag_rpm_axis",
    ),
    GROUP_TIMING: IGNITION_BASE_VVL0 + KNOCK_CONTROL + (
        "ignition_temp_correction_basic",
        "ignition_temp_rpm_axis",
    ),
    GROUP_FUELING: LAMBDA_FAMILY + LAMBDA_FLOORS + LAMBDA_FULL_LOAD + (
        "pedal_threshold_full_load",
    ),
    GROUP_AIRFLOW: (
        "airmass_setpoint_max",
        "intake_air_max_vvl0",
        "intake_air_max_vvl1",
    ),
    GROUP_LIMITERS: (
        "torque_reference_max",
    ) + STATIC_REV_LIMIT + ENGINE_SPEED_LIMIT + SPEED_LIMITER,
    GROUP_TURBO_THERMAL: TURBO_PROTECTION + ("cylinder_head_temp_setpoint",),
    GROUP_PEDAL_TORQUE: PEDAL_MAPS,
}

_SPECS = apply_groups("SCGA05", _SPECS, _GROUPS)


#: The table sets a domain call writes as one, keyed by the id the domain names.
#:
#: Declared for this car, not imported from :mod:`.sc8s50`, for the same reason
#: the groups below are written out: a set is a claim about which tables this
#: ECU holds and in what order they should be written, and borrowing the other
#: car's tuple would restate its claim as this car's.
#:
#: Two sets are shorter here than on SC8S50, and one is absent:
#:
#: * ``lambda_family`` has no shared-axis siblings to re-breakpoint (the axes
#:   are embedded — see :data:`UNAVAILABLE`), so the axes are not part of it;
#: * ``cylinder_head_temp`` is the setpoint alone, its two axes being embedded;
#: * ``static_rev_limit_active`` is **not declared**. Which of the four
#:   standstill caps this ECU resolves has not been established on this car, and
#:   the journal says nothing rather than repeating SC8S50's DCT answer. All
#:   four are still written — that is the method, and it is what makes the
#:   unknown harmless.
TABLE_SETS: dict[str, tuple[str, ...]] = {
    "ignition_base_vvl0": IGNITION_BASE_VVL0,
    "knock_control": KNOCK_CONTROL,
    "ignition_temp_correction": (
        "ignition_temp_correction_basic",
        "ignition_temp_rpm_axis",
    ),
    "lambda_family": LAMBDA_FAMILY,
    "lambda_floors": LAMBDA_FLOORS,
    "lambda_full_load": LAMBDA_FULL_LOAD,
    "wastegate_maps": WASTEGATE_MAPS,
    "speed_limiter": SPEED_LIMITER,
    "static_rev_limit": STATIC_REV_LIMIT,
    "engine_speed_limit": ENGINE_SPEED_LIMIT,
    "turbo_protection": TURBO_PROTECTION,
    "pedal_maps": PEDAL_MAPS,
    "charge_air_diag": (
        "charge_air_pressure_max_diag",
        "charge_air_diag_put_axis",
        "charge_air_diag_rpm_axis",
    ),
    "cylinder_head_temp": ("cylinder_head_temp_setpoint",),
}


# --------------------------------------------------------------------------- #
# Pinned layouts
# --------------------------------------------------------------------------- #
# Where each mapped table *is* in `SCGa05_cal.xdf`, and how its bytes decode,
# as of the review that authored this map. Generated — never hand-edited — by
#
#     python -m simoscal.tune.profiles pin SCGA05 \
#         xdf/SCGa05_cal.xdf bin/3CN906259B__0002_SCGA05.bin
#
# Resolution matches a spec by symbol and by exact shape, and a definition file
# can satisfy both while placing a table four bytes further along or reading it
# as a different type. Every gate downstream of resolution — the edit journal,
# the final-bin readback, the byte-level audit — is computed *through* this same
# file, so all three would agree with the moved table and the build would come
# back verified (CR-20260828-02). These digests are the one check that compares
# the file against something outside it.
#
# Regenerate only from a definition file a human has reviewed, and say in the
# commit message what changed and why. A digest that moves without a reason is
# the finding, not the noise.
_TABLE_LAYOUTS = {
    "airmass_setpoint_max":             "e9a637ebf85047de",
    "charge_air_diag_put_axis":         "d13ec094a61caeee",
    "charge_air_diag_rpm_axis":         "2363777c6cb1335d",
    "charge_air_pressure_max_diag":     "796e1a483de04aae",
    "compressor_air_temp_max":          "9f504f594a524a68",
    "compressor_air_temp_max_setpoint": "20fed2f47301e4f6",
    "cylinder_head_temp_setpoint":      "6148d6b023c285ac",
    "engine_speed_limit_vvl0":          "660737e8f5e6f44d",
    "engine_speed_limit_vvl1":          "6dc3809fb1fabb52",
    "ignition_base_vvl0_i0_e0":         "baf0fce137e3ff97",
    "ignition_base_vvl0_i0_e1":         "85dd1f1f52a4afae",
    "ignition_base_vvl0_i0_e2":         "d85dd279f366d20b",
    "ignition_base_vvl0_i1_e0":         "2c09b7dc7044e4cc",
    "ignition_base_vvl0_i1_e1":         "36e36a21b90701da",
    "ignition_base_vvl0_i1_e2":         "d4f406aaa7df0cff",
    "ignition_base_vvl0_i2_e0":         "e971e6a3cf876966",
    "ignition_base_vvl0_i2_e1":         "3a9546cb1ddd709e",
    "ignition_base_vvl0_i2_e2":         "9fe2aaa9da5e8765",
    "ignition_temp_correction_basic":   "876443f00f8ce0d3",
    "ignition_temp_rpm_axis":           "7c404651544ac1c2",
    "intake_air_max_vvl0":              "b2f5c7cf00b29b58",
    "intake_air_max_vvl1":              "01f849b0a9827865",
    "knock_recovery_delay":             "01d16e738be835d9",
    "knock_recovery_step":              "a097688ae504a57c",
    "knock_retard":                     "0af0fb248a57a05a",
    "lambda_basic":                     "406ff2a60b3af418",
    "lambda_basic_hpdi":                "535c1667421854f3",
    "lambda_basic_mpi":                 "e5228a0c93395c75",
    "lambda_catalyst_min":              "e8767b0bd16d9fe6",
    "lambda_full_load":                 "7547d823494a2ede",
    "lambda_full_load_iat":             "a1282490769831a0",
    "lambda_full_load_iat_hysteresis":  "63bb36503ee88dd1",
    "lambda_full_load_iat_threshold":   "f04955bfc21a4a8d",
    "lambda_setpoint_min":              "f30f2367ca376e16",
    "lambda_turbo_min":                 "0b67b90035329780",
    "manifold_pressure_limit_offset":   "81209b2e5f1c7467",
    "manifold_pressure_max":            "61d2caeebe0a9481",
    "overboost_threshold":              "f775846872a84e1a",
    "pedal_dct_high":                   "370f7d9cb47e5287",
    "pedal_dct_low":                    "e9a57873dc64db87",
    "pedal_dct_sport_high":             "8359738bf6276b8b",
    "pedal_dct_sport_low":              "9d28ddad0d18249b",
    "pedal_drive_off":                  "d3cc933a992623e3",
    "pedal_threshold_full_load":        "7d2e3ff350288c54",
    "pressure_quotient_max":            "982b7b9b827442d2",
    "put_from_ambient_enable":          "65de85f74f27786c",
    "put_setpoint":                     "35f27adbbde002ea",
    "put_setpoint_map_axis":            "99be016ee748a12a",
    "put_setpoint_rpm_axis":            "a2a074b602acae2c",
    "speed_limiter_inactive":           "4ac4f7399098324b",
    "speed_limiter_level1":             "63bb4c6b4eb76357",
    "speed_limiter_level2":             "89a7c6a0162aa2f3",
    "speed_limiter_level3":             "6ac0931199b54cd1",
    "static_rev_limit_at":              "a7590045a927434a",
    "static_rev_limit_cvt":             "407da39ab6747caf",
    "static_rev_limit_dct":             "0fd63635b3418982",
    "static_rev_limit_mt":              "aeb8e9e7c0244a2f",
    "torque_reference_max":             "a65b8c5d89a30cf1",
    "turbo_speed_max":                  "85929bf45bde10b4",
    "turbo_speed_max_setpoint":         "306206a7b97245de",
    "wastegate_exh_flow_axis":          "92919845f71ca366",
    "wastegate_feedforward_vvl0":       "a5432060269e49c9",
    "wastegate_feedforward_vvl1":       "57c1be56f7c0242b",
    "wastegate_intake_flow_axis":       "8dfcde53cd8b3077",
}


SCGA05 = Profile(
    name="SCGA05",
    xdf="SCGa05_cal.xdf",
    specs={s.name: s for s in _SPECS},
    structure=SCGA05_STRUCTURE,
    # Deliberately none. Nobody has measured what stock reads on this car for
    # the purpose of advising a change, and the guidance strings render without
    # their comparison clause rather than quoting a 5G0906259L figure at a
    # 3CN906259B owner. Silence is the correct output here; see
    # `Profile.stock_references`.
    stock_references={},
    table_sets=TABLE_SETS,
    unavailable=UNAVAILABLE,
    # `SCGa05_cal.xdf` is written against the extracted calibration block, not
    # the whole bin — the `_cal` in its name — so it declares BASEOFFSET 0 and
    # every address is relative to the CAL block at 0x220000. Measured, not
    # assumed: rebased by 0x220000, 214 of 270 candidate breakpoint axes read
    # strictly monotonic where 3 do at the declared base, and
    # `C_PRS_IM_SP_LIM` — Maximum requested intake-manifold pressure setpoint
    # reads 271696.0 as float32 against 0. Every address in the file
    # (0xad4..0x8f8c3) also fits inside the 0x9FC00 block, which is the
    # structural signature of this convention.
    xdf_addresses_cal_relative=True,
    table_layouts=_TABLE_LAYOUTS,
)

__all__ = [
    "SCGA05",
    "TABLE_SETS",
    "UNAVAILABLE",
    "IGNITION_GRID_SHAPE",
    "KNOCK_CONTROL",
    "IGNITION_BASE_VVL0",
    "LAMBDA_FAMILY",
    "LAMBDA_FLOORS",
    "LAMBDA_FULL_LOAD",
    "PEDAL_MAPS",
    "SPEED_LIMITER",
    "STATIC_REV_LIMIT",
    "ENGINE_SPEED_LIMIT",
    "TURBO_PROTECTION",
    "WASTEGATE_MAPS",
]
