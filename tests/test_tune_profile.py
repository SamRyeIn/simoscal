"""Unit tests for :mod:`simoscal.tune.profile` — logical names → XDF tables (U1).

Edge cases (unknown name, ambiguity, shape mismatch, suggestions) run on the
hand-written ``mini.xdf`` fixture; the integration checks that the shipped
SC8S50 and switch-patch maps actually resolve run against the real XDFs and
skip cleanly when those files are absent from a lean checkout.
"""

from __future__ import annotations

import struct
from pathlib import Path

import numpy as np
import pytest

from simoscal import CalFile, parse_xdf
from simoscal.binimage import BinImage
from simoscal.tune import profile as prof
from simoscal.tune.profile import (
    Profile,
    ProfileResolutionError,
    TableSpec,
    resolve,
)
from simoscal.tune.profiles import PROFILES, SC8S50, SCGA05, SWITCH_PATCH_2933
from simoscal.tune.profiles import sc8s50 as sc_map
from simoscal.tune.profiles import switchpatch_2933 as sp_map
from simoscal.checksum import SC8S50_STRUCTURE

FIXTURES = Path(__file__).parent / "fixtures"
MINI_XDF = FIXTURES / "mini.xdf"


@pytest.fixture
def mini_cal() -> CalFile:
    """A tiny CalFile over ``mini.xdf`` with decodable bytes.

    Provides ``SYM_10X10`` (happy resolve), ``SYM_DUP`` (ambiguous) and lets a
    bogus name exercise the missing path — none of which needs the real bin.
    """
    model = parse_xdf(str(MINI_XDF))
    buf = bytearray(model.base_offset + 0x6000)
    off = model.base_offset + 0x1000
    buf[off : off + 200] = struct.pack("<100h", *range(100))
    image = BinImage(
        bytes(buf), region_start=model.region_start, region_size=model.region_size
    )
    return CalFile(model, image, structure=SC8S50_STRUCTURE)


def _mini_profile(**specs: TableSpec) -> Profile:
    return Profile(name="Mini", xdf="mini.xdf", specs=specs)


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #
def test_resolves_a_mapped_name_to_its_table(mini_cal: CalFile) -> None:
    p = _mini_profile(
        grid=TableSpec("grid", "SYM_10X10", "Ten by ten test grid", "%", (10, 10))
    )
    resolved = resolve(p, mini_cal)

    assert resolved.names() == ["grid"]
    assert resolved["grid"].view.symbol == "SYM_10X10"
    assert resolved["grid"].label == "`SYM_10X10` — Ten by ten test grid"
    assert resolved["grid"].units == "%"


def test_unmapped_name_falls_back_to_an_exact_symbol(mini_cal: CalFile) -> None:
    """The escape hatch: reach a table the map doesn't name, exactly."""
    resolved = resolve(_mini_profile(), mini_cal, names=["SYM_10X10"])

    assert resolved["SYM_10X10"].view.uniqueid == 0x100
    # It borrows the XDF title, since the map carries no description for it.
    assert resolved["SYM_10X10"].spec.description == "Ten by Ten"


def test_resolve_can_be_limited_to_named_subset(mini_cal: CalFile) -> None:
    p = _mini_profile(
        grid=TableSpec("grid", "SYM_10X10", "Ten by ten test grid"),
        gone=TableSpec("gone", "NO_SUCH_SYMBOL", "Not in this XDF"),
    )
    # Resolving everything would fail on 'gone'; resolving the subset must not.
    assert resolve(p, mini_cal, names=["grid"]).names() == ["grid"]


# --------------------------------------------------------------------------- #
# Fail-loud paths (AE3)
# --------------------------------------------------------------------------- #
def test_unknown_name_raises_naming_it(mini_cal: CalFile) -> None:
    p = _mini_profile(
        ghost=TableSpec("ghost", "NO_SUCH_SYMBOL", "A table this XDF lacks")
    )
    with pytest.raises(ProfileResolutionError) as excinfo:
        resolve(p, mini_cal)

    assert [m.name for m in excinfo.value.misses] == ["ghost"]
    text = str(excinfo.value)
    assert "ghost" in text and "NO_SUCH_SYMBOL" in text
    assert "mini.xdf" in text


