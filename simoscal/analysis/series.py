"""U8 — the evidence-plot inventory as data, plus the series extractor.

Every rpm-axis evidence plot the battery produces is declared here as a
:class:`PlotSpec` and nowhere else. Two consumers read these declarations:

- :mod:`simoscal.analysis.evidence` renders them to PNG with matplotlib, on a
  desktop where the ``plot`` extra is installed;
- :func:`simoscal.bridge.dispatch`'s ``analyze_logs`` op serializes them to JSON
  for the Android app, which draws them on a Compose canvas.

The reason this module exists is drift. matplotlib is deliberately outside the
mobile dependency closure (see the app's ``build.gradle.kts``), so the phone
cannot render the library's own PNGs and must draw its own. The moment the two
halves each decide for themselves *which channel belongs on which plot*, the app
and the desktop report start describing the same log differently — and the whole
point of the battery is that it is identical and enumerable (R1/R6). So the
inventory is data, the sample extraction (:func:`series_segments`) is shared
code, and only the mark-making differs.

What is **not** here: the per-file time-axis plots (``overview``,
``tc_activity``). They stay imperative in :mod:`evidence`, are desktop-only, and
carry no ``plot_ref`` on any finding.

Encoding rule, inherited unchanged from the evidence layer (D1): **quantity =
line style, pull = colour.** A ``primary`` series is the measured value, solid,
one colour per pull. A ``reference`` series is what was *asked for* — a
setpoint, a base table, a target — dashed and neutral, one legend entry however
many pulls are drawn. A ``secondary`` series is a second measured quantity
sharing the panel, dash-dot and neutral. A ``transient`` series is the
loaded-but-not-settled samples, drawn as faint scatter because a transient is
genuinely not curve-like.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import numpy as np

from .checks import (
    BOOST_HIGH_KPA,
    BOOST_WATCH_KPA,
    HPFP_WATCH_PCT,
    KNOCK_HIGH_DEG,
    KNOCK_WATCH_DEG,
    LAMBDA_WATCH,
    LPFP_WATCH_PCT,
    TURBO_SPEED_LIMIT_K,
    TURBO_SPEED_WATCH_K,
    WG_I_CLAMP_WATCH_PCT,
    _KNOCK_CHANNELS,
    _col,
    _loaded_mask,
    _settled_mask,
)
from .registry import CheckContext

__all__ = [
    "PSI_PER_KPA",
    "DERIVED",
    "Role",
    "Tone",
    "SeriesSpec",
    "ThresholdSpec",
    "PanelSpec",
    "PlotSpec",
    "PLOT_SPECS",
    "SPEC_BY_ID",
    "OVERLAY_PANEL_INDEX",
    "OVERLAY_PLOT_ID",
    "Segment",
    "SeriesData",
    "gear_trim_mask",
    "overlay_payload",
    "series_segments",
    "panel_available",
    "plot_payload",
    "contiguous_runs",
    "min_knock_arrays",
    "pull_ordinals",
]

#: 1 psi in kPa — for the gauge-boost reframe of PUT. Same constant the
#: evidence layer used before the inventory moved here.
PSI_PER_KPA = 6.894757


# --------------------------------------------------------------------------- #
# Derived sources
# --------------------------------------------------------------------------- #
# A spec names its y data with a string so the inventory stays plain data. A
# name that is a canonical channel id is read straight off the log; a name in
# DERIVED is computed from two or more channels. Anything else is a spec bug and
# raises rather than silently plotting nothing.
def min_knock_arrays(stacked) -> Optional[np.ndarray]:
    """Per-sample most-retarded value across the present knock-cylinder arrays.

    Takes raw arrays rather than a context so the whole-log time-axis plots in
    :mod:`evidence`, which never build a pull slice, can share it.
    """
    stacked = [a for a in stacked if a is not None]
    if not stacked:
        return None
    arr = np.vstack(stacked)
    with np.errstate(all="ignore"):
        return np.nanmin(np.where(np.isfinite(arr), arr, np.nan), axis=0)


def _min_knock_fn(ctx: CheckContext, pull) -> Optional[np.ndarray]:
    return min_knock_arrays([_col(ctx, pull, c) for c in _KNOCK_CHANNELS])


def _put_error_fn(ctx: CheckContext, pull) -> Optional[np.ndarray]:
    put = _col(ctx, pull, "put")
    sp = _col(ctx, pull, "put_sp")
    return None if put is None or sp is None else put - sp


def _boost_fn(ctx: CheckContext, pull) -> Optional[np.ndarray]:
    """Gauge boost (psi): PUT above ambient. The wastegate loop's controlled var."""
    put = _col(ctx, pull, "put")
    amb = _col(ctx, pull, "ambient_press")
    return None if put is None or amb is None else (put - amb) / PSI_PER_KPA


