"""U1 — log loading, channel resolution, and the log-quality preflight.

Parses a folder of ``simostools-*.csv`` datalogs into a :class:`LogSet`: one
:class:`LogFile` per CSV, each carrying time-indexed numpy arrays keyed by
**canonical channel ID** with normalized units, the resolved gear channel, the
set of columns that could not be mapped, and a :class:`LogQuality` preflight.

Design rules (fail loud, never guess):

- **Channel resolution is header-driven.** A CSV header column ``Name (Unit)``
  is matched against :data:`CHANNEL_SPECS` by name (case-insensitive). A known
  name with a *recognized* unit is normalized to the canonical unit; a known
  name with an *unrecognized* unit is left **unmapped and reported**, never
  guessed at some scale. Unknown names are retained-but-unmapped too.
- **Gear obeys the confirmed header rule only** (see the project ``CLAUDE.md``):
  ``Gear (gear)`` is the actual gear; ``Gear ()`` is zero-indexed, so actual =
  logged + 1; any other form leaves gear *unresolved* (a sentinel), and
  gear-dependent checks downstream land in SKIPPED. Never guess an offset.
- **Two columns mapping to the same canonical channel is corruption** and
  raises :class:`DuplicateChannelError` at load time — the one hard failure.
- The **preflight never mutates or repairs data** — it annotates: per-file
  sample-interval statistics, time-gap events, stuck/frozen dynamic channels,
  and non-numeric/short-row accounting.
"""

from __future__ import annotations

import csv
import math
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional

import numpy as np

__all__ = [
    "AnalysisError",
    "DuplicateChannelError",
    "ChannelSpec",
    "CHANNEL_SPECS",
    "GearResolution",
    "GapEvent",
    "LogQuality",
    "LogFile",
    "LogSet",
    "load_logset",
    "load_logset_files",
]

CSV_GLOB = "simostools-*.csv"


class AnalysisError(Exception):
    """Base class for every fail-loud error raised by ``simoscal.analysis``."""


class DuplicateChannelError(AnalysisError):
    """Two CSV columns resolve to the same canonical channel — ambiguous scale.

    Raised at load time rather than silently keeping one column, because a log
    whose headers contradict cannot be trusted to feed the safety checks.
    """


# --------------------------------------------------------------------------- #
# Canonical channel map
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ChannelSpec:
    """One canonical channel and how CSV columns map onto it.

    ``names`` are the accepted header names (case-insensitive, unit stripped).
    ``units`` maps each accepted unit string (lowercased) to the multiplicative
    factor that converts it to :attr:`canonical_unit`; a header whose unit is
    absent from this map is treated as *unrecognized* — the column is reported
    unmapped rather than assigned a guessed scale. A ``units`` map of
    ``{"": 1.0}`` accepts the unit-less ``Name ()`` / ``Name`` forms.
    """

    id: str
    names: tuple[str, ...]
    canonical_unit: str
    units: dict[str, float]
    description: str = ""


def _spec(id, names, canonical_unit, units, description=""):
    if isinstance(names, str):
        names = (names,)
    return ChannelSpec(id, tuple(names), canonical_unit, units, description)


# Identity unit maps used repeatedly below.
_UNITLESS = {"": 1.0}


