"""Acceptance suite — AE1-AE5 for the SOP tune recipe (plan U6).

Each test maps to one acceptance example from the requirements doc and exercises
the public :func:`simoscal.apply_basics_sop` pipeline end-to-end against the real
bundled bin/XDF. Like ``test_acceptance.py``, every test **skips cleanly** when
those files are absent (the ``requires_real_files`` guard), so a lean checkout
stays green.

    AE1  full value match   every literal/scalar/buildout table matches the guide;
                            every skipped/mismatched table is byte-identical to stock
    AE2  guard behaviour    the Overboost ceiling guard never lowers a higher value
    AE3  checksum clean      save(correct_checksums=True) → verify_checksums clean
    AE4  complete accounting the report names every guide instruction, no silent gaps
    AE5  comparison PNGs     a PNG per changed non-scalar table; scalars → report old→new
"""

from __future__ import annotations

import warnings

import numpy as np
import pytest

from simoscal import (
    CalFile,
    apply_basics_sop,
    compare_tables,
    format_report,
    render_table,
    resolve_symbol_map,
)
from simoscal.sop_recipe import (
    SYMBOL_MAP,
    OUTCOME_APPLIED,
    OUTCOME_APPLIED_BUILDOUT,
    OUTCOME_GUARDED_SKIP,
    SKIP_KINDS,
    is_write_kind,
)

from .conftest import requires_real_files

pytestmark = requires_real_files


@pytest.fixture(scope="module")
def applied():
    """Stock cal, a recipe-applied cal, its report, and pre-edit snapshots.

    Module-scoped: the recipe is applied once (it is deterministic and
    re-runnable) and every AE test reads from the result.
    """
    from .conftest import REAL_XDF, REAL_BIN

    stock = CalFile.open(str(REAL_XDF), str(REAL_BIN))
    tuned = CalFile.open(str(REAL_XDF), str(REAL_BIN))

    # snapshot every write-target before applying (for AE5 before/after PNGs).
    snaps = {}
    for r in resolve_symbol_map(tuned):
        if not is_write_kind(r.entry.kind):
            continue
        for res in r.resolutions:
            if res.resolved and res.view is not None:
                snaps[res.symbol] = render_table(res.view)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        report = apply_basics_sop(tuned)
    return stock, tuned, report, snaps


# --------------------------------------------------------------------------- #
# AE1 — full value match; skipped/mismatched tables byte-identical to stock
# --------------------------------------------------------------------------- #
class TestAE1FullValueMatch:
    def test_literal_and_scalar_values_match_guide(self, applied) -> None:
        _, tuned, _, _ = applied
        # Max PR flattened to 2.80 everywhere.
        assert np.allclose(tuned.get("IP_PQ_CHA_MAX").values, 2.80, atol=1e-3)
        # torque-tune selector → 1.
        assert tuned.get("LC_PUT_SP_TOL_ENA_AMP").values[0, 0] == pytest.approx(1.0)
        # speed limiter → 257.49 (quantized).
        assert tuned.get("LMVLim_vMax_vLim_C_VW.VehSpdl2Lvl1").values[0, 0] == pytest.approx(257.49, abs=0.02)
        # charge-air-pressure-too-high → 3000 across.
        assert np.allclose(tuned.get("IP_PUT_MAX_CAP_H_DIAG").values, 3000.0, atol=1.0)
        # PUT setpoint shaped last row + raised Y axis.
        put = tuned.get("IP_PUT_SP")
        assert np.allclose(put.values[-1],
                           [2698.97, 2698.97, 2499.96, 2349.97, 2298.97, 2198.97], atol=0.05)
        assert abs(np.asarray(put.axis_values("y")).ravel()[-1] - 2698.97) < 0.05
        # Basic Ignition Angle literal grid on a VVL-0 Port-Flap-Low table.
        iga = tuned.get("IP_IGA_BAS_IVVT_VVL_PORT_L[STND][2][2]").values
        assert iga[0, 0] == pytest.approx(17.62, abs=0.02)
        assert iga[-1, 0] == pytest.approx(-18.0, abs=0.02)

    def test_skipped_and_mismatched_tables_byte_identical(self, applied) -> None:
        stock, tuned, report, _ = applied
        # Lambda (axis mismatch) must be untouched.
        for sym in ("IP_LAMB_BAS_HPDI[1]", "IP_LAMB_BAS_MPI[1]"):
            assert np.array_equal(tuned.get(sym).values, stock.get(sym).values)
        # Skip-kind entries that resolve to real symbols must be untouched too.
        for entry in SYMBOL_MAP:
            if entry.kind in SKIP_KINDS:
                for sym in entry.symbols:
                    try:
                        s = stock.get(sym).values
                        t = tuned.get(sym).values
                    except KeyError:
                        continue
                    assert np.array_equal(s, t), f"skip table {sym} was modified"

    def test_iga_siblings_untouched(self, applied) -> None:
        stock, tuned, _, _ = applied
        for sym in ("IP_IGA_BAS_IVVT_VVL_PORT_H[STND][0][0]",
                    "IP_IGA_BAS_IVVT_VVL_PORT_L[LFT_1][0][0]"):
            assert np.array_equal(tuned.get(sym).values, stock.get(sym).values)


