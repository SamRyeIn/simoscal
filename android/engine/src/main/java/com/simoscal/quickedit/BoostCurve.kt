package com.simoscal.quickedit

import kotlin.math.abs
import kotlin.math.floor
import kotlin.math.max
import kotlin.math.min
import kotlin.math.roundToInt
import org.json.JSONObject

/**
 * The boost editor's read model and every rule the fingertip obeys — pure,
 * Android-free, JVM-testable.
 *
 * The five switch-patch slots each cap boost with a per-rpm grid, and what the
 * ECU actually targets is `min(base ceiling, slot)`. Two *different* limits fall
 * out of that, and conflating them is the mistake this file exists to prevent:
 *
 * * [BoostCurveModel.baseCeilingPsi] — the base `IP_PUT_SP` — Pressure up
 *   throttle setpoint full-load row interpolated onto the slot rpm axis. A slot
 *   point above this is *legal* but *ineffective*: the base caps it at that rpm.
 *   This is what the "capped by base" band shades.
 * * [BoostCurveModel.refusalCeilingPsi] — the **scalar maximum** of that same
 *   base row. The engine's guard (`switchpatch._check_below_base_ceiling`)
 *   refuses the whole edit if *any* point reaches this, because a slot that can
 *   never out-rank the base has stopped meaning anything. This is a hard stop.
 *
 * The refusal ceiling is scalar and the shading ceiling is per-rpm, so between
 * them lies a real band of values the engine accepts and the base still swallows.
 * The editor must draw that band rather than pretend the two limits are one, or
 * a person would either be blocked from values the engine allows or allowed to
 * ask for values it will reject.
 */

/** The switch-patch map slots, in the order the patch numbers them. */
val SLOT_IDS: List<Int> = listOf(1, 2, 3, 4, 5)

/**
 * The psi granularity the editor works in.
 *
 * Not a storage quantum — the ECU stores hPa — but the step a drag snaps to and
 * the resolution [BoostCurveModel.maxSettablePsi] backs off by, so a dragged
 * value is a number a person could also have typed.
 */
const val PSI_STEP: Double = 0.01

/** One slot's cap as per-rpm psi gauge. The stored grid is row-tiled; this is row 0. */
data class SlotCurve(val slot: Int, val psi: List<Double>) {

    /** Whether every breakpoint holds the same cap — a flat cap rather than a curve. */
    val isFlat: Boolean
        get() = psi.all { abs(it - psi.first()) < 1e-6 }
}

/**
 * Everything the per-slot editor draws, in psi gauge on the shared rpm axis.
 *
 * Mirrors `simoscal.tune.boostcurve.BoostCurveModel` field for field; the
 * derived limits below are computed here rather than sent so that the rule the
 * fingertip enforces and the rule the tests pin are the same code.
 */
data class BoostCurveModel(
    val rpmAxis: List<Double>,
    val slots: List<SlotCurve>,
    /** Base ceiling interpolated onto [rpmAxis] — the per-rpm shading limit. */
    val baseCeilingPsi: List<Double>,
    /** The base table's own, coarser rpm axis. */
    val baseRpmAxis: List<Double>,
    /** Base ceiling on its own axis. Its maximum is the engine's refusal ceiling. */
    val baseCeilingOwnPsi: List<Double>,
) {

    /**
     * The scalar cap the engine refuses at, in psi gauge.
     *
     * Deliberately `max` of the base row and not the local per-rpm value: that is
     * exactly what `_check_below_base_ceiling` compares against, and a UI limit
     * that disagreed with the engine's would either block accepted edits or
     * forward rejected ones.
     */
    val refusalCeilingPsi: Double
        get() = baseCeilingOwnPsi.maxOrNull() ?: 0.0

    /**
     * The highest value the editor will ever set: one [PSI_STEP] below refusal.
     *
     * The engine's test is `>=`, so landing *on* the ceiling is a rejection. psi
     * is floored on its way to hPa, so any value strictly below the ceiling in
     * psi is also strictly below it in stored hPa — backing off one step is
     * therefore sufficient, not merely cautious.
     */
    val maxSettablePsi: Double
        get() {
            // The `+ 1e-6` absorbs binary-representation error in `x / 0.01`,
            // which otherwise floors a clean 21.00 down to 20.99 before the
            // back-off even runs and loses a whole step of usable range.
            val stepped = floor(refusalCeilingPsi / PSI_STEP + 1e-6) * PSI_STEP
            return if (stepped >= refusalCeilingPsi - 1e-9) stepped - PSI_STEP else stepped
        }

    fun curve(slot: Int): SlotCurve? = slots.firstOrNull { it.slot == slot }

    /** The `min(base, slot)` the ECU would actually target for [slot]. */
    fun effectivePsi(slot: Int): List<Double> {
        val curve = curve(slot) ?: return emptyList()
        return curve.psi.mapIndexed { index, value ->
            min(value, baseCeilingPsi.getOrElse(index) { value })
        }
    }

    /**
     * Indices of [slot] where the base ceiling is what actually limits boost.
     *
     * These are not errors — they are the honest answer to "why did asking for
     * more here change nothing", and the screen names them rather than leaving a
     * person to infer it from two lines crossing.
     */
    fun cappedByBase(slot: Int): List<Int> {
        val curve = curve(slot) ?: return emptyList()
        return curve.psi.indices.filter { index ->
            curve.psi[index] > baseCeilingPsi.getOrElse(index) { Double.MAX_VALUE } + 1e-9
        }
    }

    companion object {

        /** Parse the `boost_curve` payload of the V6 bridge's `boost_curve` op. */
        fun fromJson(payload: JSONObject): BoostCurveModel {
            val slotsArray = payload.optJSONArray("slots")
            val slots = (0 until (slotsArray?.length() ?: 0)).mapNotNull { index ->
                slotsArray?.optJSONObject(index)?.let { entry ->
                    SlotCurve(slot = entry.optInt("slot"), psi = entry.doubleList("psi"))
                }
            }
            return BoostCurveModel(
                rpmAxis = payload.doubleList("rpm_axis"),
                slots = slots,
                baseCeilingPsi = payload.doubleList("base_ceiling_psi"),
                baseRpmAxis = payload.doubleList("base_rpm_axis"),
                baseCeilingOwnPsi = payload.doubleList("base_ceiling_own_psi"),
            )
        }
    }
}