def test_every_miss_is_reported_not_just_the_first(mini_cal: CalFile) -> None:
    p = _mini_profile(
        a=TableSpec("a", "MISSING_A", "First gap"),
        ok=TableSpec("ok", "SYM_10X10", "Present"),
        b=TableSpec("b", "MISSING_B", "Second gap"),
    )
    with pytest.raises(ProfileResolutionError) as excinfo:
        resolve(p, mini_cal)

    assert sorted(m.name for m in excinfo.value.misses) == ["a", "b"]


def test_ambiguous_symbol_is_an_error_never_a_guess(mini_cal: CalFile) -> None:
    p = _mini_profile(dup=TableSpec("dup", "SYM_DUP", "Two tables share this"))
    with pytest.raises(ProfileResolutionError) as excinfo:
        resolve(p, mini_cal)

    (miss,) = excinfo.value.misses
    assert "ambiguous" in miss.reason
    # The candidate uniqueids are named so a human can bind one deliberately.
    assert "0x300" in miss.reason and "0x400" in miss.reason


def test_shape_mismatch_refuses_the_binding(mini_cal: CalFile) -> None:
    p = _mini_profile(
        grid=TableSpec("grid", "SYM_10X10", "Ten by ten", shape=(8, 12))
    )
    with pytest.raises(ProfileResolutionError) as excinfo:
        resolve(p, mini_cal)

    (miss,) = excinfo.value.misses
    assert "(10, 10)" in miss.reason and "(8, 12)" in miss.reason


def test_suggestions_surface_a_near_title(mini_cal: CalFile) -> None:
    """A renamed symbol should point the reader at plausible neighbours."""
    p = _mini_profile(dup=TableSpec("dup", "SYM_DUPLICATE", "Renamed variant"))
    with pytest.raises(ProfileResolutionError) as excinfo:
        resolve(p, mini_cal)

    (miss,) = excinfo.value.misses
    assert any("SYM_DUP" in s for s in miss.suggestions)
    assert "did you mean" in str(excinfo.value)


def test_no_bin_write_can_precede_resolution(mini_cal: CalFile) -> None:
    """Resolution failure leaves the image byte-identical (nothing staged)."""
    before = mini_cal.binimage.to_bytes()
    with pytest.raises(ProfileResolutionError):
        resolve(_mini_profile(x=TableSpec("x", "NOPE", "gone")), mini_cal)

    assert mini_cal.binimage.to_bytes() == before
    assert not mini_cal.edited


# --------------------------------------------------------------------------- #
# Profile mechanics
# --------------------------------------------------------------------------- #
def test_profile_rejects_a_key_name_mismatch() -> None:
    with pytest.raises(ValueError, match="does not match"):
        Profile(name="Bad", xdf="x.xdf",
                specs={"wrong": TableSpec("right", "SYM", "desc")})


def test_merged_profiles_reject_overlapping_names() -> None:
    a = _mini_profile(shared=TableSpec("shared", "SYM_A", "A"))
    b = Profile(name="Other", xdf="o.xdf",
                specs={"shared": TableSpec("shared", "SYM_B", "B")})
    with pytest.raises(ValueError, match="both define shared"):
        a.merged_with(b)


def test_merged_profile_is_the_union() -> None:
    a = _mini_profile(one=TableSpec("one", "SYM_A", "A"))
    b = Profile(name="Other", xdf="o.xdf",
                specs={"two": TableSpec("two", "SYM_B", "B")})
    merged = a.merged_with(b)

    assert merged.names() == ["one", "two"]
    assert merged.name == "Mini+Other"


# --------------------------------------------------------------------------- #
# Per-car facts on the profile (U3)
# --------------------------------------------------------------------------- #
def test_float_bug_symbols_are_derived_from_the_tagged_specs() -> None:
    """The tag on the spec is the only place a table is flagged.

    Declaring the set a second time beside the specs is what let
    ``safety.FLOAT_BUG_SYMBOLS`` drift into naming a symbol no spec tagged.
    """
    p = _mini_profile(
        flagged=TableSpec("flagged", "SYM_A", "A",
                          tags=frozenset({prof.TAG_FLOAT_BUG})),
        plain=TableSpec("plain", "SYM_B", "B"),
    )
    assert p.float_bug_symbols == frozenset({"SYM_A"})


