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

from dataclasses import dataclass
from typing import Optional

from .calfile import CalFile, TableView
from .model import AmbiguousTableError

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
        description="Raise the overboost (P0234) limit to 2700 hPa; never write over a higher value",
        kind=KIND_GUARDED_CEILING,
        symbols=("C_PRS_IM_SP_LIM",),  # candidate only — see reason; resolver will accept, U3 guards
        target=2700.0,
        reason=(
            "C_PRS_IM_SP_LIM is an OFFSET-to-baro constant whose stock value does "
            "not match the guide's overboost-limit screenshot; treated as a "
            "guarded raise, but flagged for manual confirmation before flashing."
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
