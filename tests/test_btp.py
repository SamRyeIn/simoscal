"""Unit tests for the BTP patching adapter — synthetic fixtures (plan U6).

These exercise the adapter's own guard / state-machine / confinement logic against
tiny **synthetic** ``.btp`` files and synthetic 4 MB bins, so they need neither the
real 4 MB calibration nor the real switch patch — only the BinToolz source tree the
adapter wraps (``requires_bintoolz``; skips cleanly when absent).

The synthetic ``.btp`` writer (:func:`make_btp`) is built from the format
description in ``knowledge/bintoolz-btp-patching.md`` (100-byte header + per-block
``[offset,length] + original + modified``) using the stdlib ``zlib.crc32`` — it is
fixture-authoring code, **not** a port of BinToolz.
"""

from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

import pytest

from simoscal import btp

from .conftest import requires_bintoolz

pytestmark = requires_bintoolz

BTP_VERSION = "BinToolz Patch v1.1"
BIN_SIZE = 0x400000            # 4 MB, matches BinToolz "Simos 18.1" binSize
BOX_CODE_OFF = 0x200060        # Simos 18.1 boxCodeStart
SW_CODE_OFF = 0x200023         # Simos 18.1 softCodeStart (8 bytes)
CAL_START = 0x200000           # Simos 18.1 block 4 (CAL) binPosition
SW_CODE = "TESTSW01"           # exactly 8 chars

# Two patch blocks: one in ASW (< CAL_START), one in CAL (>= CAL_START), both
# clear of the box/software-code bytes.
ASW_OFF = 0x50000
CAL_OFF = 0x210000
ORIG_ASW = b"\x11\x22\x33\x44"
MOD_ASW = b"\xaa\xbb\xcc\xdd"
ORIG_CAL = b"\x01\x02\x03\x04\x05\x06"
MOD_CAL = b"\xf1\xf2\xf3\xf4\xf5\xf6"
BLOCKS = [(ASW_OFF, ORIG_ASW, MOD_ASW), (CAL_OFF, ORIG_CAL, MOD_CAL)]


# --------------------------------------------------------------------------- #
# synthetic fixture writers (built from the documented .btp format)
# --------------------------------------------------------------------------- #
def make_btp(
    path: Path,
    *,
    software_code: str = SW_CODE,
    file_size: int = BIN_SIZE,
    blocks=BLOCKS,
    corrupt_crc: bool = False,
) -> Path:
    payload = b""
    for off, orig, mod in blocks:
        assert len(orig) == len(mod)
        payload += struct.pack("<II", off, len(orig)) + orig + mod
    crc = zlib.crc32(payload) & 0xFFFFFFFF
    if corrupt_crc:
        crc ^= 0xFFFFFFFF
    header = bytearray(100)
    v = BTP_VERSION.encode("latin1")[:20]
    header[0 : len(v)] = v
    sc = software_code.encode("latin1")[:8]
    header[20 : 20 + len(sc)] = sc
    struct.pack_into("<I", header, 28, len(blocks))
    struct.pack_into("<I", header, 32, crc)
    struct.pack_into("<I", header, 36, file_size)
    path.write_bytes(bytes(header) + payload)
    return path


def make_bin(
    path: Path,
    *,
    software_code: str = SW_CODE,
    seed: str = "original",  # "original" | "modified" | "neither"
    size: int = BIN_SIZE,
) -> Path:
    data = bytearray(size)
    data[BOX_CODE_OFF : BOX_CODE_OFF + 11] = b"TESTBOX0001"
    sc = software_code.encode("latin1")[:8].ljust(8, b"\x00")
    data[SW_CODE_OFF : SW_CODE_OFF + 8] = sc
    for off, orig, mod in BLOCKS:
        if seed == "original":
            data[off : off + len(orig)] = orig
        elif seed == "modified":
            data[off : off + len(mod)] = mod
        else:  # "neither" — bytes that match no reference
            data[off : off + len(orig)] = b"\x77" * len(orig)
    path.write_bytes(bytes(data))
    return path


@pytest.fixture
def synth(tmp_path):
    """A synthetic patch plus original/modified/neither bins in ``tmp_path``."""
    patch = make_btp(tmp_path / "synth.btp")
    return {
        "patch": patch,
        "original": make_bin(tmp_path / "orig.bin", seed="original"),
        "modified": make_bin(tmp_path / "mod.bin", seed="modified"),
        "neither": make_bin(tmp_path / "neither.bin", seed="neither"),
        "dir": tmp_path,
    }


# --------------------------------------------------------------------------- #
# check state machine + read-only guarantee (AE1)
# --------------------------------------------------------------------------- #
class TestCheck:
    def test_ready_to_accept_and_identity(self, synth):
        r = btp.check(synth["original"], synth["patch"])
        assert r.readiness == btp.READY_TO_ACCEPT
        assert r.ready_to_apply and not r.already_patched
        assert r.bin_hardware == "Simos 18.1"
        assert r.bin_software_code == SW_CODE
        assert r.patch.block_count == 2
        assert r.patch.software_code == SW_CODE
        assert r.patch.declared_bytes == len(ORIG_ASW) + len(ORIG_CAL)

    def test_patch_found_on_already_patched_bin(self, synth):
        r = btp.check(synth["modified"], synth["patch"])
        assert r.readiness == btp.PATCH_FOUND
        assert r.already_patched

    def test_not_ready_on_drifted_bin(self, synth):
        assert btp.check(synth["neither"], synth["patch"]).readiness == btp.NOT_READY

    def test_check_leaves_bin_byte_identical(self, synth):
        before = Path(synth["original"]).read_bytes()
        btp.check(synth["original"], synth["patch"])
        assert Path(synth["original"]).read_bytes() == before  # AE1: read-only