def test_float_bug_symbols_skip_uniqueid_keyed_specs() -> None:
    """A patch-added table has no symbol for the guard to match on."""
    p = _mini_profile(
        by_id=TableSpec("by_id", 0x7D41A, "no symbol",
                        tags=frozenset({prof.TAG_FLOAT_BUG})),
    )
    assert p.float_bug_symbols == frozenset()


def test_a_profile_flagging_nothing_has_an_empty_set() -> None:
    """Not an error and not a missing answer — some cars flag nothing."""
    assert _mini_profile(plain=TableSpec("plain", "SYM", "d")).float_bug_symbols == (
        frozenset()
    )


def test_sc8s50_flags_the_four_float32_ceilings() -> None:
    """The exact set the deleted ``safety.FLOAT_BUG_SYMBOLS`` global carried.

    Pinned by symbol rather than by count so that dropping one — the way
    `C_PRS_IM_SP_LIM` — Offset to the pressure behind the air cleaner for the
    limitation of the manifold setpoint was never tagged before U3 — fails here.
    """
    assert SC8S50.float_bug_symbols == frozenset({
        "C_M_AIR_CYL_FL",
        "C_M_AIR_CYL_SP_MAX",
        "C_PRS_IM_SP_LIM",
        "C_PRS_IM_SP_MAX",
    })


def test_sc8s50_carries_its_own_cal_structure() -> None:
    assert SC8S50.structure is SC8S50_STRUCTURE


def test_merged_profile_inherits_the_declared_structure() -> None:
    """The patch profile declares none and takes the base profile's."""
    base = Profile(name="Base", xdf="b.xdf", structure=SC8S50_STRUCTURE)
    patch = Profile(name="Patch", xdf="p.xdf")
    assert base.merged_with(patch).structure is SC8S50_STRUCTURE
    assert patch.merged_with(base).structure is SC8S50_STRUCTURE


def test_merging_profiles_with_different_structures_raises() -> None:
    """Two profiles over one bin cannot disagree about where its CAL block is."""
    from dataclasses import replace

    other = replace(SC8S50_STRUCTURE, name="A05", cal_file_offset=0x220000)
    a = Profile(name="A", xdf="a.xdf", structure=SC8S50_STRUCTURE)
    b = Profile(name="B", xdf="b.xdf", structure=other)
    with pytest.raises(ValueError, match="different CAL structures"):
        a.merged_with(b)


def test_merging_profiles_with_conflicting_stock_references_raises() -> None:
    a = Profile(name="A", xdf="a.xdf", stock_references={"lambda_floors": "one"})
    b = Profile(name="B", xdf="b.xdf", stock_references={"lambda_floors": "two"})
    with pytest.raises(ValueError, match="different stock references"):
        a.merged_with(b)


#: Text that only makes sense for one car. A module-level binding whose *code*
#: contains one of these is a per-car fact stored where a second calibration
#: cannot override it — the class of defect this whole effort removes.
_PER_CAR_MARKERS = ("5G0906259L", "FLOAT_BUG_SYMBOLS")

#: Where per-car facts are allowed to live: the profile modules themselves.
_PROFILES_PKG = "simoscal/tune/profiles/"


def test_no_per_car_constant_lives_outside_the_profiles_package() -> None:
    """Per-car facts belong to a profile, not to a module that imports one.

    Checked over the *code* of module-level bindings via ``ast.unparse``, not the
    raw text, so a docstring or comment explaining why a symbol matters — as
    ``safety.py`` and ``checksum.py`` both now do — is not a violation. Only an
    actual constant is.
    """
    import ast

    code_root = Path(__file__).resolve().parents[1]
    offenders: list[str] = []
    for path in sorted((code_root / "simoscal").rglob("*.py")):
        rel = path.relative_to(code_root).as_posix()
        if rel.startswith(_PROFILES_PKG):
            continue
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            src = ast.unparse(node)
            for marker in _PER_CAR_MARKERS:
                if marker in src:
                    offenders.append(f"{rel}:{node.lineno} names {marker!r}")

    assert not offenders, (
        "per-car constants outside " + _PROFILES_PKG + ":\n  " +
        "\n  ".join(offenders)
    )


