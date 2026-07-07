"""Unit tests for :mod:`simoscal.sop_recipe` (SOP tune recipe).

Structured by plan unit:

* **U1** — symbol map validity + ``resolve_symbol_map`` (this section runs on the
  mini fixture for edge cases and ``real_cal`` for the confirmed-symbol
  integration, skipping cleanly when the real files are absent).

Later units (U2 write paths, U3 guard, U4 build-out, U5 report) append their own
sections below as they land.
"""

from __future__ import annotations

import struct
import warnings
from pathlib import Path

import numpy as np
import pytest

from simoscal import BinImage, CalFile, parse_xdf
from simoscal.sop_recipe import (
    SKIP_KINDS,
    SYMBOL_MAP,
    WRITE_KINDS,
    RecipeEntry,
    is_write_kind,
    resolve_symbol_map,
)

FIXTURES = Path(__file__).parent / "fixtures"
MINI_XDF = FIXTURES / "mini.xdf"


@pytest.fixture
def mini_cal() -> CalFile:
    """A tiny CalFile with decodable bytes (mirrors the export test's fixture).

    Provides ``SYM_10X10`` (happy resolve), ``SYM_DUP`` (ambiguous) and lets a
    bogus symbol exercise the missing path — none of which needs the real bin.
    Function-scoped so write tests get a fresh, unedited image each time.
    """
    model = parse_xdf(str(MINI_XDF))
    size = model.base_offset + 0x6000
    buf = bytearray(size)
    off = model.base_offset + 0x1000
    buf[off : off + 200] = struct.pack("<100h", *range(100))
    buf[model.base_offset + 0x2000] = 200
    foff = model.base_offset + 0x4000
    buf[foff : foff + 4] = struct.pack("<f", 12.5)
    xoff = model.base_offset + 0x5000
    buf[xoff : xoff + 10] = struct.pack("<5H", 1000, 2000, 3000, 4000, 5000)
    zoff = model.base_offset + 0x5010
    buf[zoff : zoff + 10] = struct.pack("<5H", 10, 20, 30, 40, 50)
    img = BinImage(buf, region_start=model.region_start, region_size=len(buf))
    return CalFile(model, img)


# --------------------------------------------------------------------------- #
# U1 — symbol map validity
# --------------------------------------------------------------------------- #
class TestSymbolMapShape:
    def test_every_entry_has_section_and_valid_kind(self) -> None:
        assert SYMBOL_MAP, "symbol map must not be empty"
        for e in SYMBOL_MAP:
            assert e.guide_section.strip(), f"empty guide_section on {e!r}"
            assert e.description.strip(), f"empty description on {e!r}"
            assert e.kind in WRITE_KINDS | SKIP_KINDS, f"bad kind {e.kind!r}"

    def test_write_entries_declare_symbols_or_prefixes(self) -> None:
        # A write entry that resolves to nothing could never apply — every write
        # kind must declare either explicit symbols or a search prefix.
        for e in SYMBOL_MAP:
            if is_write_kind(e.kind):
                assert e.symbols or e.search_prefixes, (
                    f"write entry {e.guide_section!r} declares no symbols"
                )

    def test_skip_entries_carry_a_reason(self) -> None:
        for e in SYMBOL_MAP:
            if e.kind in SKIP_KINDS:
                assert e.reason.strip(), f"skip entry {e.guide_section!r} has no reason"

    def test_bad_kind_is_rejected_at_construction(self) -> None:
        with pytest.raises(ValueError, match="unknown kind"):
            RecipeEntry(guide_section="x", description="y", kind="not_a_kind")

    def test_guide_sections_are_unique(self) -> None:
        sections = [e.guide_section for e in SYMBOL_MAP]
        assert len(sections) == len(set(sections)), "duplicate guide_section entries"