def _boost_sp_fn(ctx: CheckContext, pull) -> Optional[np.ndarray]:
    """Gauge boost setpoint (psi): PUT setpoint above ambient, same basis as `_boost_fn`."""
    sp = _col(ctx, pull, "put_sp")
    amb = _col(ctx, pull, "ambient_press")
    return None if sp is None or amb is None else (sp - amb) / PSI_PER_KPA


def _lambda_error_fn(ctx: CheckContext, pull) -> Optional[np.ndarray]:
    lam = _col(ctx, pull, "lambda")
    sp = _col(ctx, pull, "lambda_sp")
    return None if lam is None or sp is None else lam - sp


def _di_error_fn(ctx: CheckContext, pull) -> Optional[np.ndarray]:
    di = _col(ctx, pull, "fp_di")
    sp = _col(ctx, pull, "fp_di_sp")
    return None if di is None or sp is None else di - sp


#: Computed y sources, by the name a :class:`SeriesSpec` uses.
DERIVED: dict[str, Callable[[CheckContext, Any], Optional[np.ndarray]]] = {
    "min_knock": _min_knock_fn,
    "put_error": _put_error_fn,
    "boost": _boost_fn,
    "boost_sp": _boost_sp_fn,
    "lambda_error": _lambda_error_fn,
    "di_error": _di_error_fn,
}

#: Which canonical channels each derived source needs. Used to decide whether a
#: panel has any hope of drawing before any sample is touched, so a panel that
#: cannot be drawn is reported as such rather than rendered empty.
DERIVED_REQUIRES: dict[str, tuple[str, ...]] = {
    "min_knock": (),  # any one of the four knock channels will do
    "put_error": ("put", "put_sp"),
    "boost": ("put", "ambient_press"),
    "boost_sp": ("put_sp", "ambient_press"),
    "lambda_error": ("lambda", "lambda_sp"),
    "di_error": ("fp_di", "fp_di_sp"),
}


# --------------------------------------------------------------------------- #
# The declarative types
# --------------------------------------------------------------------------- #
class Role:
    """What a series *is*, which fixes how it is drawn. See the module docstring."""

    PRIMARY = "primary"        # measured; solid; one colour per pull
    REFERENCE = "reference"    # asked-for; dashed; neutral; one legend entry
    SECONDARY = "secondary"    # a second measured quantity; dash-dot; neutral
    TRANSIENT = "transient"    # loaded-but-unsettled samples; faint scatter


class Tone:
    """What a horizontal threshold line means. Never a limit the ECU enforces."""

    ZERO = "zero"      # the neutral reference line at 0
    WATCH = "watch"    # where this tool starts paying attention
    HIGH = "high"      # where it raises a High finding


@dataclass(frozen=True)
class SeriesSpec:
    """One line on a panel.

    ``source`` is a canonical channel id or a :data:`DERIVED` key. ``mask``
    selects the samples: ``"loaded"`` (loaded WOT), ``"settled"`` (loaded and
    not a shift/torque-cut transient), or ``"none"``.
    """

    source: str
    role: str = Role.PRIMARY
    label: str = ""
    mask: str = "loaded"


@dataclass(frozen=True)
class ThresholdSpec:
    """A horizontal line at a fixed value, with the meaning its tone carries."""

    value: float
    tone: str
    label: str = ""


@dataclass(frozen=True)
class PanelSpec:
    """One set of axes: a title, its labels, its series, and its threshold lines.

    ``requires`` names channels the *whole panel* needs before it is worth
    drawing at all — the gauge-boost panel is the case that motivates it, since
    without ambient pressure there is no honest baseline to zero boost against
    and guessing one is exactly what this library does not do.
    """

    title: str
    y_label: str
    series: tuple[SeriesSpec, ...]
    x_source: str = "rpm"
    x_label: str = "Engine speed (rpm)"
    thresholds: tuple[ThresholdSpec, ...] = ()
    requires: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlotSpec:
    """One evidence plot: a stack of panels sharing an id with its check.

    ``description`` says which parameters are drawn — it is the line that sits
    above the plot in the app and answers "what am I looking at". ``tip`` says
    how to *read* it. Both live here rather than in the app so the two halves
    describe the same plot in the same words.
    """

    id: str
    title: str
    description: str
    tip: str
    panels: tuple[PanelSpec, ...]


