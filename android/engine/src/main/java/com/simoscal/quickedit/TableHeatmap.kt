package com.simoscal.quickedit

/**
 * Value → colour mapping for the calibration grid.
 *
 * Deliberately outside the `ui` package and free of Compose types so it can be
 * tested on the JVM, the same split [BoostPlotScale] uses.
 *
 * A calibration table is a *surface*, and a grid of bare numerals hides its
 * shape: a ramp that sags in the middle, a cell somebody fat-fingered an order
 * of magnitude, a row that does not follow its neighbours. Colour makes those
 * legible at a glance, which is why every desktop tuning tool paints its maps.
 *
 * The scale is per-table and relative, taken from the values on screen. There is
 * no absolute palette to use instead: this grid shows boost in hPa, lambda,
 * timing in degrees, and raw axis breakpoints, and no fixed value→colour mapping
 * spans those. Relative shading answers "where is this table high and low", which
 * is the question the shape is being read for. It cannot answer "is this value
 * large in absolute terms" — read the numerals for that; they are always drawn.
 */

/** A plain sRGB triple, 0..255 per channel. */
data class HeatColor(val red: Int, val green: Int, val blue: Int) {

    /**
     * Relative luminance, 0..1, by the Rec. 709 coefficients.
     *
     * Used only to choose readable ink for a cell, which is the whole reason the
     * heatmap can be dropped behind text at all: the ramp runs from a dark blue
     * to a mid red, so neither black nor white ink is legible across all of it.
     */
    val luminance: Double
        get() = (0.2126 * red + 0.7152 * green + 0.0722 * blue) / 255.0

    /** True when this background is bright enough to need dark ink over it. */
    val prefersDarkInk: Boolean
        get() = luminance > 0.55
}

/**
 * The ramp stops, cool → warm.
 *
 * Blue-low/red-high is the convention every ECU tuning tool uses and the one a
 * person arriving from TunerPro already reads fluently, so it is kept. The stops
 * in between climb steadily in luminance to the amber and then fall into the
 * red, so the ramp still orders correctly when it is read as brightness alone —
 * which is what a red/green colour-blind viewer, or a phone in direct sun, gets.
 */
private val RAMP: List<HeatColor> = listOf(
    HeatColor(49, 84, 158),    // deep blue
    HeatColor(60, 150, 190),   // cyan
    HeatColor(120, 190, 140),  // green
    HeatColor(240, 190, 80),   // amber
    HeatColor(200, 60, 50),    // red
)

/** The ramp colour at [fraction] of the way from low to high, clamped to 0..1. */
fun rampColor(fraction: Double): HeatColor {
    if (fraction.isNaN()) return RAMP.first()
    val t = fraction.coerceIn(0.0, 1.0)

    val span = 1.0 / (RAMP.size - 1)
    val index = ((t / span).toInt()).coerceAtMost(RAMP.size - 2)
    val local = (t - index * span) / span

    val lo = RAMP[index]
    val hi = RAMP[index + 1]
    fun mix(a: Int, b: Int) = (a + (b - a) * local).toInt().coerceIn(0, 255)
    return HeatColor(mix(lo.red, hi.red), mix(lo.green, hi.green), mix(lo.blue, hi.blue))
}

/**
 * The value range one table's colours are scaled against.
 *
 * [flat] tables — every cell equal, or a single cell, or nothing finite — get no
 * colour at all rather than an arbitrary one. Painting a constant table in the
 * middle of the ramp would imply a variation that is not there, and painting it
 * at the bottom would imply it is low when it may be the highest table in the bin.
 */
class HeatScale(val min: Double, val max: Double) {

    /** Too little spread to shade honestly. */
    val flat: Boolean = !(min.isFinite() && max.isFinite()) || (max - min) <= SPAN_EPSILON

    /** Where [value] sits between [min] and [max], clamped to 0..1. */
    fun fraction(value: Double): Double {
        if (flat || !value.isFinite()) return 0.0
        return ((value - min) / (max - min)).coerceIn(0.0, 1.0)
    }

    /** The colour for [value], or null when there is nothing honest to paint. */
    fun colorFor(value: Double): HeatColor? {
        if (flat || !value.isFinite()) return null
        return rampColor(fraction(value))
    }

    companion object {
        /**
         * Spread below this counts as flat. Well under the display precision, so
         * a table that differs only in the far decimals is not given a full-range
         * rainbow that reads as real structure.
         */
        const val SPAN_EPSILON: Double = 1e-9

        /** Scale a grid against its own finite values; non-finite cells are ignored. */
        fun of(values: List<List<Double>>): HeatScale {
            val finite = values.asSequence().flatten().filter { it.isFinite() }
            val min = finite.minOrNull()
            val max = finite.maxOrNull()
            if (min == null || max == null) return HeatScale(Double.NaN, Double.NaN)
            return HeatScale(min, max)
        }
    }
}