# --------------------------------------------------------------------------- #
# U1 — resolution against a live CalFile (no real files needed)
# --------------------------------------------------------------------------- #
class TestResolveMini:
    def test_missing_symbol_is_data_not_crash(self, mini_cal: CalFile) -> None:
        entry = RecipeEntry(
            guide_section="test", description="bogus", kind="literal_scalar",
            symbols=("NO_SUCH_SYMBOL",), target=1.0,
        )
        [resolved] = resolve_symbol_map(mini_cal, (entry,))
        assert resolved.all_resolved is False
        assert resolved.resolutions[0].resolved is False
        assert "missing" in resolved.resolutions[0].reason

    def test_ambiguous_symbol_gets_its_own_reason(self, mini_cal: CalFile) -> None:
        # SYM_DUP maps to two distinct tables in the mini fixture.
        entry = RecipeEntry(
            guide_section="test", description="dup", kind="literal_scalar",
            symbols=("SYM_DUP",), target=1.0,
        )
        [resolved] = resolve_symbol_map(mini_cal, (entry,))
        assert resolved.resolutions[0].resolved is False
        assert "ambiguous" in resolved.resolutions[0].reason

    def test_happy_resolution_carries_shape_and_view(self, mini_cal: CalFile) -> None:
        entry = RecipeEntry(
            guide_section="test", description="ok", kind="literal_broadcast",
            symbols=("SYM_10X10",), target=0.0,
        )
        [resolved] = resolve_symbol_map(mini_cal, (entry,))
        r = resolved.resolutions[0]
        assert r.resolved is True
        assert r.shape == (10, 10)
        assert r.view is not None
        assert resolved.all_resolved is True

    def test_search_prefix_no_match_yields_visible_placeholder(self, mini_cal: CalFile) -> None:
        entry = RecipeEntry(
            guide_section="test", description="none", kind="literal_broadcast",
            search_prefixes=("ZZZ_NOTHING",), target=0.0,
        )
        [resolved] = resolve_symbol_map(mini_cal, (entry,))
        assert resolved.all_resolved is False
        assert len(resolved.resolutions) == 1
        assert "no symbols matched" in resolved.resolutions[0].reason

    def test_search_prefix_discovers_all_matches(self, mini_cal: CalFile) -> None:
        # "SYM_" prefixes SYM_10X10, SYM_SCALAR, SYM_DUP (the last ambiguous).
        entry = RecipeEntry(
            guide_section="test", description="fam", kind="literal_broadcast",
            search_prefixes=("SYM_1",), target=0.0,
        )
        [resolved] = resolve_symbol_map(mini_cal, (entry,))
        syms = {r.symbol for r in resolved.resolutions}
        assert syms == {"SYM_10X10"}

    def test_pure_skip_entry_has_no_resolutions(self, mini_cal: CalFile) -> None:
        entry = RecipeEntry(
            guide_section="test", description="skip", kind="skip_vague",
            reason="nothing to resolve",
        )
        [resolved] = resolve_symbol_map(mini_cal, (entry,))
        assert resolved.resolutions == ()
        assert resolved.is_skip is True
        # A skip with no symbols is vacuously "all resolved" — never a false gap.
        assert resolved.all_resolved is True


# --------------------------------------------------------------------------- #
# U1 — resolution against the real bundled bin (AE4 accounting)
# --------------------------------------------------------------------------- #
class TestResolveReal:
    def test_full_map_resolves_all_write_kinds(self, real_cal: CalFile) -> None:
        resolved = resolve_symbol_map(real_cal)
        assert len(resolved) == len(SYMBOL_MAP)
        for r in resolved:
            if is_write_kind(r.entry.kind):
                assert r.all_resolved, (
                    f"{r.entry.guide_section!r} did not fully resolve: "
                    f"{r.unresolved_reason()}"
                )

    @pytest.mark.parametrize(
        "symbol, shape, units",
        [
            ("IP_PUT_SP", (4, 6), "hPa"),
            ("LC_PUT_SP_TOL_ENA_AMP", (1, 1), "-"),
            ("IP_IGA_BAS_TEMP_N_32", (10, 10), "°CRK"),
            ("IP_PQ_CHA_MAX", (8, 8), "-"),
            ("CoTE_tHdCtlSp_M_VW", (6, 6), "°C"),
            ("C_N_TCHA_MAX", (1, 1), "rpm"),
            ("IP_IGA_BAS_IVVT_VVL_PORT_L[STND][0][0]", (16, 16), "°CRK"),
        ],
    )
    def test_confirmed_symbols_have_expected_shape_units(
        self, real_cal: CalFile, symbol: str, shape, units: str
    ) -> None:
        v = real_cal.get(symbol)
        assert v.shape == shape
        assert v.units == units

    def test_torque_curve_resolves_all_pc_variants(self, real_cal: CalFile) -> None:
        # 15 AT + 10 MT + 5 ECO = 30 Max-Torque symbols, all resolvable.
        [entry] = [
            e for e in SYMBOL_MAP if e.kind == "torque_curve"
        ]
        [resolved] = resolve_symbol_map(real_cal, (entry,))
        assert len(resolved.resolutions) == 30
        assert resolved.all_resolved

    def test_iga_writes_only_the_nine_vvl0_port_low_tables(self, real_cal: CalFile) -> None:
        [entry] = [
            e for e in SYMBOL_MAP
            if e.guide_section.startswith("Timing — Basic Ignition")
        ]
        assert len(entry.symbols) == 9
        for sym in entry.symbols:
            assert "_PORT_L[STND]" in sym  # never _PORT_H or [LFT_1]


