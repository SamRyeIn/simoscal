"""Guard the distribution boundary declared in ``pyproject.toml``.

A wheel installs exactly the packages named in ``[tool.setuptools] packages``.
An explicit list disables setuptools' auto-discovery, so a subpackage added to
the source tree but not to that list is silently dropped from every non-editable
install — importable in the source checkout and in editable/test runs, and
absent only where it is hardest to notice (a real install). ``simoscal.tune``'s
lazily-imported ``domains`` package was omitted exactly this way (CR-20260720-03).

This test pins the declared list to the packages actually on disk, so the next
omission fails here instead of at a user's first ``tune.boost`` access.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import tomllib
from pathlib import Path

CODE_ROOT = Path(__file__).resolve().parents[1]


def _discovered_packages() -> set[str]:
    """Every importable package under ``simoscal/`` (a dir with ``__init__.py``)."""
    pkg_root = CODE_ROOT / "simoscal"
    packages = set()
    for init in pkg_root.rglob("__init__.py"):
        rel = init.parent.relative_to(CODE_ROOT)
        packages.add(".".join(rel.parts))
    return packages


def _declared_packages() -> set[str]:
    data = tomllib.loads((CODE_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return set(data["tool"]["setuptools"]["packages"])


def test_every_source_package_is_declared_for_distribution() -> None:
    discovered = _discovered_packages()
    declared = _declared_packages()
    missing = discovered - declared
    assert not missing, (
        "packages present on disk but absent from pyproject's package list "
        f"(they will be dropped from any wheel/install): {sorted(missing)}"
    )


def test_no_declared_package_is_missing_from_disk() -> None:
    declared = _declared_packages()
    discovered = _discovered_packages()
    stale = declared - discovered
    assert not stale, (
        f"pyproject declares packages that no longer exist on disk: {sorted(stale)}"
    )


# --------------------------------------------------------------------------- #
# Mobile-closure boundary
# --------------------------------------------------------------------------- #
# The Android engine embeds simoscal via Chaquopy, which ships numpy but not
# matplotlib or openpyxl. The library must therefore import — and do the whole
# read/edit/build/audit loop — with only numpy present. matplotlib (PNG plots)
# and openpyxl (xlsx export) are optional extras resolved lazily on first use.
# These tests pin that boundary so re-adding a top-level heavy import (which
# would make ``import simoscal`` fail on-device) is caught here.

def _pyproject() -> dict:
    return tomllib.loads((CODE_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_every_demo_and_script_byte_compiles() -> None:
    """The runnable scripts outside ``simoscal/`` must at least parse.

    Nothing imports ``demos/`` during a test run, so a broken one is invisible
    until a person runs it. ``demos/apply_sop_recipe.py`` sat with an import
    statement spliced into the middle of a parenthesised import list — a hard
    SyntaxError — from the U2 structure refactor until U3 found it.
    """
    scripts = sorted(CODE_ROOT.glob("demos/*.py")) + sorted(CODE_ROOT.glob("*.py"))
    assert scripts, "no scripts found to compile — the glob is wrong"
    broken = []
    for path in scripts:
        try:
            compile(path.read_text(encoding="utf-8"), str(path), "exec")
        except SyntaxError as exc:
            broken.append(f"{path.relative_to(CODE_ROOT)}:{exc.lineno}: {exc.msg}")
    assert not broken, "scripts that do not parse:\n  " + "\n  ".join(broken)


def test_core_dependencies_are_numpy_only() -> None:
    """The always-installed deps must be numpy alone — nothing heavy."""
    core = _pyproject()["project"]["dependencies"]
    names = {d.split(">")[0].split("<")[0].split("=")[0].split("[")[0].strip().lower() for d in core}
    assert names == {"numpy"}, (
        "core dependencies must be numpy-only so the library imports on-device "
        f"(Chaquopy has no matplotlib/openpyxl); found: {sorted(names)}"
    )


def test_heavy_dependencies_live_behind_extras() -> None:
    """matplotlib and openpyxl must be declared only as optional extras."""
    extras = _pyproject()["project"]["optional-dependencies"]
    plot = " ".join(extras["plot"]).lower()
    export = " ".join(extras["export"]).lower()
    assert "matplotlib" in plot, "matplotlib must be declared in the 'plot' extra"
    assert "openpyxl" in export, "openpyxl must be declared in the 'export' extra"


# The pin guarding CR-20260724-05 — the embedded kernel must use the exact NumPy
# proven by the cross-runtime parity gate — was asserted here by reading the
# client's Gradle file. That file left this repository on 2026-08-18, so the
# assertion moved to the client repo, where the file it guards actually lives.
# The two tests above still pin what this repo owns: the dependency closure that
# makes an embedded install possible at all.


# A subprocess runs a fresh interpreter with matplotlib/openpyxl hard-blocked at
# import, proving the closure holds independent of what the test env happens to
# have installed. The blocker is a meta-path finder that raises ImportError for
# those roots.
_CLOSURE_SCRIPT = textwrap.dedent(
    """
    import sys, importlib.abc, importlib.machinery

    _BLOCKED = {"matplotlib", "openpyxl"}

    class _Blocker(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            root = name.split(".")[0]
            if root in _BLOCKED:
                raise ImportError(f"blocked for mobile-closure test: {name}")
            return None

    sys.meta_path.insert(0, _Blocker())

    # The whole read/edit/build substrate must import with numpy only.
    import simoscal
    import simoscal.tune
    import simoscal.analysis

    # numpy is genuinely available; the heavy extras are genuinely blocked.
    import numpy  # noqa: F401
    for mod in ("matplotlib", "openpyxl"):
        try:
            __import__(mod)
        except ImportError:
            pass
        else:
            raise SystemExit(f"blocker failed: {mod} imported")

    # Touching a plot symbol and invoking the xlsx writer must raise actionable
    # errors that name the extra, not a bare ModuleNotFoundError.
    for sym, extra in (("plot_table", "plot"),):
        try:
            getattr(simoscal, sym)
        except ImportError as exc:
            assert extra in str(exc), (sym, str(exc))
        else:
            raise SystemExit(f"{sym} did not raise without its extra")
    try:
        simoscal.write_xlsx([], "never-written.xlsx")
    except ImportError as exc:
        assert "export" in str(exc), str(exc)
    else:
        raise SystemExit("write_xlsx did not raise without its extra")

    print("MOBILE_CLOSURE_OK")
    """
)


def test_library_imports_and_operates_without_heavy_extras() -> None:
    proc = subprocess.run(
        [sys.executable, "-c", _CLOSURE_SCRIPT],
        capture_output=True,
        text=True,
        cwd=str(CODE_ROOT),
    )
    assert proc.returncode == 0 and "MOBILE_CLOSURE_OK" in proc.stdout, (
        "importing simoscal (+ tune + analysis) with matplotlib/openpyxl blocked "
        f"must succeed and give actionable errors.\nSTDOUT:\n{proc.stdout}\n"
        f"STDERR:\n{proc.stderr}"
    )


# --------------------------------------------------------------------------- #
# Installed-wheel closure — the strong boundary
# --------------------------------------------------------------------------- #
# The closure tests above import from the *source checkout* (cwd=CODE_ROOT), so
# every subpackage is present on disk regardless of what a real wheel would
# carry. `test_every_source_package_is_declared_for_distribution` catches a
# declaration mismatch, but nothing above proves an actually-built wheel, once
# installed into a clean location, still imports the whole on-device closure.
# implementation_details.md flagged this exact gap ("checks package declarations
# and import closure rather than installing a wheel in a separate environment").
#
# This test builds a wheel, installs ONLY simoscal (--no-deps) into an isolated
# target, then imports the mobile closure from *that installed tree* — cwd is a
# neutral dir so the source checkout can never satisfy the import, and it asserts
# `simoscal.__file__` resolves under the install target. A subpackage dropped
# from the wheel (the CR-20260720-03 failure mode) fails here at its real blast
# radius, not only at the declaration level.

# Every module the Android engine must be able to import with numpy only. Any
# subpackage silently dropped from the wheel makes one of these fail on-device.
_MOBILE_CLOSURE_MODULES = (
    "simoscal",
    "simoscal.preflight",
    "simoscal.bridge",
    "simoscal.tune",
    "simoscal.tune.domains",
    "simoscal.tune.domains.switchpatch",
    "simoscal.tune.profiles",
    "simoscal.tune.recovery",
    "simoscal.analysis",
)

_INSTALLED_CLOSURE_SCRIPT = textwrap.dedent(
    """
    import importlib, sys, importlib.abc

    _BLOCKED = {"matplotlib", "openpyxl"}

    class _Blocker(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name.split(".")[0] in _BLOCKED:
                raise ImportError(f"blocked for installed-wheel test: {name}")
            return None

    sys.meta_path.insert(0, _Blocker())

    target = sys.argv[1]
    modules = sys.argv[2:]
    for name in modules:
        mod = importlib.import_module(name)
        origin = getattr(mod, "__file__", "") or ""
        # Prove we imported the INSTALLED wheel, not any source tree that may
        # linger on sys.path — every closure module must live under the target.
        if not origin.startswith(target):
            raise SystemExit(f"{name} imported from {origin!r}, not the wheel at {target!r}")

    print("INSTALLED_CLOSURE_OK")
    """
)


def test_built_wheel_installs_and_imports_the_whole_mobile_closure(tmp_path) -> None:
    """Build a real wheel, install it isolated, import the on-device closure from it.

    Stronger than the source-tree closure test: it fails if the wheel omits a
    subpackage even though that package is present on disk.
    """
    wheel_dir = tmp_path / "wheels"
    built = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps",
         "--wheel-dir", str(wheel_dir), str(CODE_ROOT)],
        capture_output=True, text=True,
    )
    assert built.returncode == 0, f"wheel build failed:\n{built.stdout}\n{built.stderr}"
    wheels = sorted(wheel_dir.glob("simoscal-*.whl"))
    assert len(wheels) == 1, f"expected exactly one simoscal wheel, found {wheels}"

    target = tmp_path / "site"
    installed = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--no-deps",
         "--target", str(target), str(wheels[0])],
        capture_output=True, text=True,
    )
    assert installed.returncode == 0, f"wheel install failed:\n{installed.stdout}\n{installed.stderr}"

    # Run from a neutral cwd with the install target leading sys.path (PYTHONPATH
    # is prepended before site-packages, so the wheel's simoscal wins; numpy is
    # supplied by the ambient env exactly as Chaquopy supplies it on-device).
    env = {**os.environ, "PYTHONPATH": str(target)}
    proc = subprocess.run(
        [sys.executable, "-c", _INSTALLED_CLOSURE_SCRIPT, str(target), *_MOBILE_CLOSURE_MODULES],
        capture_output=True, text=True, cwd=str(tmp_path), env=env,
    )
    assert proc.returncode == 0 and "INSTALLED_CLOSURE_OK" in proc.stdout, (
        "an installed wheel must import the whole numpy-only mobile closure "
        f"from the wheel itself.\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )


def test_plot_extra_does_not_require_openpyxl() -> None:
    """Plotting must import with matplotlib present and only openpyxl blocked."""
    script = textwrap.dedent(
        """
        import sys, importlib.abc

        class _Blocker(importlib.abc.MetaPathFinder):
            def find_spec(self, name, path=None, target=None):
                if name.split(".")[0] == "openpyxl":
                    raise ImportError(f"blocked for plot-extra test: {name}")
                return None

        sys.meta_path.insert(0, _Blocker())
        from simoscal import plot_table
        assert callable(plot_table)
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=str(CODE_ROOT),
    )
    assert proc.returncode == 0, (
        "the plot extra must not import the xlsx-only openpyxl dependency.\n"
        f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )
