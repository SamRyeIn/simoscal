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


# Golden multi-patch oracle: a bin Sam produced with the BinToolz Windows GUI by
# applying CBRICK → HSL → switch-patch 29.33 to the R04 BasicsGuide tune. Applying
# the same patches in the same order through the adapter must reproduce it byte for
# byte. All inputs are gitignored / may be absent → the fixture skips.
GOLDEN_MULTIPATCH_BASE = (
    PROJECT_ROOT
    / "Tunes/TuningBasicsGuide/TUNE_Basics_Guide_out"
    / "R04_20260709-140100/5G0906259L_0002_BasicsGuide_R04.bin"
)
GOLDEN_MULTIPATCH_RESULT = (
    PROJECT_ROOT / "References/CB_HSL_SP2933_5G0906259L_0002_BasicsGuide_R04.bin"
)
GOLDEN_MULTIPATCH_PATCHES = (
    BINTOOLZ_ROOT / "patches" / "SL CBRICK v1.2 - S50.btp",
    BINTOOLZ_ROOT / "patches" / "SL HSL v1.1 - S50.btp",
    BINTOOLZ_ROOT / "patches" / "SL PATCH.29.33 - S50.btp",
)


@pytest.fixture(scope="session")
def golden_multipatch() -> dict:
    """``{base, result, patches}`` for the GUI-parity oracle; skips if any absent."""
    needed = [GOLDEN_MULTIPATCH_BASE, GOLDEN_MULTIPATCH_RESULT, *GOLDEN_MULTIPATCH_PATCHES]
    missing = [p for p in needed if not p.is_file()]
    if missing:
        pytest.skip(f"golden multipatch inputs absent: {missing}")
    return {
        "base": GOLDEN_MULTIPATCH_BASE,
        "result": GOLDEN_MULTIPATCH_RESULT,
        "patches": list(GOLDEN_MULTIPATCH_PATCHES),
    }


# Frozen output bins from the R00–R12 revision lineage. The tune-API domain
# modules are distilled from those revisions' private helpers, so the strongest
# check available is that a domain call reproduces the table the corresponding
# historical revision actually produced. They live in the root repo, are
# gitignored, and may be absent → the fixture SKIPS rather than fails.
TUNES_OUT = PROJECT_ROOT / "Tunes" / "TuningBasicsGuide" / "TUNE_Basics_Guide_out"
_PLAIN = "5G0906259L_0002_BasicsGuide_R{}.bin"
_PATCHED = "CB_HSL_SP2933_5G0906259L_0002_BasicsGuide_R{}.bin"
HISTORICAL_BINS = {
    "R07": TUNES_OUT / "R07_20260711-223757" / _PLAIN.format("07"),
    "R08": TUNES_OUT / "R08_20260712-170312" / _PLAIN.format("08"),
    "R09": TUNES_OUT / "R09_20260712-213556" / _PLAIN.format("09"),
    "R10": TUNES_OUT / "R10_20260713-000102" / _PATCHED.format("10"),
    "R11": TUNES_OUT / "R11_20260713-112124" / _PATCHED.format("11"),
    "R12": TUNES_OUT / "R12_20260715-165615" / _PATCHED.format("12"),
}


@pytest.fixture(scope="session")
def historical_bins() -> dict:
    """``{"R07": Path, …}`` for the frozen revision outputs; skips if any absent."""
    missing = [rev for rev, path in HISTORICAL_BINS.items() if not path.is_file()]
    if missing:
        pytest.skip(f"historical revision bins absent: {', '.join(sorted(missing))}")
    return dict(HISTORICAL_BINS)


# Human-reviewed log folders for the analysis acceptance replay (plan U6). They
# live in the root repo (not the Code/ checkout) and may be absent, so the
# fixtures SKIP rather than fail.
ANALYSIS_R01_DIR = PROJECT_ROOT / "Logs" / "BasicsGuide_R01"
ANALYSIS_R04_DIR = PROJECT_ROOT / "Logs" / "BasicsGuide_R04"


@pytest.fixture(scope="session")
def r01_log_dir() -> Path:
    """The human-reviewed R01 log folder; skips if absent from a lean checkout."""
    if not ANALYSIS_R01_DIR.is_dir():
        pytest.skip(f"R01 log folder not present: {ANALYSIS_R01_DIR}")
    return ANALYSIS_R01_DIR


@pytest.fixture(scope="session")
def r04_log_dir() -> Path:
    """The human-reviewed R04 log folder; skips if absent from a lean checkout."""
    if not ANALYSIS_R04_DIR.is_dir():
        pytest.skip(f"R04 log folder not present: {ANALYSIS_R04_DIR}")
    return ANALYSIS_R04_DIR


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
