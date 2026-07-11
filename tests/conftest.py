"""Shared fixtures for the simoscal test suite (U6).

The acceptance suite runs end-to-end against the real bundled files:

    xdf/SC8S50.V1.0.xdf      the TunerPro definition
    bin/5G0906259L__0002.bin the 4 MB stock calibration

Both live at the repo root (``Code/``). They are large binaries that may be
absent from a lean checkout, so every fixture that needs them **skips cleanly
rather than failing** when they are missing — the TunerPro-free acceptance
subset (AE2-AE5) then simply doesn't collect, and CI on a machine without the
files stays green.

AE1 (TunerPro parity) additionally needs a one-time capture,
``tests/fixtures/tunerpro_oracle.json``, recorded on Windows (see
``tests/fixtures/README.md``). It is gated behind the ``tunerpro`` marker and
the :func:`tunerpro_oracle` fixture, both of which skip when the capture is
absent — the default state on the Mac dev box.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# Repo root is one level above Code/tests/.
REPO_ROOT = Path(__file__).resolve().parents[1]
# Project root (holds Code/ and the vendored BinToolz-main/) is one level above that.
PROJECT_ROOT = REPO_ROOT.parent
REAL_XDF = REPO_ROOT / "xdf" / "SC8S50.V1.0.xdf"
REAL_BIN = REPO_ROOT / "bin" / "5G0906259L__0002.bin"
TUNERPRO_ORACLE = Path(__file__).resolve().parent / "fixtures" / "tunerpro_oracle.json"

# Vendored BinToolz tree + the real switch patch / switch-patch XDF for the BTP
# adapter tests (plan U6). All are gitignored / may be absent from a lean
# checkout, so the fixtures below SKIP (never fail) when missing.
BINTOOLZ_ROOT = PROJECT_ROOT / "BinToolz-main"
REAL_PATCH = BINTOOLZ_ROOT / "patches" / "SL PATCH.29.33 - S50.btp"
SWITCH_PATCH_XDF = BINTOOLZ_ROOT / "definitions" / "S50 Switch Patch.29.33.V2.xdf"

# Reusable skip guard for the real-file acceptance tests.
requires_real_files = pytest.mark.skipif(
    not (REAL_XDF.exists() and REAL_BIN.exists()),
    reason=f"real XDF/BIN not present ({REAL_XDF}, {REAL_BIN})",
)

# BinToolz is imported at runtime by the adapter; without its source tree the
# adapter cannot run, so its tests skip rather than fail (AE7's failure mode is
# covered separately by pointing the loader at a bogus path).
requires_bintoolz = pytest.mark.skipif(
    not (BINTOOLZ_ROOT / "source").is_dir(),
    reason=f"BinToolz source not present ({BINTOOLZ_ROOT / 'source'})",
)


@pytest.fixture(scope="session")
def bintoolz_root() -> Path:
    """Path to the vendored BinToolz tree; skips the test if it is not present."""
    if not (BINTOOLZ_ROOT / "source").is_dir():
        pytest.skip(f"BinToolz source not present: {BINTOOLZ_ROOT / 'source'}")
    return BINTOOLZ_ROOT


@pytest.fixture(scope="session")
def real_patch() -> Path:
    """Path to the real ``SL PATCH.29.33 - S50.btp``; skips if it is not present."""
    if not REAL_PATCH.is_file():
        pytest.skip(f"real switch patch not present: {REAL_PATCH}")
    return REAL_PATCH


@pytest.fixture(scope="session")
def switch_patch_xdf() -> Path:
    """Path to BinToolz's S50 switch-patch XDF; skips if it is not present."""
    if not SWITCH_PATCH_XDF.is_file():
        pytest.skip(f"switch-patch XDF not present: {SWITCH_PATCH_XDF}")
    return SWITCH_PATCH_XDF


@pytest.fixture(scope="session")
def real_xdf() -> Path:
    """Path to the real SC8S50 XDF; skips the test if it is not present."""
    if not REAL_XDF.exists():
        pytest.skip(f"real XDF not present: {REAL_XDF}")
    return REAL_XDF


@pytest.fixture(scope="session")
def real_bin() -> Path:
    """Path to the real 4 MB stock bin; skips the test if it is not present."""
    if not REAL_BIN.exists():
        pytest.skip(f"real BIN not present: {REAL_BIN}")
    return REAL_BIN


@pytest.fixture
def real_cal(real_xdf: Path, real_bin: Path):
    """A freshly opened :class:`~simoscal.CalFile` over the real files.

    Function-scoped so each test gets an independent, unedited image — edits
    stage into the in-memory buffer and must not leak between tests.
    """
    from simoscal import CalFile

    return CalFile.open(str(real_xdf), str(real_bin))


@pytest.fixture(scope="session")
def tunerpro_oracle() -> dict:
    """The captured TunerPro oracle (``tunerpro_oracle.json``), or skip.

    Schema is documented in ``tests/fixtures/README.md``. Skips when the
    capture is absent so AE1 never *fails* for lack of a Windows session.
    """
    if not TUNERPRO_ORACLE.exists():
        pytest.skip(
            "tunerpro_oracle.json not captured; see tests/fixtures/README.md "
            "for the one-time recording procedure"
        )
    try:
        data = json.loads(TUNERPRO_ORACLE.read_text())
    except json.JSONDecodeError as exc:  # pragma: no cover - malformed capture
        pytest.fail(f"tunerpro_oracle.json is not valid JSON: {exc}")
    tables = data.get("tables")
    if not tables:
        pytest.fail("tunerpro_oracle.json has no 'tables' entries")
    return data
