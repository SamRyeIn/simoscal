"""Acceptance suite — AE1-AE7 for the BTP patching adapter (plan U6).

Each test maps to one acceptance example from the origin doc and runs against the
**real** stock bin, the real ``SL PATCH.29.33 - S50.btp``, and BinToolz's real
source tree + S50 switch-patch XDF. Every test **skips cleanly** (never fails) when
any of those are absent — the ``requires_real_files`` / ``requires_bintoolz`` guards
plus the ``real_patch`` / ``switch_patch_xdf`` fixtures.

    AE1  check is read-only     stock bin byte-identical after check; state correct
    AE2  confined apply         changed bytes all inside the declared blocks
    AE3  round-trip             apply then remove → byte-identical to stock
    AE4  identity guard         a different car's patch is rejected loudly
    AE5  checksum report        CAL_CRC / ECM3 states asserted, not assumed
    AE6  XDF sanity             slot tables resolve on the patched bin, differ from stock
    AE7  missing dependency      bogus bintoolz_root → loud BinToolzNotFound
"""

from __future__ import annotations

from pathlib import Path

import pytest

from simoscal import btp

from .conftest import (
    BINTOOLZ_ROOT,
    REAL_BIN,
    requires_bintoolz,
    requires_real_files,
)

pytestmark = [requires_real_files, requires_bintoolz]


@pytest.fixture(scope="module")
def applied(tmp_path_factory, real_patch):
    """Apply the real switch patch to the real stock bin once; reuse everywhere."""
    if not REAL_BIN.exists():
        pytest.skip("real bin absent")
    out_dir = tmp_path_factory.mktemp("btp_accept")
    out_bin = out_dir / "patched.bin"
    result = btp.apply(REAL_BIN, real_patch, out_bin)
    return {"result": result, "out_bin": out_bin, "dir": out_dir, "patch": real_patch}


# --------------------------------------------------------------------------- #
# AE1 — check is read-only, returns the definitive readiness state
# --------------------------------------------------------------------------- #
class TestAE1CheckReadOnly:
    def test_stock_is_ready_to_accept(self, real_patch):
        r = btp.check(REAL_BIN, real_patch)
        assert r.readiness == btp.READY_TO_ACCEPT
        assert r.bin_hardware == "Simos 18.1"
        assert r.bin_software_code == "SC800S50"
        assert r.patch.block_count == 38

    def test_check_does_not_touch_the_bin(self, real_patch):
        before = REAL_BIN.read_bytes()
        btp.check(REAL_BIN, real_patch)
        assert REAL_BIN.read_bytes() == before


# --------------------------------------------------------------------------- #
# AE2 — apply confined to declared blocks
# --------------------------------------------------------------------------- #
class TestAE2ConfinedApply:
    def test_changes_are_inside_declared_blocks(self, applied):
        res = applied["result"]
        assert res.confined
        assert res.changed_bytes > 0
        src = REAL_BIN.read_bytes()
        dst = Path(applied["out_bin"]).read_bytes()
        assert len(src) == len(dst)
        changed = [i for i in range(len(dst)) if dst[i] != src[i]]
        declared = res.patch.blocks
        outside = [i for i in changed if not any(b.offset <= i < b.end for b in declared)]
        assert outside == []


# --------------------------------------------------------------------------- #
# AE3 — round-trip apply then remove is byte-identical
# --------------------------------------------------------------------------- #
class TestAE3RoundTrip:
    def test_apply_then_remove_restores_stock(self, applied):
        restored = applied["dir"] / "restored.bin"
        btp.remove(applied["out_bin"], applied["patch"], restored)
        assert restored.read_bytes() == REAL_BIN.read_bytes()


# --------------------------------------------------------------------------- #
# AE4 — identity guard rejects a mismatched patch
# --------------------------------------------------------------------------- #
class TestAE4IdentityGuard:
    def test_other_cars_patch_rejected(self, tmp_path):
        # A patch for a different car (V30) has a different software code.
        other = BINTOOLZ_ROOT / "patches" / "SL PATCH.29.33 - V30.btp"
        if not other.is_file():
            pytest.skip(f"comparison patch absent: {other}")
        with pytest.raises(btp.PatchIdentityError):
            btp.check(REAL_BIN, other)


# --------------------------------------------------------------------------- #
# AE5 — checksum state is reported explicitly, never assumed
# --------------------------------------------------------------------------- #
class TestAE5ChecksumReport:
    def test_cal_crc_stale_ecm3_clean_after_apply(self, applied):
        res = applied["result"]
        # U1 finding: the .btp carries no corrected CAL CRC → CAL_CRC goes stale;
        # ECM3 is untouched → stays clean. Both are *reported*, not assumed.
        cal = res.cal_crc
        ecm3 = res.ecm3
        assert cal is not None and cal.can_verify and cal.is_stale
        assert ecm3 is not None and ecm3.can_verify and not ecm3.is_stale

    def test_report_states_each_checksum(self, applied):
        text = btp.format_change_report(applied["result"])
        assert "CAL_CRC" in text and "ECM3" in text
        assert "not-verifiable" in text  # ASW/code blocks stated, not assumed clean


# --------------------------------------------------------------------------- #
# AE6 — switch-patch XDF sanity distinguishes patched from stock
# --------------------------------------------------------------------------- #
class TestAE6XdfSanity:
    def test_patched_bin_slot_tables_plausible(self, applied, switch_patch_xdf):
        s = btp.switch_patch_sanity(
            applied["out_bin"], xdf_path=switch_patch_xdf, stock_bin_path=REAL_BIN
        )
        assert s.tables_resolved > 0
        assert s.tables_decoded == s.tables_resolved
        assert s.decode_errors == ()
        assert s.all_finite
        assert s.differ_from_stock and s.differ_from_stock > 0
        assert s.plausible

    def test_stock_bin_does_not_false_pass(self, switch_patch_xdf):
        # Loaded against itself as the stock reference, nothing differs → not
        # plausible: the sanity check cannot mistake an unpatched bin for patched.
        s = btp.switch_patch_sanity(
            REAL_BIN, xdf_path=switch_patch_xdf, stock_bin_path=REAL_BIN
        )
        assert s.differ_from_stock == 0
        assert not s.plausible


# --------------------------------------------------------------------------- #
# AE7 — missing dependency fails loud (works even with BinToolz present)
# --------------------------------------------------------------------------- #
class TestAE7MissingDependency:
    def test_bogus_bintoolz_root_raises(self, tmp_path, real_patch):
        bogus = tmp_path / "absent_bintoolz"
        with pytest.raises(btp.BinToolzNotFound) as exc:
            btp.check(REAL_BIN, real_patch, bintoolz_root=bogus)
        assert str(bogus) in str(exc.value)


# --------------------------------------------------------------------------- #
# Golden parity — reproduce a BinToolz Windows-GUI multi-patch bin byte-for-byte
# --------------------------------------------------------------------------- #
class TestGoldenGuiParity:
    def test_sequential_patches_match_gui_output(self, golden_multipatch, tmp_path):
        # Applying CBRICK → HSL → switch-patch 29.33 in order to the R04 tune must
        # reproduce the bin Sam made with the BinToolz GUI, byte for byte.
        cur = golden_multipatch["base"]
        for i, patch in enumerate(golden_multipatch["patches"]):
            assert btp.check(cur, patch).readiness == btp.READY_TO_ACCEPT
            out = tmp_path / f"step{i}.bin"
            res = btp.apply(cur, patch, out)
            assert res.confined
            cur = out
        assert Path(cur).read_bytes() == golden_multipatch["result"].read_bytes()