# --------------------------------------------------------------------------- #
# The inventory
# --------------------------------------------------------------------------- #
#: Every rpm-axis evidence plot, in id order.
#:
#: The ids are the check ids (so a fired finding can carry the plot as a
#: ``plot_ref``), except ``ignition``, which has no check and is standalone.
#: Listed alphabetically because that is also the order the app presents them
#: in, and one order everywhere is one less thing to reconcile.
PLOT_SPECS: tuple[PlotSpec, ...] = (
    PlotSpec(
        id="boost",
        title="Boost tracking",
        description=(
            "Gauge boost (PUT minus ambient) and absolute PUT against their "
            "setpoints, plus the PUT-minus-setpoint error, all against engine speed."
        ),
        tip=(
            "Solid is what the turbo actually delivered; dashed is what the ECU "
            "asked for. Where the solid line runs above the dashed one, boost is "
            "overshooting the setpoint — read the error panel underneath to see by "
            "how much, and whether it is a brief spike on the way up or a ridge "
            "that holds across the pull. A ridge is the one that matters."
        ),
        panels=(
            PanelSpec(
                title="Gauge boost actual vs setpoint (loaded WOT)",
                y_label="Boost / Boost SP (psi)",
                # Only drawn when ambient pressure was logged: without it there is
                # no baseline to zero gauge boost against, and guessing one would
                # put a plausible wrong number on the screen.
                requires=("put", "ambient_press", "put_sp"),
                series=(
                    SeriesSpec("boost", Role.PRIMARY, "Boost"),
                    SeriesSpec("boost_sp", Role.REFERENCE, "Boost SP"),
                ),
            ),
            PanelSpec(
                title="PUT actual vs setpoint (loaded WOT)",
                y_label="PUT / PUT SP (kPa)",
                series=(
                    SeriesSpec("put", Role.PRIMARY, "PUT"),
                    SeriesSpec("put_sp", Role.REFERENCE, "PUT SP"),
                ),
            ),
            PanelSpec(
                title="PUT overshoot",
                y_label="PUT - PUT SP (kPa)",
                series=(SeriesSpec("put_error", Role.PRIMARY),),
                thresholds=(
                    ThresholdSpec(0.0, Tone.ZERO),
                    ThresholdSpec(BOOST_WATCH_KPA, Tone.WATCH, f"+{BOOST_WATCH_KPA:.0f} watch"),
                    ThresholdSpec(BOOST_HIGH_KPA, Tone.HIGH, f"+{BOOST_HIGH_KPA:.0f} high"),
                ),
            ),
        ),
    ),
    PlotSpec(
        id="ignition",
        title="Delivered vs table timing",
        description=(
            "Ignition advance the engine actually ran (Ign Avg) against the advance "
            "the table asked for (Ign Table), over loaded WOT samples vs engine speed."
        ),
        tip=(
            "The gap between the two lines is timing the ECU pulled back out. A "
            "steady offset across the whole pull is ordinary correction; a notch "
            "that appears at one rpm and comes back on every pull is worth "
            "cross-checking against the knock plot before it is called a fuel issue."
        ),
        panels=(
            PanelSpec(
                title="Delivered vs table timing (loaded WOT)",
                y_label="Ignition advance (deg)",
                series=(
                    SeriesSpec("ign_avg", Role.PRIMARY, "Ign Avg"),
                    SeriesSpec("ign_table", Role.REFERENCE, "Ign Table"),
                ),
            ),
        ),
    ),
    PlotSpec(
        id="knock",
        title="Knock retard",
        description=(
            "The most-retarded cylinder at each sample — the minimum across all four "
            "per-cylinder knock channels — over loaded WOT samples vs engine speed."
        ),
        tip=(
            "Watch where the line dives, not how much it wiggles. Retard that lands "
            "at the same rpm on every pull is the calibration asking for more timing "
            "than the fuel will carry there; a single dip that never repeats is more "
            "often a bad tank or a heat-soaked run than a table problem."
        ),
        panels=(
            PanelSpec(
                title="Most-retarded cylinder (loaded WOT)",
                y_label="Knock retard (deg)",
                series=(SeriesSpec("min_knock", Role.PRIMARY),),
                thresholds=(
                    ThresholdSpec(0.0, Tone.ZERO),
                    ThresholdSpec(KNOCK_WATCH_DEG, Tone.WATCH, f"{KNOCK_WATCH_DEG} watch"),
                    ThresholdSpec(KNOCK_HIGH_DEG, Tone.HIGH, f"{KNOCK_HIGH_DEG} high"),
                ),
            ),
        ),
    ),
    PlotSpec(
        id="lambda",
        title="Lambda error",
        description=(
            "Measured lambda minus the lambda setpoint over settled WOT samples vs "
            "engine speed. The faint dots are loaded-but-transient samples — shift "
            "recovery and torque cuts — shown for context and excluded from the lines."
        ),
        tip=(
            "Above the zero line is lean of target. An error that climbs steadily "
            "with rpm usually means the fuel system is running out of capacity "
            "rather than that the target is wrong, so read the rail-pressure plot "
            "before changing anything in fuelling."
        ),
        panels=(
            PanelSpec(
                title="Settled-WOT lambda error",
                y_label="Lambda - Lambda SP",
                series=(
                    SeriesSpec("lambda_error", Role.TRANSIENT, "loaded transient", mask="loaded"),
                    SeriesSpec("lambda_error", Role.PRIMARY, mask="settled"),
                ),
                thresholds=(
                    ThresholdSpec(0.0, Tone.ZERO),
                    ThresholdSpec(LAMBDA_WATCH, Tone.WATCH, f"+{LAMBDA_WATCH} lean watch"),
                ),
            ),
        ),
    ),
    PlotSpec(
        id="rail_pressure",
        title="Rail pressure and pump headroom",
        description=(
            "Direct-injection rail pressure minus its setpoint (top), and low-pressure "
            "pump duty alongside high-pressure pump effective volume (bottom), both "
            "over loaded WOT samples vs engine speed."
        ),
        tip=(
            "A rail that sags below its setpoint at the top of the pull, at the same "
            "rpm as a pump line pressing up against its watch threshold, is a "
            "fuel-supply limit rather than a calibration choice — more boost will not "
            "fix it and will make the lambda plot worse."
        ),
        panels=(
            PanelSpec(
                title="DI rail pressure error (loaded WOT)",
                y_label="FP DI - FP DI SP (bar)",
                series=(SeriesSpec("di_error", Role.PRIMARY),),
                thresholds=(ThresholdSpec(0.0, Tone.ZERO),),
            ),
            PanelSpec(
                title="Fuel pump headroom",
                y_label="Percent",
                series=(
                    SeriesSpec("lpfp_duty", Role.PRIMARY, "LPFP"),
                    SeriesSpec("hpfp_eff_vol", Role.SECONDARY, "HPFP"),
                ),
                thresholds=(
                    ThresholdSpec(LPFP_WATCH_PCT, Tone.WATCH, f"{LPFP_WATCH_PCT:.0f}% LPFP"),
                    ThresholdSpec(HPFP_WATCH_PCT, Tone.HIGH, f"{HPFP_WATCH_PCT:.0f}% HPFP"),
                ),
            ),
        ),
    ),
    PlotSpec(
        id="turbo_heat",
        title="Turbo speed",
        description=(
            "Turbocharger shaft speed over loaded WOT samples vs engine speed, "
            "against the watch and hardware-limit lines."
        ),
        tip=(
            "The limit line here is a hardware one, not a number the calibration "
            "chose. A pull that reaches toward it on a cool day will go past it on a "
            "hot one, so the margin visible on this plot is the margin you are "
            "actually running."
        ),
        panels=(
            PanelSpec(
                title="Turbo speed (loaded WOT)",
                y_label="Turbo speed (krpm logged)",
                series=(SeriesSpec("turbo_speed", Role.PRIMARY),),
                thresholds=(
                    ThresholdSpec(TURBO_SPEED_WATCH_K, Tone.WATCH, f"{TURBO_SPEED_WATCH_K:.0f}k watch"),
                    ThresholdSpec(TURBO_SPEED_LIMIT_K, Tone.HIGH, f"{TURBO_SPEED_LIMIT_K:.0f}k limit"),
                ),
            ),
        ),
    ),
    PlotSpec(
        id="wastegate",
        title="Wastegate position and correction",
        description=(
            "Final wastegate position against the base (feedforward) position it "
            "started from (top), and the closed loop's integral and P-D correction "
            "terms (bottom), over loaded WOT samples vs engine speed."
        ),
        tip=(
            "The gap between solid (final) and dashed (base) is how hard the closed "
            "loop is having to correct the feedforward table. If the integral term is "
            "sitting on its clamp while boost is still overshooting, the loop has run "
            "out of authority and the fix belongs in the base table, not in the "
            "controller."
        ),
        panels=(
            PanelSpec(
                title="Wastegate final vs base position",
                y_label="WG position (%)",
                series=(
                    SeriesSpec("wg_pos_final", Role.PRIMARY, "Final"),
                    SeriesSpec("wg_pos_base", Role.REFERENCE, "Base"),
                ),
            ),
            PanelSpec(
                title="Wastegate closed-loop correction terms",
                y_label="Correction (%)",
                series=(
                    SeriesSpec("wg_i_value", Role.PRIMARY, "I term"),
                    SeriesSpec("wg_pd_value", Role.SECONDARY, "P-D term"),
                ),
                thresholds=(
                    ThresholdSpec(0.0, Tone.ZERO),
                    ThresholdSpec(
                        WG_I_CLAMP_WATCH_PCT, Tone.HIGH,
                        f"{WG_I_CLAMP_WATCH_PCT:.0f}% clamp watch",
                    ),
                ),
            ),
        ),
    ),
)

