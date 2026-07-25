package com.simoscal.quickedit

import kotlin.math.abs
import kotlin.math.max

/**
 * The boost canvas's coordinate math, in plain floats.
 *
 * Deliberately outside the `ui` package and free of Compose types so it can be
 * tested on the JVM. This is where a fingertip becomes a boost number: [psiAt]
 * is the inverse of [y], and if the two ever disagreed a drag would set a value
 * other than the one the curve was drawn at — a silent mismatch between what a
 * person sees and what gets written to a bin.
 */

/** Where the plot sits inside the canvas, in pixels. */
class BoostPlotGeometry(canvasWidth: Float, canvasHeight: Float) {

    /** Room for psi labels left, rpm labels below, slot labels right. */
    val left: Float = 56f
    val top: Float = 14f
    val width: Float = max(1f, canvasWidth - left - 40f)
    val height: Float = max(1f, canvasHeight - top - 34f)
    val right: Float get() = left + width
    val bottom: Float get() = top + height
}

/**
 * Value ↔ pixel mapping for the plot.
 *
 * The rpm axis maps by *value*, not by index: the slot breakpoints are not evenly
 * spaced, and drawing them as if they were would misplace every curve against the
 * base ceiling — the one comparison this screen exists to make.
 */
class BoostPlotScale(
    val rpmMin: Double,
    val rpmMax: Double,
    val psiMax: Double,
    val rpmAxis: List<Double>,
) {

    fun x(geometry: BoostPlotGeometry, rpm: Double): Float {
        val span = (rpmMax - rpmMin).takeIf { it > 1e-9 } ?: 1.0
        return geometry.left + ((rpm - rpmMin) / span).toFloat() * geometry.width
    }

    fun y(geometry: BoostPlotGeometry, psi: Double): Float {
        val span = psiMax.takeIf { it > 1e-9 } ?: 1.0
        return geometry.bottom - (psi / span).toFloat() * geometry.height
    }

    /** Invert [y] — the psi a fingertip at this pixel is asking for. */
    fun psiAt(geometry: BoostPlotGeometry, pixelY: Float): Double {
        val fraction = (geometry.bottom - pixelY) / geometry.height
        return fraction.toDouble() * psiMax
    }

    /** The breakpoint nearest a horizontal position — what a drag or tap grabbed. */
    fun nearestIndex(geometry: BoostPlotGeometry, pixelX: Float): Int {
        if (rpmAxis.isEmpty()) return 0
        return rpmAxis.indices.minByOrNull { abs(x(geometry, rpmAxis[it]) - pixelX) } ?: 0
    }

    companion object {
        fun of(model: BoostCurveModel, draft: List<Double>): BoostPlotScale {
            val highest = maxOf(
                model.refusalCeilingPsi,
                draft.maxOrNull() ?: 0.0,
                model.slots.flatMap { it.psi }.maxOrNull() ?: 0.0,
            )
            return BoostPlotScale(
                rpmMin = model.rpmAxis.minOrNull() ?: 0.0,
                rpmMax = model.rpmAxis.maxOrNull() ?: 1.0,
                // Headroom above the highest line so the refusal dash and the top
                // of a curve are never drawn on the frame itself.
                psiMax = if (highest > 0) highest * 1.12 else 1.0,
                rpmAxis = model.rpmAxis,
            )
        }
    }
}
