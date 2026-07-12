"""U4 — the v1 check battery.

Nine check families implemented as :class:`~simoscal.analysis.registry.Check`
registry entries, with thresholds seeded from the human-reviewed R01/R04
BasicsGuide logs, plus a calibration-aware boost variant (``needs_cal``). Each
compute function reads its thresholds from the check's ``thresholds`` dict — so
they are inspectable and print with the battery — and returns findings ranked
High/Medium/Low with supporting evidence.

The families: knock retard, boost tracking, wastegate duty, lambda, rail
pressure + pump headroom, timing envelope, turbo/heat, torque limiter, and data
quality (surfacing the U1 preflight as findings). A High is emitted only for a
genuine problem; clean states emit a Low informational finding (or nothing),
never a false High — the acceptance gate in U6 depends on this.
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
    "LAMBDA_WATCH",
    "LAMBDA_HIGH",
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
HPFP_WATCH_PCT = 98.0            # high-pressure pump effective-volume watch (R01)

WG_SATURATION_PCT = 99.0         # final wastegate command at/above this is saturated

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


# --------------------------------------------------------------------------- #
# 1. Knock retard
# --------------------------------------------------------------------------- #
_KNOCK_CHANNELS = ("knock_1", "knock_2", "knock_3", "knock_4")


def _check_knock(ctx: CheckContext, check: Check) -> list[Finding]:
    worst = 0.0
    worst_pull = None
    worst_rpm = None
    recurrence: list[int] = []      # pulls at/below the High threshold

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
          "recurrence_pulls": recurrence}
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
    return [Finding(check.id, Severity.LOW, check.title,
                    "knock retard clean: 0.0 deg on all cylinders through the loaded WOT pulls",
                    evidence=ev)]


# --------------------------------------------------------------------------- #
# 2. Boost tracking
# --------------------------------------------------------------------------- #
def _check_boost(ctx: CheckContext, check: Check) -> list[Finding]:
    peak_err = None
    peak_pull = None
    peak_rpm = None
    peak_put = None
    high_pulls: list[int] = []

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

    if peak_err is None:
        return []
    ev = {"peak_overshoot_kpa": peak_err, "peak_put_kpa": peak_put, "peak_rpm": peak_rpm,
          "high_pulls": high_pulls}
    loc = f" near {peak_rpm:.0f} rpm" if peak_rpm else ""
    if peak_err >= BOOST_HIGH_KPA:
        return [Finding(check.id, Severity.HIGH, check.title,
                        f"PUT overshoots setpoint by +{peak_err:.1f} kPa (peak PUT {peak_put:.1f} kPa){loc}",
                        evidence=ev, pull_refs=tuple(high_pulls) or (peak_pull,))]
    if peak_err >= BOOST_WATCH_KPA:
        return [Finding(check.id, Severity.MEDIUM, check.title,
                        f"PUT overshoots setpoint by +{peak_err:.1f} kPa{loc} (below the +20 kPa High line)",
                        evidence=ev, pull_refs=(peak_pull,))]
    return [Finding(check.id, Severity.LOW, check.title,
                    f"boost tracks setpoint within +{peak_err:.1f} kPa",
                    evidence=ev)]


# --------------------------------------------------------------------------- #
# 3. Wastegate duty
# --------------------------------------------------------------------------- #
def _check_wastegate(ctx: CheckContext, check: Check) -> list[Finding]:
    max_final = None
    saturated_pull = None
    boost_overshoots = False
    for pull in ctx.pulls:
        final = _col(ctx, pull, "wg_pos_final")
        if final is not None:
            finite = final[np.isfinite(final)]
            if finite.size:
                m = float(np.max(finite))
                if max_final is None or m > max_final:
                    max_final = m
                if m >= WG_SATURATION_PCT and saturated_pull is None:
                    saturated_pull = pull.index
        if (pull.max_put_error or 0.0) >= BOOST_WATCH_KPA:
            boost_overshoots = True

    if max_final is None:
        return []
    ev = {"max_wg_final_pct": max_final}
    if saturated_pull is not None and boost_overshoots:
        return [Finding(check.id, Severity.MEDIUM, check.title,
                        f"final wastegate command saturates at {max_final:.1f}% while boost still "
                        "overshoots — little closed-loop headroom to pull boost down",
                        evidence=ev, pull_refs=(saturated_pull,))]
    if saturated_pull is not None:
        return [Finding(check.id, Severity.LOW, check.title,
                        f"final wastegate command reaches {max_final:.1f}% (saturated) but boost tracks target",
                        evidence=ev, pull_refs=(saturated_pull,))]
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
def _check_timing(ctx: CheckContext, check: Check) -> list[Finding]:
    vals: list[float] = []
    for pull in ctx.pulls:
        ign = _col(ctx, pull, "ign_avg")
        loaded = _loaded_mask(ctx, pull)
        if ign is not None:
            sel = ign[loaded & np.isfinite(ign)]
            if sel.size:
                vals.extend(sel.tolist())
    if not vals:
        return []
    arr = np.array(vals)
    ev = {"ign_min_deg": float(np.min(arr)), "ign_max_deg": float(np.max(arr)),
          "ign_mean_deg": float(np.mean(arr))}
    return [Finding(check.id, Severity.LOW, check.title,
                    f"delivered timing under loaded WOT spans {ev['ign_min_deg']:.1f} to "
                    f"{ev['ign_max_deg']:.1f} deg (mean {ev['ign_mean_deg']:.1f}); "
                    "cross-reference the knock finding for local pull-back",
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
    for pull in ctx.pulls:
        lim = _col(ctx, pull, "torque_lim")
        settled = _settled_mask(ctx, pull)
        if lim is not None:
            finite = lim[np.isfinite(lim)]
            if finite.size:
                lim_max = float(np.max(finite)) if lim_max is None else max(lim_max, float(np.max(finite)))
                lim_nonzero += int(np.count_nonzero(finite))
        tq = _col(ctx, pull, "torque")
        req = _col(ctx, pull, "torque_req")
        if tq is not None and req is not None:
            gap = np.where(settled & np.isfinite(tq) & np.isfinite(req), req - tq, np.nan)
            if np.any(np.isfinite(gap)) and float(np.nanmax(gap)) > 150.0 and collapse_pull is None:
                collapse_pull = pull.index

    ev = {"torque_lim_max": lim_max, "torque_lim_nonzero_samples": lim_nonzero}
    if collapse_pull is not None:
        return [Finding(check.id, Severity.MEDIUM, check.title,
                        "delivered torque falls well short of request during settled WOT — "
                        "a limiter is likely intervening", evidence=ev, pull_refs=(collapse_pull,))]
    if lim_max:
        return [Finding(check.id, Severity.LOW, check.title,
                        f"torque-limiter source is nonzero (max code {lim_max:.0f}) but torque does not "
                        "collapse during settled WOT — treat as context", evidence=ev)]
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


def _check_boost_cal(ctx: CheckContext, check: Check) -> list[Finding]:
    """Compare logged manifold-pressure peak against the flashed IM-SP ceiling.

    Read-only: reports how close the log ran to the calibrated ceiling. Never
    proposes a change. Degrades to nothing if the symbol is not in the XDF.
    """
    cal = ctx.cal
    try:
        view = cal.get(CAL_BOOST_CEILING_SYMBOL)
        ceiling = float(np.nanmax(np.asarray(view.values, dtype=float)))
    except Exception:
        return []

    peak_map = None
    for pull in ctx.pulls:
        for cid in ("map", "put"):
            arr = _col(ctx, pull, cid)
            if arr is not None:
                finite = arr[np.isfinite(arr)]
                if finite.size:
                    v = float(np.max(finite))
                    peak_map = v if peak_map is None else max(peak_map, v)
            if peak_map is not None:
                break
    if peak_map is None:
        return []

    ev = {"symbol": CAL_BOOST_CEILING_SYMBOL, "ceiling": ceiling, "logged_peak_kpa": peak_map,
          "margin_kpa": ceiling - peak_map}
    if peak_map >= ceiling:
        return [Finding(check.id, Severity.MEDIUM, check.title,
                        f"logged manifold pressure ({peak_map:.1f} kPa) reaches or exceeds the "
                        f"`{CAL_BOOST_CEILING_SYMBOL}` — Maximum requested intake-manifold pressure "
                        f"setpoint ceiling ({ceiling:.1f} kPa)", evidence=ev)]
    return [Finding(check.id, Severity.LOW, check.title,
                    f"logged manifold pressure peak {peak_map:.1f} kPa stays under the "
                    f"`{CAL_BOOST_CEILING_SYMBOL}` ceiling {ceiling:.1f} kPa "
                    f"(margin {ceiling - peak_map:.1f} kPa)", evidence=ev)]


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
              thresholds={"watch_kpa": BOOST_WATCH_KPA, "high_kpa": BOOST_HIGH_KPA},
              description="PUT vs PUT setpoint overshoot bands and peaks."),
        Check("wastegate", "Wastegate duty", ("wg_pos_final",), _check_wastegate,
              optional_channels=("wg_pos_base", "wg_i_value", "wg_pd_value"),
              thresholds={"saturation_pct": WG_SATURATION_PCT},
              description="Final wastegate command saturation vs remaining boost overshoot."),
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
              optional_channels=("airmass",),
              thresholds={},
              description="Delivered ignition envelope under loaded WOT (context for knock)."),
        Check("turbo_heat", "Turbo and heat", ("iat",), _check_turbo_heat,
              optional_channels=("turbo_speed", "turbo_air_temp", "coolant_temp", "oil_temp"),
              thresholds={"turbo_watch_krpm": TURBO_SPEED_WATCH_K,
                          "turbo_limit_krpm": TURBO_SPEED_LIMIT_K,
                          "turbo_air_temp_watch_c": TURBO_AIR_TEMP_WATCH_C,
                          "iat_watch_c": IAT_WATCH_C},
              description="Turbo speed, turbo-air/intake temps, and coolant/oil sanity."),
        Check("torque_limiter", "Torque limiter", ("torque", "torque_req"), _check_torque,
              optional_channels=("torque_lim",),
              thresholds={},
              description="Torque-limiter source activity and any settled-WOT torque collapse."),
        Check("data_quality", "Data quality", (), _check_data_quality,
              thresholds={},
              description="Surfaces the load-quality preflight (gaps, stuck channels, "
                          "unmapped units, short rows) as findings."),
        Check("boost_cal", "Boost vs calibrated ceiling", ("put_sp",), _check_boost_cal,
              optional_channels=("map", "put"), needs_cal=True,
              thresholds={"symbol": CAL_BOOST_CEILING_SYMBOL},
              description="Logged manifold-pressure peak vs the flashed "
                          "`C_PRS_IM_SP_MAX` intake-manifold pressure ceiling (read-only)."),
    ]