# The v1 canonical channel map. Seeded from the R01 and R04 BasicsGuide log
# headers plus the PIDs/ CSVs. Unit normalization pins a single canonical unit
# per channel so a check never has to branch on how a given log spelled it:
#   airmass -> mg/stk   (R04 logs g/stk, x1000)
#   fuel rail pressures -> bar   (R04 logs kPa, /100 == x0.01)
CHANNEL_SPECS: tuple[ChannelSpec, ...] = (
    _spec("time", "Time", "s", {"": 1.0, "s": 1.0}, "Log timestamp"),
    _spec("rpm", "Engine Speed", "rpm", {"rpm": 1.0}, "Engine speed"),
    _spec("airmass", "Airmass", "mg/stk",
          {"mg/stk": 1.0, "g/stk": 1000.0}, "Airmass per stroke"),
    _spec("airmass_sp", "Airmass SP", "mg/stk",
          {"mg/stk": 1.0, "g/stk": 1000.0}, "Airmass setpoint per stroke"),
    _spec("pedal", "Pedal Pos", "%", {"%": 1.0, "": 1.0}, "Accelerator pedal position"),
    _spec("tps", "TPS", "%", {"%": 1.0, "": 1.0}, "Throttle plate position"),
    _spec("gear", "Gear", "gear", {"gear": 1.0, "": 1.0}, "Transmission gear"),
    # Boost / manifold pressure tracking.
    _spec("put", "PUT", "kpa", {"kpa": 1.0}, "Pressure upstream of throttle (actual)"),
    _spec("put_sp", "PUT SP", "kpa", {"kpa": 1.0}, "Pressure upstream of throttle setpoint"),
    _spec("map", "MAP", "kpa", {"kpa": 1.0}, "Manifold absolute pressure (actual)"),
    _spec("map_sp", "MAP SP", "kpa", {"kpa": 1.0}, "Manifold absolute pressure setpoint"),
    _spec("boost", "Boost", "psi", {"psi": 1.0}, "Gauge boost pressure"),
    # Fueling.
    _spec("lambda", "Lambda", "l", {"l": 1.0, "": 1.0}, "Lambda (actual)"),
    _spec("lambda_sp", "Lambda SP", "l", {"l": 1.0, "": 1.0}, "Lambda setpoint"),
    _spec("fp_di", "FP DI", "bar", {"bar": 1.0, "kpa": 0.01}, "Direct-injection rail pressure"),
    _spec("fp_di_sp", "FP DI SP", "bar", {"bar": 1.0, "kpa": 0.01},
          "Direct-injection rail pressure setpoint"),
    _spec("fp_mpi", "FP MPI", "bar", {"bar": 1.0, "kpa": 0.01}, "Port-injection rail pressure"),
    _spec("fp_mpi_sp", "FP MPI SP", "bar", {"bar": 1.0, "kpa": 0.01},
          "Port-injection rail pressure setpoint"),
    _spec("lpfp_duty", "LPFP Duty", "%", {"%": 1.0, "": 1.0}, "Low-pressure fuel pump duty"),
    _spec("hpfp_eff_vol", "HPFP Eff Vol", "%", {"%": 1.0, "": 1.0},
          "High-pressure fuel pump effective volume"),
    _spec("eth_content", "Eth Content", "%", {"%": 1.0, "": 1.0}, "Ethanol content"),
    # Knock / timing.
    _spec("knock_1", ("Knock Cyl 1",), "deg", {"°": 1.0, "deg": 1.0}, "Knock retard cylinder 1"),
    _spec("knock_2", ("Knock Cyl 2",), "deg", {"°": 1.0, "deg": 1.0}, "Knock retard cylinder 2"),
    _spec("knock_3", ("Knock Cyl 3",), "deg", {"°": 1.0, "deg": 1.0}, "Knock retard cylinder 3"),
    _spec("knock_4", ("Knock Cyl 4",), "deg", {"°": 1.0, "deg": 1.0}, "Knock retard cylinder 4"),
    _spec("ign_avg", "Ign Avg", "deg", {"°": 1.0, "deg": 1.0}, "Average ignition advance"),
    _spec("ign_table", "Ign Table", "deg", {"°": 1.0, "deg": 1.0}, "Table ignition advance"),
    # Temperatures / environment.
    _spec("iat", "IAT", "degc", {"°c": 1.0, "degc": 1.0}, "Intake air temperature"),
    _spec("coolant_temp", "Coolant Temp", "degc", {"°c": 1.0, "degc": 1.0}, "Coolant temperature"),
    _spec("oil_temp", "Oil Temp", "degc", {"°c": 1.0, "degc": 1.0}, "Oil temperature"),
    _spec("trans_temp", "Trans Temp", "degc", {"°c": 1.0, "degc": 1.0}, "Transmission temperature"),
    _spec("turbo_air_temp", "Turbo Air Temp", "degc", {"°c": 1.0, "degc": 1.0},
          "Turbo compressor outlet air temperature"),
    _spec("ambient_temp", "Ambient Temp", "degc", {"°c": 1.0, "degc": 1.0}, "Ambient air temperature"),
    _spec("ambient_press", "Ambient Press", "kpa", {"kpa": 1.0}, "Ambient (barometric) pressure"),
    # Turbo / wastegate.
    _spec("turbo_speed", "Turbo Speed", "rpm", {"rpm": 1.0}, "Turbocharger shaft speed"),
    _spec("intake_flow_fact", "Intake Flow Fact", "-", _UNITLESS, "Intake flow factor"),
    _spec("exh_flow_factor", "Exh Flow Factor", "-", _UNITLESS, "Exhaust flow factor"),
    _spec("wg_pos_base", "WG Pos Base", "%", {"%": 1.0, "": 1.0}, "Wastegate base/feedforward position"),
    _spec("wg_pos_final", "WG Pos Final", "%", {"%": 1.0, "": 1.0}, "Wastegate final position"),
    _spec("wg_i_value", "WG I Value", "%", {"%": 1.0, "": 1.0}, "Wastegate integral correction"),
    _spec("wg_pd_value", "WG P-D Value", "%", {"%": 1.0, "": 1.0}, "Wastegate proportional/derivative correction"),
    _spec("wg_flow_des", "WG Flow Des", "kg/hr", {"kg/hr": 1.0}, "Wastegate desired flow"),
    _spec("wastegate", "Wastegate", "%", {"%": 1.0, "": 1.0}, "Wastegate command (actual)"),
    _spec("wastegate_sp", "Wastegate SP", "%", {"%": 1.0, "": 1.0}, "Wastegate command setpoint"),
    # Torque / performance.
    _spec("torque", "Torque", "nm", {"nm": 1.0}, "Delivered engine torque"),
    _spec("torque_req", "Torque Req", "nm", {"nm": 1.0}, "Requested engine torque"),
    _spec("torque_lim", "Torque Lim", "-", _UNITLESS, "Torque-limiter source bitmask"),
    _spec("calc_hp", "Calc HP", "hp", {"hp": 1.0}, "Calculated power"),
    _spec("vehicle_speed", "Vehicle Speed", "km/h",
          {"km/h": 1.0, "km/hr": 1.0}, "Vehicle speed"),
    # Per-wheel speeds — the switch-patch slip-based TC inputs (FWD: front driven).
    _spec("wheel_fl", "Wheel Speed FL", "km/h",
          {"km/h": 1.0, "km/hr": 1.0}, "Wheel speed front-left"),
    _spec("wheel_fr", "Wheel Speed FR", "km/h",
          {"km/h": 1.0, "km/hr": 1.0}, "Wheel speed front-right"),
    _spec("wheel_rl", "Wheel Speed RL", "km/h",
          {"km/h": 1.0, "km/hr": 1.0}, "Wheel speed rear-left"),
    _spec("wheel_rr", "Wheel Speed RR", "km/h",
          {"km/h": 1.0, "km/hr": 1.0}, "Wheel speed rear-right"),
)