def test_profile_getitem_lists_known_names_on_a_miss() -> None:
    p = _mini_profile(known=TableSpec("known", "SYM", "desc"))
    with pytest.raises(KeyError, match="known"):
        p["unknown"]


# --------------------------------------------------------------------------- #
# The shipped maps
# --------------------------------------------------------------------------- #
def test_shipped_profiles_are_registered() -> None:
    assert PROFILES == {
        "SC8S50": SC8S50,
        "SCGA05": SCGA05,
        "SwitchPatch2933": SWITCH_PATCH_2933,
    }


def test_sc8s50_map_covers_the_whole_r00_r12_lineage() -> None:
    """Every base-calibration symbol the frozen lineage touches has a name.

    The list is the union of the tables written by R00–R12; the new domain
    modules may only reach tables through the map, so a gap here is a gap in
    what a new-style revision can express.
    """
    lineage = {
        # R00/R03 lambda family + axes
        "ldpm_n_32_1_lasp", "ldpm_maf_1_lasp",
        "IP_LAMB_BAS[1]", "IP_LAMB_BAS_HPDI[1]", "IP_LAMB_BAS_MPI[1]",
        # R01/R03 limiter + fuelling writes
        "ID_PV_AV_FL", "C_PRS_IM_SP_MAX",
        "IP_M_AIR_CYL_MAX_STND_VVL[STND]", "IP_M_AIR_CYL_MAX_STND_VVL[LFT_1]",
        "IP_TQI_REF_MAX_MON", "C_M_AIR_CYL_SP_MAX",
        # R03 lambda floors
        "C_LAMB_BAS_COR_MIN", "IP_LAMB_COP_MIN", "IP_LAMB_TUR_OHP_MIN",
        # R04 timing overlay (all nine cam-position grids)
        *(f"IP_IGA_BAS_IVVT_VVL_PORT_L[STND][{i}][{e}]"
          for i in range(3) for e in range(3)),
        # R05/R08 wastegate feedforward + its shared axis
        "IP_FAC_BPA_SP[0]", "IP_FAC_BPA_SP[1]", "ldp_fac_1_ip_fac_bpa_sp",
        # R06 overboost threshold, R09/R11 PUT, R10 compressor cap
        "IP_PUT_AMP_DIF_MAX_PRS_DIF_THR", "IP_PUT_SP", "ldp_n_ip_put_sp",
        "IP_PQ_CHA_MAX",
    }
    mapped = {spec.key for spec in SC8S50.specs.values()}
    assert not lineage - mapped, f"unmapped lineage symbols: {sorted(lineage - mapped)}"


def test_airmass_cap_carries_the_kg_per_stroke_tag() -> None:
    """The one table whose physical value must never be written raw."""
    spec = SC8S50["airmass_setpoint_max"]
    assert spec.key == "C_M_AIR_CYL_SP_MAX"
    assert spec.has(prof.TAG_KG_PER_STROKE)
    # Its mg/stk neighbours are genuine mg/stk and must NOT carry the tag.
    for name in ("intake_air_max_vvl0", "intake_air_max_vvl1"):
        assert not SC8S50[name].has(prof.TAG_KG_PER_STROKE)


def test_switch_patch_map_binds_every_slot_by_uniqueid() -> None:
    for kind in ("put_setpoint", "enable_sl_tc", "disable_oem_tc"):
        names = sp_map.slot_names(kind)
        assert len(names) == 5
        for name in names:
            spec = SWITCH_PATCH_2933[name]
            # Patch-added tables have no symbol; binding must be an address.
            assert spec.has(prof.TAG_NO_SYMBOL)
            assert isinstance(spec.key, str) and spec.key.startswith("0x")


def test_lineage_helper_tuples_name_real_entries() -> None:
    for name in (*sc_map.IGNITION_BASE_VVL0, *sc_map.IGNITION_TEMP_CORRECTION,
                 *sc_map.LAMBDA_FAMILY, *sc_map.LAMBDA_FLOORS,
                 *sc_map.WASTEGATE_MAPS, *sc_map.TURBO_PROTECTION,
                 *sc_map.SPEED_LIMITER, *sc_map.CHARGE_AIR_DIAG,
                 *sc_map.CYLINDER_HEAD_TEMP, *sc_map.PEDAL_MAPS,
                 *sc_map.LAMBDA_FULL_LOAD):
        assert name in SC8S50