# --------------------------------------------------------------------------- #
# U2 — axis-matching helpers (pure functions, no fixture needed)
# --------------------------------------------------------------------------- #
from simoscal.sop_recipe import (  # noqa: E402
    AXIS_MATCH_TOL,
    OUTCOME_ALREADY_SATISFIED,
    OUTCOME_APPLIED,
    OUTCOME_AXIS_MISMATCH,
    OUTCOME_GUARD_BLOCKED,
    OUTCOME_SKIPPED,
    OUTCOME_UNRESOLVED,
    CutRule,
    LiteralGrid,
    TorqueCurve,
    _key_column_match,
    _positional_axis_match,
    _run_write,
    apply_entry,
)
from simoscal.model import FloatBugGuardError, RawRangeError  # noqa: E402
from simoscal.safety import EditRangeWarning  # noqa: E402


class TestAxisMatching:
    def test_positional_match_exact(self) -> None:
        axis = np.array([400.0, 700.0, 1000.0])
        assert _positional_axis_match(axis, (400, 700, 1000)) == [0, 1, 2]

    def test_positional_match_tolerates_transcription_noise(self) -> None:
        # guide 498.99 vs a stock 499.985 (~1.0 apart, ~100 from neighbours).
        axis = np.array([399.988, 499.985, 599.982])
        assert _positional_axis_match(axis, (399.99, 498.99, 599.98)) == [0, 1, 2]

    def test_positional_match_rejects_count_mismatch(self) -> None:
        axis = np.array([1.0, 2.0])
        assert _positional_axis_match(axis, (1, 2, 3)) is None

    def test_positional_match_rejects_far_breakpoints(self) -> None:
        # lambda-style: same count, but breakpoints genuinely different.
        axis = np.array([70.0, 120.0, 180.0])
        assert _positional_axis_match(axis, (150, 300, 500)) is None

    def test_positional_match_none_axis(self) -> None:
        assert _positional_axis_match(None, (1, 2)) is None

    def test_key_column_match_exact_and_nearmiss(self) -> None:
        # 4200 must NOT snap to the 4250 key (near-miss guard), 4000 must match.
        axis = np.array([4000.0, 4200.0, 4250.0])
        m = _key_column_match(axis, (4000, 4250, 4360), tol=10.0)
        assert m == {0: 0, 2: 1}  # col1 (4200) unmatched


class TestRunWrite:
    def test_captures_edit_range_warning(self) -> None:
        def w() -> None:
            warnings.warn(EditRangeWarning("cell over max"))

        status, text = _run_write(w)
        assert status == "ok"
        assert "over max" in text

    def test_float_bug_guard_maps_to_guard_blocked(self) -> None:
        def w() -> None:
            raise FloatBugGuardError("boom")

        status, text = _run_write(w)
        assert status == "guard_blocked"
        assert text == "boom"

    def test_raw_range_maps_to_guard_blocked(self) -> None:
        def w() -> None:
            raise RawRangeError("overflow")

        status, text = _run_write(w)
        assert status == "guard_blocked"
        assert "overflow" in text

    def test_clean_write_returns_ok_no_text(self) -> None:
        assert _run_write(lambda: None) == ("ok", "")


# --------------------------------------------------------------------------- #
# U2 — write paths on the mini fixture
# --------------------------------------------------------------------------- #
def _entry(**kw) -> RecipeEntry:
    kw.setdefault("guide_section", "test")
    kw.setdefault("description", "test")
    return RecipeEntry(**kw)


