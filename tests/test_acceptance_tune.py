"""Acceptance tests for the tune API (plan U6 / requirement AE1).

The claim under test: a revision written in the new style reproduces the old
authoring path **exactly**. ``TUNE_Basics_Guide_R13.py`` re-declares the whole
R00–R12 calibration as one flat script; its output bin must equal R12's byte
for byte.

Byte identity is the only version of this claim worth making. Table-level
comparison would miss a stray write outside the tables anyone thought to check
— which is the failure mode the whole layer exists to prevent.

The revision script and the frozen R12 bin live in the root repo and may be
absent from a lean checkout, so these skip rather than fail.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from tests.conftest import PROJECT_ROOT, requires_bintoolz

R13_SCRIPT = (
    PROJECT_ROOT / "Tunes" / "TuningBasicsGuide" / "TUNE_Basics_Guide_R13.py"
)

pytestmark = requires_bintoolz


def _load_r13() -> ModuleType:
    if not R13_SCRIPT.is_file():
        pytest.skip(f"R13 revision script absent: {R13_SCRIPT}")
    spec = importlib.util.spec_from_file_location("tune_basics_guide_r13", R13_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


@pytest.fixture(scope="module")
def r13() -> ModuleType:
    return _load_r13()


def test_r13_imports_nothing_from_another_revision_script() -> None:
    """The self-containment requirement, checked in the source itself.

    R12 imported private helpers from five earlier revisions and monkey-patched
    one of them. A revision that reaches into another is not a one-page
    declaration of what it flashes, whatever it looks like at the top.
    """
    if not R13_SCRIPT.is_file():
        pytest.skip(f"R13 revision script absent: {R13_SCRIPT}")

    import ast

    tree = ast.parse(R13_SCRIPT.read_text())
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")

    offenders = [name for name in imported if "TUNE_Basics_Guide" in name]
    assert not offenders, f"R13 imports from other revision scripts: {offenders}"
    # And it really does use the tune API, rather than reimplementing it.
    assert any(name.startswith("simoscal.tune") for name in imported)


def test_r13_reproduces_the_r12_bin_byte_for_byte(
    r13: ModuleType, real_xdf: Path, real_bin: Path, tmp_path: Path
) -> None:
    """AE1: the new authoring path is equivalent to the old one, exactly."""
    if not r13.R12_REFERENCE.is_file():
        pytest.skip(f"frozen R12 bin absent: {r13.R12_REFERENCE}")
    if not all(p.path.is_file() for p in r13.PATCHES):
        pytest.skip("BinToolz .btp patches absent")

    from simoscal.tune import SC8S50, SWITCH_PATCH_2933, Tune, build
    from simoscal.tune.domains.switchpatch import PATCH_SPACE

    tune = Tune.open(
        SC8S50, xdf=r13.XDF_PATH, bin=r13.BIN_PATH, patches=r13.PATCHES,
        extra_spaces={PATCH_SPACE: (SWITCH_PATCH_2933, r13.SWITCH_XDF)},
    )
    r13.declare(tune)
    result = build(
        tune, "R13", out_root=tmp_path, bin_name=r13.OUT_BIN_NAME,
        reference_bin=r13.R12_REFERENCE, plots=False,
    )

    assert result.ok
    assert result.diff is not None
    assert result.diff.changed == 0, (
        f"{result.diff.changed} byte(s) differ from R12: {result.diff.summary()}"
    )
    assert result.bin_path.read_bytes() == r13.R12_REFERENCE.read_bytes()


def test_r13_build_passes_every_gate(
    r13: ModuleType, real_xdf: Path, tmp_path: Path
) -> None:
    """AE6: the standard artifact set and every verdict, from domain calls alone."""
    if not r13.R12_REFERENCE.is_file():
        pytest.skip(f"frozen R12 bin absent: {r13.R12_REFERENCE}")
    if not all(p.path.is_file() for p in r13.PATCHES):
        pytest.skip("BinToolz .btp patches absent")

    from simoscal.tune import SC8S50, SWITCH_PATCH_2933, Tune, build
    from simoscal.tune.domains.switchpatch import PATCH_SPACE

    tune = Tune.open(
        SC8S50, xdf=r13.XDF_PATH, bin=r13.BIN_PATH, patches=r13.PATCHES,
        extra_spaces={PATCH_SPACE: (SWITCH_PATCH_2933, r13.SWITCH_XDF)},
    )
    r13.declare(tune)
    result = build(
        tune, "R13", out_root=tmp_path, bin_name=r13.OUT_BIN_NAME,
        reference_bin=r13.R12_REFERENCE, plots=False,
    )

    assert result.checksums_clean
    assert result.readback_failures == ()
    assert tune.journal.blocked() == ()
    assert result.report_path.is_file()

    report = result.report_path.read_text()
    # AE4: the journal names tables as `ID` — Description, with the gates.
    assert "`IP_PUT_SP` — Pressure up throttle setpoint" in report
    assert "`C_M_AIR_CYL_SP_MAX`" in report
    assert "switch-patch sanity" in report
    assert "unexplained = 0" in report
    # AE5: the airmass cap is recorded as the kg/stk conversion it performed.
    assert "0.002" in report and "kg/stk" in report


def test_r13_declaration_stays_about_a_page(r13: ModuleType) -> None:
    """The readability requirement: the calibration fits on one screen.

    Counts statements in ``declare()``, not lines of the file — the constants
    above it are the calibration's *values*, which a reviewer wants spelled
    out, while ``declare()`` is its *structure*.
    """
    import inspect

    body = inspect.getsource(r13.declare)
    calls = [
        line for line in body.splitlines()
        if line.strip().startswith("tune.")
    ]
    assert 10 <= len(calls) <= 30, f"{len(calls)} domain calls in declare()"


def test_r13_every_calibration_call_declares_intent() -> None:
    """CR-20260720-04: the authoring rule — an explicit ``intent=`` on every write.

    ``CLAUDE.md`` names R13 the template for R14 onward and requires physical
    units, named constants, and an explicit ``intent=`` on every
    calibration-changing domain call. A copied template teaches by example, so
    the rule has to hold in the source, not just in the prose. Exempt: the bulk
    SOP pass (``apply_basics_sop``, journaled per table with its own reasons)
    and gates that move no calibration bytes (``require_sanity``).
    """
    if not R13_SCRIPT.is_file():
        pytest.skip(f"R13 revision script absent: {R13_SCRIPT}")

    import ast

    tree = ast.parse(R13_SCRIPT.read_text())
    declare = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "declare"
    )

    exempt = {"require_sanity"}
    missing: list[str] = []
    for node in ast.walk(declare):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # A calibration write is ``tune.<domain>.<method>(...)`` — a two-level
        # attribute rooted at the ``tune`` parameter. ``tune.apply_basics_sop``
        # is one level and so is not counted.
        if not (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Attribute)
            and isinstance(func.value.value, ast.Name)
            and func.value.value.id == "tune"
        ):
            continue
        if func.attr in exempt:
            continue
        if not any(kw.arg == "intent" for kw in node.keywords):
            missing.append(f"tune.{func.value.attr}.{func.attr}")

    assert not missing, (
        "R13 calibration calls missing an explicit intent= keyword "
        f"(CLAUDE.md authoring rule): {missing}"
    )