def test_iat_timing_correction_pair_is_mapped_with_its_shared_axes() -> None:
    """The spark-vs-IAT tables, mapped as a pair with both shared axes.

    Basic and Reference are separate corrections that share *both* breakpoint
    axes, so a re-breakpoint of either axis moves both grids (and, on the rpm
    axis, eight further IGA correction tables this profile does not map). The
    axes are tagged so a generic write must keep them strictly increasing.
    """
    basic = SC8S50["ignition_temp_correction_basic"]
    reference = SC8S50["ignition_temp_correction_reference"]
    assert basic.key == "IP_IGA_BAS_TEMP_N_32"
    assert reference.key == "IP_IGA_REF_TEMP_N_32"
    for spec in (basic, reference):
        assert spec.shape == (10, 10)
        assert not spec.domain_owned, "the generic grid editor must reach these"
        assert not spec.has(prof.TAG_AXIS)

    rpm_axis = SC8S50["ignition_temp_rpm_axis"]
    iat_axis = SC8S50["ignition_temp_iat_axis"]
    assert rpm_axis.key == "ldpm_n_32_5_igsp"
    assert iat_axis.key == "ldpm_tia_iga_cor_sel"
    for spec in (rpm_axis, iat_axis):
        assert spec.shape == (1, 10)
        assert spec.has(prof.TAG_AXIS)
    assert iat_axis.units == "\N{DEGREE SIGN}C"


def test_every_basics_sop_write_target_is_mapped() -> None:
    """Every table the Tuning Basics recipe *writes* is reachable from the app.

    The recipe resolves ECU symbols directly, so it could always write these;
    the table browser only offers what this profile declares. That gap is what
    let a person run the SOP from Python and then find half its targets missing
    on the tablet, so it is asserted rather than remembered.

    Deliberate skips are excluded: a `skip_stock` entry is a documented decision
    to leave a table alone, not a table the profile owes an entry.
    """
    from simoscal import sop_recipe as sop

    mapped = {SC8S50[name].key for name in SC8S50}
    unmapped = sorted(
        symbol
        for entry in sop.SYMBOL_MAP
        if sop.is_write_kind(entry.kind)
        for symbol in (entry.symbols if isinstance(entry.symbols, (list, tuple))
                       else [entry.symbols])
        if symbol not in mapped
    )
    assert unmapped == [], f"SOP writes tables the browser cannot reach: {unmapped}"


def test_turbo_protection_pairs_a_limit_with_its_setpoint() -> None:
    """Each protection ceiling is a (limit, setpoint) pair, both browsable.

    The setpoint is what the closed loop targets and the limit is where
    protection acts, so on stock the setpoint sits *below* the limit. Raising one
    without the other narrows that gap or inverts it.
    """
    for limit, setpoint in (
        ("turbo_speed_max", "turbo_speed_max_setpoint"),
        ("compressor_air_temp_max", "compressor_air_temp_max_setpoint"),
    ):
        for name in (limit, setpoint):
            spec = SC8S50[name]
            assert spec.shape == (1, 1)
            assert not spec.domain_owned, "chosen to stay browsable"
            assert not spec.has(prof.TAG_AXIS)
    assert SC8S50["turbo_speed_max"].units == "rpm"
    assert SC8S50["compressor_air_temp_max"].units == "\N{DEGREE SIGN}C"


def test_speed_limiter_is_four_scalars_of_one_number() -> None:
    """All four levels are mapped, and all four are owned by one coherent writer.

    Four tables holding one number: a generic write to one alone leaves the car
    limited by whichever un-written level the ECU selects, so the quartet is
    domain-owned by ``tune.limits.speed_limiter()`` (2026-08-20 plan U1,
    resolving the coverage brainstorm's blocking `owner` question).
    """
    assert len(sc_map.SPEED_LIMITER) == 4
    for name in sc_map.SPEED_LIMITER:
        spec = SC8S50[name]
        assert spec.shape == (1, 1)
        assert spec.units == "km/h"
        assert spec.key.startswith("LMVLim_vMax_vLim_C_VW.")
        assert spec.domain_owned
        assert "limits.speed_limiter" in spec.owner