// --------------------------------------------------------------------- editing

/**
 * Why a typed cap cannot be used, or null if it can.
 *
 * Typed entry is *validated*, never clamped. A person who types 22.0 psi stated
 * a number; silently storing 21.99 would be the library's cardinal sin — quietly
 * altering a value — committed one layer up. Dragging is different (see
 * [clampDraggedPsi]): a fingertip never stated an exact number, so snapping it
 * to the legal range alters nothing anyone asked for.
 */
fun BoostCurveModel.rejectTypedPsi(psi: Double): String? = when {
    psi.isNaN() || psi.isInfinite() -> "Enter a number in psi gauge."
    psi < 0.0 -> "A boost cap cannot be negative."
    psi >= refusalCeilingPsi ->
        "${psi.display("%.2f")} psi reaches the base ceiling of " +
            "${refusalCeilingPsi.display("%.2f")} psi. Above it the base " +
            "`IP_PUT_SP` — Pressure up throttle setpoint table caps the slot " +
            "instead, so the engine refuses the edit."
    else -> null
}

/**
 * Snap a dragged value into the legal range: `[0, maxSettablePsi]`, on the step.
 *
 * The zero floor is a usability choice, not a safety rule — the engine has no
 * lower guard — but a cap dragged into vacuum is never what someone meant, and
 * the numeric field remains available for anyone who genuinely wants one.
 */
fun BoostCurveModel.clampDraggedPsi(psi: Double): Double {
    if (psi.isNaN()) return 0.0
    val snapped = (psi / PSI_STEP).roundToInt() * PSI_STEP
    return max(0.0, min(maxSettablePsi, snapped))
}

/** Replace one breakpoint of [current], clamped as a drag. */
fun BoostCurveModel.withDraggedPoint(current: List<Double>, index: Int, psi: Double): List<Double> {
    if (index !in current.indices) return current
    return current.toMutableList().also { it[index] = clampDraggedPsi(psi) }
}

/** A flat cap across every breakpoint, clamped as a drag. */
fun BoostCurveModel.flatCap(psi: Double): List<Double> =
    List(rpmAxis.size) { clampDraggedPsi(psi) }

/**
 * Three-point moving average, endpoints held.
 *
 * Every output is an average of inputs already inside the legal range, so
 * smoothing cannot push a curve into the ceiling — but it is clamped anyway,
 * because "cannot" resting on an arithmetic argument is worth one cheap call.
 */
fun BoostCurveModel.smooth(current: List<Double>): List<Double> {
    if (current.size < 3) return current
    return current.mapIndexed { index, value ->
        val smoothed = when (index) {
            0, current.lastIndex -> value
            else -> (current[index - 1] + value + current[index + 1]) / 3.0
        }
        clampDraggedPsi(smoothed)
    }
}

/**
 * Copy one slot's curve onto another.
 *
 * Returns null when the source slot is missing, rather than an empty curve: a
 * copy that silently produced a zero cap would be a calibration change nobody
 * asked for.
 */
fun BoostCurveModel.copySlot(from: Int): List<Double>? =
    curve(from)?.psi?.map { clampDraggedPsi(it) }
