"""U4 — the v1 check battery.

Nine check families implemented as :class:`~simoscal.analysis.registry.Check`
registry entries, with thresholds seeded from the human-reviewed R01/R04
BasicsGuide logs, plus two calibration-aware variants (``needs_cal``). Each
compute function reads its thresholds from the check's ``thresholds`` dict — so
they are inspectable and print with the battery — and returns findings ranked
High/Medium/Low with supporting evidence.

The families: knock retard, boost tracking, wastegate duty, lambda, rail
pressure + pump headroom, timing envelope, turbo/heat, torque limiter, and data
quality (surfacing the U1 preflight as findings); the two cal-aware checks
compare the manifold-pressure setpoint against the ``C_PRS_IM_SP_MAX`` ceiling
and the logged PUT-vs-ambient differential against the P0234 overboost
threshold. A High is emitted only for a genuine problem; clean states emit a Low
informational finding (or nothing), never a false High — the acceptance gate in
U6 depends on this.

Cross-channel reasoning (hardened after the R04 battery audit, see
``Logs/BasicsGuide_R04/battery_audit.md``): boost/wastegate co-occurrence is
evaluated on the *same samples* (never per-log maxima of separate quantities),
overshoot is reported as contiguous zones (transient spike vs sustained ridge),
the timing and torque-limiter findings cross-reference each other, and knock
"clean" carries a PID-liveness caveat when the channel never left 0.00.
"""

from __future__ import annotations

import warnings
from typing import Optional

import numpy as np

from .log import LogFile
from .pulls import Pull
from .registry import Check, CheckContext, Finding, Severity

__all__ = [
    "AIRMASS_LOADED_MG",
    "KNOCK_WATCH_DEG",
    "KNOCK_HIGH_DEG",
    "BOOST_WATCH_KPA",
    "BOOST_HIGH_KPA",
    "BOOST_SUSTAINED_S",
    "BOOST_SUSTAINED_MEAN_KPA",
    "WG_SATURATION_PCT",
    "WG_I_CLAMP_WATCH_PCT",
    "LAMBDA_WATCH",
    "LAMBDA_HIGH",
    "CAL_BOOST_CEILING_SYMBOL",
    "P0234_SYMBOL",
    "default_battery",
]

# --------------------------------------------------------------------------- #
# Thresholds — seeded from the R01/R04 reviews (see Logs/*/log_review.md)
# --------------------------------------------------------------------------- #
AIRMASS_LOADED_MG = 900.0        # airmass floor for a "loaded WOT" sample

KNOCK_WATCH_DEG = -1.5           # per-cyl retard below this is a watch item
KNOCK_HIGH_DEG = -3.0            # ... below this is the R01 High knock finding

BOOST_WATCH_KPA = 10.0           # PUT-SP overshoot above this is a watch item
BOOST_HIGH_KPA = 20.0            # ... above this is a High overshoot (R01/R04)
BOOST_SUSTAINED_S = 0.5          # a zone above the watch line lasting this long is a
                                 # sustained ridge, not a transient spool spike
BOOST_SUSTAINED_MEAN_KPA = 15.0  # ... and if its mean overshoot clears this, it is a
                                 # High even without a >=20 kPa instantaneous peak
                                 # (duration-weighted severity, per the R04 audit)

LAMBDA_WATCH = 0.03              # settled-WOT lean error above this is a watch (Medium)
LAMBDA_HIGH = 0.05              # ... above this is a High lean finding
LAMBDA_OVERRUN = 1.3             # lambda at/above this is fuel-cut/overrun, not combustion
LAMBDA_SP_MAX = 0.98             # a lambda target at/above this is overrun/part-throttle, not power
SETTLED_TORQUE_MIN_NM = 250.0    # torque floor for a "settled" (non-transient) sample
SETTLED_TORQUE_GAP_NM = 120.0    # req-minus-delivered gap above which a sample is transient
TPS_WOT_MIN = 60.0               # throttle-plate floor for a loaded-WOT sample
SHIFT_LOOKBACK_S = 0.3           # window over which a settled sample's rpm must not have fallen
RPM_FALL_TOL = 150.0             # rpm a sample may sit below its lookback rpm and still be "settled"

RAIL_SAG_WATCH_BAR = -10.0       # DI rail below setpoint by more than this: watch
RAIL_SAG_HIGH_BAR = -25.0        # ... a genuine High sag (kept below the R01 envelope)
LPFP_WATCH_PCT = 85.0            # low-pressure pump duty watch (R01)
HPFP_WATCH_PCT = 95.0            # high-pressure pump effective-volume watch. Lowered from
                                 # 98 after the R04 audit: two pulls at 93.8/95.8% read as
                                 # "OK" against the old line despite little fueling headroom

WG_SATURATION_PCT = 99.0         # final wastegate command at/above this is saturated
                                 # (100% == gate CLOSED, spooling — see the R04 audit)
WG_I_CLAMP_WATCH_PCT = -20.0     # WG integral driven at/below this while boost still
                                 # overshoots == integral near its opening clamp, i.e.
                                 # the closed loop is out of authority to pull boost down
                                 # (R04 saw the clamp at -29.5%)

TURBO_SPEED_WATCH_K = 190.0      # krpm watch line (R01)
TURBO_SPEED_LIMIT_K = 220.0      # krpm revised hard limit
TURBO_AIR_TEMP_WATCH_C = 150.0   # compressor-outlet air temp watch (R01 saw 176)
IAT_WATCH_C = 50.0               # intake air temp heat-soak watch
COOLANT_HIGH_C = 115.0           # coolant overheat (High)
OIL_HIGH_C = 135.0               # oil overheat (High)


# --------------------------------------------------------------------------- #
# Per-pull sample access
# --------------------------------------------------------------------------- #
def _logfile_for(ctx: CheckContext, pull: Pull) -> Optional[LogFile]:
    for lf in ctx.logset.files:
        if lf.name == pull.file:
            return lf
    return None


def _col(ctx: CheckContext, pull: Pull, cid: str) -> Optional[np.ndarray]:
    """The pull's samples of a canonical channel, or ``None`` if absent."""
    lf = _logfile_for(ctx, pull)
    if lf is None:
        return None
    arr = lf.channel(cid)
    return arr[pull.start_row : pull.end_row + 1] if arr is not None else None