def test_pedal_maps_are_the_dct_family_and_stay_dual_path() -> None:
    """The DSG's driver-interpretation maps are mapped, generically writable.

    Only the DCT family plus drive-off: the MT/AT variants are dead tables for
    this transmission and offering an editor for them invites editing a map the
    car never reads. No owner — no unit lies and no cross-table invariant binds
    them (plan Key Decision 4).
    """
    assert len(sc_map.PEDAL_MAPS) == 7
    for name in sc_map.PEDAL_MAPS:
        spec = SC8S50[name]
        assert spec.key.startswith("IP_FAC_TQ_REQ_DRIV_")
        assert not spec.domain_owned, "pedal maps are dual-path by decision"
        assert not spec.has(prof.TAG_AXIS)
        assert spec.units == "-"
    assert SC8S50["pedal_dct_high"].shape == (12, 12)
    assert SC8S50["pedal_drive_off"].shape == (8, 8)
    # No MT/AT variant sneaks in under any logical name.
    mapped_keys = {spec.key for spec in SC8S50.specs.values()}
    for dead in ("IP_FAC_TQ_REQ_DRIV_H_VS_MT", "IP_FAC_TQ_REQ_DRIV_H_VS_AT",
                 "IP_FAC_TQ_REQ_DRIV_SPT_MT", "IP_FAC_TQ_REQ_DRIV_RVG"):
        assert dead not in mapped_keys


def test_lambda_full_load_main_map_is_owned_and_its_context_is_not() -> None:
    """The FL enrichment map is owned (lean bound ≥ 1.00 refused engine-side);
    the IAT variant and its threshold/hysteresis stay grid-editable context."""
    main = SC8S50["lambda_full_load"]
    assert main.key == "IP_LAMB_FL_SP"
    assert main.shape == (8, 12)
    assert main.domain_owned
    assert "fueling.full_load_enrichment" in main.owner

    iat = SC8S50["lambda_full_load_iat"]
    assert iat.key == "IP_LAMB_FL_SP_TIA"
    assert iat.shape == (8, 12)
    assert not iat.domain_owned

    for name in ("lambda_full_load_iat_threshold", "lambda_full_load_iat_hysteresis"):
        spec = SC8S50[name]
        assert spec.shape == (1, 1)
        assert spec.units == "\N{DEGREE SIGN}C"
        assert not spec.domain_owned


def test_the_two_new_grids_carry_their_own_axes() -> None:
    """Both 6x6 grids are mapped with both axes, and the axes are tagged."""
    for grid, axes in (
        ("cylinder_head_temp_setpoint",
         ("cylinder_head_temp_rpm_axis", "cylinder_head_temp_charge_axis")),
        ("charge_air_pressure_max_diag",
         ("charge_air_diag_put_axis", "charge_air_diag_rpm_axis")),
    ):
        assert SC8S50[grid].shape == (6, 6)
        assert not SC8S50[grid].has(prof.TAG_AXIS)
        for axis in axes:
            spec = SC8S50[axis]
            assert spec.shape == (1, 6)
            assert spec.has(prof.TAG_AXIS), "a breakpoint axis must be tagged"


def test_put_setpoint_now_has_both_of_its_axes() -> None:
    """The y axis the revision lineage's axis-write moves is mapped at last."""
    y = SC8S50["put_setpoint_map_axis"]
    assert y.key == "ldp_map_sp_ip_put_sp"
    assert y.shape == (1, 4)
    assert y.units == "hPa"
    assert y.has(prof.TAG_AXIS)
    # The grid is 4 rows x 6 columns: y indexes the rows, x the columns.
    assert SC8S50["put_setpoint"].shape == (4, 6)
    assert SC8S50["put_setpoint_rpm_axis"].shape == (1, 6)


