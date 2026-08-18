package com.simoscal.quickedit

import java.util.Locale
import kotlin.math.abs

/**
 * Number formatting for the editor. Fixed-point, US-locale, never scientific.
 *
 * Two rules, both load-bearing rather than cosmetic.
 *
 * **Always a `.` decimal separator.** `String.format` uses the *default* locale,
 * so on a comma-decimal phone a value shown as "10,50" is fed straight back into
 * a text field whose only parser is `toDoubleOrNull` — which rejects it, leaving
 * the Set button permanently disabled. Every number that can round-trip through
 * the UI, and every number quoted in a refusal message next to one, goes through
 * here.
 *
 * **Never scientific notation.** `%g` switches to an exponent below 1e-5, which
 * is how a y-axis breakpoint of exactly 0 °C came to be displayed as
 * `4.40536e-13`: the XDF's scaling equation is evaluated in floating point, and
 * the residue is the arithmetic's, not the calibration's. An exponent on a
 * calibration screen is therefore never information — it is either noise dressed
 * up as a number, or a real value made unreadable. Fixed point shows the noise as
 * the zero it is.
 */

/**
 * How far apart two values must be before the editor calls them different.
 *
 * Defined once because three things have to agree on it: whether a cell counts
 * as changed, whether a draft counts as dirty, and how much precision
 * [displayExact] must print to be sure a value read back off the screen is the
 * same value. Were the last of those looser than the first two, seeding an edit
 * field would manufacture a change out of a rounding step.
 */
internal const val CHANGE_EPSILON = 1e-12

/** Most decimals a grid formatter will print. Past this, doubles are noise. */
private const val MAX_DECIMALS = 9

/** Most decimals [displayExact] will print before giving up on round-tripping. */
private const val MAX_EXACT_DECIMALS = 17

/**
 * Format with an explicit `printf` pattern, US locale.
 *
 * No default pattern on purpose: the old default was `%.6g`, and every caller
 * that took it inherited both false precision and the exponent problem above.
 * A caller that wants "whatever suits this number" wants [displayExact]; one
 * formatting a whole grid wants [ValueFormat].
 */
internal fun Double.display(pattern: String): String =
    String.format(Locale.US, pattern, this)

/**
 * The shortest fixed-point text that reads back as this value, zeros trimmed.
 *
 * This is what a single number under scrutiny gets — the value seeded into an
 * edit dialog above all. The grid rounds for readability, but the dialog must
 * not: seeding it with a rounded value would mean that opening a cell and
 * pressing Set, changing nothing, silently wrote a *different* number than the
 * one that was there. The grid answers "what shape is this table"; this answers
 * "what exactly is in this cell".
 *
 * "Reads back as this value" is measured with [CHANGE_EPSILON], not with exact
 * double equality, and the difference is the whole point: a breakpoint holding
 * 4.4e-13 needs no decimals at all to satisfy it, so the residue prints as `0`
 * rather than as nineteen digits of arithmetic noise, while a genuine
 * 1/4096-quantized cell gets every digit it needs.
 */
internal fun Double.displayExact(): String {
    if (!isFinite()) return nonFinite()
    for (decimals in 0..MAX_EXACT_DECIMALS) {
        val text = display("%.${decimals}f")
        if (abs(text.toDouble() - this) <= CHANGE_EPSILON) return text.tidied()
    }
    return display("%.${MAX_EXACT_DECIMALS}f").tidied()
}

private fun String.tidied(): String =
    (if (contains('.')) trimEnd('0').trimEnd('.') else this).zeroNormalised()

/**
 * Flip the sign of a number *as typed text*, for the ± control.
 *
 * Text, not a parsed Double, and deliberately so. The control has to work while
 * the field holds something half-written — "", "-", "3." — which is most of the
 * time someone is typing into it, and a parse-flip-reformat round trip would
 * either refuse those or rewrite what the person had already entered.
 *
 * It exists because `KeyboardType.Decimal` gets a decimal point out of the IME
 * but never a sign key: Android has no signed-decimal input type, and Compose
 * exposes none. Ignition timing, a downward Offset, and a sub-zero temperature
 * breakpoint are all ordinary values here and none of them could be typed.
 * Flipping in the app depends on no IME behaviour at all.
 *
 * Leading whitespace is preserved rather than trimmed — the field is the
 * person's text, and this control is only meant to move the sign.
 */