def _loaded_mask(ctx: CheckContext, pull: Pull) -> np.ndarray:
    """Boolean mask of loaded-WOT samples within the pull.

    Matches the hand-review "loaded WOT" definition: airmass at/above the load
    floor and (where logged) throttle plate open — so ramp-in/ramp-out and
    part-throttle edges of the detected pull are excluded from the load-only
    checks (lambda, rail, timing).
    """
    n = pull.n_samples
    mask = np.ones(n, dtype=bool)
    airmass = _col(ctx, pull, "airmass")
    if airmass is not None:
        mask &= np.isfinite(airmass) & (airmass >= AIRMASS_LOADED_MG)
    tps = _col(ctx, pull, "tps")
    if tps is not None:
        mask &= np.isfinite(tps) & (tps >= TPS_WOT_MIN)
    return mask


def _ascending_mask(ctx: CheckContext, pull: Pull) -> np.ndarray:
    """Samples where rpm has not fallen over the shift-lookback window.

    A settled WOT pull sweeps rpm upward; a DSG shift makes rpm drop sharply and
    fueling lean out as it recovers (the transients the human review discounted).
    Requiring rpm not to have fallen excludes those post-shift recovery frames —
    robust to SimosTools oversampling (repeated ECU frames), which defeats a
    plain consecutive-sample filter.
    """
    n = pull.n_samples
    rpm = _col(ctx, pull, "rpm")
    if rpm is None:
        return np.ones(n, dtype=bool)
    lf = _logfile_for(ctx, pull)
    dt = lf.quality.interval_median_s if lf is not None else float("nan")
    if not np.isfinite(dt) or dt <= 0:
        dt = 0.04
    lookback = max(1, int(round(SHIFT_LOOKBACK_S / dt)))
    prior = np.concatenate([np.full(min(lookback, n), rpm[0]), rpm[:-lookback]]) if n > lookback else np.full(n, rpm[0])
    return np.isfinite(rpm) & (rpm >= prior - RPM_FALL_TOL)


def _settled_mask(ctx: CheckContext, pull: Pull) -> np.ndarray:
    """Loaded samples that are also settled (not shift/torque-cut transients)."""
    loaded = _loaded_mask(ctx, pull) & _ascending_mask(ctx, pull)
    torque = _col(ctx, pull, "torque")
    torque_req = _col(ctx, pull, "torque_req")
    if torque is None or torque_req is None:
        return loaded
    settled = (
        np.isfinite(torque)
        & (torque >= SETTLED_TORQUE_MIN_NM)
        & np.isfinite(torque_req)
        & ((torque_req - torque) <= SETTLED_TORQUE_GAP_NM)
    )
    return loaded & settled


def _median3(a: np.ndarray) -> np.ndarray:
    """NaN-aware median-of-3 smoother — removes lone single-sample spikes.

    Used where a finding must reflect a *sustained* condition (settled-WOT
    lambda), not a one-sample sensor/transient glitch at a pull boundary.
    """
    if a.size < 3:
        return a
    out = a.copy()
    for i in range(a.size):
        w = a[max(0, i - 1) : min(a.size, i + 2)]
        w = w[np.isfinite(w)]
        if w.size:
            out[i] = float(np.median(w))
    return out


def _rpm_at(ctx: CheckContext, pull: Pull, local_idx: int) -> Optional[float]:
    rpm = _col(ctx, pull, "rpm")
    if rpm is None or not (0 <= local_idx < rpm.size) or not np.isfinite(rpm[local_idx]):
        return None
    return float(rpm[local_idx])


def _contiguous_true(mask: np.ndarray) -> list[tuple[int, int]]:
    """Inclusive ``(lo, hi)`` index runs where ``mask`` is True."""
    runs: list[tuple[int, int]] = []
    n = mask.size
    i = 0
    while i < n:
        if mask[i]:
            j = i
            while j + 1 < n and mask[j + 1]:
                j += 1
            runs.append((i, j))
            i = j + 1
        else:
            i += 1
    return runs


def _overshoot_zones(ctx: CheckContext, pull: Pull, watch_kpa: float) -> list[dict]:
    """Contiguous PUT-overshoot regions within one pull, each summarized.

    A zone is a run of samples where ``put - put_sp >= watch_kpa``. Reporting
    zones (rather than the single global peak) separates a sub-second spool
    spike from a sustained top-end ridge — the R04 audit's finding 3.2, that a
    0.25 s transient and a 1 s saturated ridge are different problems and must
    not be ranked by instantaneous peak alone. Each dict carries the pull index,
    rpm span, duration, mean and peak error, and a ``sustained`` flag.
    """
    put = _col(ctx, pull, "put")
    sp = _col(ctx, pull, "put_sp")
    if put is None or sp is None:
        return []
    err = put - sp
    rpm = _col(ctx, pull, "rpm")
    lf = _logfile_for(ctx, pull)
    dt = lf.quality.interval_median_s if lf is not None else float("nan")
    if not np.isfinite(dt) or dt <= 0:
        dt = 0.04
    mask = np.isfinite(err) & (err >= watch_kpa)
    zones: list[dict] = []
    for lo, hi in _contiguous_true(mask):
        seg = err[lo : hi + 1]
        seg = seg[np.isfinite(seg)]
        if not seg.size:
            continue
        peak_i = lo + int(np.nanargmax(err[lo : hi + 1]))
        duration = (hi - lo + 1) * dt
        rpm_lo = float(rpm[lo]) if rpm is not None and np.isfinite(rpm[lo]) else None
        rpm_hi = float(rpm[hi]) if rpm is not None and np.isfinite(rpm[hi]) else None
        zones.append({
            "pull": pull.index,
            "rpm_lo": rpm_lo,
            "rpm_hi": rpm_hi,
            "duration_s": duration,
            "mean_kpa": float(np.mean(seg)),
            "peak_kpa": float(np.max(seg)),
            "peak_rpm": _rpm_at(ctx, pull, peak_i),
            "n_samples": hi - lo + 1,
            "sustained": duration >= BOOST_SUSTAINED_S,
        })
    return zones


# --------------------------------------------------------------------------- #
# 1. Knock retard
# --------------------------------------------------------------------------- #
_KNOCK_CHANNELS = ("knock_1", "knock_2", "knock_3", "knock_4")