class TestWriteMini:
    def test_literal_scalar_applies_then_already_satisfied(self, mini_cal: CalFile) -> None:
        e = _entry(kind="literal_scalar", symbols=("SYM_SCALAR",), target=42.0)
        [out] = apply_entry(mini_cal, resolve_symbol_map(mini_cal, (e,))[0])
        assert out.outcome == OUTCOME_APPLIED
        assert out.new == 42.0
        assert mini_cal.get("SYM_SCALAR").values[0, 0] == 42.0
        # re-applying the same value is already_satisfied, not a re-write.
        [out2] = apply_entry(mini_cal, resolve_symbol_map(mini_cal, (e,))[0])
        assert out2.outcome == OUTCOME_ALREADY_SATISFIED

    def test_literal_broadcast_sets_every_cell(self, mini_cal: CalFile) -> None:
        e = _entry(kind="literal_broadcast", symbols=("SYM_10X10",), target=0.15)
        [rentry] = resolve_symbol_map(mini_cal, (e,))
        [out] = apply_entry(mini_cal, rentry)
        assert out.outcome == OUTCOME_APPLIED
        vals = mini_cal.get("SYM_10X10").values
        # every cell equal (broadcast) and near the requested value (quantized).
        assert np.allclose(vals, vals[0, 0])
        assert abs(vals[0, 0] - 0.15) < 0.05

    def test_cut_transform_only_touches_cells_over_threshold(self, mini_cal: CalFile) -> None:
        before = mini_cal.get("SYM_10X10").values.copy()
        thresh = float(np.median(before))
        e = _entry(kind="cut_transform", symbols=("SYM_10X10",),
                   target=CutRule(threshold=thresh, amount=0.06))
        [rentry] = resolve_symbol_map(mini_cal, (e,))
        [out] = apply_entry(mini_cal, rentry)
        assert out.outcome == OUTCOME_APPLIED
        after = mini_cal.get("SYM_10X10").values
        over = before > thresh
        # cells at/under threshold are byte-identical; cells over it dropped.
        assert np.array_equal(after[~over], before[~over])
        assert np.all(after[over] < before[over])

    def test_axis_mismatch_leaves_table_untouched(self, mini_cal: CalFile) -> None:
        # A grid whose y-keys can't match SYM_10X10's axis → mismatch, no write.
        before = mini_cal.get("SYM_10X10").values.copy()
        grid = LiteralGrid(
            x_keys=tuple(range(10)),
            y_keys=tuple(9000 + i for i in range(10)),  # nowhere near the real axis
            cells=tuple(tuple(1.0 for _ in range(10)) for _ in range(10)),
        )
        e = _entry(kind="literal_table", symbols=("SYM_10X10",), target=grid)
        [rentry] = resolve_symbol_map(mini_cal, (e,))
        [out] = apply_entry(mini_cal, rentry)
        assert out.outcome == OUTCOME_AXIS_MISMATCH
        assert np.array_equal(mini_cal.get("SYM_10X10").values, before)
        assert mini_cal.edited is False  # nothing staged

    def test_unresolved_symbol_becomes_unresolved_outcome(self, mini_cal: CalFile) -> None:
        e = _entry(kind="literal_scalar", symbols=("NOPE",), target=1.0)
        [rentry] = resolve_symbol_map(mini_cal, (e,))
        [out] = apply_entry(mini_cal, rentry)
        assert out.outcome == OUTCOME_UNRESOLVED
        assert mini_cal.edited is False

    def test_skip_entry_yields_single_skipped_outcome(self, mini_cal: CalFile) -> None:
        e = _entry(kind="skip_vague", reason="no symbol")
        [rentry] = resolve_symbol_map(mini_cal, (e,))
        [out] = apply_entry(mini_cal, rentry)
        assert out.outcome == OUTCOME_SKIPPED
        assert "no symbol" in out.detail


# --------------------------------------------------------------------------- #
# U2 — integration read-backs on the real bin (AE1)
# --------------------------------------------------------------------------- #
def _find(section_prefix: str) -> RecipeEntry:
    return next(e for e in SYMBOL_MAP if e.guide_section.startswith(section_prefix))


def _apply_one(cal: CalFile, entry: RecipeEntry):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        [rentry] = resolve_symbol_map(cal, (entry,))
        return apply_entry(cal, rentry)


