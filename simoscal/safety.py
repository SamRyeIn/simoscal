"""Edit-safety policy: warn+allow, float-bug guard, raw-range guard (U4).

Implements the two safety decisions from the plan, both grounded in the mandate
*fail loud, change nothing silently*:

* **Warn + allow (Decision 8, Q1).** A write whose physical value falls outside
  the table's declared *display* ``<min>``/``<max>`` still succeeds — tuners
  deliberately exceed conservative XDF limits — but emits a structured
  :class:`EditRangeWarning` naming the table, cell, value, and limit. The value
  is never silently altered.
* **Float-bug hard guard (Decision 9).** A small, explicit flagged-list of
  overboost / max-airmass calibrations hard-rejects a write that exceeds the
  table's declared upper limit — **even with** ``override=True`` — because the
  SOP calls this corruption case irreversible. *Which* symbols those are is a
  per-car fact, so this module does not hold the list: the caller supplies the
  active profile's :attr:`~simoscal.tune.profile.Profile.float_bug_symbols`.

Separately, the **raw-range guard** hard-fails (:class:`RawRangeError`) any
inverted value that would overflow the element's integer width, for *every*
table — writing wrapped bytes would silently corrupt the cell.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from typing import Collection

import numpy as np

from .model import EmbeddedData, FloatBugGuardError, RawRangeError, Table

__all__ = [
    "EditRangeWarning",
    "RangeBreach",
    "is_float_bug_table",
    "check_raw_fits",
    "check_display_range",
]

# The flagged-list of overboost / max-airmass constants used to live here, as a
# module global naming four ``SC8S50.V1.0.xdf`` symbols. That made this module
# single-car: a second calibration's flagged symbols had nowhere to go, and the
# same four symbols were *also* tagged ``TAG_FLOAT_BUG`` on their profile specs,
# so the two could drift with nothing to catch it. The profile is now the only
# place a table is flagged; every function here takes the set from its caller.


class EditRangeWarning(UserWarning):
    """A write exceeded the table's declared display min/max (warn+allow)."""


@dataclass(frozen=True)
class RangeBreach:
    """A single cell whose written value fell outside the display limits."""

    uniqueid_hex: str
    row: int
    col: int
    value: float
    limit: float
    bound: str  # "min" or "max"

    def message(self) -> str:
        return (
            f"table {self.uniqueid_hex} cell ({self.row},{self.col}): "
            f"value {self.value:g} exceeds declared {self.bound} {self.limit:g} "
            f"(written anyway — warn+allow)"
        )


def is_float_bug_table(table: Table, float_bug_symbols: Collection[str]) -> bool:
    """Whether ``table``'s symbol is on this car's float-bug flagged-list.

    ``float_bug_symbols`` comes from the active profile
    (:attr:`~simoscal.tune.profile.Profile.float_bug_symbols`). An empty set is a
    legitimate answer — a profile may flag nothing — but it must be *stated*, so
    there is no default: "nobody passed a list" and "this car has no flagged
    tables" are different facts and are never allowed to look alike.
    """
    return table.symbol is not None and table.symbol in float_bug_symbols


def check_raw_fits(emb: EmbeddedData, raw_values) -> None:
    """Hard-fail with :class:`RawRangeError` if any raw int overflows the width.

    A no-op for float elements (their range is the IEEE-754 range, not a small
    integer interval). This runs before the narrowing cast, so an out-of-width
    value can never silently wrap.
    """
    rng = emb.raw_int_range
    if rng is None:  # float element
        return
    lo, hi = rng
    arr = np.asarray(raw_values)
    if arr.size == 0:
        return
    vmin, vmax = int(np.min(arr)), int(np.max(arr))
    if vmin < lo or vmax > hi:
        raise RawRangeError(
            f"raw value(s) [{vmin}, {vmax}] do not fit a "
            f"{emb.elem_bits}-bit {'signed' if emb.signed else 'unsigned'} "
            f"element (range [{lo}, {hi}]) — write would corrupt the cell."
        )


def check_display_range(
    table: Table,
    phys_values,
    *,
    float_bug_symbols: Collection[str],
    override: bool = False,
    origin: tuple[int, int] = (0, 0),
) -> list[RangeBreach]:
    """Enforce the warn+allow / float-bug policy over ``phys_values``.

    Returns the list of :class:`RangeBreach` records for cells outside the
    declared display limits (also emitted as :class:`EditRangeWarning`). For a
    table named in ``float_bug_symbols``, a value above the declared upper limit
    raises :class:`FloatBugGuardError` regardless of ``override``.

    ``float_bug_symbols`` is required and comes from the active profile; see
    :func:`is_float_bug_table` for why it carries no default.

    ``origin`` offsets the reported ``(row, col)`` so a single-cell edit can pass
    a 1×1 array and still report its true coordinates.
    """
    z = table.z
    mn = z.min if z is not None else None
    mx = z.max if z is not None else None
    arr = np.asarray(phys_values, dtype=np.float64).reshape(
        np.asarray(phys_values).shape or (1,)
    )
    arr = np.atleast_2d(arr)
    flagged = is_float_bug_table(table, float_bug_symbols)

    span = 1.0
    if mn is not None and mx is not None:
        span = (mx - mn) or 1.0
    tol = 1e-9 * (abs(span) + 1.0)

    breaches: list[RangeBreach] = []
    for (r, c), raw_v in np.ndenumerate(arr):
        v = float(raw_v)
        row, col = origin[0] + r, origin[1] + c
        if mx is not None and v > mx + tol:
            if flagged:
                raise FloatBugGuardError(
                    f"table {table.uniqueid_hex} ({table.symbol}) is float-bug "
                    f"flagged: cell ({row},{col}) value {v:g} exceeds the declared "
                    f"upper limit {mx:g} — rejected even with override "
                    f"(irreversible-corruption guard, Decision 9)."
                )
            breaches.append(RangeBreach(table.uniqueid_hex, row, col, v, mx, "max"))
        elif mn is not None and v < mn - tol:
            breaches.append(RangeBreach(table.uniqueid_hex, row, col, v, mn, "min"))

    for b in breaches:
        warnings.warn(EditRangeWarning(b.message()), stacklevel=3)
    return breaches