def _check_knock(ctx: CheckContext, check: Check) -> list[Finding]:
    worst = 0.0
    worst_pull = None
    worst_rpm = None
    recurrence: list[int] = []      # pulls at/below the High threshold
    saw_nonzero = False             # did any knock sample ever leave 0.00?

    for pull in ctx.pulls:
        # Per-sample most-retarded cylinder across the loaded portion.
        loaded = _loaded_mask(ctx, pull)
        stacked = []
        for cid in _KNOCK_CHANNELS:
            arr = _col(ctx, pull, cid)
            if arr is not None:
                stacked.append(np.where(loaded & np.isfinite(arr), arr, np.nan))
        if not stacked:
            continue
        stack = np.vstack(stacked)
        if not np.any(np.isfinite(stack)):
            continue
        if np.any(np.isfinite(stack) & (stack != 0.0)):
            saw_nonzero = True
        with warnings.catch_warnings():   # all-NaN columns (unloaded samples) are expected
            warnings.simplefilter("ignore", RuntimeWarning)
            per_sample_min = np.nanmin(stack, axis=0)
        if not np.any(np.isfinite(per_sample_min)):
            continue
        idx = int(np.nanargmin(per_sample_min))
        pull_min = float(per_sample_min[idx])
        if pull_min <= KNOCK_HIGH_DEG:
            recurrence.append(pull.index)
        if worst_pull is None or pull_min < worst:
            worst = pull_min
            worst_pull = pull.index
            worst_rpm = _rpm_at(ctx, pull, idx)

    if worst_pull is None:
        return []
    ev = {"worst_retard_deg": worst, "worst_pull": worst_pull, "worst_rpm": worst_rpm,
          "recurrence_pulls": recurrence, "channel_moved": saw_nonzero}
    if worst <= KNOCK_HIGH_DEG:
        loc = f" near {worst_rpm:.0f} rpm" if worst_rpm else ""
        rec = (f", recurring across {len(recurrence)} pulls ({', '.join(map(str, recurrence))})"
               if len(recurrence) > 1 else "")
        return [Finding(check.id, Severity.HIGH, check.title,
                        f"knock retard reaches {worst:.1f} deg{loc}{rec}",
                        evidence=ev, pull_refs=(worst_pull,))]
    if worst <= KNOCK_WATCH_DEG:
        return [Finding(check.id, Severity.MEDIUM, check.title,
                        f"minor knock retard to {worst:.1f} deg (below the -3.0 deg High line)",
                        evidence=ev, pull_refs=(worst_pull,))]
    # A whole-log constant 0.00 is indistinguishable from a dead PID; flag it for
    # a liveness check rather than crediting an unqualified "clean" (R04 audit 3.5,
    # on the very revision whose knock-mitigation overlay is being validated).
    if not saw_nonzero:
        return [Finding(check.id, Severity.LOW, check.title,
                        "knock retard reads a flat 0.00 deg on every cylinder across all loaded WOT "
                        "pulls — the channel never deviated at all; verify PID liveness after any "
                        "PID-list change before crediting this as clean",
                        evidence=ev)]
    return [Finding(check.id, Severity.LOW, check.title,
                    "knock retard clean: worst 0.0 deg with live movement on the cylinders through "
                    "the loaded WOT pulls",
                    evidence=ev)]


# --------------------------------------------------------------------------- #
# 2. Boost tracking
# --------------------------------------------------------------------------- #
def _zone_desc(z: dict) -> str:
    """One-line human description of an overshoot zone."""
    span = ""
    if z["rpm_lo"] is not None and z["rpm_hi"] is not None:
        span = f" at {z['rpm_lo']:.0f}-{z['rpm_hi']:.0f} rpm"
    kind = "sustained ridge" if z["sustained"] else "transient spike"
    return (f"{kind}{span}: mean +{z['mean_kpa']:.1f} / peak +{z['peak_kpa']:.1f} kPa "
            f"over {z['duration_s']:.2f} s (pull {z['pull']})")


def _check_boost(ctx: CheckContext, check: Check) -> list[Finding]:
    peak_err = None
    peak_pull = None
    peak_rpm = None
    peak_put = None
    high_pulls: list[int] = []
    zones: list[dict] = []

    for pull in ctx.pulls:
        put = _col(ctx, pull, "put")
        sp = _col(ctx, pull, "put_sp")
        if put is None or sp is None:
            continue
        err = put - sp
        finite = np.isfinite(err)
        if not np.any(finite):
            continue
        idx = int(np.nanargmax(np.where(finite, err, -np.inf)))
        e = float(err[idx])
        if e >= BOOST_HIGH_KPA:
            high_pulls.append(pull.index)
        if peak_err is None or e > peak_err:
            peak_err = e
            peak_pull = pull.index
            peak_rpm = _rpm_at(ctx, pull, idx)
            peak_put = float(put[idx])
        zones.extend(_overshoot_zones(ctx, pull, BOOST_WATCH_KPA))

    if peak_err is None:
        return []

    # Rank zones by how actionable they are: a sustained ridge outranks a
    # transient spike of equal peak (duration-weighted, per R04 audit 3.2).
    zones.sort(key=lambda z: (z["sustained"], z["duration_s"], z["peak_kpa"]), reverse=True)
    sustained_high = next(
        (z for z in zones if z["sustained"] and z["mean_kpa"] >= BOOST_SUSTAINED_MEAN_KPA),
        None,
    )
    ev = {"peak_overshoot_kpa": peak_err, "peak_put_kpa": peak_put, "peak_rpm": peak_rpm,
          "high_pulls": high_pulls, "zones": zones}
    loc = f" near {peak_rpm:.0f} rpm" if peak_rpm else ""
    primary = f"; primary zone — {_zone_desc(zones[0])}" if zones else ""

    if peak_err >= BOOST_HIGH_KPA or sustained_high is not None:
        refs = tuple(dict.fromkeys(high_pulls + ([sustained_high["pull"]] if sustained_high else [])))
        return [Finding(check.id, Severity.HIGH, check.title,
                        f"PUT overshoots setpoint by up to +{peak_err:.1f} kPa "
                        f"(peak PUT {peak_put:.1f} kPa){loc}{primary}",
                        evidence=ev, pull_refs=refs or (peak_pull,))]
    if peak_err >= BOOST_WATCH_KPA:
        return [Finding(check.id, Severity.MEDIUM, check.title,
                        f"PUT overshoots setpoint by +{peak_err:.1f} kPa{loc} "
                        f"(below the +20 kPa High line){primary}",
                        evidence=ev, pull_refs=(peak_pull,))]
    return [Finding(check.id, Severity.LOW, check.title,
                    f"boost tracks setpoint within +{peak_err:.1f} kPa",
                    evidence=ev)]