# --------------------------------------------------------------------------- #
# Integration against the real XDFs (skips on a lean checkout)
# --------------------------------------------------------------------------- #
def test_sc8s50_profile_resolves_completely_on_the_real_xdf(real_cal: CalFile) -> None:
    resolved = resolve(SC8S50, real_cal)

    assert len(resolved) == len(SC8S50)
    # Spot-check that a resolved view really is the intended table.
    put = resolved["put_setpoint"]
    assert put.view.symbol == "IP_PUT_SP"
    assert put.view.shape == (4, 6)
    assert put.label == "`IP_PUT_SP` — Pressure up throttle setpoint"

    # The IAT timing pair really does share one y axis: the axis this profile
    # names is the same table the Basic grid embeds as its own y breakpoints.
    basic = resolved["ignition_temp_correction_basic"]
    reference = resolved["ignition_temp_correction_reference"]
    iat_axis = resolved["ignition_temp_iat_axis"]
    assert basic.view.shape == reference.view.shape == (10, 10)
    for grid in (basic, reference):
        assert grid.view.table.y.link_uniqueid == iat_axis.view.uniqueid
        assert grid.view.table.x.link_uniqueid == (
            resolved["ignition_temp_rpm_axis"].view.uniqueid
        )
    # Stock IAT breakpoints, in degC — the guide's re-breakpoint starts here.
    assert iat_axis.view.values.ravel() == pytest.approx(
        [-30, -20.25, -9.75, 0, 30, 40.5, 50.25, 60, 70.5, 80.25], abs=1e-6
    )


def test_domain_screen_specs_resolve_at_their_declared_shapes(real_cal: CalFile) -> None:
    """The U1 pedal + lambda-FL + quartet specs against the real XDF.

    Resolution asserts every declared shape (a mismatch is a miss); this adds
    the facts the screens lean on: the FL map's y axis is time at full load
    (0–60 s) and its x axis engine speed, the pedal maps are torque-fraction
    grids over pedal % (y) and rpm (x), and every pedal map writes back from
    physical units (reversible — the curve editor's precondition).
    """
    resolved = resolve(SC8S50, real_cal)

    fl = resolved["lambda_full_load"]
    assert fl.view.shape == (8, 12)
    y = np.asarray(fl.view.axis_values("y")).ravel()
    x = np.asarray(fl.view.axis_values("x")).ravel()
    assert y[0] == 0.0 and y[-1] == 60.0, "rows are time at full load, seconds"
    assert x[0] > 400 and x[-1] > 6000, "columns are engine speed"
    # Stock is flat 1.00 — no FL enrichment; anything below 1.00 is added.
    assert np.allclose(np.asarray(fl.view.values), 1.0)

    for name in sc_map.PEDAL_MAPS:
        view = resolved[name].view
        z = view.table.z
        assert z.scaling is not None and z.scaling.is_linear, (
            f"{name} must be writable from physical units"
        )
    pedal = resolved["pedal_dct_high"].view
    py = np.asarray(pedal.axis_values("y")).ravel()
    assert py[0] == 0.0 and py[-1] > 99.0, "rows are pedal value, percent"

    for name in sc_map.SPEED_LIMITER:
        view = resolved[name].view
        assert float(np.asarray(view.values).ravel()[0]) == 200.0, (
            "stock quartet is 200 km/h in every scalar"
        )


def test_switch_patch_profile_resolves_on_its_real_xdf(
    switch_patch_xdf: Path, real_bin: Path
) -> None:
    cal = CalFile.open(str(switch_patch_xdf), str(real_bin), structure=SC8S50_STRUCTURE)
    resolved = resolve(SWITCH_PATCH_2933, cal)

    assert len(resolved) == len(SWITCH_PATCH_2933)
    slot5 = resolved["slot5_put_setpoint"]
    assert slot5.view.uniqueid == 0x7D71A
    assert slot5.view.shape == sp_map.SLOT_GRID_SHAPE


def test_wrong_xdf_fails_loud_before_any_edit(
    switch_patch_xdf: Path, real_bin: Path
) -> None:
    """AE3: point a base-calibration profile at the switch-patch XDF."""
    cal = CalFile.open(str(switch_patch_xdf), str(real_bin), structure=SC8S50_STRUCTURE)
    with pytest.raises(ProfileResolutionError) as excinfo:
        resolve(SC8S50, cal)

    # Not one gap — the switch-patch XDF defines none of the base tables.
    assert len(excinfo.value.misses) == len(SC8S50)
    assert "IP_PUT_SP" in str(excinfo.value)
    assert not cal.edited


