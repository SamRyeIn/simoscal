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

from dataclasses import replace

from simoscal import CalFile, parse_xdf
from simoscal.calfile import TableView
from simoscal.model import ScalingEquation
from simoscal.tune.project import Tune
from simoscal.binimage import BinImage
from simoscal.tune import profile as prof
from simoscal.tune.profile import (
    Profile,
    ProfileResolutionError,
    TableSpec,
    resolve,
)
from simoscal.tune.profiles import (
    PATCH_PROFILES,
    PROFILES,
    SC8S50,
    SCGA05,
    SWITCH_PATCH_2933,
    SWITCH_PATCH_2933_A05,
)
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
        "SwitchPatch2933_A05": SWITCH_PATCH_2933_A05,
    }
    # Every car has exactly one patch map, and every patch map belongs to a
    # registered car — the pairing preflight follows instead of guessing.
    assert PATCH_PROFILES == {
        "SC8S50": SWITCH_PATCH_2933,
        "SCGA05": SWITCH_PATCH_2933_A05,
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


def test_switch_patch_binds_the_five_spark_modifier_grids() -> None:
    """The 92-table book gains five, and they are owned like everything else."""
    assert len(SWITCH_PATCH_2933.specs) == 92 + 5

    names = sp_map.slot_names("spark_modifier")
    assert names == tuple(f"slot{n}_spark_modifier" for n in (1, 2, 3, 4, 5))

    for slot, uid in sp_map.S50_SPARK_GRID_UIDS.items():
        spec = SWITCH_PATCH_2933[f"slot{slot}_spark_modifier"]
        assert spec.key == uid
        assert spec.shape == sp_map.S50_SPARK_GRID_SHAPE == (16, 16)
        assert spec.units == "\N{DEGREE SIGN}CRK"
        assert spec.has(prof.TAG_NO_SYMBOL)
        # Domain-owned, so the generic editor refuses it (CR-20260813-01).
        assert "slot_spark_map" in spec.owner


def test_spark_grids_are_optional_and_a05_declines_them() -> None:
    """A05's uniqueids have never been read off its own XDF; it keeps its 92."""
    assert len(SWITCH_PATCH_2933_A05.specs) == 92
    assert not [n for n in SWITCH_PATCH_2933_A05.specs if "spark_modifier" in n]


def _s50_book(**overrides):
    book = dict(
        name="probe", xdf="S50 Switch Patch.29.33.V2.xdf",
        standalone_uids=sp_map.S50_STANDALONE_UIDS,
        put_grid_uids=sp_map.S50_PUT_GRID_UIDS,
        slot_setting_uids=sp_map.S50_SLOT_SETTING_UIDS,
        spark_grid_uids=sp_map.S50_SPARK_GRID_UIDS,
        spark_grid_shape=sp_map.S50_SPARK_GRID_SHAPE,
    )
    book.update(overrides)
    return book


def test_spark_uids_without_a_shape_are_refused() -> None:
    """S50 is (16, 16) and A05 is (16, 18), so a defaulted shape would be a lie."""
    with pytest.raises(ValueError, match="16x18"):
        sp_map.build_switch_patch_profile(**_s50_book(spark_grid_shape=None))

    with pytest.raises(ValueError, match="together"):
        sp_map.build_switch_patch_profile(**_s50_book(spark_grid_uids=None))


def test_a_spark_book_missing_a_slot_fails_at_build_time() -> None:
    short = {s: u for s, u in sp_map.S50_SPARK_GRID_UIDS.items() if s != 3}
    with pytest.raises(ValueError, match="Spark modifier grids"):
        sp_map.build_switch_patch_profile(**_s50_book(spark_grid_uids=short))


def test_a_spark_grid_reusing_a_put_grid_uniqueid_fails_at_build_time() -> None:
    """The copy-a-column typo: two logical names on one table."""
    collide = dict(sp_map.S50_SPARK_GRID_UIDS)
    collide[5] = sp_map.S50_PUT_GRID_UIDS[5]
    with pytest.raises(ValueError) as excinfo:
        sp_map.build_switch_patch_profile(**_s50_book(spark_grid_uids=collide))

    message = str(excinfo.value)
    assert "slot5_spark_modifier" in message and "slot5_put_setpoint" in message


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

    spark5 = resolved["slot5_spark_modifier"]
    assert spark5.view.uniqueid == 0x7D31A
    assert spark5.view.shape == sp_map.S50_SPARK_GRID_SHAPE


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


# --------------------------------------------------------------------------- #
# The XDF addressing convention, declared per car
# --------------------------------------------------------------------------- #
def test_the_two_shipped_profiles_declare_opposite_conventions() -> None:
    """Both conventions are real and both are in use, which is the whole point.

    `SC8S50.V1.0.xdf` numbers tables from the start of the bin; `SCGa05_cal.xdf`
    numbers them from the start of the CAL block. Neither is a defect, and a
    library that assumed either one would be wrong about the other car.
    """
    assert SC8S50.xdf_addresses_cal_relative is False
    assert SC8S50.expected_xdf_base_offset == SC8S50.structure.cal_file_offset
    assert SC8S50.xdf_base_offset is None, "a full-bin XDF needs no override"

    assert SCGA05.xdf_addresses_cal_relative is True
    assert SCGA05.expected_xdf_base_offset == 0
    assert SCGA05.xdf_base_offset == SCGA05.structure.cal_file_offset


def test_both_shipped_profiles_match_the_file_they_name() -> None:
    """The declaration is only worth anything if it matches the real header."""
    for profile, path in (
        (SC8S50, Path(__file__).resolve().parents[1] / "xdf" / SC8S50.xdf),
        (SCGA05, Path(__file__).resolve().parents[1] / "xdf" / SCGA05.xdf),
    ):
        if not path.is_file():
            pytest.skip(f"{path.name} not present")
        assert parse_xdf(str(path)).base_offset == profile.expected_xdf_base_offset


def test_a_cal_relative_declaration_needs_a_structure_to_count_from() -> None:
    """"Relative to the CAL block" is meaningless without one."""
    with pytest.raises(ValueError, match="declares no structure"):
        Profile(name="T", xdf="t.xdf", xdf_addresses_cal_relative=True)


def test_the_convention_travels_with_the_structure_through_a_merge() -> None:
    """A patch space shares the base space's bytes, so it shares its arithmetic.

    The patch profile declares no structure and no convention; merging must take
    the base profile's rather than silently resetting to the full-bin default,
    which would put the merged profile's addresses 0x220000 away from the base
    profile's on the same bin.
    """
    patch = Profile(name="P", xdf="p.xdf")
    merged = SCGA05.merged_with(patch, name="M")
    assert merged.xdf_addresses_cal_relative is True
    assert merged.xdf_base_offset == SCGA05.structure.cal_file_offset
    # And in the other direction, where the structure comes from `other`.
    assert patch.merged_with(SCGA05, name="M2").xdf_addresses_cal_relative is True


def test_a_profile_with_no_structure_expects_nothing() -> None:
    """The switch patch is checked through whatever it is merged into."""
    assert SWITCH_PATCH_2933.expected_xdf_base_offset is None
    assert SWITCH_PATCH_2933.xdf_base_offset is None


def test_an_override_is_refused_on_a_subtract_mode_xdf(tmp_path: Path) -> None:
    """Subtract mode makes the addresses ECU addresses, not offsets.

    An override has no defined meaning on top of that, so it is refused rather
    than applied to the wrong quantity. No shipped XDF uses subtract mode; this
    pins what happens the day one does.
    """
    xdf = Path(__file__).resolve().parents[1] / "xdf" / SCGA05.xdf
    binp = Path(__file__).resolve().parents[1] / "bin" / "3CN906259B__0002_SCGA05.bin"
    if not (xdf.is_file() and binp.is_file()):
        pytest.skip("A05 files not present")
    text = xdf.read_text(encoding="utf-8", errors="surrogateescape")
    tampered = tmp_path / "subtract.xdf"
    tampered.write_text(
        text.replace(
            '<BASEOFFSET offset="0" subtract="0" />',
            '<BASEOFFSET offset="0" subtract="1" />',
        ),
        encoding="utf-8", errors="surrogateescape",
    )
    with pytest.raises(ValueError, match="subtract"):
        CalFile.open(
            str(tampered), str(binp), structure=SCGA05.structure,
            base_offset=SCGA05.xdf_base_offset,
        )


# --------------------------------------------------------------------------- #
# Pinned layouts — the map is bound to a reviewed definition file (CR-...-02)
# --------------------------------------------------------------------------- #
# Resolution proves a definition file *names* this car's tables in the right
# shapes. It says nothing about where those tables are or how their bytes
# decode, and every gate after resolution — the journal, the readback, the byte
# audit — is computed through that same file, so all of them agree with a table
# that moved. The pin is the only check that compares the file with something
# outside it.

#: The z-axis declaration of `C_M_AIR_CYL_SP_MAX` — Maximum allowed airmass
#: setpoint in the real SC8S50 XDF. Unique in the file, so the tamper below
#: moves exactly one table and nothing else.
_AIRMASS_Z_DECL = (
    '<EMBEDDEDDATA mmedtypeflags="0x10006" mmedaddress="0x9bd4" '
    'mmedelementsizebits="32" mmedcolcount="1" mmedrowcount="1" '
    'mmedmajorstridebits="32" mmedminorstridebits="0" />'
)


def test_the_shipped_base_maps_are_fully_pinned() -> None:
    """Every spec on a base profile carries a layout, and no pin is orphaned.

    Enforced here rather than at import, following ``ungrouped``: what counts as
    acceptable is per-profile, and a *derived* profile (a decoy, a subset) is
    legitimately part-pinned. What must not happen is a spec added to a shipped
    map arriving unauthenticated, or a renamed one leaving its pin behind.
    """
    for profile in (SC8S50, SCGA05):
        assert profile.unpinned == [], (
            f"{profile.name} has unpinned specs — re-run "
            f"`python -m simoscal.tune.profiles pin {profile.name} ...`"
        )
        assert profile.stale_pins == [], (
            f"{profile.name} pins names it no longer maps: {profile.stale_pins}"
        )


@pytest.mark.parametrize(
    "key, field, value",
    [
        # A 4x6 uint16 grid carries every field except the float flag, which is
        # only legal on a 32-bit element — so that one is mutated on the float32
        # airmass ceiling instead.
        ("IP_PUT_SP", "address", 0x1B6E6),
        ("IP_PUT_SP", "elem_bits", 32),
        ("IP_PUT_SP", "rows", 5),
        ("IP_PUT_SP", "cols", 7),
        ("IP_PUT_SP", "signed", True),
        ("IP_PUT_SP", "little_endian", False),
        ("IP_PUT_SP", "column_major", False),
        ("IP_PUT_SP", "major_stride_bits", 8),
        ("IP_PUT_SP", "minor_stride_bits", 16),
        ("C_M_AIR_CYL_SP_MAX", "is_float", False),
    ],
)
def test_layout_digest_moves_for_every_load_bearing_field(
    real_cal: CalFile, key: str, field: str, value: object
) -> None:
    """Each field that decides which bytes a table is, or how they decode."""
    view = real_cal.get(key)
    before = prof.layout_digest(view)
    emb = replace(view.table.z.embedded, **{field: value})
    moved = replace(view.table, z=replace(view.table.z, embedded=emb))
    assert prof.layout_digest(TableView(moved, real_cal)) != before, field


def test_layout_digest_moves_when_the_scaling_changes(real_cal: CalFile) -> None:
    """A re-scaled table writes different bytes for the same physical value."""
    view = real_cal.get("C_M_AIR_CYL_SP_MAX")
    before = prof.layout_digest(view)
    rescaled = replace(
        view.table,
        z=replace(view.table.z, scaling=ScalingEquation.from_expression("X * 2")),
    )
    assert prof.layout_digest(TableView(rescaled, real_cal)) != before


def test_layout_digest_ignores_what_changes_no_byte(real_cal: CalFile) -> None:
    """A retitled table is the same table; the pin must not churn on metadata."""
    view = real_cal.get("C_M_AIR_CYL_SP_MAX")
    renamed = replace(view.table, title="Something else entirely")
    assert prof.layout_digest(TableView(renamed, real_cal)) == prof.layout_digest(view)


def test_layout_digest_ignores_the_packed_stride_spelling(real_bin: Path) -> None:
    """Two definition files for one car must agree on every pinned layout.

    ``SC8S50.V1.0.xdf`` writes a major stride of ``elem_bits`` where
    ``SC8S50.ALL.xdf`` writes ``0``, for the very same tables. Both mean packed
    contiguous — the same bytes — so a pin that moved between them would be
    noise, and noise is what gets a real refusal ignored.
    """
    alternate = Path(__file__).resolve().parents[1] / "xdf" / "SC8S50.ALL.xdf"
    if not alternate.is_file():
        pytest.skip(f"alternate XDF not present: {alternate}")
    cal = CalFile.open(str(alternate), str(real_bin), structure=SC8S50_STRUCTURE)
    assert prof.pin_layouts(replace(SC8S50, table_layouts={}), cal) == dict(
        SC8S50.table_layouts
    )


def test_a_table_that_moved_is_refused_before_anything_is_written(
    real_xdf: Path, real_bin: Path, tmp_path: Path
) -> None:
    """The reproducer from CR-20260828-02, end to end.

    Move one table four bytes along in a copy of the reviewed XDF, changing
    nothing else. Its symbol still resolves and its shape still matches, so
    before the pins this built a verified, shareable bin whose airmass ceiling
    was written at ``0x209bd8`` while the real calibration sat at ``0x209bd4``.
    The refusal has to land at resolution, which is before a session exists.
    """
    text = real_xdf.read_text(encoding="utf-8", errors="surrogateescape")
    assert text.count(_AIRMASS_Z_DECL) == 1, "the tamper anchor is no longer unique"
    tampered = tmp_path / "moved-table.xdf"
    tampered.write_text(
        text.replace(_AIRMASS_Z_DECL, _AIRMASS_Z_DECL.replace("0x9bd4", "0x9bd8"), 1),
        encoding="utf-8", errors="surrogateescape",
    )

    cal = CalFile.open(str(tampered), str(real_bin), structure=SC8S50_STRUCTURE)
    with pytest.raises(ProfileResolutionError) as excinfo:
        resolve(SC8S50, cal, xdf_label=str(tampered))
    misses = {m.name: m for m in excinfo.value.misses}
    assert set(misses) == {"airmass_setpoint_max"}, (
        "exactly the moved table may fail; anything else means the tamper hit "
        "more than one declaration"
    )
    assert "layout" in misses["airmass_setpoint_max"].reason

    # And through the public door, where a revision script would meet it.
    with pytest.raises(ProfileResolutionError):
        Tune.open(SC8S50, xdf=tampered, bin=real_bin)