# --------------------------------------------------------------------------- #
# 3. Wastegate duty
# --------------------------------------------------------------------------- #
def _check_wastegate(ctx: CheckContext, check: Check) -> list[Finding]:
    """Wastegate authority, evaluated on the *same samples* as the overshoot.

    Rewritten after R04 audit 3.1 — the worst defect in the v1 battery. The old
    check took per-log maxima of two independent things (any-pull WG saturation,
    any-pull boost overshoot) and asserted they co-occurred; on the R04 log they
    never did (WG hit 100% only while *under* setpoint during spool). It also had
    the direction backwards: 100% == gate CLOSED, which limits boost *raising*,
    not cutting. This version reads the wastegate state on the overshoot samples
    themselves and uses the integral term ``wg_i_value`` — the real
    closed-loop-headroom signal the old check ignored.
    """
    max_final = None
    # Stats accumulated over the overshoot samples themselves (never per-log maxima
    # of separate quantities): the worst overshoot, the most-clamped integral, and
    # the most-open final command — each is the extreme across *all* overshoot
    # samples, so the reported integral reflects how hard the loop was actually
    # driven, not whichever pull happened to hold the peak spike.
    worst_over_err = None
    wg_i_at_clamp = None
    final_during_over = None
    over_pull = None                 # pull holding the worst overshoot sample
    out_of_authority_pull = None     # a pull whose overshoot integral hit the clamp
    # Gate fully closed while *under* setpoint == normal spool, informational.
    spool_closed_pull = None

    for pull in ctx.pulls:
        final = _col(ctx, pull, "wg_pos_final")
        put = _col(ctx, pull, "put")
        sp = _col(ctx, pull, "put_sp")
        wg_i = _col(ctx, pull, "wg_i_value")
        if final is not None:
            finite = final[np.isfinite(final)]
            if finite.size:
                m = float(np.max(finite))
                max_final = m if max_final is None else max(max_final, m)
        if put is None or sp is None:
            continue
        err = put - sp

        # Same-sample masks (never per-log maxima of separate quantities).
        over = np.isfinite(err) & (err >= BOOST_WATCH_KPA)
        if final is not None:
            under = np.isfinite(err) & (err < 0) & np.isfinite(final) & (final >= WG_SATURATION_PCT)
            if np.any(under) and spool_closed_pull is None:
                spool_closed_pull = pull.index

        if np.any(over):
            over_peak = float(np.nanmax(np.where(over, err, -np.inf)))
            if worst_over_err is None or over_peak > worst_over_err:
                worst_over_err = over_peak
                over_pull = pull.index
            if final is not None:
                fo = final[over & np.isfinite(final)]
                if fo.size:
                    m = float(np.min(fo))
                    final_during_over = m if final_during_over is None else min(final_during_over, m)
            if wg_i is not None:
                wi = wg_i[over & np.isfinite(wg_i)]
                if wi.size:
                    m = float(np.min(wi))
                    wg_i_at_clamp = m if wg_i_at_clamp is None else min(wg_i_at_clamp, m)
                    if m <= WG_I_CLAMP_WATCH_PCT and out_of_authority_pull is None:
                        out_of_authority_pull = pull.index

    if max_final is None:
        return []
    ev = {"max_wg_final_pct": max_final, "worst_overshoot_kpa": worst_over_err,
          "wg_i_min_during_overshoot_pct": wg_i_at_clamp,
          "wg_final_min_during_overshoot_pct": final_during_over}

    if out_of_authority_pull is not None:
        return [Finding(check.id, Severity.MEDIUM, check.title,
                        f"closed-loop out of authority: boost overshoots by up to +{worst_over_err:.1f} kPa "
                        f"while the WG integral is driven to {wg_i_at_clamp:.1f}% (near its opening clamp) "
                        f"and the final command is already down to {final_during_over:.1f}% — "
                        "the controller cannot open the gate further to pull boost down",
                        evidence=ev, pull_refs=(out_of_authority_pull,))]
    if worst_over_err is not None and worst_over_err >= BOOST_WATCH_KPA:
        gate = (f"; final command at {final_during_over:.1f}% there"
                if final_during_over is not None else "")
        integ = (f", WG integral min {wg_i_at_clamp:.1f}%" if wg_i_at_clamp is not None
                 else " (WG integral not logged — cannot assess authority)")
        return [Finding(check.id, Severity.LOW, check.title,
                        f"boost overshoots by up to +{worst_over_err:.1f} kPa but the wastegate integral "
                        f"has headroom{integ}{gate}", evidence=ev,
                        pull_refs=(over_pull,) if over_pull else ())]
    if spool_closed_pull is not None:
        return [Finding(check.id, Severity.LOW, check.title,
                        f"wastegate closes to {max_final:.1f}% only while boost is below setpoint "
                        "(normal gate-closed spool); boost tracks target once on boost",
                        evidence=ev, pull_refs=(spool_closed_pull,))]
    return [Finding(check.id, Severity.LOW, check.title,
                    f"wastegate has headroom (final command peaks at {max_final:.1f}%)",
                    evidence=ev)]


# --------------------------------------------------------------------------- #
# 4. Lambda
# --------------------------------------------------------------------------- #
def _check_lambda(ctx: CheckContext, check: Check) -> list[Finding]:
    max_lean = None       # most-positive (lean) settled error
    lean_pull = None
    lean_rpm = None
    for pull in ctx.pulls:
        lam = _col(ctx, pull, "lambda")
        sp = _col(ctx, pull, "lambda_sp")
        if lam is None or sp is None:
            continue
        settled = _settled_mask(ctx, pull)
        # Only real combustion samples count: exclude fuel-cut/overrun (lambda
        # pegged lean, or an overrun target ~1.0) — those are shift/lift
        # transients, not steady fueling.
        valid = (
            settled
            & np.isfinite(lam) & np.isfinite(sp)
            & (lam < LAMBDA_OVERRUN) & (sp < LAMBDA_SP_MAX)
        )
        idxs = np.flatnonzero(valid)
        if idxs.size == 0:
            continue
        # Median-smooth the *settled-valid subsequence* so a lone recovery
        # sample right after a fuel cut cannot register as sustained lean.
        sub = _median3((lam - sp)[idxs])
        i_local = int(np.argmax(sub))
        e = float(sub[i_local])
        if max_lean is None or e > max_lean:
            max_lean = e
            lean_pull = pull.index
            lean_rpm = _rpm_at(ctx, pull, int(idxs[i_local]))

    if max_lean is None:
        return []
    ev = {"max_settled_lean_error": max_lean, "watch_line": LAMBDA_WATCH, "pull": lean_pull,
          "rpm": lean_rpm}
    if max_lean > LAMBDA_HIGH:
        return [Finding(check.id, Severity.HIGH, check.title,
                        f"settled-WOT lambda runs lean by +{max_lean:.3f} (above the +{LAMBDA_HIGH} High line)",
                        evidence=ev, pull_refs=(lean_pull,))]
    if max_lean > LAMBDA_WATCH:
        return [Finding(check.id, Severity.MEDIUM, check.title,
                        f"settled-WOT lambda lean by +{max_lean:.3f} (above the +{LAMBDA_WATCH} watch line)",
                        evidence=ev, pull_refs=(lean_pull,))]
    return [Finding(check.id, Severity.LOW, check.title,
                    f"settled-WOT lambda tracks target (max lean +{max_lean:.3f}, below the +{LAMBDA_WATCH} watch line)",
                    evidence=ev)]