SPEC_BY_ID: dict[str, PlotSpec] = {spec.id: spec for spec in PLOT_SPECS}


# --------------------------------------------------------------------------- #
# Sample extraction — the half both renderers share
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Segment:
    """One unbroken run of samples: parallel x and y, already x-sorted for lines.

    Split at mask holes rather than drawn through them, so a line never bridges
    a region the mask deliberately excluded (a shift, a lift) with a straight
    segment that was never measured.
    """

    x: np.ndarray
    y: np.ndarray


@dataclass(frozen=True)
class SeriesData:
    """One spec's samples from one pull."""

    spec: SeriesSpec
    pull_index: int
    segments: tuple[Segment, ...]

    @property
    def has_data(self) -> bool:
        return any(seg.x.size for seg in self.segments)


def _mask_for(ctx: CheckContext, pull, mask: str, role: str) -> np.ndarray:
    if role == Role.TRANSIENT:
        # Loaded but not settled: exactly the samples the settled lines drop.
        return _loaded_mask(ctx, pull) & ~_settled_mask(ctx, pull)
    if mask == "loaded":
        return _loaded_mask(ctx, pull)
    if mask == "settled":
        return _settled_mask(ctx, pull)
    if mask == "none":
        return np.ones(pull.n_samples, dtype=bool)
    raise ValueError(f"unknown mask {mask!r}")


