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


@pytest.fixture(scope="module")
def mini_cal() -> CalFile:
    """A tiny CalFile with decodable bytes (mirrors the export test's fixture).

    Provides ``SYM_10X10`` (happy resolve), ``SYM_DUP`` (ambiguous) and lets a
    bogus symbol exercise the missing path — none of which needs the real bin.
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