# --------------------------------------------------------------------------- #
# 5. Rail pressure + pump headroom
# --------------------------------------------------------------------------- #
def _check_rail(ctx: CheckContext, check: Check) -> list[Finding]:
    worst_sag = None
    sag_pull = None
    max_lpfp = None
    max_hpfp = None
    for pull in ctx.pulls:
        di = _col(ctx, pull, "fp_di")
        sp = _col(ctx, pull, "fp_di_sp")
        loaded = _loaded_mask(ctx, pull)
        if di is not None and sp is not None:
            err = np.where(loaded & np.isfinite(di) & np.isfinite(sp), di - sp, np.nan)
            if np.any(np.isfinite(err)):
                m = float(np.nanmin(err))
                if worst_sag is None or m < worst_sag:
                    worst_sag = m
                    sag_pull = pull.index
        for cid, ref in (("lpfp_duty", "lpfp"), ("hpfp_eff_vol", "hpfp")):
            arr = _col(ctx, pull, cid)
            if arr is not None:
                finite = arr[np.isfinite(arr)]
                if finite.size:
                    v = float(np.max(finite))
                    if ref == "lpfp":
                        max_lpfp = v if max_lpfp is None else max(max_lpfp, v)
                    else:
                        max_hpfp = v if max_hpfp is None else max(max_hpfp, v)

    if worst_sag is None and max_lpfp is None and max_hpfp is None:
        return []

    ev = {"worst_di_sag_bar": worst_sag, "max_lpfp_pct": max_lpfp, "max_hpfp_pct": max_hpfp}
    sev = Severity.LOW
    notes: list[str] = []
    if worst_sag is not None:
        if worst_sag <= RAIL_SAG_HIGH_BAR:
            sev = Severity.HIGH
        elif worst_sag <= RAIL_SAG_WATCH_BAR:
            sev = max(sev, Severity.MEDIUM, key=_sev_rank)
        notes.append(f"worst DI rail sag {worst_sag:.1f} bar")
    if max_lpfp is not None:
        if max_lpfp >= LPFP_WATCH_PCT:
            sev = max(sev, Severity.MEDIUM, key=_sev_rank)
        notes.append(f"LPFP duty peaks {max_lpfp:.1f}%")
    if max_hpfp is not None:
        if max_hpfp >= HPFP_WATCH_PCT:
            sev = max(sev, Severity.MEDIUM, key=_sev_rank)
        notes.append(f"HPFP eff. volume peaks {max_hpfp:.1f}%")

    prefix = {Severity.HIGH: "fuel supply limited: ", Severity.MEDIUM: "fuel headroom watch: ",
              Severity.LOW: "fuel supply OK: "}[sev]
    return [Finding(check.id, sev, check.title, prefix + "; ".join(notes),
                    evidence=ev, pull_refs=(sag_pull,) if sag_pull else ())]


def _sev_rank(sev: str) -> int:
    return {Severity.HIGH: 2, Severity.MEDIUM: 1, Severity.LOW: 0}.get(sev, -1)


# --------------------------------------------------------------------------- #
# 6. Timing envelope
# --------------------------------------------------------------------------- #
_TIMING_MASK_DESC = "loaded WOT: airmass >= 900 mg/stk and TPS >= 60%"


def _check_timing(ctx: CheckContext, check: Check) -> list[Finding]:
    vals: list[float] = []
    knock_active = False              # any cylinder retarded past the watch line
    torque_lim_active = False         # torque-limiter source nonzero over loaded WOT
    lim_max = None
    for pull in ctx.pulls:
        loaded = _loaded_mask(ctx, pull)
        ign = _col(ctx, pull, "ign_avg")
        if ign is not None:
            sel = ign[loaded & np.isfinite(ign)]
            if sel.size:
                vals.extend(sel.tolist())
        for cid in _KNOCK_CHANNELS:
            arr = _col(ctx, pull, cid)
            if arr is not None:
                kn = arr[loaded & np.isfinite(arr)]
                if kn.size and float(np.min(kn)) <= KNOCK_WATCH_DEG:
                    knock_active = True
        lim = _col(ctx, pull, "torque_lim")
        if lim is not None:
            ls = lim[loaded & np.isfinite(lim)]
            if ls.size and np.any(ls != 0):
                torque_lim_active = True
                lim_max = float(np.max(ls)) if lim_max is None else max(lim_max, float(np.max(ls)))
    if not vals:
        return []
    arr = np.array(vals)
    # Only point at the finding that can actually explain a local pull-back:
    # knock when knock is real; otherwise the torque limiter (R04 audit 3.3 — the
    # old text said "cross-reference the knock finding" on a log where knock was
    # a flat 0.0, which cannot explain the timing floor).
    if knock_active:
        xref = "cross-reference the knock finding for local pull-back"
    elif torque_lim_active:
        xref = (f"knock is clean, so local pull-back tracks the torque limiter "
                f"(source code {lim_max:.0f}) — see the torque-limiter finding")
    else:
        xref = "knock is clean and no torque-limiter activity — pull-back is spark-map scheduling"
    ev = {"ign_min_deg": float(np.min(arr)), "ign_max_deg": float(np.max(arr)),
          "ign_mean_deg": float(np.mean(arr)), "sample_mask": _TIMING_MASK_DESC,
          "n_samples": int(arr.size), "knock_active": knock_active,
          "torque_lim_active": torque_lim_active}
    return [Finding(check.id, Severity.LOW, check.title,
                    f"delivered timing over {_TIMING_MASK_DESC} spans {ev['ign_min_deg']:.1f} to "
                    f"{ev['ign_max_deg']:.1f} deg (mean {ev['ign_mean_deg']:.1f}, n={arr.size}); {xref}",
                    evidence=ev)]


