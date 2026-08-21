"""Unit tests for :mod:`simoscal.tune.profile` — logical names → XDF tables (U1).

Edge cases (unknown name, ambiguity, shape mismatch, suggestions) run on the
hand-written ``mini.xdf`` fixture; the integration checks that the shipped
SC8S50 and switch-patch maps actually resolve run against the real XDFs and
skip cleanly when those files are absent from a lean checkout.
"""

from __future__ import annotations

import struct
from pathlib import Path

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
from simoscal.tune.profiles import PROFILES, SC8S50, SWITCH_PATCH_2933
from simoscal.tune.profiles import sc8s50 as sc_map
from simoscal.tune.profiles import switchpatch_2933 as sp_map

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
    return CalFile(model, image)


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


def test_profile_getitem_lists_known_names_on_a_miss() -> None:
    p = _mini_profile(known=TableSpec("known", "SYM", "desc"))
    with pytest.raises(KeyError, match="known"):
        p["unknown"]


# --------------------------------------------------------------------------- #
# The shipped maps
# --------------------------------------------------------------------------- #
def test_shipped_profiles_are_registered() -> None:
    assert PROFILES == {"SC8S50": SC8S50, "SwitchPatch2933": SWITCH_PATCH_2933}


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
                 *sc_map.CYLINDER_HEAD_TEMP):
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
    """All four levels are mapped, so raising the limiter can write all four."""
    assert len(sc_map.SPEED_LIMITER) == 4
    for name in sc_map.SPEED_LIMITER:
        spec = SC8S50[name]
        assert spec.shape == (1, 1)
        assert spec.units == "km/h"
        assert spec.key.startswith("LMVLim_vMax_vLim_C_VW.")
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


def test_switch_patch_profile_resolves_on_its_real_xdf(
    switch_patch_xdf: Path, real_bin: Path
) -> None:
    cal = CalFile.open(str(switch_patch_xdf), str(real_bin))
    resolved = resolve(SWITCH_PATCH_2933, cal)

    assert len(resolved) == len(SWITCH_PATCH_2933)
    slot5 = resolved["slot5_put_setpoint"]
    assert slot5.view.uniqueid == 0x7D71A
    assert slot5.view.shape == sp_map.SLOT_GRID_SHAPE


def test_wrong_xdf_fails_loud_before_any_edit(
    switch_patch_xdf: Path, real_bin: Path
) -> None:
    """AE3: point a base-calibration profile at the switch-patch XDF."""
    cal = CalFile.open(str(switch_patch_xdf), str(real_bin))
    with pytest.raises(ProfileResolutionError) as excinfo:
        resolve(SC8S50, cal)

    # Not one gap — the switch-patch XDF defines none of the base tables.
    assert len(excinfo.value.misses) == len(SC8S50)
    assert "IP_PUT_SP" in str(excinfo.value)
    assert not cal.edited