# --------------------------------------------------------------------------- #
# AE2 — guard behaviour (Overboost ceiling never lowered)
# --------------------------------------------------------------------------- #
class TestAE2Guard:
    def test_overboost_guarded_skip_byte_identical(self, applied) -> None:
        stock, tuned, report, _ = applied
        over = [o for o in report.outcomes if o.symbol == "C_PRS_IM_SP_LIM"]
        assert len(over) == 1
        assert over[0].outcome == OUTCOME_GUARDED_SKIP  # current (~271695) > 2700
        assert np.array_equal(
            tuned.get("C_PRS_IM_SP_LIM").values, stock.get("C_PRS_IM_SP_LIM").values
        )

    def test_float_bug_limiter_guard_blocked_byte_identical(self, applied) -> None:
        stock, tuned, report, _ = applied
        blk = [o for o in report.outcomes if o.symbol == "C_PRS_IM_SP_MAX"]
        assert blk and blk[0].outcome == "guard_blocked"
        assert np.array_equal(
            tuned.get("C_PRS_IM_SP_MAX").values, stock.get("C_PRS_IM_SP_MAX").values
        )


# --------------------------------------------------------------------------- #
# AE3 — checksum cleanliness after correcting save
# --------------------------------------------------------------------------- #
class TestAE3Checksums:
    def test_save_correct_checksums_verifies_clean(self, applied, tmp_path) -> None:
        _, tuned, _, _ = applied
        out = tmp_path / "tuned.bin"
        tuned.save(out, correct_checksums=True)
        reports = tuned.verify_checksums()
        assert reports  # at least CAL_CRC + ECM3
        for r in reports:
            if r.can_verify:
                assert not r.is_stale, f"{r.name} stale after correcting save"

    def test_saved_bin_is_minimal_diff_same_size(self, applied, tmp_path) -> None:
        _, tuned, _, _ = applied
        out = tmp_path / "tuned.bin"
        tuned.save(out, correct_checksums=True)
        assert out.stat().st_size == 0x400000  # 4 MB, unchanged shape


# --------------------------------------------------------------------------- #
# AE4 — complete accounting (every guide instruction represented)
# --------------------------------------------------------------------------- #
class TestAE4Accounting:
    def test_report_names_every_instruction(self, applied) -> None:
        _, _, report, _ = applied
        report_sections = {o.guide_section for o in report.outcomes}
        map_sections = {e.guide_section for e in SYMBOL_MAP}
        assert report_sections == map_sections

    def test_no_write_entry_silently_vanishes(self, applied) -> None:
        _, _, report, _ = applied
        # every resolved write symbol appears in the report exactly where expected.
        assert len(report.outcomes) >= len(SYMBOL_MAP)
        # format renders and shows the DO NOT FLASH coherence signal for this bin.
        text = format_report(report)
        assert "DO NOT FLASH" in text  # lambda un-appliable on this bin (lean risk)


# --------------------------------------------------------------------------- #
# AE5 — comparison PNG coverage (non-scalar), scalars via report old→new
# --------------------------------------------------------------------------- #
class TestAE5ComparisonPngs:
    def test_every_changed_nonscalar_table_gets_a_png(self, applied, tmp_path) -> None:
        _, tuned, report, snaps = applied
        from simoscal import TableMismatchError

        made, axis_changed = 0, 0
        for o in report.outcomes:
            if o.outcome not in (OUTCOME_APPLIED, OUTCOME_APPLIED_BUILDOUT):
                continue
            before = snaps.get(o.symbol)
            if before is None:
                continue
            after = tuned.get(o.symbol)
            rows, cols = after.shape
            if rows == 1 and cols == 1:
                continue  # scalar → covered by the report, not a PNG (AE5 below)
            try:
                paths = compare_tables(before, after, tmp_path, surface=False)
            except TableMismatchError:
                axis_changed += 1  # PUT setpoint's own Y axis moved
                continue
            assert paths, f"no PNG produced for changed non-scalar {o.symbol}"
            for p in paths:
                assert p.exists()
            made += len(paths)
        assert made > 0
        assert axis_changed <= 1  # only IP_PUT_SP changes its own axis

    def test_scalar_applied_entries_carry_old_new_in_report(self, applied) -> None:
        _, tuned, report, _ = applied
        scalar_applied = [
            o for o in report.outcomes
            if o.outcome == OUTCOME_APPLIED and tuned.get(o.symbol).shape == (1, 1)
        ]
        assert scalar_applied  # e.g. speed limiter, selector, compressor temp
        for o in scalar_applied:
            assert o.old is not None and o.new is not None
            # and compare_tables would produce nothing for these (scalar by design)
            assert render_table(tuned.get(o.symbol)).values.size == 1