internal fun String.withFlippedSign(): String {
    val lead = takeWhile { it.isWhitespace() }
    val body = drop(lead.length)
    return lead + if (body.startsWith("-")) body.drop(1) else "-$body"
}

/**
 * A signed value, for deltas, at the precision of the grid it describes.
 *
 * Sign always shown: "+12" and "-12" are different calibration decisions and a
 * summary line that drops the mark is worse than one that never had it.
 */
internal fun ValueFormat.formatSigned(value: Double): String {
    val body = format(abs(value))
    return if (value < 0 && body.any { it in '1'..'9' }) "-$body" else "+$body"
}

/**
 * One display precision, chosen for a whole set of numbers and applied to all.
 *
 * Precision is a property of the *set*, not of each number: a grid whose cells
 * are individually formatted has a ragged right edge and invites reading two
 * differently-rounded cells as differing by more than they do. So a table's
 * cells share one format, and each axis gets its own — they are different
 * quantities and there is no reason for an rpm ladder to carry a lambda grid's
 * decimals.
 *
 * The precision is picked to show roughly four significant digits of the largest
 * value present, which is what puts a 1/4096-quantized `3.100098` on screen as
 * `3.100` and a `79.9891` breakpoint back as the `80` the calibrator meant.
 *
 * It is then *raised* until every meaningfully different value in the set formats
 * differently. That guard is the reason rounding here is safe: the editor shows a
 * changed cell above its old value, and two genuinely different numbers that
 * render identically would turn a real edit into an invisible one. Differences
 * below one part in a billion are excluded from that test — those are the
 * float-arithmetic residue described above, not edits.
 */
class ValueFormat private constructor(val decimals: Int) {

    fun format(value: Double): String {
        if (!value.isFinite()) return value.nonFinite()
        return value.display("%.${decimals}f").zeroNormalised()
    }

    companion object {
        /** Fallback when there is nothing to measure — a bare integer count. */
        val PLAIN = ValueFormat(0)

        fun of(vararg groups: Iterable<Double>): ValueFormat {
            val values = groups.asSequence().flatten().filter { it.isFinite() }.toList()
            if (values.isEmpty()) return PLAIN

            val magnitude = values.maxOf { abs(it) }
            var decimals = baseDecimals(magnitude)
            // Relative, not absolute: "different" has to mean different at the
            // scale of the table, or a 6000-rpm axis would chase residue in its
            // twelfth digit forever.
            val noiseFloor = magnitude * 1e-9
            val sorted = values.sorted()
            while (decimals < MAX_DECIMALS && collides(sorted, decimals, noiseFloor)) {
                decimals++
            }
            return ValueFormat(decimals)
        }

        /** Enough decimals for ~4 significant digits of the largest value. */
        private fun baseDecimals(magnitude: Double): Int = when {
            magnitude >= 1000.0 -> 0
            magnitude >= 100.0 -> 1
            magnitude >= 10.0 -> 2
            magnitude >= 1.0 -> 3
            magnitude >= 0.1 -> 4
            magnitude >= 0.01 -> 5
            magnitude > 0.0 -> 6
            else -> 0
        }

        /** Do two values that really differ come out as the same text? */
        private fun collides(sorted: List<Double>, decimals: Int, noiseFloor: Double): Boolean {
            val format = ValueFormat(decimals)
            for (i in 1 until sorted.size) {
                val previous = sorted[i - 1]
                val current = sorted[i]
                if (current - previous <= noiseFloor) continue
                if (format.format(previous) == format.format(current)) return true
            }
            return false
        }
    }
}

/**
 * `-0.00` is a rounding artefact, never a calibration value — a cell holding a
 * hair under zero is zero at this precision, and the minus sign only invites the
 * reader to look for a negative number that is not there.
 */
private fun String.zeroNormalised(): String =
    if (startsWith("-") && none { it in '1'..'9' }) drop(1) else this

private fun Double.nonFinite(): String = when {
    isNaN() -> "not a number"
    this > 0 -> "infinite"
    else -> "-infinite"
}