# Name (lowercased) -> spec, for header matching.
_SPEC_BY_NAME: dict[str, ChannelSpec] = {}
for _s in CHANNEL_SPECS:
    for _n in _s.names:
        _SPEC_BY_NAME[_n.lower()] = _s

# Canonical id -> spec, for downstream description lookups.
SPEC_BY_ID: dict[str, ChannelSpec] = {s.id: s for s in CHANNEL_SPECS}

# Channels that MUST move while the engine sweeps rpm; a zero-variance one over a
# real rpm sweep is a stuck sensor, not a legitimately-constant signal (unlike
# eth content or cruise). Used only by the frozen-channel preflight.
_DYNAMIC_CHANNELS: tuple[str, ...] = (
    "rpm", "airmass", "put", "map", "boost", "turbo_speed", "vehicle_speed",
)

# Preflight tuning constants (named so U4 can print them with the battery).
GAP_TOLERANCE_MULT = 5.0     # a dt beyond this * median interval is a gap
GAP_MIN_ABS_S = 0.05         # ... and must exceed this absolute jump to count
RPM_SWEEP_MIN = 1500.0       # rpm span above which a dynamic channel must move
STUCK_EPS = 1e-9             # value span below this over a sweep == frozen

_HEADER_RE = re.compile(r"^\s*(?P<name>.*?)\s*\((?P<unit>[^)]*)\)\s*$")