# --------------------------------------------------------------------------- #
# Domain groups — the heading an editing client files a table under
# --------------------------------------------------------------------------- #
def test_every_group_is_from_the_declared_vocabulary() -> None:
    """Membership is closed: a typo becomes a rogue heading, so it must raise."""
    with pytest.raises(ValueError) as excinfo:
        TableSpec(name="t", key="T", description="d", group="Boostt")

    assert "Boostt" in str(excinfo.value)
    assert prof.GROUP_BOOST in str(excinfo.value), "the error lists the real ones"


def test_a_spec_may_decline_a_group() -> None:
    """Empty is legal at the type level; the profiles are what require one."""
    assert TableSpec(name="t", key="T", description="d").group == ""


def test_sc8s50_files_every_table_under_a_group() -> None:
    assert SC8S50.ungrouped() == []


def test_switch_patch_files_every_generically_editable_table() -> None:
    """Owner-locked slot tables need no heading; anything the browser sees does.

    The Boost and Slots screens are domain-shaped already, so a per-slot gauge
    bitmask is reached without ever being browsed. The two launch-control
    scalars are the only patch tables the generic catalog offers, and they are
    grouped.
    """
    orphans = [
        name for name in SWITCH_PATCH_2933.names()
        if not SWITCH_PATCH_2933[name].owner and not SWITCH_PATCH_2933[name].group
    ]
    assert orphans == []
    assert SWITCH_PATCH_2933["lc_release_speed"].group == prof.GROUP_LAUNCH_TRACTION


def test_an_axis_is_filed_with_the_map_it_indexes() -> None:
    """The whole reason the group is curated rather than taken from the XDF.

    The XDF files every breakpoint vector under a category called "Axis", which
    separates a boost setpoint from the rpm axis that indexes it. Here they sit
    together, and the same holds for the lambda and ignition-correction axes.
    """
    for table, axis in (
        ("put_setpoint", "put_setpoint_rpm_axis"),
        ("put_setpoint", "put_setpoint_map_axis"),
        ("lambda_basic", "lambda_rpm_axis"),
        ("ignition_temp_correction_basic", "ignition_temp_iat_axis"),
        ("cylinder_head_temp_setpoint", "cylinder_head_temp_rpm_axis"),
    ):
        assert SC8S50[axis].group == SC8S50[table].group, (
            f"{axis} must be filed with {table}"
        )


def test_the_domain_families_do_not_straddle_groups() -> None:
    """A family the profile declares is edited together must browse together."""
    for family in (
        sc_map.IGNITION_BASE_VVL0,
        sc_map.IGNITION_TEMP_CORRECTION,
        sc_map.LAMBDA_FAMILY,
        sc_map.LAMBDA_FLOORS,
        sc_map.LAMBDA_FULL_LOAD,
        sc_map.WASTEGATE_MAPS,
        sc_map.TURBO_PROTECTION,
        sc_map.SPEED_LIMITER,
        sc_map.STATIC_REV_LIMIT,
        sc_map.PEDAL_MAPS,
        sc_map.CHARGE_AIR_DIAG,
        sc_map.CYLINDER_HEAD_TEMP,
    ):
        groups = {SC8S50[name].group for name in family}
        assert len(groups) == 1, f"{family[0]}'s family is split across {groups}"


def test_grouping_rejects_a_table_claimed_twice() -> None:
    """A rename that leaves a name in two headings must not be a silent winner."""
    specs = [TableSpec(name="a", key="A", description="d")]
    with pytest.raises(ValueError, match="claimed by both"):
        prof.apply_groups("T", specs, {
            prof.GROUP_BOOST: ("a",), prof.GROUP_TIMING: ("a",),
        })


def test_grouping_rejects_an_unfiled_or_stale_table() -> None:
    with pytest.raises(ValueError, match="no group claims"):
        prof.apply_groups("T", [
            TableSpec(name="a", key="A", description="d"),
            TableSpec(name="b", key="B", description="d"),
        ], {prof.GROUP_BOOST: ("a",)})

    with pytest.raises(ValueError, match="does not declare"):
        prof.apply_groups("T", [], {prof.GROUP_BOOST: ("a",)})


def test_grouping_names_the_profile_it_is_complaining_about() -> None:
    """Two profiles now share this validator, so the message must say which."""
    with pytest.raises(ValueError, match="SCGA05 declares tables no group claims"):
        prof.apply_groups(
            "SCGA05", [TableSpec(name="a", key="A", description="d")], {},
        )