# --------------------------------------------------------------------------- #
# 7. Turbo / heat
# --------------------------------------------------------------------------- #
def _check_turbo_heat(ctx: CheckContext, check: Check) -> list[Finding]:
    def peak(cid):
        best = None
        for pull in ctx.pulls:
            arr = _col(ctx, pull, cid)
            if arr is not None:
                finite = arr[np.isfinite(arr)]
                if finite.size:
                    v = float(np.max(finite))
                    best = v if best is None else max(best, v)
        return best

    turbo = peak("turbo_speed")
    tat = peak("turbo_air_temp")
    iat = peak("iat")
    coolant = peak("coolant_temp")
    oil = peak("oil_temp")
    # SimosTools labels turbo speed "(rpm)" but logs it in krpm (raw ~184 == 184
    # krpm), matching the 190/220 krpm watch/limit lines — do not rescale.
    turbo_k = turbo if turbo is not None else None

    ev = {"turbo_speed_krpm": turbo_k, "turbo_air_temp_c": tat, "iat_max_c": iat,
          "coolant_max_c": coolant, "oil_max_c": oil}
    sev = Severity.LOW
    notes: list[str] = []
    if turbo_k is not None:
        if turbo_k >= TURBO_SPEED_LIMIT_K:
            sev = max(sev, Severity.HIGH, key=_sev_rank)
        elif turbo_k >= TURBO_SPEED_WATCH_K:
            sev = max(sev, Severity.MEDIUM, key=_sev_rank)
        notes.append(f"turbo speed peaks {turbo_k:.0f} krpm")
    if tat is not None:
        if tat >= TURBO_AIR_TEMP_WATCH_C:
            sev = max(sev, Severity.MEDIUM, key=_sev_rank)
        notes.append(f"turbo air temp peaks {tat:.0f} C")
    if iat is not None:
        if iat >= IAT_WATCH_C:
            sev = max(sev, Severity.MEDIUM, key=_sev_rank)
        notes.append(f"IAT peaks {iat:.0f} C")
    if coolant is not None:
        if coolant >= COOLANT_HIGH_C:
            sev = max(sev, Severity.HIGH, key=_sev_rank)
        notes.append(f"coolant peaks {coolant:.0f} C")
    if oil is not None:
        if oil >= OIL_HIGH_C:
            sev = max(sev, Severity.HIGH, key=_sev_rank)
        notes.append(f"oil peaks {oil:.0f} C")
    if not notes:
        return []
    prefix = {Severity.HIGH: "heat/turbo limit: ", Severity.MEDIUM: "heat/turbo watch: ",
              Severity.LOW: "turbo and temps OK: "}[sev]
    return [Finding(check.id, sev, check.title, prefix + "; ".join(notes), evidence=ev)]


# --------------------------------------------------------------------------- #
# 8. Torque limiter
# --------------------------------------------------------------------------- #
def _check_torque(ctx: CheckContext, check: Check) -> list[Finding]:
    lim_nonzero = 0
    lim_max = None
    collapse_pull = None
    lim_active_pull = None            # pull where the limiter is nonzero over settled WOT
    ign_during_lim = None             # min delivered timing while the limiter is active
    put_err_during_lim = None         # max boost overshoot while the limiter is active
    for pull in ctx.pulls:
        settled = _settled_mask(ctx, pull)
        lim = _col(ctx, pull, "torque_lim")
        lim_on = None
        if lim is not None:
            finite = lim[np.isfinite(lim)]
            if finite.size:
                lim_max = float(np.max(finite)) if lim_max is None else max(lim_max, float(np.max(finite)))
                lim_nonzero += int(np.count_nonzero(finite))
            lim_on = settled & np.isfinite(lim) & (lim != 0)
            if np.any(lim_on):
                if lim_active_pull is None:
                    lim_active_pull = pull.index
                # Correlate the limiter window with timing retard and boost error
                # on the *same* samples (R04 audit 3.3 — the two halves of one
                # torque-intervention story used to sit in separate findings).
                ign = _col(ctx, pull, "ign_avg")
                if ign is not None:
                    isel = ign[lim_on & np.isfinite(ign)]
                    if isel.size:
                        m = float(np.min(isel))
                        ign_during_lim = m if ign_during_lim is None else min(ign_during_lim, m)
                put = _col(ctx, pull, "put")
                sp = _col(ctx, pull, "put_sp")
                if put is not None and sp is not None:
                    esel = (put - sp)[lim_on & np.isfinite(put) & np.isfinite(sp)]
                    if esel.size:
                        m = float(np.max(esel))
                        put_err_during_lim = m if put_err_during_lim is None else max(put_err_during_lim, m)
        tq = _col(ctx, pull, "torque")
        req = _col(ctx, pull, "torque_req")
        if tq is not None and req is not None:
            gap = np.where(settled & np.isfinite(tq) & np.isfinite(req), req - tq, np.nan)
            if np.any(np.isfinite(gap)) and float(np.nanmax(gap)) > 150.0 and collapse_pull is None:
                collapse_pull = pull.index

    ev = {"torque_lim_max": lim_max, "torque_lim_nonzero_samples": lim_nonzero,
          "ign_min_during_limiter_deg": ign_during_lim,
          "put_err_during_limiter_kpa": put_err_during_lim}

    def _corr() -> str:
        parts = []
        if ign_during_lim is not None:
            parts.append(f"timing pulled to {ign_during_lim:.1f} deg")
        if put_err_during_lim is not None:
            parts.append(f"boost error up to +{put_err_during_lim:.1f} kPa")
        return (" — coincides with " + ", ".join(parts)) if parts else ""

    if collapse_pull is not None:
        return [Finding(check.id, Severity.MEDIUM, check.title,
                        "delivered torque falls well short of request during settled WOT — "
                        "a limiter is likely intervening" + _corr(),
                        evidence=ev, pull_refs=(collapse_pull,))]
    if lim_max:
        refs = (lim_active_pull,) if lim_active_pull else ()
        return [Finding(check.id, Severity.LOW, check.title,
                        f"torque-limiter source is nonzero (max code {lim_max:.0f}) but torque does not "
                        "collapse during settled WOT — treat as context" + _corr(),
                        evidence=ev, pull_refs=refs)]
    return []


# --------------------------------------------------------------------------- #
# 9. Data quality (surfaces the U1 preflight)
# --------------------------------------------------------------------------- #
def _pull_row_ranges(ctx: CheckContext) -> dict[str, list[tuple[int, int]]]:
    out: dict[str, list[tuple[int, int]]] = {}
    for p in ctx.pulls:
        out.setdefault(p.file, []).append((p.start_row, p.end_row))
    return out