class GearResolution:
    """Sentinels for how a file's gear channel was resolved."""

    ACTUAL = "actual"                  # `Gear (gear)`  — logged value is real
    LOGGED_PLUS_ONE = "logged_plus_one"  # `Gear ()`     — real = logged + 1
    UNRESOLVED = "unresolved"          # any other form — gear-dependent checks SKIP
    ABSENT = "absent"                  # no gear column at all


@dataclass(frozen=True)
class GapEvent:
    """A time discontinuity: sampling paused between two adjacent rows."""

    index: int          # row index of the sample AFTER the gap
    t_before: float
    t_after: float
    gap_s: float


@dataclass(frozen=True)
class LogQuality:
    """Preflight annotations for one CSV — never a repair, only a description."""

    n_rows: int
    n_short_rows: int                 # rows with fewer columns than the header
    interval_median_s: float
    interval_min_s: float
    interval_max_s: float
    gaps: tuple[GapEvent, ...]
    stuck_channels: tuple[str, ...]   # canonical ids frozen over a real rpm sweep
    unit_unrecognized: tuple[tuple[str, str], ...]  # (header, canonical id it would map to)

    @property
    def is_clean(self) -> bool:
        return not self.gaps and not self.stuck_channels and self.n_short_rows == 0


@dataclass(frozen=True)
class LogFile:
    """One parsed CSV: canonical channels as numpy arrays, plus metadata."""

    name: str                         # file stem, e.g. "simostools-2026_07_07-22_15_22"
    path: Path
    data: dict[str, np.ndarray]       # canonical id -> float array (NaN = missing/unparsed)
    gear_resolution: str              # a GearResolution sentinel
    unmapped_headers: tuple[str, ...]  # columns retained but not mapped to a canonical id
    quality: LogQuality

    @property
    def n_rows(self) -> int:
        return self.quality.n_rows

    def has(self, channel_id: str) -> bool:
        """True if the canonical channel is present in this file."""
        return channel_id in self.data

    def channel(self, channel_id: str) -> Optional[np.ndarray]:
        """The canonical channel array, or ``None`` if absent from this file."""
        return self.data.get(channel_id)

    @property
    def time(self) -> Optional[np.ndarray]:
        return self.data.get("time")

    @property
    def gear_resolved(self) -> bool:
        """True when the gear channel carries a trustworthy actual-gear value."""
        return self.gear_resolution in (GearResolution.ACTUAL, GearResolution.LOGGED_PLUS_ONE)


