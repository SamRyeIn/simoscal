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