def _resolve(ctx: CheckContext, pull, source: str) -> Optional[np.ndarray]:
    fn = DERIVED.get(source)
    if fn is not None:
        return fn(ctx, pull)
    return _col(ctx, pull, source)


def contiguous_runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """Inclusive index runs where ``mask`` is True — so a line never bridges a hole."""
    runs: list[tuple[int, int]] = []
    n = mask.size
    i = 0
    while i < n:
        if not mask[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and mask[j + 1]:
            j += 1
        runs.append((i, j))
        i = j + 1
    return runs


def gear_trim_mask(ctx: CheckContext, pull) -> np.ndarray:
    """Samples whose logged gear is the pull's attributed gear.

    The DSG's gear channel flips to the next ratio several samples *before* the
    shift actually pulls the engine down, so the tail of a pull that ends in an
    upshift carries samples the ECU was already treating as the next gear. For
    anything gear-weighted those samples are simply wrong (the ~50 hp step at
    the top of every ``Calc HP`` trace), and for an overlay drawn against a
    calibration curve they are samples from a pull the curve does not describe.

    All-True when gear is unresolved or unlogged: this narrows a trace to what
    was asked for, and a gear it cannot establish is not grounds for dropping
    every sample. The gear compared against is
    :attr:`~simoscal.analysis.pulls.Pull.gear`, which the log layer has already
    resolved to an *actual* gear via the channel-header rule — so no consumer
    of this mask does gear arithmetic of its own.
    """
    n = pull.n_samples
    if not pull.gear_resolved or pull.gear is None:
        return np.ones(n, dtype=bool)
    gear = _col(ctx, pull, "gear")
    if gear is None:
        return np.ones(n, dtype=bool)
    return np.isfinite(gear) & (np.round(gear).astype(int) == int(pull.gear))


def series_segments(
    ctx: CheckContext,
    spec: SeriesSpec,
    *,
    extra_mask: Optional[Callable[[CheckContext, Any], np.ndarray]] = None,
) -> list[SeriesData]:
    """Extract one series' samples, per pull, as x-sorted contiguous segments.

    This is the function that makes the desktop PNG and the on-device canvas the
    same plot: both call it, and neither decides for itself which samples belong
    on the line. A pull missing either axis contributes nothing rather than a
    partial curve.

    Scatter roles (:data:`Role.TRANSIENT`) are returned as a single unsorted
    segment — sorting a cloud of points by x would imply an ordering the samples
    do not have.

    ``extra_mask`` narrows the selection further, per pull. It exists for the
    log overlay's gear trim (:func:`gear_trim_mask`) and is deliberately a
    parameter rather than a change to the standing masks: the evidence plots and
    every finding drawn from them are computed with the masks they have always
    used, and a new caller must not quietly restate them.
    """
    out: list[SeriesData] = []
    for pull in ctx.pulls:
        x = _resolve(ctx, pull, "rpm")
        y = _resolve(ctx, pull, spec.source)
        if x is None or y is None:
            continue
        sel = _mask_for(ctx, pull, spec.mask, spec.role) & np.isfinite(x) & np.isfinite(y)
        if extra_mask is not None:
            sel = sel & extra_mask(ctx, pull)
        if not np.any(sel):
            continue
        if spec.role == Role.TRANSIENT:
            out.append(SeriesData(spec, pull.index, (Segment(x[sel], y[sel]),)))
            continue
        segments: list[Segment] = []
        for lo, hi in contiguous_runs(sel):
            xs, ys = x[lo : hi + 1], y[lo : hi + 1]
            order = np.argsort(xs, kind="stable")
            segments.append(Segment(xs[order], ys[order]))
        if segments:
            out.append(SeriesData(spec, pull.index, tuple(segments)))
    return out


def panel_available(ctx: CheckContext, panel: PanelSpec) -> bool:
    """Whether a panel's ``requires`` channels are all present in the log set.

    Cheap and sample-free: it answers "is this panel worth attempting" before any
    array is touched. It is *not* a promise that the panel will draw — a channel
    can be present and still hold no loaded-WOT samples — so both renderers still
    check whether anything was actually produced.
    """
    return all(ctx.logset.has(channel) for channel in panel.requires)


# --------------------------------------------------------------------------- #
# JSON payload — what the bridge hands the app
# --------------------------------------------------------------------------- #
def pull_ordinals(ctx: CheckContext) -> dict[int, int]:
    """Map each pull's 1-based ``index`` to its 0-based position in ``ctx.pulls``.

    Colour is assigned by *position*, not by pull number, and both renderers must
    agree: a pull that contributes nothing to one panel would otherwise shift every
    later pull's colour on that panel alone, and "the blue curve" would stop
    meaning the same run from one plot to the next.
    """
    return {pull.index: position for position, pull in enumerate(ctx.pulls)}


def _series_payload(data: SeriesData, ordinals: dict[int, int]) -> dict:
    return {
        "source": data.spec.source,
        "role": data.spec.role,
        "label": data.spec.label,
        "pull": data.pull_index,
        # The colour slot, sent rather than re-derived so the app never has to
        # reconstruct the pull list to know which colour this line takes.
        "ordinal": ordinals.get(data.pull_index, 0),
        "segments": [
            {"x": seg.x.tolist(), "y": seg.y.tolist()}
            for seg in data.segments
            if seg.x.size
        ],
    }


#: The plot and panel the boost-screen overlay draws: gauge boost actual vs
#: setpoint, in psi against engine speed. Named here rather than in the bridge so
#: the overlay and the evidence plot cannot drift into being two definitions of
#: "the boost trace".
OVERLAY_PLOT_ID = "boost"
OVERLAY_PANEL_INDEX = 0


def overlay_payload(ctx: CheckContext) -> dict:
    """The log overlay's model: the detected pulls, each with its boost traces.

    Read-only, and the counterpart of :func:`plot_payload` for the *editing*
    surface rather than the analysis one. The editor draws one chosen pull
    behind the slot curves it is editing, so the payload is organised by pull,
    each carrying the series in the same shape :func:`plot_payload` uses.

    Three things are deliberately the engine's job here, not the app's:

    * **Which samples belong on the trace.** The same
      :func:`series_segments` the desktop PNGs use, plus
      :func:`gear_trim_mask`.
    * **What "boost" means.** The gauge reframe (PUT minus ambient, in psi)
      comes from the ``boost`` :class:`PlotSpec`, so the overlay and the
      evidence plot are the same quantity computed once.
    * **Which gear a pull was in.** Already resolved to an actual gear by the
      log layer's channel-header rule; the app formats it and does no gear
      arithmetic.

    ``available`` is false when the panel's required channels are missing (no
    ambient pressure means no honest baseline to zero gauge boost against), so
    the app can say *why* nothing drew rather than showing an empty canvas.
    """
    spec = SPEC_BY_ID[OVERLAY_PLOT_ID]
    panel = spec.panels[OVERLAY_PANEL_INDEX]
    available = panel_available(ctx, panel)
    ordinals = pull_ordinals(ctx)

    by_pull: dict[int, list[dict]] = {pull.index: [] for pull in ctx.pulls}
    if available:
        for series_spec in panel.series:
            for data in series_segments(ctx, series_spec, extra_mask=gear_trim_mask):
                if data.has_data and data.pull_index in by_pull:
                    by_pull[data.pull_index].append(_series_payload(data, ordinals))

    pulls = []
    for pull in ctx.pulls:
        series = by_pull.get(pull.index, [])
        pulls.append(
            {
                "index": pull.index,
                "file": pull.file,
                # Already an actual gear, or null when the log's gear channel
                # could not be resolved — never a guessed offset.
                "gear": pull.gear,
                "gear_resolved": bool(pull.gear_resolved),
                "rpm_min": float(pull.rpm_min),
                "rpm_max": float(pull.rpm_max),
                "duration_s": pull.duration_s,
                "n_samples": int(pull.n_samples),
                "series": series,
                "drawn": any(s["segments"] for s in series),
            }
        )

    return {
        "plot_id": spec.id,
        "title": panel.title,
        "x_label": panel.x_label,
        "y_label": panel.y_label,
        "available": available,
        "missing_channels": [
            channel for channel in panel.requires if not ctx.logset.has(channel)
        ],
        "pulls": pulls,
    }


def plot_payload(ctx: CheckContext) -> list[dict]:
    """Serialize every plot in :data:`PLOT_SPECS` against ``ctx``, JSON-safe.

    Panels and plots that produced no samples are still listed, carrying
    ``drawn: false`` and an empty series list. Omitting them would leave the app
    unable to distinguish "this quantity was fine" from "this quantity was never
    logged" — which is the same reason the battery reports SKIPPED checks
    explicitly instead of dropping them.
    """
    ordinals = pull_ordinals(ctx)
    plots: list[dict] = []
    for spec in PLOT_SPECS:
        panels: list[dict] = []
        for panel in spec.panels:
            series: list[dict] = []
            if panel_available(ctx, panel):
                for series_spec in panel.series:
                    series.extend(
                        _series_payload(data, ordinals)
                        for data in series_segments(ctx, series_spec)
                        if data.has_data
                    )
            panels.append(
                {
                    "title": panel.title,
                    "x_label": panel.x_label,
                    "y_label": panel.y_label,
                    "series": series,
                    "thresholds": [
                        {"value": t.value, "tone": t.tone, "label": t.label}
                        for t in panel.thresholds
                    ],
                    # A panel is drawn if it produced at least one line. Threshold
                    # lines alone are not data and never make a panel drawable.
                    "drawn": any(s["segments"] for s in series),
                }
            )
        plots.append(
            {
                "id": spec.id,
                "title": spec.title,
                "description": spec.description,
                "tip": spec.tip,
                "panels": panels,
                "drawn": any(p["drawn"] for p in panels),
            }
        )
    return plots