@dataclass(frozen=True)
class LogSet:
    """A folder of parsed logs plus the folder path they came from.

    ``notes`` records load-time decisions the report must surface — chiefly the
    duplicate/trimmed-capture dedup (see :func:`_dedup_overlapping`).
    """

    folder: Path
    files: tuple[LogFile, ...]
    notes: tuple[str, ...] = ()

    def __iter__(self):
        return iter(self.files)

    def __len__(self) -> int:
        return len(self.files)

    def channels(self) -> frozenset[str]:
        """Union of canonical channel IDs present across all files."""
        out: set[str] = set()
        for f in self.files:
            out.update(f.data.keys())
        return frozenset(out)

    def has(self, channel_id: str) -> bool:
        """True if *every* file carries the channel (a check can rely on it)."""
        return all(f.has(channel_id) for f in self.files) and len(self.files) > 0

    @property
    def any_gear_resolved(self) -> bool:
        return any(f.gear_resolved for f in self.files)


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def _parse_header(col: str) -> tuple[str, str]:
    """Split a CSV column header into ``(name, unit_lower)``.

    ``"Airmass (mg/stk)"`` -> ``("Airmass", "mg/stk")``; ``"Gear ()"`` ->
    ``("Gear", "")``; a header with no parens (``"Time"``) -> ``("Time", "")``.
    """
    m = _HEADER_RE.match(col)
    if m:
        return m.group("name").strip(), m.group("unit").strip().lower()
    return col.strip(), ""


def _resolve_columns(
    header: list[str],
) -> tuple[dict[int, tuple[ChannelSpec, float]], list[str], str, list[tuple[str, str]]]:
    """Map header columns to canonical specs.

    Returns ``(col_map, unmapped, gear_resolution, unit_unrecognized)`` where
    ``col_map`` is ``{column_index: (spec, unit_factor)}``. Raises
    :class:`DuplicateChannelError` if two columns resolve to the same canonical
    id. The gear column is handled specially (its factor is always 1.0; the
    +1 offset, if any, is applied to the data later).
    """
    col_map: dict[int, tuple[ChannelSpec, float]] = {}
    unmapped: list[str] = []
    unit_unrecognized: list[tuple[str, str]] = []
    seen_ids: dict[str, str] = {}   # canonical id -> the header that claimed it
    gear_resolution = GearResolution.ABSENT

    for idx, col in enumerate(header):
        name, unit = _parse_header(col)
        spec = _SPEC_BY_NAME.get(name.lower())
        if spec is None:
            unmapped.append(col)
            continue

        if spec.id == "gear":
            # Header rule only; never guess an offset.
            if unit == "gear":
                gear_resolution = GearResolution.ACTUAL
            elif unit == "":
                gear_resolution = GearResolution.LOGGED_PLUS_ONE
            else:
                gear_resolution = GearResolution.UNRESOLVED
                unmapped.append(col)
                continue
            factor = 1.0
        else:
            factor = spec.units.get(unit)
            if factor is None:
                # Known channel, unrecognized unit: do NOT guess a scale.
                unit_unrecognized.append((col, spec.id))
                unmapped.append(col)
                continue

        if spec.id in seen_ids:
            raise DuplicateChannelError(
                f"columns {seen_ids[spec.id]!r} and {col!r} both map to canonical "
                f"channel {spec.id!r} — contradictory header, refusing to guess"
            )
        seen_ids[spec.id] = col
        col_map[idx] = (spec, factor)

    return col_map, unmapped, gear_resolution, unit_unrecognized


def _to_float(token: str) -> float:
    try:
        return float(token)
    except (TypeError, ValueError):
        return math.nan


