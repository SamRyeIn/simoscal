"""apply_basics_sop: script the ``ecu-tuning-basics`` SOP onto a stock CalFile.

This module bridges *the tuning guide's concrete instructions* to *a modified
``.bin`` produced via the Phase 1-3 :mod:`simoscal` API* — no new safety,
checksum, or plotting logic, and no flashing.

The one source of truth is :data:`SYMBOL_MAP`: a reviewable list of
:class:`RecipeEntry` records, each mapping a guide section to one or more XDF
symbols, a target value/curve/rule, and a *treatment* (:data:`KIND_*`). Every
guide instruction named in the requirements doc — in-scope **and**
explicitly-skipped — gets exactly one entry, so nothing falls through
uncategorised (AE4). Anything whose symbol can't be confirmed against the live
``CalFile`` becomes ``resolved=False`` *data*, never an exception and never a
guess: an unresolved symbol is reported exactly like a vague guide instruction.

Operating principle, inherited from the rest of the library: **fail loud, change
nothing silently, keep every modified bin verifiable before it is flashed.** In
this module that means:

* the literal-table writer requires the table's *own* axis breakpoints to match
  the guide's — a bin whose axes differ from the guide's example bin fails loud
  (skipped + reported), rather than writing the guide's numbers to the wrong
  cells;
* ceiling-raise limiter edits go through :func:`_guarded_ceiling_write`, which
  never writes a lower value over a higher one;
* float-bug-flagged limiter writes that trip :class:`FloatBugGuardError` are
  caught per-entry and reported ``guard_blocked``, so one table's guard never
  aborts the rest of the recipe and never passes silently.

Units U1-U6 of the plan build this file up: U1 is the symbol map + resolution
(this commit); U2-U4 add the write paths; U5 adds the report; U6 adds
``apply_basics_sop`` orchestration + the demo/acceptance harness.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np

from .calfile import CalFile, TableView
from .model import AmbiguousTableError, FloatBugGuardError, RawRangeError
from .safety import EditRangeWarning, is_float_bug_table

__all__ = [
    # kinds
    "KIND_LITERAL_TABLE",
    "KIND_LITERAL_BROADCAST",
    "KIND_LITERAL_SCALAR",
    "KIND_AXIS_WRITE",
    "KIND_GUARDED_CEILING",
    "KIND_TORQUE_CURVE",
    "KIND_CUT_TRANSFORM",
    "KIND_IAT_ROWMAP",
    "KIND_TTA_ATT_BUILDOUT",
    "KIND_SKIP_LOG_DEPENDENT",
    "KIND_SKIP_VAGUE",
    "KIND_SKIP_OUT_OF_SCOPE",
    "WRITE_KINDS",
    "SKIP_KINDS",
    "is_write_kind",
    # target payloads
    "LiteralGrid",
    "AxisWriteSpec",
    "TorqueCurve",
    "CutRule",
    "IatRowMap",
    "BuildoutSpec",
    # map + resolution
    "RecipeEntry",
    "SymbolResolution",
    "ResolvedEntry",
    "SYMBOL_MAP",
    "resolve_symbol_map",
    # outcomes + write paths
    "OUTCOME_APPLIED",
    "OUTCOME_APPLIED_BUILDOUT",
    "OUTCOME_ALREADY_SATISFIED",
    "OUTCOME_GUARDED_SKIP",
    "OUTCOME_GUARD_BLOCKED",
    "OUTCOME_AXIS_MISMATCH",
    "OUTCOME_POOR_FIT",
    "OUTCOME_UNRESOLVED",
    "OUTCOME_SKIPPED",
    "TableOutcome",
    "apply_entry",
    # report + coherence
    "CoherenceRule",
    "CoherenceFinding",
    "COHERENCE_RULES",
    "RecipeReport",
    "format_report",
    "apply_basics_sop",
]

# --------------------------------------------------------------------------- #
# Treatment kinds
# --------------------------------------------------------------------------- #
# A "kind" is *how the write step treats the entry*. Keeping them explicit (not
# inferred from shape) is what makes SYMBOL_MAP the single reviewable place a
# recipe author confirms "this symbol really means what the guide says, and this
# is exactly how we edit it" (plan Key Decision 1).
KIND_LITERAL_TABLE = "literal_table"        # full grid, matched to the table's own axes
KIND_LITERAL_BROADCAST = "literal_broadcast"  # one value written to every cell
KIND_LITERAL_SCALAR = "literal_scalar"      # one value into a (1,1) table
KIND_AXIS_WRITE = "axis_write"              # paired axis-cell + last-row write (PUT setpoint)
KIND_GUARDED_CEILING = "guarded_ceiling"    # raise a limiter, never lower it (U3)
KIND_TORQUE_CURVE = "torque_curve"          # RPM-keyed column curve, broadcast across rows
KIND_CUT_TRANSFORM = "cut_transform"        # read-modify-write rule (cyl-head cut-5)
KIND_IAT_ROWMAP = "iat_rowmap"              # row-mapped IAT correction onto stock Y breakpoints
KIND_TTA_ATT_BUILDOUT = "tta_att_buildout"  # linear build-out above a torque/airmass threshold (U4)
KIND_SKIP_LOG_DEPENDENT = "skip_log_dependent"  # method is log-driven — out of a static recipe
KIND_SKIP_VAGUE = "skip_vague"              # guide gives no usable number / value ambiguous
KIND_SKIP_OUT_OF_SCOPE = "skip_out_of_scope"  # explicitly excluded variant (V30/LB6/ethanol/…)

WRITE_KINDS = frozenset(
    {
        KIND_LITERAL_TABLE,
        KIND_LITERAL_BROADCAST,
        KIND_LITERAL_SCALAR,
        KIND_AXIS_WRITE,
        KIND_GUARDED_CEILING,
        KIND_TORQUE_CURVE,
        KIND_CUT_TRANSFORM,
        KIND_IAT_ROWMAP,
        KIND_TTA_ATT_BUILDOUT,
    }
)
SKIP_KINDS = frozenset(
    {KIND_SKIP_LOG_DEPENDENT, KIND_SKIP_VAGUE, KIND_SKIP_OUT_OF_SCOPE}
)


def is_write_kind(kind: str) -> bool:
    """Whether ``kind`` stages a bin edit (vs. a documented skip)."""
    return kind in WRITE_KINDS


# --------------------------------------------------------------------------- #
# Target payloads — kind-specific specs carried by an entry's ``target``
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class LiteralGrid:
    """A literal 2D grid keyed to axis breakpoints, not cell positions.

    ``x_keys``/``y_keys`` are the guide table's column (RPM) / row breakpoints;
    ``cells[r][c]`` is the value at ``(y_keys[r], x_keys[c])``. The writer matches
    these keys against the *table's own* decoded axis (within
    :data:`AXIS_MATCH_TOL`) so a bin whose axes differ from the guide's example
    bin fails loud instead of writing to the wrong cells (plan U2).
    """

    x_keys: tuple[float, ...]
    y_keys: tuple[float, ...]
    cells: tuple[tuple[float, ...], ...]


@dataclass(frozen=True)
class AxisWriteSpec:
    """PUT-setpoint paired write: one axis-cell edit + the shaped last row.

    ``axis_symbol`` is the standalone axis table (``ldp_map_sp_ip_put_sp``);
    ``axis_cell`` = ``(row, col)`` in it; ``axis_target`` the new breakpoint.
    ``expected_axis`` is the full stock Y breakpoint vector this entry was built
    against — the writer asserts the live axis matches it before touching
    anything (fail loud if this bin's PUT axis isn't what we expect).
    ``last_row_values`` are the 6 shaped-curve cells written into the table's
    last row after the axis edit.
    """

    axis_symbol: str
    axis_cell: tuple[int, int]
    axis_target: float
    expected_axis: tuple[float, ...]
    last_row_values: tuple[float, ...]


@dataclass(frozen=True)
class TorqueCurve:
    """An RPM → Nm curve, applied per column by matching each column's RPM.

    Columns whose RPM breakpoint is not a key (within :data:`AXIS_MATCH_TOL`) are
    left stock and reported — never interpolated. Every matched column is
    broadcast identically across all of the table's rows (gears), per the guide's
    "set them all the same".
    """

    points: tuple[tuple[float, float], ...]  # (rpm, nm) pairs


@dataclass(frozen=True)
class CutRule:
    """The cylinder-head "cut N from everything over T" transform.

    Expressed as a rule (not a literal grid) because the guide states it as one:
    read current cells, subtract ``amount`` from every cell strictly greater than
    ``threshold``, leave the rest byte-identical.
    """

    threshold: float
    amount: float


@dataclass(frozen=True)
class IatRowMap:
    """Spark-IAT correction row-mapped onto the *stock* Y breakpoints.

    ``x_keys`` are the RPM columns. ``zero_below`` sets every row whose stock Y
    breakpoint is ``<= zero_below`` to all-zeros (kills the cold-timing add).
    ``rows`` maps a stock Y breakpoint to the author's row at that breakpoint.
    Any stock Y breakpoint that is neither ``<= zero_below`` nor present in
    ``rows`` (e.g. stock 70.5, which the author's re-breakpointed table drops) is
    left byte-identical and reported — never interpolated (plan U2).
    """

    x_keys: tuple[float, ...]
    zero_below: float
    rows: tuple[tuple[float, tuple[float, ...]], ...]  # (y_breakpoint, row_values)


@dataclass(frozen=True)
class BuildoutSpec:
    """TTA/ATT proportional build-out spec (consumed by U4).

    ``threshold`` is the torque/airmass value below which rows are left untouched
    and above which the fitted linear trend is extended. ``axis`` names which
    axis carries the threshold quantity (``"y"`` for both TTA torque-rows and ATT
    airmass-rows here).
    """

    threshold: float
    axis: str = "y"


# Tolerance for matching a guide breakpoint against a decoded axis value. Axis
# breakpoints decode to physical units with small scaling residue (e.g. 79.989
# vs a guide 79.99), so an exact compare would spuriously miss. 0.6 is well
# under the smallest gap between adjacent breakpoints in any table we touch.
AXIS_MATCH_TOL = 0.6


# --------------------------------------------------------------------------- #
# Map records
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RecipeEntry:
    """One guide instruction: what it is, which symbols, and how we treat it.

    Exactly one of ``symbols`` (explicit) or ``search_prefixes`` (discovered at
    resolve time) drives symbol resolution. ``search_prefixes`` is for families
    whose per-bin cardinality varies (Power-Class torque variants, TTA/ATT cam
    variants): every unique symbol whose name starts with a prefix is included,
    so a bin with more/fewer variants resolves them all without editing the map.
    """

    guide_section: str
    description: str
    kind: str
    symbols: tuple[str, ...] = ()
    search_prefixes: tuple[str, ...] = ()
    target: object = None
    reason: str = ""  # for skip kinds: the human explanation printed in the report

    def __post_init__(self) -> None:
        if self.kind not in WRITE_KINDS and self.kind not in SKIP_KINDS:
            raise ValueError(f"unknown kind {self.kind!r} in entry {self.description!r}")


@dataclass(frozen=True)
class SymbolResolution:
    """The outcome of resolving one symbol against a live ``CalFile``."""

    symbol: str
    resolved: bool
    reason: str = ""  # "" when resolved; else why not (missing / ambiguous)
    shape: Optional[tuple[int, int]] = None
    units: Optional[str] = None
    view: Optional[TableView] = None  # the bound view, when resolved


@dataclass(frozen=True)
class ResolvedEntry:
    """A :class:`RecipeEntry` paired with its per-symbol resolution results."""

    entry: RecipeEntry
    resolutions: tuple[SymbolResolution, ...]

    @property
    def is_skip(self) -> bool:
        return self.entry.kind in SKIP_KINDS

    @property
    def all_resolved(self) -> bool:
        """Whether every symbol this entry needs resolved (vacuously true for skips)."""
        return all(r.resolved for r in self.resolutions)

    @property
    def any_resolved(self) -> bool:
        return any(r.resolved for r in self.resolutions)

    @property
    def resolved_views(self) -> list[TableView]:
        return [r.view for r in self.resolutions if r.resolved and r.view is not None]

    def unresolved_reason(self) -> str:
        """A single human string summarising why the entry isn't fully resolved."""
        bad = [r for r in self.resolutions if not r.resolved]
        if not bad:
            return ""
        return "; ".join(f"{r.symbol}: {r.reason}" for r in bad)


# =========================================================================== #
# The symbol map — one entry per guide instruction (in-scope + skipped).
#
# Every literal grid / curve below is transcribed from
# ``knowledge/ecu-tuning-basics.md`` (itself double-entry verified from the
# guide screenshots). Values are the guide's *example-bin* numbers; the write
# step matches them to this bin's own axes and fails loud on any mismatch.
# =========================================================================== #

# ---- guide §1: Max Torque at Clutch — RPM→Nm curve, all PC variants -------- #
_MAX_TORQUE_CURVE = TorqueCurve(
    points=(
        (1200, 320), (1500, 350), (1800, 375), (2000, 400), (2250, 420),
        (2500, 440), (2750, 440), (3000, 440), (3250, 440), (3500, 440),
        (3750, 440), (4000, 440), (4250, 440), (4360, 440), (4500, 440),
        (5000, 435), (5500, 400), (6000, 360), (6500, 300), (7000, 275),
    )
)

# ---- guide §Boost Option 2: PUT setpoint axis + shaped last row ------------ #
_PUT_SP_SPEC = AxisWriteSpec(
    axis_symbol="ldp_map_sp_ip_put_sp",
    axis_cell=(0, 3),
    axis_target=2698.97,
    expected_axis=(590.041, 700.073, 1050.068, 2500.046),
    last_row_values=(2698.97, 2698.97, 2499.96, 2349.97, 2298.97, 2198.97),
)

# ---- guide §Timing: Basic Ignition Angle 16×16 (VVL 0, Intake 0/Exhaust 0) - #
_IGA_X = (400, 700, 1000, 1250, 1500, 1750, 2000, 2500,
          3000, 3500, 4000, 4500, 5000, 5500, 6000, 6500)
_IGA_Y = (79.99, 100.00, 150.02, 199.99, 250.01, 299.99, 350.01, 399.99,
          498.99, 599.98, 699.98, 800.02, 900.02, 1049.97, 1200.01, 1400.00)
_IGA_CELLS = (
    (17.62, 21.37, 23.25, 26.62, 28.87, 24.00, 25.87, 27.00, 37.87, 40.12, 40.12, 40.12, 40.12, 40.12, 40.12, 40.12),
    (16.50, 18.00, 18.37, 21.37, 31.12, 37.12, 36.37, 36.75, 34.12, 37.87, 40.12, 40.12, 40.12, 40.12, 40.12, 40.12),
    (10.12, 10.12, 10.50, 15.00, 30.00, 36.37, 38.62, 33.37, 31.87, 33.37, 40.12, 40.12, 40.12, 37.50, 36.00, 40.12),
    (8.25, 8.25, 9.75, 16.87, 28.87, 32.25, 27.37, 26.25, 25.12, 25.87, 32.25, 33.75, 31.50, 30.37, 27.75, 29.25),
    (6.37, 6.37, 9.75, 16.12, 24.75, 24.37, 22.12, 21.37, 21.37, 22.12, 27.75, 27.37, 27.37, 26.25, 24.75, 25.12),
    (5.62, 5.62, 9.37, 15.00, 21.37, 21.37, 21.00, 19.50, 18.00, 19.12, 22.87, 24.37, 23.25, 23.62, 22.50, 23.25),
    (4.87, 4.87, 9.37, 14.25, 17.62, 18.75, 16.87, 18.00, 16.50, 17.62, 20.62, 20.62, 21.37, 21.37, 21.00, 21.75),
    (4.50, 4.50, 6.00, 9.00, 12.75, 17.25, 16.12, 15.00, 15.00, 16.12, 19.12, 19.12, 19.50, 20.25, 19.87, 20.62),
    (4.50, 4.50, 0.37, -5.25, 4.50, 9.37, 12.37, 12.00, 12.00, 13.87, 16.87, 16.50, 16.87, 17.62, 18.00, 19.12),
    (0.00, 0.00, 0.75, -3.00, 3.00, 4.12, 4.87, 10.12, 13.87, 14.25, 15.37, 15.75, 16.50, 16.87, 16.50, 18.00),
    (-4.12, -4.12, -2.25, -3.75, 1.12, 1.87, 1.50, 7.12, 9.75, 11.25, 12.00, 13.12, 13.87, 14.25, 11.25, 10.50),
    (-5.62, -5.62, -3.00, -4.12, 0.37, 1.12, 0.37, 1.50, 4.12, 4.12, 6.00, 7.12, 9.00, 9.37, 6.75, 7.12),
    (-12.37, -12.37, -7.87, -5.62, -3.00, -5.62, -3.00, -0.75, 0.00, 1.12, 1.50, 2.62, 2.25, 2.62, 3.37, 5.62),
    (-16.12, -16.12, -11.62, -9.00, -6.75, -8.25, -4.87, -4.12, -3.75, -2.62, -1.87, 1.12, 1.87, 1.50, 3.00, 4.50),
    (-18.00, -18.00, -14.25, -12.00, -9.75, -8.25, -6.75, -6.75, -6.75, -5.25, -4.12, -3.00, -0.75, 0.75, 1.87, 3.37),
    (-18.00, -18.00, -15.00, -12.75, -10.50, -9.00, -8.62, -8.25, -7.50, -6.75, -4.50, -3.00, -0.75, 0.75, 1.87, 3.37),
)
_IGA_GRID = LiteralGrid(x_keys=_IGA_X, y_keys=_IGA_Y, cells=_IGA_CELLS)
# The 9 VVL-0 Port Flap Low tables (Intake 0-2 × Exhaust 0-2); the literal
# Intake 0/Exhaust 0 grid is written identically to all 9 per user direction.
_IGA_SYMBOLS = tuple(
    f"IP_IGA_BAS_IVVT_VVL_PORT_L[STND][{i}][{e}]" for i in range(3) for e in range(3)
)

# ---- guide §Timing: Spark IAT correction — row-mapped onto stock Y ---------- #
_IAT_ROWMAP = IatRowMap(
    x_keys=(608, 1312, 1696, 2016, 2496, 3008, 4000, 4512, 5024, 6080),
    zero_below=30.0,  # stock rows -30…30 °C → 0.00 (no cold-timing add)
    rows=(
        (40.50, (-1.12, -1.12, -1.12, -1.12, -1.12, -1.12, -1.87, -1.87, -1.87, -1.87)),
        (50.25, (-1.87, -2.25, -2.25, -2.25, -2.62, -1.87, -3.00, -3.00, -3.75, -3.75)),
        (60.00, (-3.37, -3.37, -3.00, -4.12, -4.12, -4.12, -4.12, -4.12, -4.87, -4.87)),
        (80.25, (-7.12, -7.12, -7.50, -7.50, -7.87, -7.87, -9.00, -9.00, -10.12, -10.12)),
    ),
)

# ---- guide §Fueling: Basic lambda setpoint 8×12 (HPDI + MPI identical) ------ #
# NB: the guide's example bin re-breakpointed both axes; on the stock bin these
# axes differ entirely, so the axis-matched writer will (correctly) report a
# mismatch and skip rather than write onto the wrong cells. Kept as a literal
# entry so the mismatch is surfaced, not silently omitted.
_LAMBDA_X = (1504, 2016, 2496, 3008, 3488, 4000, 4512, 4992, 5504, 5984, 6496, 7008)
_LAMBDA_Y = (150.00, 299.99, 500.01, 700.00, 899.99, 1100.01, 1200.01, 1389.00)
_LAMBDA_CELLS = (
    (1.00, 1.00, 1.00, 1.00, 1.00, 1.00, 1.00, 1.00, 1.00, 1.00, 1.00, 1.00),
    (1.00, 1.00, 1.00, 1.00, 1.00, 1.00, 1.00, 1.00, 1.00, 1.00, 1.00, 1.00),
    (1.00, 1.00, 1.00, 1.00, 1.00, 1.00, 1.00, 1.00, 0.92, 0.89, 0.87, 0.87),
    (1.00, 1.00, 1.00, 1.00, 1.00, 1.00, 0.95, 0.92, 0.89, 0.87, 0.85, 0.85),
    (1.00, 1.00, 1.00, 1.00, 0.97, 0.95, 0.92, 0.88, 0.86, 0.84, 0.82, 0.82),
    (1.00, 1.00, 1.00, 1.00, 0.95, 0.92, 0.88, 0.84, 0.83, 0.81, 0.80, 0.80),
    (1.00, 1.00, 1.00, 0.98, 0.93, 0.89, 0.87, 0.82, 0.80, 0.80, 0.80, 0.80),
    (1.00, 1.00, 0.98, 0.95, 0.90, 0.86, 0.84, 0.82, 0.80, 0.80, 0.80, 0.80),
)
_LAMBDA_GRID = LiteralGrid(x_keys=_LAMBDA_X, y_keys=_LAMBDA_Y, cells=_LAMBDA_CELLS)


SYMBOL_MAP: tuple[RecipeEntry, ...] = (
    # ===================== Torque request / model ======================== #
    RecipeEntry(
        guide_section="1. Torque request — Max Torque at Clutch",
        description="Raise max torque out of the way (tune by boost); all PC variants set the same",
        kind=KIND_TORQUE_CURVE,
        search_prefixes=("IP_TQ_POW_MAX_AT", "IP_TQ_POW_MAX_MT", "IP_TQ_POW_MAX_ECO"),
        target=_MAX_TORQUE_CURVE,
    ),
    RecipeEntry(
        guide_section="1. Torque request — pedal-feel tables",
        description="Torque-request pedal-feel tables (DSG hi/lo speed) — tuner preference, no guide number",
        kind=KIND_SKIP_VAGUE,
        symbols=("IP_FAC_TQ_REQ_DRIV_H_VS_DCT", "IP_FAC_TQ_REQ_DRIV_L_VS_DCT"),
        reason="Pedal feel is subjective; guide gives no literal values. Resolvable for reporting only.",
    ),
    RecipeEntry(
        guide_section="Limiters — Max reference indicated engine torque",
        description="Max reference indicated engine torque — 'move out of the way' (no number given)",
        kind=KIND_SKIP_VAGUE,
        symbols=("IP_TQI_REF_MAX_MON",),
        reason="Guide says 'move out of the way' but gives no target value.",
    ),
    # ===================== TTA / ATT (airflow model) ===================== #
    RecipeEntry(
        guide_section="2. Torque → Airflow (TTA)",
        description="Build out TTA airmass above 400 Nm (all port-flap/VVL/cam variants)",
        kind=KIND_TTA_ATT_BUILDOUT,
        search_prefixes=("IP_MAF_STK_SP_VVL_CAM_H", "IP_MAF_STK_SP_VVL_CAM_L"),
        target=BuildoutSpec(threshold=400.0, axis="y"),
    ),
    RecipeEntry(
        guide_section="3. Airflow → Torque (ATT)",
        description="Build out ATT torque consistently with TTA (all variants)",
        kind=KIND_TTA_ATT_BUILDOUT,
        search_prefixes=("IP_TQI_REF_N_M_AIR_VVL_CAM_H", "IP_TQI_REF_N_M_AIR_VVL_CAM_L"),
        target=BuildoutSpec(threshold=400.0, axis="y"),
    ),
    # ===================== Boost control ================================= #
    RecipeEntry(
        guide_section="Boost — Option 2: PUT setpoint curve",
        description="Shape the boost curve via PUT setpoint last row + raise its Y axis",
        kind=KIND_AXIS_WRITE,
        symbols=("IP_PUT_SP",),
        target=_PUT_SP_SPEC,
    ),
    RecipeEntry(
        guide_section="Boost — Max PR flatten (Option 2)",
        description="Flatten Max Pressure Ratio to 2.80 (moved out of the way for Option 2)",
        kind=KIND_LITERAL_BROADCAST,
        symbols=("IP_PQ_CHA_MAX",),
        target=2.80,
    ),
    RecipeEntry(
        guide_section="Boost — Option 3 torque-tune selector",
        description="Enable PUT-out-of-PR calc (selector → 1) after the boost curve is set",
        kind=KIND_LITERAL_SCALAR,
        symbols=("LC_PUT_SP_TOL_ENA_AMP",),
        target=1.0,
    ),
    RecipeEntry(
        guide_section="Wastegate (flow-factor tuning)",
        description="Wastegate exhaust/intake flow-factor tables — log-driven by design",
        kind=KIND_SKIP_LOG_DEPENDENT,
        reason="Guide's method reads flow factors from a datalog where PUT deviates; no static target.",
    ),
    # ===================== Timing ======================================== #
    RecipeEntry(
        guide_section="Timing — Basic Ignition Angle (VVL 0 Port Flap Low)",
        description="Write the literal base-timing grid to all 9 VVL-0 Port Flap Low tables",
        kind=KIND_LITERAL_TABLE,
        symbols=_IGA_SYMBOLS,
        target=_IGA_GRID,
    ),
    RecipeEntry(
        guide_section="Timing — Spark IAT correction",
        description="Row-map author's IAT correction onto stock Y breakpoints (no cold add, no pull <40°C)",
        kind=KIND_IAT_ROWMAP,
        symbols=("IP_IGA_BAS_TEMP_N_32",),
        target=_IAT_ROWMAP,
    ),
    # ===================== Fueling / lambda ============================== #
    RecipeEntry(
        guide_section="Fueling — Basic lambda setpoint (HPDI + MPI)",
        description="Lean during spool → rich at full load; HPDI and MPI made identical",
        kind=KIND_LITERAL_TABLE,
        symbols=("IP_LAMB_BAS_HPDI[1]", "IP_LAMB_BAS_MPI[1]"),
        target=_LAMBDA_GRID,
    ),
    RecipeEntry(
        guide_section="Fueling — fueling-influence tables → 0.80",
        description="Three fueling-influence tables reduced to 0.80",
        kind=KIND_SKIP_VAGUE,
        reason="No symbol confirmed for the three fueling-influence tables this session; not guessed.",
    ),
    RecipeEntry(
        guide_section="Fueling — heavy-throttle table ~70–75",
        description="Heavy-throttle enrichment table set ~70–75 across",
        kind=KIND_SKIP_VAGUE,
        reason="No symbol confirmed for the heavy-throttle table this session; not guessed.",
    ),
    RecipeEntry(
        guide_section="Fueling — two tables set entirely to 1",
        description="Two fueling tables that must be entirely 1",
        kind=KIND_SKIP_VAGUE,
        reason="No symbol confirmed for the two 'set to 1' tables this session; not guessed.",
    ),
    RecipeEntry(
        guide_section="Fueling — Ethanol / Flex Fuel",
        description="Flex Fuel enable / ethanol % / sensor tables",
        kind=KIND_SKIP_OUT_OF_SCOPE,
        reason="Ethanol/Flex Fuel is out of scope (car runs pump fuel per project config).",
    ),
    # ===================== Cooling ======================================= #
    RecipeEntry(
        guide_section="Cooling — cylinder head temp setpoint",
        description="Cut 5 °C from every cell over 90 °C (lowers head/oil temps)",
        kind=KIND_CUT_TRANSFORM,
        symbols=("CoTE_tHdCtlSp_M_VW",),
        target=CutRule(threshold=90.0, amount=5.0),
    ),
    # ===================== Limiters ====================================== #
    RecipeEntry(
        guide_section="Limiters — Compressor temp maps → 300",
        description="Raise both compressor-temp limiter constants to 300 °C",
        kind=KIND_GUARDED_CEILING,
        symbols=("C_TIA_THR_TCHA_MAX", "C_TIA_THR_TCHA_MAX_SP"),
        target=300.0,
    ),
    RecipeEntry(
        guide_section="Limiters — Turbo shaft speed → 220k",
        description="Raise both turbo shaft speed limiter constants to 220000 rpm",
        kind=KIND_GUARDED_CEILING,
        symbols=("C_N_TCHA_MAX", "C_N_TCHA_MAX_SP"),
        target=220000.0,
    ),
    RecipeEntry(
        guide_section="Limiters — Overboost limit → 2700",
        description="Raise the overboost (P0234) limit to 2700 hPa across all cells; never lower a higher cell",
        kind=KIND_GUARDED_CEILING,          # broadcasts across all cells, never-lower guarded
        symbols=("IP_PUT_AMP_DIF_MAX_PRS_DIF_THR",),
        target=2700.0,
        reason=(
            "IP_PUT_AMP_DIF_MAX_PRS_DIF_THR  — Overpressure upstream throttle "
            "threshold for turbocharger overpressure diagnosis (P0234); 1x6 int16 "
            "hPa, stock ~1800. XDF hard max is 2716.96 hPa, so 2700 is intentionally "
            "just under the ceiling — do not exceed. (Corrected 2026-07-09 from "
            "C_PRS_IM_SP_LIM  — Offset to the pressure behind air cleaner for the "
            "limitation of the manifold setpoint, which is a manifold-setpoint "
            "limit, not the overboost threshold.)"
        ),
    ),
    RecipeEntry(
        guide_section="Limiters — Charge air pressure too high → 3000",
        description="Set the charge-air-pressure-too-high diagnosis map to 3000 across",
        kind=KIND_LITERAL_BROADCAST,
        symbols=("IP_PUT_MAX_CAP_H_DIAG",),
        target=3000.0,
    ),
    RecipeEntry(
        guide_section="Limiters — Max requested pressure → 350000 (float-bug)",
        description="Raise max requested pressure limiter to 350000 hPa",
        kind=KIND_GUARDED_CEILING,
        symbols=("C_PRS_IM_SP_MAX",),
        target=350000.0,
        reason=(
            "Float-bug-flagged: the 350000 target exceeds the declared upper "
            "limit, so the FloatBugGuard rejects it (guard_blocked) — apply "
            "manually in TunerPro/SimosTools per the guide's save+reopen note."
        ),
    ),
    RecipeEntry(
        guide_section="Limiters — Max allowed airmass → 2000 (float-bug)",
        description="Raise max allowed airmass limiter (guide: type 0.002 due to display bug)",
        kind=KIND_SKIP_VAGUE,
        symbols=("C_M_AIR_CYL_SP_MAX",),
        reason=(
            "Float-bug display anomaly: stock reads 0.001 and the guide says to "
            "type 0.002, not 2000 — the true target is ambiguous under this XDF's "
            "scaling. Not written by the recipe; apply manually and verify by reopen."
        ),
    ),
    RecipeEntry(
        guide_section="Limiters — two max intake air tables → 2000",
        description="Two 'max intake air' tables set to 2000 across",
        kind=KIND_SKIP_VAGUE,
        reason="No confident symbol found for the two max-intake-air tables; not guessed.",
    ),
    RecipeEntry(
        guide_section="Limiters — Speed limiter (four overall maximal velocity)",
        description="Set all four overall-maximal-velocity tables to 257.49 km/h (~160 mph)",
        kind=KIND_LITERAL_SCALAR,
        symbols=(
            "LMVLim_vMax_vLim_C_VW.VehSpdl2Lvl1",
            "LMVLim_vMax_vLim_C_VW.VehSpdl2Lvl2",
            "LMVLim_vMax_vLim_C_VW.VehSpdl2Lvl3",
            "LMVLim_vMax_vLim_C_VW.VehSpdl2NotAcv",
        ),
        target=257.49,
    ),
    RecipeEntry(
        guide_section="Limiters — misc / V30 / LB6 out-of-the-way",
        description="Misc 1000/800 limiter tables, V30 10-table set, LB6 table",
        kind=KIND_SKIP_OUT_OF_SCOPE,
        reason="Variant-specific (V30/LB6) and vague misc limiters are out of scope for this recipe.",
    ),
    # ===================== DSG / pops & bangs ============================ #
    RecipeEntry(
        guide_section="DSG farts",
        description="Min-spark-during-gearshift / fuel-cut tables for shift farts",
        kind=KIND_SKIP_OUT_OF_SCOPE,
        reason="DSG farts are explicitly out of scope.",
    ),
    RecipeEntry(
        guide_section="Pops & bangs (impulse combustion)",
        description="Impulse-combustion parameter set",
        kind=KIND_SKIP_OUT_OF_SCOPE,
        reason="Pops & bangs are explicitly out of scope.",
    ),
)


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #
def _resolve_one(cal: CalFile, symbol: str) -> SymbolResolution:
    """Resolve a single symbol against ``cal`` — never raises, returns data."""
    try:
        view = cal.get(symbol)
    except KeyError:
        return SymbolResolution(symbol, resolved=False, reason="missing (no such symbol in this bin)")
    except AmbiguousTableError as exc:
        return SymbolResolution(
            symbol, resolved=False,
            reason=f"ambiguous ({len(exc.candidates)} tables) — disambiguate by uniqueid",
        )
    return SymbolResolution(
        symbol, resolved=True, shape=view.shape, units=view.units, view=view,
    )


def _discover_symbols(cal: CalFile, prefixes: tuple[str, ...]) -> list[str]:
    """Every unique symbol whose name starts with one of ``prefixes``, sorted.

    Uses ``search`` (substring) then filters to a genuine *prefix* match on the
    symbol, so ``IP_TQ_POW_MAX_AT`` doesn't accidentally sweep in a table that
    merely mentions the string mid-name. Sorted for deterministic report order.
    """
    found: set[str] = set()
    for prefix in prefixes:
        for view in cal.search(prefix):
            sym = view.symbol
            if sym and sym.startswith(prefix):
                found.add(sym)
    return sorted(found)


def resolve_symbol_map(
    cal: CalFile, symbol_map: tuple[RecipeEntry, ...] = SYMBOL_MAP
) -> list[ResolvedEntry]:
    """Resolve every entry in ``symbol_map`` against a live ``CalFile``.

    Resolution failures are *data*, not exceptions: a missing or ambiguous symbol
    yields ``resolved=False`` with a reason, and the entry's ``kind`` is
    unchanged. Skip-kind entries with explicit ``symbols`` (resolvable for
    reporting, e.g. pedal-feel) are still resolved so the report can name them;
    skip-kind entries with no symbols simply carry no resolutions.

    For entries using ``search_prefixes``, the concrete symbol set is discovered
    here; if a prefix matches nothing, a single unresolved placeholder records
    that so the gap is visible rather than silent.
    """
    resolved: list[ResolvedEntry] = []
    for entry in symbol_map:
        if entry.search_prefixes:
            discovered = _discover_symbols(cal, entry.search_prefixes)
            if not discovered:
                resolutions = (
                    SymbolResolution(
                        symbol=" | ".join(entry.search_prefixes),
                        resolved=False,
                        reason="no symbols matched the declared search prefixes in this bin",
                    ),
                )
            else:
                resolutions = tuple(_resolve_one(cal, s) for s in discovered)
        elif entry.symbols:
            resolutions = tuple(_resolve_one(cal, s) for s in entry.symbols)
        else:
            resolutions = ()  # a pure skip with nothing to resolve
        resolved.append(ResolvedEntry(entry=entry, resolutions=resolutions))
    return resolved


# =========================================================================== #
# U2-U4 — write paths
#
# Every write is staged through the existing Phase 1 ``TableView`` edit API
# (``set`` / ``set_cell``): inverse-scaled, range-checked, minimal-diff. No new
# safety logic — the recipe only *catches* the existing guards per entry so one
# table's guard never aborts the rest, and records the outcome for the report.
# =========================================================================== #

# ---- outcome vocabulary ---------------------------------------------------- #
OUTCOME_APPLIED = "applied"                    # literal / scalar / broadcast write staged
OUTCOME_APPLIED_BUILDOUT = "applied_buildout"  # TTA/ATT derived build-out (U4)
OUTCOME_ALREADY_SATISFIED = "already_satisfied"  # target already met — nothing staged
OUTCOME_GUARDED_SKIP = "guarded_skip"          # ceiling guard: current already past target (U3)
OUTCOME_GUARD_BLOCKED = "guard_blocked"        # FloatBugGuard/RawRange rejected the write
OUTCOME_AXIS_MISMATCH = "axis_mismatch"        # table axes differ from the guide's — not written
OUTCOME_POOR_FIT = "poor_fit"                  # TTA/ATT sub-threshold rows not linear (U4) — not written
OUTCOME_UNRESOLVED = "unresolved"              # symbol not found/ambiguous in this bin
OUTCOME_SKIPPED = "skipped"                    # documented skip (log-dependent / vague / out-of-scope)


@dataclass(frozen=True)
class TableOutcome:
    """One table's outcome, the atom the report (U5) is built from.

    ``old``/``new`` carry the pre/post scalar value for (1,1) edits — for scalars
    this old→new pair *is* the review artifact, since ``compare_tables`` produces
    no PNG for a single cell (plan Key Decision 6). ``detail`` carries prose for
    non-scalar edits (coverage, warnings, skip reasons).
    """

    symbol: str
    guide_section: str
    outcome: str
    detail: str = ""
    old: Optional[float] = None
    new: Optional[float] = None
    warning: str = ""  # captured EditRangeWarning text, if any


def _fmt(v: float) -> str:
    return f"{v:.6g}"


# ---- axis matching --------------------------------------------------------- #
def _positional_axis_match(
    axis_vals: Optional[np.ndarray],
    keys: tuple[float, ...],
    *,
    tol_frac: float = 0.4,
    tol_floor: float = 0.6,
) -> Optional[list[int]]:
    """Match a full guide axis to a table axis position-for-position.

    Returns ``[0, 1, …, n-1]`` when the table's axis has the *same count* as
    ``keys`` and every key sits within a spacing-relative tolerance of the axis
    value at the same position; otherwise ``None`` (a genuine axis mismatch).

    The tolerance is ``max(tol_floor, tol_frac × local_spacing)`` so transcription
    noise (e.g. a guide breakpoint 498.99 vs a stock 499.985, ~1.0 apart but
    ~100 from its neighbours) matches, while a table whose breakpoints are truly
    different (e.g. lambda's 150 vs a stock 70) is rejected.
    """
    if axis_vals is None:
        return None
    a = np.asarray(axis_vals, dtype=np.float64).ravel()
    if a.size != len(keys):
        return None
    for i, k in enumerate(keys):
        neighbours = []
        if i > 0:
            neighbours.append(abs(a[i] - a[i - 1]))
        if i < a.size - 1:
            neighbours.append(abs(a[i + 1] - a[i]))
        spacing = min(neighbours) if neighbours else (abs(a[i]) or 1.0)
        tol = max(tol_floor, tol_frac * spacing)
        if abs(a[i] - k) > tol:
            return None
    return list(range(len(keys)))


def _key_column_match(
    axis_vals: Optional[np.ndarray],
    keys: tuple[float, ...],
    *,
    tol: float = 10.0,
) -> dict[int, int]:
    """Map each table column index → the index of the ``keys`` entry it equals.

    Used for the torque curve, where the guide's RPM keys are looked up per
    column (unlike a full grid). A column whose axis value is not within ``tol``
    of any key is simply absent from the result (left stock, reported). ``tol`` is
    absolute and well under the smallest gap between distinct RPM keys (110), so
    a near-miss (ECO's 4200 vs the curve's 4250) does not falsely match.
    """
    out: dict[int, int] = {}
    if axis_vals is None:
        return out
    a = np.asarray(axis_vals, dtype=np.float64).ravel()
    for c, av in enumerate(a):
        best_j, best_d = None, tol
        for j, k in enumerate(keys):
            d = abs(av - k)
            if d <= best_d:
                best_j, best_d = j, d
        if best_j is not None:
            out[c] = best_j
    return out


# ---- staging wrapper: catch the existing guards, capture warnings ---------- #
def _run_write(fn: Callable[[], None]) -> tuple[str, str]:
    """Run a staging closure; return ``(status, text)`` without ever raising.

    ``status`` ∈ {``"ok"``, ``"guard_blocked"``}. A :class:`FloatBugGuardError`
    or :class:`RawRangeError` (both leave the table byte-identical — the range
    check runs before staging) maps to ``guard_blocked`` with the error text; a
    successful write returns any :class:`EditRangeWarning` text it emitted.
    """
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            fn()
        except (FloatBugGuardError, RawRangeError) as exc:
            return "guard_blocked", str(exc)
    warn_text = "; ".join(
        str(w.message) for w in caught if issubclass(w.category, EditRangeWarning)
    )
    return "ok", warn_text


# ---- per-view writers ------------------------------------------------------ #
def _apply_literal_scalar(view: TableView, section: str, value: float) -> TableOutcome:
    old = float(view.values.ravel()[0])
    status, text = _run_write(lambda: view.set_cell(0, 0, value))
    if status == "guard_blocked":
        return TableOutcome(view.symbol, section, OUTCOME_GUARD_BLOCKED,
                            detail=text, old=old, new=value)
    if abs(old - value) < 1e-9:
        return TableOutcome(view.symbol, section, OUTCOME_ALREADY_SATISFIED,
                            old=old, new=value)
    return TableOutcome(view.symbol, section, OUTCOME_APPLIED,
                        old=old, new=value, warning=text)


def _apply_literal_broadcast(view: TableView, section: str, value: float) -> TableOutcome:
    rows, cols = view.shape
    arr = np.full((rows, cols), float(value), dtype=np.float64)
    status, text = _run_write(lambda: view.set(arr))
    if status == "guard_blocked":
        return TableOutcome(view.symbol, section, OUTCOME_GUARD_BLOCKED, detail=text)
    return TableOutcome(view.symbol, section, OUTCOME_APPLIED,
                        detail=f"broadcast {_fmt(value)} to all {rows * cols} cells",
                        warning=text)


def _apply_literal_table(view: TableView, section: str, grid: "LiteralGrid") -> TableOutcome:
    rows, cols = view.shape
    # A non-None _positional_axis_match is always the identity index list
    # (list(range(n))); correctness rests on its count-and-alignment guarantee,
    # so the full-grid write below is axis-aligned. Keep only the pass/fail here.
    x_ok = _positional_axis_match(view.axis_values("x"), grid.x_keys) is not None
    y_ok = _positional_axis_match(view.axis_values("y"), grid.y_keys) is not None
    if not x_ok or not y_ok:
        which = []
        if not x_ok:
            which.append("x")
        if not y_ok:
            which.append("y")
        return TableOutcome(
            view.symbol, section, OUTCOME_AXIS_MISMATCH,
            detail=(
                f"table {'/'.join(which)} axis differs from the guide's example bin "
                "(count or breakpoints) — not written; needs manual axis setup"
            ),
        )
    target = np.array(grid.cells, dtype=np.float64)
    status, text = _run_write(lambda: view.set(target))
    if status == "guard_blocked":
        return TableOutcome(view.symbol, section, OUTCOME_GUARD_BLOCKED, detail=text)
    return TableOutcome(view.symbol, section, OUTCOME_APPLIED,
                        detail=f"wrote {rows}x{cols} literal grid (axis-matched)",
                        warning=text)


def _apply_torque_curve(view: TableView, section: str, curve: "TorqueCurve") -> TableOutcome:
    rows, cols = view.shape
    keys = tuple(p[0] for p in curve.points)
    vals = tuple(p[1] for p in curve.points)
    col_to_key = _key_column_match(view.axis_values("x"), keys)
    if not col_to_key:
        return TableOutcome(view.symbol, section, OUTCOME_AXIS_MISMATCH,
                            detail="no RPM column matched the torque curve — not written")
    target = view.values.astype(np.float64).copy()
    for c, j in col_to_key.items():
        target[:, c] = vals[j]
    status, text = _run_write(lambda: view.set(target))
    if status == "guard_blocked":
        return TableOutcome(view.symbol, section, OUTCOME_GUARD_BLOCKED, detail=text)
    matched = len(col_to_key)
    detail = f"applied curve to {matched}/{cols} RPM columns × {rows} rows"
    if matched < cols:
        unmatched = sorted(
            float(np.asarray(view.axis_values("x")).ravel()[c])
            for c in range(cols) if c not in col_to_key
        )
        detail += f"; columns left stock (no curve key): {unmatched}"
    return TableOutcome(view.symbol, section, OUTCOME_APPLIED, detail=detail, warning=text)


def _apply_cut_transform(view: TableView, section: str, rule: "CutRule") -> TableOutcome:
    vals = view.values.astype(np.float64)
    # Build the whole target grid, then stage it in one range-checked write so a
    # guard trip leaves the table byte-identical (CR-20260707-01).
    target = vals.copy()
    mask = vals > rule.threshold
    target[mask] = vals[mask] - rule.amount
    changed = int(np.count_nonzero(mask))
    if changed == 0:
        return TableOutcome(view.symbol, section, OUTCOME_ALREADY_SATISFIED,
                            detail=f"no cell over {_fmt(rule.threshold)}")
    status, text = _run_write(lambda: view.set(target))
    if status == "guard_blocked":
        return TableOutcome(view.symbol, section, OUTCOME_GUARD_BLOCKED,
                            detail=text)
    return TableOutcome(
        view.symbol, section, OUTCOME_APPLIED,
        detail=f"cut {_fmt(rule.amount)} from {changed} cells over {_fmt(rule.threshold)}",
        warning=text,
    )


def _apply_iat_rowmap(view: TableView, section: str, rowmap: "IatRowMap") -> TableOutcome:
    yvals = view.axis_values("y")
    if yvals is None:
        return TableOutcome(view.symbol, section, OUTCOME_AXIS_MISMATCH,
                            detail="IAT table has no decodable Y axis — not written")
    y = np.asarray(yvals, dtype=np.float64).ravel()
    rows, cols = view.shape
    if cols != len(rowmap.x_keys):
        return TableOutcome(
            view.symbol, section, OUTCOME_AXIS_MISMATCH,
            detail=f"IAT column count {cols} ≠ {len(rowmap.x_keys)} guide keys — not written",
        )
    author = {bp: row for bp, row in rowmap.rows}
    # Assemble the full target grid (rows with no author breakpoint keep their
    # stock values), then stage it in one range-checked write so a guard trip
    # leaves the table byte-identical (CR-20260707-01).
    target = view.values.astype(np.float64).copy()
    changed_rows, left_rows = [], []
    for r in range(rows):
        yb = y[r]
        if yb <= rowmap.zero_below + 1e-6:
            target_row = [0.0] * cols
        else:
            match = next((bp for bp in author if abs(bp - yb) <= AXIS_MATCH_TOL), None)
            if match is None:
                left_rows.append(round(float(yb), 2))
                continue
            target_row = list(author[match])
        target[r, :] = target_row
        changed_rows.append(round(float(yb), 2))
    status, text = _run_write(lambda: view.set(target))
    if status == "guard_blocked":
        return TableOutcome(view.symbol, section, OUTCOME_GUARD_BLOCKED, detail=text)
    detail = f"row-mapped {len(changed_rows)} Y rows onto stock breakpoints"
    if left_rows:
        detail += f"; left stock (no author breakpoint): {left_rows}"
    return TableOutcome(view.symbol, section, OUTCOME_APPLIED, detail=detail,
                        warning=text)


def _apply_axis_write(cal: CalFile, view: TableView, section: str,
                      spec: "AxisWriteSpec") -> list[TableOutcome]:
    # 1) confirm this bin's PUT Y axis is the stock shape we built the spec against
    y = view.axis_values("y")
    if y is None or _positional_axis_match(y, spec.expected_axis) is None:
        return [TableOutcome(
            view.symbol, section, OUTCOME_AXIS_MISMATCH,
            detail="PUT setpoint Y axis differs from the expected stock axis — not written",
        )]
    # 2) confirm the standalone axis table is present and monotonic after the edit
    try:
        axis_view = cal.get(spec.axis_symbol)
    except (KeyError, AmbiguousTableError) as exc:
        return [TableOutcome(spec.axis_symbol, section, OUTCOME_UNRESOLVED,
                             detail=f"axis table not resolvable: {exc}")]
    ar, ac = spec.axis_cell
    axis_now = np.asarray(axis_view.values, dtype=np.float64).ravel()
    prev = axis_now[ac - 1] if ac - 1 >= 0 else -np.inf
    nxt = axis_now[ac + 1] if ac + 1 < axis_now.size else np.inf
    if not (prev < spec.axis_target < nxt):
        return [TableOutcome(
            spec.axis_symbol, section, OUTCOME_AXIS_MISMATCH,
            detail=(f"axis target {_fmt(spec.axis_target)} would break monotonicity "
                    f"({_fmt(prev)} < target < {_fmt(nxt)}) — not written"),
        )]
    outcomes: list[TableOutcome] = []
    old_bp = float(axis_now[ac])
    status, text = _run_write(lambda: axis_view.set_cell(ar, ac, spec.axis_target))
    if status == "guard_blocked":
        return [TableOutcome(spec.axis_symbol, section, OUTCOME_GUARD_BLOCKED, detail=text)]
    outcomes.append(TableOutcome(
        spec.axis_symbol, section, OUTCOME_APPLIED,
        detail=f"raised PUT Y breakpoint {spec.axis_cell}", old=old_bp,
        new=spec.axis_target, warning=text))
    # 3) write the shaped last row of IP_PUT_SP — stage the whole grid in one
    # range-checked write so a guard trip leaves the table byte-identical
    # (CR-20260707-01); columns beyond last_row_values keep their stock values.
    last = view.shape[0] - 1
    target = view.values.astype(np.float64).copy()
    for c, v in enumerate(spec.last_row_values):
        target[last, c] = v
    status, text = _run_write(lambda: view.set(target))
    if status == "guard_blocked":
        outcomes.append(TableOutcome(view.symbol, section, OUTCOME_GUARD_BLOCKED,
                                     detail=text))
        return outcomes
    outcomes.append(TableOutcome(
        view.symbol, section, OUTCOME_APPLIED,
        detail=f"shaped boost curve into last row ({len(spec.last_row_values)} cells)",
        warning=text))
    return outcomes


# ---- U3: guarded ceiling write --------------------------------------------- #
def _guarded_ceiling_write(view: TableView, section: str, target: float) -> TableOutcome:
    """Raise every cell of a limiter to ``target`` — but never lower a higher cell.

    Reads each cell first (per the guide's "if already >2700, don't touch"):
    stages ``target`` only in cells below ``target`` and leaves cells at or above
    it untouched (never lowered). The whole grid is staged in one range-checked
    write, so a guard trip leaves the table byte-identical. Outcomes:

    * ``applied``           — at least one cell was below target and got raised;
    * ``already_satisfied`` — every cell already equals the target (nothing staged);
    * ``guarded_skip``      — every cell already at/above target, none equal it
      (byte-identical — the never-lower guard, plan Key Decision 3 / AE2);
    * ``guard_blocked``     — the write would exceed the table's declared upper
      limit (float-bug tables raise :class:`FloatBugGuardError` inside the staged
      write; any other table is rejected here rather than warn-and-overflow) —
      table byte-identical, recipe continues.

    Works for both 1x1 limiter constants (compressor temp, turbo speed, ...) and
    multi-cell limiter maps: ``IP_PUT_AMP_DIF_MAX_PRS_DIF_THR``  — Overpressure
    upstream throttle threshold for turbocharger overpressure diagnosis (P0234),
    1x6 hPa, is broadcast across all six cells.
    """
    current = view.values.astype(np.float64)
    tol = 1e-6 * (abs(target) + 1.0)

    # Never write above the table's declared ceiling. Float-bug tables raise
    # FloatBugGuardError inside the staged write below (kept for their specific
    # message + guard_blocked). Any other table would only warn-and-write, so
    # reject it here — fail loud, never overflow a limiter's element width (2b).
    zmax = view.table.z.max if view.table.z is not None else None
    if zmax is not None and target > zmax + tol and not is_float_bug_table(view.table):
        return TableOutcome(
            view.symbol, section, OUTCOME_GUARD_BLOCKED,
            old=float(current.min()), new=target,
            detail=(f"target {_fmt(target)} exceeds the table's declared upper "
                    f"limit {_fmt(zmax)} — refusing to write (never overflow a "
                    "limiter ceiling); table left byte-identical."),
        )

    below = current < target - tol
    if not below.any():
        # Nothing to raise: either every cell is exactly at target, or one or more
        # sit above it (never lowered). Report the extreme cell so it is auditable.
        if float(np.abs(current - target).max()) <= tol:
            return TableOutcome(view.symbol, section, OUTCOME_ALREADY_SATISFIED,
                                old=float(current.min()), new=target)
        return TableOutcome(
            view.symbol, section, OUTCOME_GUARDED_SKIP,
            old=float(current.max()), new=target,
            detail=(f"all {current.size} cell(s) already at/above target "
                    f"{_fmt(target)} (max {_fmt(float(current.max()))}) — left "
                    "unchanged (never lowered)"),
        )

    staged = current.copy()
    staged[below] = target
    old_min = float(current[below].min())
    raised = int(below.sum())
    status, text = _run_write(lambda: view.set(staged))
    if status == "guard_blocked":
        return TableOutcome(view.symbol, section, OUTCOME_GUARD_BLOCKED,
                            detail=text, old=old_min, new=target)
    detail = ""
    if current.size > 1:
        detail = (f"raised {raised} of {current.size} cell(s) below target to "
                  f"{_fmt(target)} (min {_fmt(old_min)} -> {_fmt(target)}); any "
                  "cell already at/above target left unchanged (never lowered)")
    return TableOutcome(view.symbol, section, OUTCOME_APPLIED,
                        old=old_min, new=target, warning=text, detail=detail)


# ---- U4: TTA/ATT proportional build-out ------------------------------------ #
# Minimum per-column linear fit quality (R²) over the sub-threshold rows before
# a table's build-out is trusted. A column below this is a "not well-approximated
# by a line" case — the whole table is reported (poor_fit) rather than written
# with a poor fit (plan U4 test scenarios / verification).
_BUILDOUT_MIN_R2 = 0.95


def _column_linear_fit(y: np.ndarray, z: np.ndarray) -> tuple[float, float, float]:
    """Least-squares line ``z ≈ m·y + b`` plus its R². Degenerate → R²=1.0."""
    m, b = np.polyfit(y, z, 1)
    resid = z - (m * y + b)
    ss_res = float(np.sum(resid ** 2))
    ss_tot = float(np.sum((z - z.mean()) ** 2))
    r2 = 1.0 if ss_tot < 1e-12 else 1.0 - ss_res / ss_tot
    return float(m), float(b), r2


def _apply_tta_att_buildout(
    view: TableView, section: str, spec: "BuildoutSpec"
) -> TableOutcome:
    """Extend a TTA/ATT table's linear torque↔airmass trend above the threshold.

    For every column, fit a line to the rows at/below ``spec.threshold`` (left
    untouched per the guide's "only modify above 400") and *raise* any higher row
    that sits below the fitted trend up to it — never lowering a row that already
    meets or exceeds the trend. This "fill up to the line" rule matches the
    guide's "build it out" intent, keeps TTA and its paired ATT consistent (both
    extend their own linear physics), and never reduces an airmass/torque target.

    A table whose sub-threshold rows are not well-approximated by a line
    (any column R² < :data:`_BUILDOUT_MIN_R2`) is reported ``poor_fit`` and left
    byte-identical rather than written with a bad extrapolation.
    """
    yv = view.axis_values(spec.axis)
    if yv is None:
        return TableOutcome(view.symbol, section, OUTCOME_AXIS_MISMATCH,
                            detail=f"no decodable {spec.axis} axis — build-out not attempted")
    y = np.asarray(yv, dtype=np.float64).ravel()
    vals = view.values.astype(np.float64)
    rows, cols = view.shape
    fit_mask = y <= spec.threshold
    build_mask = ~fit_mask
    if int(fit_mask.sum()) < 3:
        return TableOutcome(
            view.symbol, section, OUTCOME_AXIS_MISMATCH,
            detail=f"only {int(fit_mask.sum())} rows ≤ {_fmt(spec.threshold)} — too few to fit",
        )
    if int(build_mask.sum()) == 0:
        return TableOutcome(view.symbol, section, OUTCOME_ALREADY_SATISFIED,
                            detail=f"no rows above {_fmt(spec.threshold)} to build out")

    new = vals.copy()
    worst_r2 = 1.0
    raised = 0
    yb = y[build_mask]
    for c in range(cols):
        m, b, r2 = _column_linear_fit(y[fit_mask], vals[fit_mask, c])
        worst_r2 = min(worst_r2, r2)
        line = m * yb + b
        cur = vals[build_mask, c]
        filled = np.maximum(cur, line)
        raised += int(np.sum(filled > cur + 1e-6))
        new[build_mask, c] = filled

    if worst_r2 < _BUILDOUT_MIN_R2:
        return TableOutcome(
            view.symbol, section, OUTCOME_POOR_FIT,
            detail=(f"sub-{_fmt(spec.threshold)} rows are not linear (min column "
                    f"R²={worst_r2:.3f} < {_BUILDOUT_MIN_R2}) — not written; needs manual build-out"),
        )
    if raised == 0:
        return TableOutcome(
            view.symbol, section, OUTCOME_ALREADY_SATISFIED,
            detail=f"already built out above {_fmt(spec.threshold)} (min R²={worst_r2:.3f})",
        )
    status, text = _run_write(lambda: view.set(new))
    if status == "guard_blocked":
        return TableOutcome(view.symbol, section, OUTCOME_GUARD_BLOCKED, detail=text)
    return TableOutcome(
        view.symbol, section, OUTCOME_APPLIED_BUILDOUT,
        detail=(f"raised {raised} cells above {_fmt(spec.threshold)} to the linear "
                f"trend (min column R²={worst_r2:.3f})"),
        warning=text,
    )


# ---- entry dispatch -------------------------------------------------------- #
# Per-view writers keyed by kind.
_PER_VIEW_WRITERS: dict[str, Callable[[TableView, str, object], TableOutcome]] = {
    KIND_LITERAL_SCALAR: lambda v, s, t: _apply_literal_scalar(v, s, float(t)),
    KIND_LITERAL_BROADCAST: lambda v, s, t: _apply_literal_broadcast(v, s, float(t)),
    KIND_LITERAL_TABLE: _apply_literal_table,
    KIND_TORQUE_CURVE: _apply_torque_curve,
    KIND_CUT_TRANSFORM: _apply_cut_transform,
    KIND_IAT_ROWMAP: _apply_iat_rowmap,
    KIND_GUARDED_CEILING: lambda v, s, t: _guarded_ceiling_write(v, s, float(t)),
    KIND_TTA_ATT_BUILDOUT: _apply_tta_att_buildout,
}


def apply_entry(cal: CalFile, resolved: ResolvedEntry) -> list[TableOutcome]:
    """Apply one resolved entry, returning one :class:`TableOutcome` per table.

    Skip-kind entries yield a single documented ``skipped`` outcome. Write-kind
    entries yield one outcome per symbol: an unresolved symbol becomes an
    ``unresolved`` outcome (never a guess), a resolved one is dispatched to its
    per-kind writer. ``axis_write`` is special-cased (it drives a second, axis
    table beyond its own symbol). ``guarded_ceiling`` / ``tta_att_buildout`` are
    registered by U3 / U4.
    """
    entry = resolved.entry
    section = entry.guide_section

    if entry.kind in SKIP_KINDS:
        syms = ", ".join(entry.symbols) if entry.symbols else (
            " | ".join(entry.search_prefixes) if entry.search_prefixes else "—"
        )
        return [TableOutcome(syms, section, OUTCOME_SKIPPED,
                             detail=f"[{entry.kind}] {entry.reason}")]

    outcomes: list[TableOutcome] = []

    if entry.kind == KIND_AXIS_WRITE:
        res = resolved.resolutions[0]
        if not res.resolved or res.view is None:
            return [TableOutcome(res.symbol, section, OUTCOME_UNRESOLVED, detail=res.reason)]
        return _apply_axis_write(cal, res.view, section, entry.target)

    writer = _PER_VIEW_WRITERS.get(entry.kind)
    if writer is None:
        raise NotImplementedError(
            f"no writer registered for kind {entry.kind!r} "
            f"(section {section!r}) — U3/U4 add guarded_ceiling / tta_att_buildout"
        )
    for res in resolved.resolutions:
        if not res.resolved or res.view is None:
            outcomes.append(TableOutcome(res.symbol, section, OUTCOME_UNRESOLVED,
                                         detail=res.reason))
            continue
        outcomes.append(writer(res.view, section, entry.target))
    return outcomes


# =========================================================================== #
# U5 — report + coherence
# =========================================================================== #
# Outcomes that mean "the guide's target state is in place for this entry"
# (whether we wrote it or found it already correct). Used by the coherence
# check, which reasons about *state*, not about whether a byte changed.
_IN_PLACE = frozenset({OUTCOME_APPLIED, OUTCOME_APPLIED_BUILDOUT, OUTCOME_ALREADY_SATISFIED})

# Human ordering for the grouped report (most-actionable first).
_OUTCOME_ORDER = (
    OUTCOME_APPLIED,
    OUTCOME_APPLIED_BUILDOUT,
    OUTCOME_ALREADY_SATISFIED,
    OUTCOME_GUARDED_SKIP,
    OUTCOME_GUARD_BLOCKED,
    OUTCOME_AXIS_MISMATCH,
    OUTCOME_POOR_FIT,
    OUTCOME_UNRESOLVED,
    OUTCOME_SKIPPED,
)


@dataclass(frozen=True)
class CoherenceRule:
    """A declared coupling: if ``when_section`` is in place, ``needs_section`` must be too.

    The tune is a coupled system but the recipe applies entries independently, so
    these rules catch dangerous divergences (e.g. boost without fueling) and mark
    the report **DO NOT FLASH**. ``severity`` is ``"DO NOT FLASH"`` or ``"note"``.
    """

    when_section: str
    needs_section: str
    severity: str
    message: str


@dataclass(frozen=True)
class CoherenceFinding:
    severity: str
    message: str


# The coherence rules live here, alongside the symbol map — a small declared
# list, not logic scattered across the writer (plan U5).
COHERENCE_RULES: tuple[CoherenceRule, ...] = (
    CoherenceRule(
        "Boost — Option 2", "Fueling — Basic lambda", "DO NOT FLASH",
        "boost curve applied without lambda enrichment — LEAN RISK at full load",
    ),
    CoherenceRule(
        "Boost — Option 2", "Boost — Max PR flatten", "DO NOT FLASH",
        "boost curve applied without flattening Max PR — the PR cap may defeat the curve",
    ),
    CoherenceRule(
        "Boost — Option 2", "Boost — Option 3 torque-tune selector", "DO NOT FLASH",
        "boost curve applied without the torque-tune selector — Option 2 not activated",
    ),
    CoherenceRule(
        "Fueling — Basic lambda", "Boost — Option 2", "note",
        "lambda enrichment applied without the boost curve — harmless, but the "
        "fuelling assumes the higher load",
    ),
)


@dataclass(frozen=True)
class RecipeReport:
    """Every table's outcome from one recipe run, plus derived views.

    A frozen wrapper over a tuple of :class:`TableOutcome`. No new file format —
    :func:`format_report` renders it to Markdown for the demo to write to disk.
    """

    outcomes: tuple[TableOutcome, ...]

    def by_outcome(self) -> dict[str, list[TableOutcome]]:
        groups: dict[str, list[TableOutcome]] = {}
        for o in self.outcomes:
            groups.setdefault(o.outcome, []).append(o)
        return groups

    def counts(self) -> dict[str, int]:
        return {k: len(v) for k, v in self.by_outcome().items()}

    def _sections_in_place(self) -> set[str]:
        return {o.guide_section for o in self.outcomes if o.outcome in _IN_PLACE}

    def coherence(self) -> list[CoherenceFinding]:
        """Evaluate :data:`COHERENCE_RULES` against this run's outcomes."""
        in_place = self._sections_in_place()

        def any_startswith(prefix: str) -> bool:
            return any(s.startswith(prefix) for s in in_place)

        findings: list[CoherenceFinding] = []
        for rule in COHERENCE_RULES:
            if any_startswith(rule.when_section) and not any_startswith(rule.needs_section):
                findings.append(CoherenceFinding(rule.severity, rule.message))
        return findings

    def do_not_flash(self) -> bool:
        return any(f.severity == "DO NOT FLASH" for f in self.coherence())


def _md_table(headers: list[str], rows: list[list[str]]) -> str:
    """Render an aligned GitHub-Markdown table (padded columns, like the docs)."""
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    def fmt(cells: list[str]) -> str:
        return "| " + " | ".join(c.ljust(widths[i]) for i, c in enumerate(cells)) + " |"
    sep = "|-" + "-|-".join("-" * w for w in widths) + "-|"
    return "\n".join([fmt(headers), sep, *(fmt(r) for r in rows)])


def _oldnew(o: TableOutcome) -> str:
    if o.old is None and o.new is None:
        return ""
    return f"{_fmt(o.old) if o.old is not None else '—'} → {_fmt(o.new) if o.new is not None else '—'}"


def format_report(report: RecipeReport, *, title: str = "SOP Tune Recipe — Report") -> str:
    """Render a :class:`RecipeReport` to a human-readable Markdown string.

    Opens with the coherence check: any **DO NOT FLASH** finding is the first
    thing shown (the bin still saved — the human gate decides). Then a per-outcome
    count summary, then one grouped table per outcome. For applied scalar entries
    the old→new column *is* the review artifact (no PNG exists for a single cell,
    Key Decision 6), so it is always shown.
    """
    lines: list[str] = [f"# {title}", ""]

    findings = report.coherence()
    dnf = [f for f in findings if f.severity == "DO NOT FLASH"]
    notes = [f for f in findings if f.severity != "DO NOT FLASH"]
    if dnf:
        lines += ["## ⛔ DO NOT FLASH", ""]
        lines += [f"- **{f.message}**" for f in dnf]
        lines.append("")
    else:
        lines += ["## ✅ Coherence check passed", "",
                  "No dependent-entry divergence detected. (Still pass the human "
                  "review gate + checksum verify before flashing.)", ""]
    if notes:
        lines += ["### Notes", ""] + [f"- {f.message}" for f in notes] + [""]

    counts = report.counts()
    lines += ["## Summary", ""]
    summary_rows = [[k, str(counts[k])] for k in _OUTCOME_ORDER if k in counts]
    # any outcome not in the canonical order (future-proofing) still appears.
    summary_rows += [[k, str(v)] for k, v in counts.items() if k not in _OUTCOME_ORDER]
    lines += [_md_table(["Outcome", "Tables"], summary_rows), ""]

    groups = report.by_outcome()
    ordered = [k for k in _OUTCOME_ORDER if k in groups]
    ordered += [k for k in groups if k not in _OUTCOME_ORDER]
    for key in ordered:
        outs = groups[key]
        lines += [f"## {key} ({len(outs)})", ""]
        rows = [
            [o.symbol or "—", o.guide_section, _oldnew(o),
             (o.detail + (f" ⚠ {o.warning}" if o.warning else "")).strip()]
            for o in outs
        ]
        lines += [_md_table(["Symbol", "Guide section", "Old → New", "Detail"], rows), ""]

    return "\n".join(lines).rstrip() + "\n"


# =========================================================================== #
# U6 — top-level orchestration
# =========================================================================== #
def apply_basics_sop(
    cal: CalFile, symbol_map: tuple[RecipeEntry, ...] = SYMBOL_MAP
) -> RecipeReport:
    """Apply the whole ``ecu-tuning-basics`` SOP to an open ``CalFile``.

    Resolves the symbol map (U1) against ``cal``, then applies every entry in the
    order the guide presents it (torque request → TTA/ATT → boost → timing →
    fueling → cooling → limiters — the map's own order), collecting one
    :class:`TableOutcome` per table into a :class:`RecipeReport` (U5).

    **Pure with respect to the filesystem:** it stages edits into the
    ``CalFile``'s in-memory buffer via the existing ``TableView`` API and returns
    the report — it does not save, verify checksums, or write PNGs. That is the
    caller's job (see ``demos/apply_sop_recipe.py``), keeping the library function
    testable and side-effect-free and letting the human gate decide what to do
    with a **DO NOT FLASH** report before anything touches disk or a flasher.

    Deterministic and re-runnable: it always starts from whatever ``cal`` was
    opened against (the stock bin) and applies the full map, so re-running after a
    map tweak regenerates the whole result rather than layering edits.
    """
    outcomes: list[TableOutcome] = []
    for resolved in resolve_symbol_map(cal, symbol_map):
        outcomes.extend(apply_entry(cal, resolved))
    return RecipeReport(tuple(outcomes))