# --------------------------------------------------------------------------- #
# identity + integrity guards (AE4)
# --------------------------------------------------------------------------- #
class TestGuards:
    def test_corrupt_crc_fails_loud(self, tmp_path):
        make_bin(tmp_path / "b.bin", seed="original")
        make_btp(tmp_path / "bad.btp", corrupt_crc=True)
        with pytest.raises(btp.PatchIntegrityError, match="CRC32"):
            btp.check(tmp_path / "b.bin", tmp_path / "bad.btp")

    def test_software_code_mismatch_names_both(self, tmp_path):
        make_bin(tmp_path / "b.bin", software_code="TESTSW01", seed="original")
        make_btp(tmp_path / "p.btp", software_code="OTHERSW1")
        with pytest.raises(btp.PatchIdentityError) as exc:
            btp.check(tmp_path / "b.bin", tmp_path / "p.btp")
        assert "TESTSW01" in str(exc.value) and "OTHERSW1" in str(exc.value)

    def test_file_size_mismatch_names_both(self, tmp_path):
        make_bin(tmp_path / "b.bin", seed="original")
        make_btp(tmp_path / "p.btp", file_size=BIN_SIZE - 1)
        with pytest.raises(btp.PatchIdentityError) as exc:
            btp.check(tmp_path / "b.bin", tmp_path / "p.btp")
        assert str(BIN_SIZE) in str(exc.value) and str(BIN_SIZE - 1) in str(exc.value)


# --------------------------------------------------------------------------- #
# apply / remove confinement + round-trip (AE2, AE3)
# --------------------------------------------------------------------------- #
class TestApplyRemove:
    def test_apply_confined_to_declared_blocks(self, synth):
        out = synth["dir"] / "applied.bin"
        res = btp.apply(synth["original"], synth["patch"], out)
        assert res.confined
        assert res.changed_bytes == len(MOD_ASW) + len(MOD_CAL)
        assert res.changed_in_cal == len(MOD_CAL)
        # only the declared regions differ from the input
        src = Path(synth["original"]).read_bytes()
        dst = out.read_bytes()
        changed = [i for i in range(len(dst)) if dst[i] != src[i]]
        declared = {i for off, o, m in BLOCKS for i in range(off, off + len(o))}
        assert set(changed) <= declared
        # the input file was not modified
        assert Path(synth["original"]).read_bytes() == src

    def test_apply_then_remove_round_trip(self, synth):
        applied = synth["dir"] / "applied.bin"
        btp.apply(synth["original"], synth["patch"], applied)
        restored = synth["dir"] / "restored.bin"
        btp.remove(applied, synth["patch"], restored)
        assert restored.read_bytes() == Path(synth["original"]).read_bytes()  # AE3

    def test_apply_reports_both_checksums(self, synth):
        res = btp.apply(synth["original"], synth["patch"], synth["dir"] / "a.bin")
        names = {r.name for r in res.checksum_reports}
        assert names == {"CAL_CRC", "ECM3"}  # AE5: every checksum reported, explicitly

    def test_apply_refused_on_already_patched(self, synth):
        out = synth["dir"] / "x.bin"
        with pytest.raises(btp.PatchStateError, match="PATCH_FOUND"):
            btp.apply(synth["modified"], synth["patch"], out)
        assert not out.exists()  # nothing written

    def test_remove_refused_on_unpatched(self, synth):
        out = synth["dir"] / "x.bin"
        with pytest.raises(btp.PatchStateError):
            btp.remove(synth["original"], synth["patch"], out)
        assert not out.exists()


# --------------------------------------------------------------------------- #
# confinement is real, not vacuous (AE2) — wrapper-level fault injection
# --------------------------------------------------------------------------- #
class TestConfinementNotVacuous:
    def test_change_outside_declared_blocks_is_caught(self, synth, monkeypatch):
        import dataclasses

        real = btp._load_bintoolz(None)
        stray = 0x300000  # far outside any declared block

        class StrayBTP(real.BTP):  # type: ignore[misc,valid-type]
            def changeBin(self, binf, remove, doCalBlock):
                ret = super().changeBin(binf, remove, doCalBlock)
                data = bytearray(binf.data)
                data[stray] ^= 0xFF  # corrupt a byte outside the patch's blocks
                binf.data = bytes(data)
                return ret

        faulty = dataclasses.replace(real, BTP=StrayBTP)
        monkeypatch.setattr(btp, "_load_bintoolz", lambda *a, **k: faulty)

        out = synth["dir"] / "evil.bin"
        with pytest.raises(btp.PatchConfinementError, match="outside"):
            btp.apply(synth["original"], synth["patch"], out)
        assert not out.exists()  # loud failure leaves no partial output


# --------------------------------------------------------------------------- #
# loader / missing dependency (AE7)
# --------------------------------------------------------------------------- #
class TestLoader:
    def test_missing_bintoolz_root_fails_loud(self, tmp_path):
        bogus = tmp_path / "no_such_bintoolz"
        make_bin(tmp_path / "b.bin", seed="original")
        make_btp(tmp_path / "p.btp")
        with pytest.raises(btp.BinToolzNotFound) as exc:
            btp.check(tmp_path / "b.bin", tmp_path / "p.btp", bintoolz_root=bogus)
        assert str(bogus) in str(exc.value)

    def test_no_pyqt6_imported_by_adapter(self, synth):
        # The adapter must never touch BinToolz's GUI-coupled Patch.py.
        btp.check(synth["original"], synth["patch"])
        assert "PyQt6" not in sys.modules
