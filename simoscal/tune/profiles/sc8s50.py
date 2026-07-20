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

from ..profile import TAG_FLOAT_BUG, TAG_KG_PER_STROKE, Profile, TableSpec


def _spec(name, key, description, units="", shape=None, tags=frozenset()):
    return TableSpec(
        name=name, key=key, description=description,
        units=units, shape=shape, tags=tags,
    )


_SPECS = [
    # ---- boost ------------------------------------------------------------ #
    _spec("put_setpoint", "IP_PUT_SP",
          "Pressure up throttle setpoint", "hPa", (4, 6)),
    _spec("put_setpoint_rpm_axis", "ldp_n_ip_put_sp",
          "Pressure up throttle setpoint : x axis (engine speed)", "rpm", (1, 6)),
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
    # raises the ceiling ~1.44 million-fold, i.e. removes the limiter.
    _spec("airmass_setpoint_max", "C_M_AIR_CYL_SP_MAX",
          "Maximum allowed M_AIR_CYL_SP (maximum allowed airmass setpoint)",
          "mg/stk", (1, 1), frozenset({TAG_KG_PER_STROKE, TAG_FLOAT_BUG})),
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
          "(exhaust flow factor), shared by VVL 0 and VVL 1", "-", (1, 16)),

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
          "BAS/HPDI/MPI", "rpm", (1, 12)),
    _spec("lambda_load_axis", "ldpm_maf_1_lasp",
          "Basic lambda setpoint : y axis (airmass load), shared by "
          "BAS/HPDI/MPI", "mg/stk", (1, 8)),
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

#: Every base-timing logical name, in the order the ECU's cam grid runs.
IGNITION_BASE_VVL0 = tuple(
    f"ignition_base_vvl0_i{i}_e{e}" for i in range(3) for e in range(3)
)

#: The three lambda grids that share ``lambda_rpm_axis`` / ``lambda_load_axis``.
LAMBDA_FAMILY = ("lambda_basic", "lambda_basic_hpdi", "lambda_basic_mpi")

#: The two wastegate feedforward maps, which must always be edited together.
WASTEGATE_MAPS = ("wastegate_feedforward_vvl0", "wastegate_feedforward_vvl1")

#: The three lambda minimum-value floors the basics guide sets to 0.80.
LAMBDA_FLOORS = ("lambda_setpoint_min", "lambda_catalyst_min", "lambda_turbo_min")


SC8S50 = Profile(
    name="SC8S50",
    xdf="SC8S50.V1.0.xdf",
    specs={s.name: s for s in _SPECS},
)

__all__ = [
    "SC8S50",
    "IGNITION_BASE_VVL0",
    "LAMBDA_FAMILY",
    "LAMBDA_FLOORS",
    "WASTEGATE_MAPS",
]