class TestWriteReal:
    def test_iga_written_to_all_nine_siblings_stay_stock(self, real_cal: CalFile) -> None:
        stock_h = real_cal.get("IP_IGA_BAS_IVVT_VVL_PORT_H[STND][0][0]").values.copy()
        outs = _apply_one(real_cal, _find("Timing — Basic Ignition"))
        assert len(outs) == 9
        assert all(o.outcome == OUTCOME_APPLIED for o in outs)
        # the literal grid landed (row 0 first cell = guide 17.62, quantized).
        v = real_cal.get("IP_IGA_BAS_IVVT_VVL_PORT_L[STND][2][2]").values
        assert abs(v[0, 0] - 17.62) < 0.02
        # Port-Flap-High sibling untouched.
        assert np.array_equal(
            real_cal.get("IP_IGA_BAS_IVVT_VVL_PORT_H[STND][0][0]").values, stock_h
        )

    def test_put_setpoint_axis_and_last_row(self, real_cal: CalFile) -> None:
        outs = _apply_one(real_cal, _find("Boost — Option 2"))
        assert [o.outcome for o in outs] == [OUTCOME_APPLIED, OUTCOME_APPLIED]
        put = real_cal.get("IP_PUT_SP")
        assert abs(np.asarray(put.axis_values("y")).ravel()[-1] - 2698.97) < 0.05
        expected = [2698.97, 2698.97, 2499.96, 2349.97, 2298.97, 2198.97]
        assert np.allclose(put.values[-1], expected, atol=0.05)

    def test_iat_rowmap_zeroes_cold_keeps_70_5_stock(self, real_cal: CalFile) -> None:
        iat = real_cal.get("IP_IGA_BAS_TEMP_N_32")
        y = np.asarray(iat.axis_values("y")).ravel()
        stock = iat.values.copy()
        i_705 = int(np.argmin(np.abs(y - 70.5)))
        outs = _apply_one(real_cal, _find("Timing — Spark IAT"))
        assert outs[0].outcome == OUTCOME_APPLIED
        assert "70.5" in outs[0].detail
        after = real_cal.get("IP_IGA_BAS_TEMP_N_32").values
        # cold rows (<=30 °C) zeroed; the 70.5 row byte-identical to stock.
        cold = y <= 30 + 1e-6
        assert np.allclose(after[cold], 0.0)
        assert np.array_equal(after[i_705], stock[i_705])

    def test_lambda_axis_mismatch_leaves_stock(self, real_cal: CalFile) -> None:
        stock = real_cal.get("IP_LAMB_BAS_HPDI[1]").values.copy()
        outs = _apply_one(real_cal, _find("Fueling — Basic lambda"))
        assert all(o.outcome == OUTCOME_AXIS_MISMATCH for o in outs)
        assert np.array_equal(real_cal.get("IP_LAMB_BAS_HPDI[1]").values, stock)

    def test_torque_curve_full_on_at_partial_on_eco(self, real_cal: CalFile) -> None:
        outs = _apply_one(real_cal, _find("1. Torque request — Max Torque"))
        assert len(outs) == 30
        assert all(o.outcome == OUTCOME_APPLIED for o in outs)
        at = next(o for o in outs if "IP_TQ_POW_MAX_AT" in o.symbol)
        eco = next(o for o in outs if "IP_TQ_POW_MAX_ECO" in o.symbol)
        assert "20/20" in at.detail
        assert "left stock" in eco.detail  # ECO has 2 columns with no curve key
        # AT peak row cell at 2500 rpm == 440 (guide), broadcast across gears.
        atv = real_cal.get("IP_TQ_POW_MAX_AT[POW_1][0]").values
        assert np.allclose(atv[:, 5], 440.0, atol=0.5)  # col 5 == 2500 rpm

    def test_cut_transform_real_cyl_head(self, real_cal: CalFile) -> None:
        before = real_cal.get("CoTE_tHdCtlSp_M_VW").values.copy()
        outs = _apply_one(real_cal, _find("Cooling — cylinder head"))
        assert outs[0].outcome == OUTCOME_APPLIED
        after = real_cal.get("CoTE_tHdCtlSp_M_VW").values
        over = before > 90.0
        assert np.all(after[over] < before[over])          # cut applied
        assert np.array_equal(after[~over], before[~over])  # ≤90 untouched
