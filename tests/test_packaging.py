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
# Mobile-closure boundary (Quick Edit V1)
# --------------------------------------------------------------------------- #
# The Android engine embeds simoscal via Chaquopy, which ships numpy but not
# matplotlib or openpyxl. The library must therefore import — and do the whole
# read/edit/build/audit loop — with only numpy present. matplotlib (PNG plots)
# and openpyxl (xlsx export) are optional extras resolved lazily on first use.
# These tests pin that boundary so re-adding a top-level heavy import (which
# would make ``import simoscal`` fail on-device) is caught here.

def _pyproject() -> dict:
    return tomllib.loads((CODE_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


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

    # Touching a plot/export symbol must raise an actionable ImportError that
    # names the extra to install, not a bare ModuleNotFoundError.
    for sym, extra in (("plot_table", "plot"), ("write_xlsx", "export")):
        try:
            getattr(simoscal, sym)
        except ImportError as exc:
            assert extra in str(exc), (sym, str(exc))
        else:
            raise SystemExit(f"{sym} did not raise without its extra")

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