def _preflight(
    data: dict[str, np.ndarray],
    n_rows: int,
    n_short_rows: int,
    unit_unrecognized: list[tuple[str, str]],
) -> LogQuality:
    """Compute sample-interval stats, gaps, and stuck channels. No mutation."""
    time = data.get("time")
    gaps: list[GapEvent] = []
    med = mn = mx = math.nan
    if time is not None and time.size >= 2:
        dt = np.diff(time)
        finite = dt[np.isfinite(dt)]
        if finite.size:
            med = float(np.median(finite))
            mn = float(np.min(finite))
            mx = float(np.max(finite))
            if med > 0:
                thresh = max(GAP_TOLERANCE_MULT * med, GAP_MIN_ABS_S)
                for i, step in enumerate(dt):
                    if np.isfinite(step) and step > thresh:
                        gaps.append(GapEvent(i + 1, float(time[i]), float(time[i + 1]), float(step)))

    stuck: list[str] = []
    rpm = data.get("rpm")
    rpm_sweeps = False
    if rpm is not None:
        finite_rpm = rpm[np.isfinite(rpm)]
        if finite_rpm.size and (float(np.max(finite_rpm)) - float(np.min(finite_rpm))) > RPM_SWEEP_MIN:
            rpm_sweeps = True
    if rpm_sweeps:
        for cid in _DYNAMIC_CHANNELS:
            arr = data.get(cid)
            if arr is None:
                continue
            finite = arr[np.isfinite(arr)]
            if finite.size >= 2 and (float(np.max(finite)) - float(np.min(finite))) < STUCK_EPS:
                stuck.append(cid)

    return LogQuality(
        n_rows=n_rows,
        n_short_rows=n_short_rows,
        interval_median_s=med,
        interval_min_s=mn,
        interval_max_s=mx,
        gaps=tuple(gaps),
        stuck_channels=tuple(stuck),
        unit_unrecognized=tuple(unit_unrecognized),
    )


def load_logfile(path: Path) -> LogFile:
    """Parse a single ``simostools-*.csv`` into a :class:`LogFile`."""
    path = Path(path)
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.reader(handle)
        try:
            header = next(reader)
        except StopIteration:
            raise AnalysisError(f"empty CSV (no header row): {path}") from None
        rows = [r for r in reader if r]  # drop fully-empty trailing rows

    col_map, unmapped, gear_resolution, unit_unrecognized = _resolve_columns(header)

    n_rows = len(rows)
    n_cols = len(header)
    n_short_rows = sum(1 for r in rows if len(r) < n_cols)

    # Build one float array per mapped canonical channel, applying unit factors.
    data: dict[str, np.ndarray] = {}
    for idx, (spec, factor) in col_map.items():
        arr = np.empty(n_rows, dtype=float)
        for i, r in enumerate(rows):
            arr[i] = _to_float(r[idx]) if idx < len(r) else math.nan
        if factor != 1.0:
            arr = arr * factor
        data[spec.id] = arr

    # Apply the gear offset per the resolved header rule.
    if "gear" in data and gear_resolution == GearResolution.LOGGED_PLUS_ONE:
        data["gear"] = data["gear"] + 1.0

    quality = _preflight(data, n_rows, n_short_rows, unit_unrecognized)

    return LogFile(
        name=path.stem,
        path=path,
        data=data,
        gear_resolution=gear_resolution,
        unmapped_headers=tuple(unmapped),
        quality=quality,
    )


def _time_interval(lf: LogFile) -> Optional[tuple[float, float]]:
    """The file's ``[t_min, t_max]`` from finite timestamps, or ``None``."""
    t = lf.time
    if t is None:
        return None
    finite = t[np.isfinite(t)]
    if not finite.size:
        return None
    return float(np.min(finite)), float(np.max(finite))


# A capture and its trimmed re-export share the same absolute time base; two
# genuinely-distinct captures never do (one device logs one file at a time), so
# any substantial time-range overlap means "same capture, counted twice".
DEDUP_OVERLAP_FRACTION = 0.5