def _check_data_quality(ctx: CheckContext, check: Check) -> list[Finding]:
    findings: list[Finding] = []
    pull_ranges = _pull_row_ranges(ctx)

    for note in ctx.logset.notes:
        findings.append(Finding(
            check.id, Severity.LOW, check.title,
            f"duplicate-capture dedup: {note}", evidence={"note": note}))

    for lf in ctx.logset.files:
        q = lf.quality
        ranges = pull_ranges.get(lf.name, [])

        for gap in q.gaps:
            overlaps = any(lo <= gap.index <= hi for lo, hi in ranges)
            sev = Severity.MEDIUM if overlaps else Severity.LOW
            findings.append(Finding(
                check.id, sev, check.title,
                f"{lf.name}: {gap.gap_s:.2f} s time gap at t={gap.t_after:.2f}s"
                + (" (inside a WOT pull — undermines that pull's findings)" if overlaps else ""),
                evidence={"file": lf.name, "gap_s": gap.gap_s, "t_after": gap.t_after,
                          "overlaps_pull": overlaps}))

        for cid in q.stuck_channels:
            findings.append(Finding(
                check.id, Severity.MEDIUM, check.title,
                f"{lf.name}: channel '{cid}' is frozen while rpm sweeps — likely a stuck sensor",
                evidence={"file": lf.name, "channel": cid}))

        for header, cid in q.unit_unrecognized:
            findings.append(Finding(
                check.id, Severity.LOW, check.title,
                f"{lf.name}: column {header!r} has an unrecognized unit for channel '{cid}' — "
                "left unmapped, dependent checks may be skipped",
                evidence={"file": lf.name, "header": header, "channel": cid}))

        if q.n_short_rows:
            findings.append(Finding(
                check.id, Severity.LOW, check.title,
                f"{lf.name}: {q.n_short_rows} short/truncated rows",
                evidence={"file": lf.name, "n_short_rows": q.n_short_rows}))
    return findings


# --------------------------------------------------------------------------- #
# 10. Calibration-aware boost ceiling (needs_cal)
# --------------------------------------------------------------------------- #
CAL_BOOST_CEILING_SYMBOL = "C_PRS_IM_SP_MAX"
HPA_PER_KPA = 10.0               # C_PRS_IM_SP_MAX / P0234 thresholds store hPa; logs are kPa


def _check_boost_cal(ctx: CheckContext, check: Check) -> list[Finding]:
    """Compare the demanded manifold-pressure *setpoint* against the IM-SP ceiling.

    Rewritten after R04 audit 3.4, which found two bugs: (1) a 10x unit error —
    ``C_PRS_IM_SP_MAX`` stores **hPa** (stock ~240000 hPa == 24000 kPa), so the
    printed ceiling and margin were 10x off and labeled kPa; (2) it compared the
    logged *actual* manifold pressure against a *setpoint* ceiling, when the
    decision-relevant question is how close the demanded setpoint runs to its
    clamp. This version converts hPa -> kPa and compares the peak setpoint
    (``map_sp`` if logged, else ``put_sp``). Read-only; never proposes a change;
    degrades to nothing if the symbol is not in the XDF.
    """
    cal = ctx.cal
    try:
        view = cal.get(CAL_BOOST_CEILING_SYMBOL)
        ceiling = float(np.nanmax(np.asarray(view.values, dtype=float))) / HPA_PER_KPA
    except Exception:
        return []

    peak_sp = None
    sp_channel = None
    for pull in ctx.pulls:
        for cid in ("map_sp", "put_sp"):
            arr = _col(ctx, pull, cid)
            if arr is None:
                continue
            finite = arr[np.isfinite(arr)]
            if finite.size:
                v = float(np.max(finite))
                if peak_sp is None or v > peak_sp:
                    peak_sp = v
                    sp_channel = cid
            break
    if peak_sp is None:
        return []

    ev = {"symbol": CAL_BOOST_CEILING_SYMBOL, "ceiling_kpa": ceiling,
          "setpoint_peak_kpa": peak_sp, "setpoint_channel": sp_channel,
          "margin_kpa": ceiling - peak_sp}
    if peak_sp >= ceiling:
        return [Finding(check.id, Severity.MEDIUM, check.title,
                        f"demanded manifold-pressure setpoint ({sp_channel}, {peak_sp:.1f} kPa) reaches or "
                        f"exceeds the `{CAL_BOOST_CEILING_SYMBOL}` — Maximum allowed intake-manifold "
                        f"pressure setpoint ceiling ({ceiling:.1f} kPa)", evidence=ev)]
    return [Finding(check.id, Severity.LOW, check.title,
                    f"demanded manifold-pressure setpoint peak ({sp_channel}) {peak_sp:.1f} kPa stays under "
                    f"the `{CAL_BOOST_CEILING_SYMBOL}` ceiling {ceiling:.1f} kPa "
                    f"(margin {ceiling - peak_sp:.1f} kPa)", evidence=ev)]


# --------------------------------------------------------------------------- #
# 11. P0234 overboost threshold (needs_cal) — the check the R04 log crossed
# --------------------------------------------------------------------------- #
P0234_SYMBOL = "IP_PUT_AMP_DIF_MAX_PRS_DIF_THR"


def _check_p0234(ctx: CheckContext, check: Check) -> list[Finding]:
    """Logged max(PUT - ambient) vs the P0234 overboost-diagnosis threshold.

    Added per R04 audit 3.6 — the single most decision-relevant calibration
    comparison available in that log (it motivated the R06 raise). The table
    ``IP_PUT_AMP_DIF_MAX_PRS_DIF_THR`` — Overpressure upstream throttle threshold
    for turbocharger overpressure diagnosis — stores hPa; the log's PUT and
    ambient are kPa, so the differential is converted kPa -> hPa for the compare.
    Needs both the flashed table and an ambient-pressure channel; degrades to
    nothing otherwise. Read-only.
    """
    cal = ctx.cal
    try:
        view = cal.get(P0234_SYMBOL)
        thr_hpa = float(np.nanmax(np.asarray(view.values, dtype=float)))
    except Exception:
        return []

    worst_diff_kpa = None
    worst_pull = None
    worst_rpm = None
    for pull in ctx.pulls:
        put = _col(ctx, pull, "put")
        amb = _col(ctx, pull, "ambient_press")
        if put is None or amb is None:
            continue
        diff = put - amb
        finite = np.isfinite(diff)
        if not np.any(finite):
            continue
        idx = int(np.nanargmax(np.where(finite, diff, -np.inf)))
        v = float(diff[idx])
        if worst_diff_kpa is None or v > worst_diff_kpa:
            worst_diff_kpa = v
            worst_pull = pull.index
            worst_rpm = _rpm_at(ctx, pull, idx)
    if worst_diff_kpa is None:
        return []

    diff_hpa = worst_diff_kpa * HPA_PER_KPA
    loc = f" near {worst_rpm:.0f} rpm" if worst_rpm else ""
    ev = {"symbol": P0234_SYMBOL, "threshold_hpa": thr_hpa,
          "logged_put_minus_ambient_hpa": diff_hpa, "margin_hpa": thr_hpa - diff_hpa,
          "logged_put_minus_ambient_kpa": worst_diff_kpa}
    if diff_hpa >= thr_hpa:
        return [Finding(check.id, Severity.MEDIUM, check.title,
                        f"logged PUT-minus-ambient reaches {diff_hpa:.0f} hPa{loc}, at or above the "
                        f"`{P0234_SYMBOL}` — Overpressure upstream throttle (P0234) threshold "
                        f"{thr_hpa:.0f} hPa — overboost diagnosis is exposed",
                        evidence=ev, pull_refs=(worst_pull,) if worst_pull else ())]
    return [Finding(check.id, Severity.LOW, check.title,
                    f"logged PUT-minus-ambient peaks {diff_hpa:.0f} hPa, under the `{P0234_SYMBOL}` "
                    f"P0234 threshold {thr_hpa:.0f} hPa (margin {thr_hpa - diff_hpa:.0f} hPa)",
                    evidence=ev)]


# --------------------------------------------------------------------------- #
# The battery
# --------------------------------------------------------------------------- #
def default_battery() -> list[Check]:
    """The v1 check battery, in a stable registry order."""
    return [
        Check("knock", "Knock retard", _KNOCK_CHANNELS, _check_knock,
              optional_channels=("airmass", "rpm"),
              thresholds={"watch_deg": KNOCK_WATCH_DEG, "high_deg": KNOCK_HIGH_DEG},
              description="Per-cylinder knock retard magnitude, location, and recurrence."),
        Check("boost", "Boost tracking", ("put", "put_sp"), _check_boost,
              optional_channels=("rpm",),
              thresholds={"watch_kpa": BOOST_WATCH_KPA, "high_kpa": BOOST_HIGH_KPA,
                          "sustained_s": BOOST_SUSTAINED_S,
                          "sustained_mean_kpa": BOOST_SUSTAINED_MEAN_KPA},
              description="PUT vs PUT setpoint overshoot, reported as contiguous "
                          "zones (transient spike vs sustained ridge)."),
        Check("wastegate", "Wastegate duty", ("wg_pos_final", "put", "put_sp"), _check_wastegate,
              optional_channels=("wg_pos_base", "wg_i_value", "wg_pd_value"),
              thresholds={"saturation_pct": WG_SATURATION_PCT,
                          "wg_i_clamp_watch_pct": WG_I_CLAMP_WATCH_PCT},
              description="Wastegate authority on the overshoot samples: integral "
                          "driven to its opening clamp while boost still overshoots."),
        Check("lambda", "Lambda", ("lambda", "lambda_sp"), _check_lambda,
              optional_channels=("torque", "torque_req", "airmass"),
              thresholds={"watch": LAMBDA_WATCH, "high": LAMBDA_HIGH,
                          "settled_torque_min_nm": SETTLED_TORQUE_MIN_NM},
              description="Settled-WOT lambda lean error vs the +0.03 watch line; transients excluded."),
        Check("rail_pressure", "Rail pressure and pump headroom", ("fp_di", "fp_di_sp"),
              _check_rail, optional_channels=("lpfp_duty", "hpfp_eff_vol"),
              thresholds={"sag_watch_bar": RAIL_SAG_WATCH_BAR, "sag_high_bar": RAIL_SAG_HIGH_BAR,
                          "lpfp_watch_pct": LPFP_WATCH_PCT, "hpfp_watch_pct": HPFP_WATCH_PCT},
              description="DI rail sag under demand plus LPFP/HPFP headroom."),
        Check("timing", "Timing envelope", ("ign_avg",), _check_timing,
              optional_channels=("airmass", "tps") + _KNOCK_CHANNELS + ("torque_lim",),
              thresholds={},
              description="Delivered ignition envelope under loaded WOT; cross-references "
                          "knock (when live) or the torque limiter for local pull-back."),
        Check("turbo_heat", "Turbo and heat", ("iat",), _check_turbo_heat,
              optional_channels=("turbo_speed", "turbo_air_temp", "coolant_temp", "oil_temp"),
              thresholds={"turbo_watch_krpm": TURBO_SPEED_WATCH_K,
                          "turbo_limit_krpm": TURBO_SPEED_LIMIT_K,
                          "turbo_air_temp_watch_c": TURBO_AIR_TEMP_WATCH_C,
                          "iat_watch_c": IAT_WATCH_C},
              description="Turbo speed, turbo-air/intake temps, and coolant/oil sanity."),
        Check("torque_limiter", "Torque limiter", ("torque", "torque_req"), _check_torque,
              optional_channels=("torque_lim", "ign_avg", "put", "put_sp"),
              thresholds={},
              description="Torque-limiter source activity and any settled-WOT torque "
                          "collapse, correlated with timing retard and boost error."),
        Check("data_quality", "Data quality", (), _check_data_quality,
              thresholds={},
              description="Surfaces the load-quality preflight (gaps, stuck channels, "
                          "unmapped units, short rows) as findings."),
        Check("boost_cal", "Boost vs calibrated ceiling", ("put_sp",), _check_boost_cal,
              optional_channels=("map_sp",), needs_cal=True,
              thresholds={"symbol": CAL_BOOST_CEILING_SYMBOL},
              description="Peak demanded manifold-pressure setpoint vs the flashed "
                          "`C_PRS_IM_SP_MAX` ceiling (hPa->kPa; read-only)."),
        Check("boost_p0234", "P0234 overboost margin", ("put",), _check_p0234,
              optional_channels=("ambient_press", "rpm"), needs_cal=True,
              thresholds={"symbol": P0234_SYMBOL},
              description="Logged PUT-minus-ambient vs the flashed "
                          "`IP_PUT_AMP_DIF_MAX_PRS_DIF_THR` P0234 threshold (hPa; read-only)."),
    ]