def _dedup_overlapping(files: tuple[LogFile, ...]) -> tuple[tuple[LogFile, ...], list[str]]:
    """Drop duplicate/trimmed re-exports of the same capture, with a note.

    Two files whose time ranges overlap by at least
    :data:`DEDUP_OVERLAP_FRACTION` of the shorter range are the same underlying
    capture (e.g. R01's ``..._22_50_43.csv`` and ``..._22_50_43_trim.csv``).
    Naive globbing would double-count that pull in every summary, recurrence,
    and coverage count, so we keep the file with more rows (the superset) and
    record an explicit note — never silently counting twice.
    """
    intervals = [_time_interval(f) for f in files]
    notes: list[str] = []

    # Prefer the larger file as the survivor of any overlapping group.
    order = sorted(range(len(files)), key=lambda i: (-files[i].n_rows, files[i].name))
    dropped: set[int] = set()
    for i in order:
        if i in dropped:
            continue
        iv_i = intervals[i]
        if iv_i is None:
            continue
        for j in order:
            if j == i or j in dropped:
                continue
            iv_j = intervals[j]
            if iv_j is None:
                continue
            overlap = max(0.0, min(iv_i[1], iv_j[1]) - max(iv_i[0], iv_j[0]))
            shorter = min(iv_i[1] - iv_i[0], iv_j[1] - iv_j[0])
            if shorter > 0 and overlap >= DEDUP_OVERLAP_FRACTION * shorter:
                dropped.add(j)
                notes.append(
                    f"dropped '{files[j].name}' ({files[j].n_rows} rows): its time range overlaps "
                    f"'{files[i].name}' ({files[i].n_rows} rows) — same capture counted twice; "
                    "kept the larger file"
                )

    kept = tuple(f for idx, f in enumerate(files) if idx not in dropped)
    return kept, notes


def load_logset(folder: str | Path, *, glob: str = CSV_GLOB, dedup: bool = True) -> LogSet:
    """Load every ``simostools-*.csv`` under ``folder`` into a :class:`LogSet`.

    Files are loaded in sorted (deterministic) filename order. Raises
    :class:`AnalysisError` — naming the glob — if the folder holds no matching
    CSV, so an empty log folder fails loud rather than producing empty output.
    When ``dedup`` is set (default), duplicate/trimmed re-exports of the same
    capture are dropped with a note (see :func:`_dedup_overlapping`).
    """
    folder = Path(folder)
    if not folder.is_dir():
        raise AnalysisError(f"not a directory: {folder}")
    paths = sorted(folder.glob(glob))
    if not paths:
        raise AnalysisError(f"no {glob} files found under {folder}")
    return load_logset_files(paths, folder=folder, dedup=dedup)


def load_logset_files(
    paths,
    *,
    folder: Optional[str | Path] = None,
    dedup: bool = True,
    names: Optional[dict] = None,
) -> LogSet:
    """Load an explicit, ordered list of CSV paths into a :class:`LogSet`.

    :func:`load_logset` is the folder form and delegates here. This form exists
    for the embedded client: the Android app copies each picked CSV into
    app-private storage under a *content-addressed* name, so there is no folder
    to glob and no ``simostools-*.csv`` filename left to match. It is handed the
    verified paths instead.

    ``names`` optionally maps a path (as ``str``) to the display name the file
    should carry — again for the app, where the content-addressed filename on
    disk is a hash and the name a person recognises is the one the picker showed.
    A path absent from the map keeps its own stem, so the desktop path through
    this function is unchanged.

    Deduplication of overlapping captures still applies (see
    :func:`_dedup_overlapping`), because counting one pull twice is just as wrong
    whichever way the files arrived.
    """
    paths = [Path(p) for p in paths]
    if not paths:
        raise AnalysisError("no log files given")
    missing = [p for p in paths if not p.is_file()]
    if missing:
        raise AnalysisError(f"log file not found: {missing[0]}")

    files = []
    for path in paths:
        logfile = load_logfile(path)
        display = (names or {}).get(str(path))
        # Rename only the label. `LogFile.name` is what pulls, findings, and the
        # per-file plots key off, so it has to be the name a person will
        # recognise — but nothing about the parse depends on it.
        files.append(replace(logfile, name=display) if display else logfile)
    files = tuple(files)

    notes: list[str] = []
    if dedup:
        files, notes = _dedup_overlapping(files)
    return LogSet(
        folder=Path(folder) if folder is not None else paths[0].parent,
        files=files,
        notes=tuple(notes),
    )
