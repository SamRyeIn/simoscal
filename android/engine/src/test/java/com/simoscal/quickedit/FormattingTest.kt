package com.simoscal.quickedit

import java.util.Locale
import kotlin.math.abs
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * How numbers reach the screen.
 *
 * Two properties are load-bearing and everything else here is in service of
 * them: nothing is ever shown in scientific notation, and no two meaningfully
 * different values are ever shown as the same text. The first is what turns a
 * float-arithmetic residue back into the zero it is; the second is what makes
 * the rounding that achieves it safe.
 */
class FormattingTest {

    /** Every table axis and cell set in the curated catalog, plus the edges. */
    private val realWorldSets = listOf(
        // IP_PQ_CHA_MAX y axis — the residue that started this: an exactly-zero
        // breakpoint decoded through the XDF's scaling equation.
        listOf(-20.24999999999975, -9.749999999999652, 4.405364961712621e-13,
               9.750000000000533, 20.25000000000064, 50.250000000000924),
        // IP_PQ_CHA_MAX cells — 1/4096-quantized.
        listOf(3.100097656250, 1.699951171875, 1.75),
        // Ignition airmass axis — 1/81.92-quantized, meant as round numbers.
        listOf(79.9891, 99.997, 150.0167, 1400.0001),
        // Wastegate flow factors.
        listOf(0.0, 0.2502, 0.35, 0.8505, 1.25),
        // Boost setpoints, hPa.
        listOf(590.0411, 700.0727, 2500.0463),
    )

    @Test
    fun `no value is ever formatted in scientific notation`() {
        realWorldSets.forEach { values ->
            val format = ValueFormat.of(values)
            values.forEach { value ->
                assertFalse(
                    "scientific notation for $value",
                    format.format(value).contains('e', ignoreCase = true),
                )
                assertFalse(
                    "scientific notation for $value",
                    value.displayExact().contains('e', ignoreCase = true),
                )
            }
        }
    }

    @Test
    fun `a float-arithmetic residue is shown as the zero it is`() {
        // 4.4e-13 °C on an axis whose breakpoints step by 0.75 is not a
        // temperature — it is what evaluating the scaling equation in floating
        // point leaves behind.
        val axis = realWorldSets[0]
        assertEquals("0.00", ValueFormat.of(axis).format(4.405364961712621e-13))
    }

    @Test
    fun `precision follows the size of the values, not the width of a double`() {
        assertEquals("3.100", ValueFormat.of(realWorldSets[1]).format(3.100097656250))
        assertEquals("-20.25", ValueFormat.of(realWorldSets[0]).format(-20.24999999999975))
        // A quantized breakpoint shown as the round number the calibrator meant.
        assertEquals("80", ValueFormat.of(realWorldSets[2]).format(79.9891))
        assertEquals("0.250", ValueFormat.of(realWorldSets[3]).format(0.2502))
    }

    @Test
    fun `two values that really differ never render as the same text`() {
        // This is what makes the rounding safe: a changed cell is drawn above its
        // old value, so a proposal that renders identically to what it replaces
        // would be an edit the reviewer cannot see.
        val values = listOf(2400.0, 2400.5, 2401.0)
        val format = ValueFormat.of(values)
        assertEquals(values.size, values.map(format::format).distinct().size)
    }

    @Test
    fun `an edit finer than the default precision widens the whole grid`() {
        val committed = listOf(1.0, 2.0, 3.0)
        val draft = listOf(1.0005, 2.0, 3.0)
        val format = ValueFormat.of(draft, committed)
        assertTrue(format.format(1.0005) != format.format(1.0))
    }

    @Test
    fun `residue below one part in a billion does not chase precision`() {
        // The counterpart to the test above: 0 and 4.4e-13 are different doubles
        // but not a different calibration, and escalating for them would put a
        // whole grid at nine decimals to render noise.
        assertEquals(2, ValueFormat.of(listOf(0.0, 4.4e-13, 9.75, 50.25)).decimals)
    }

    @Test
    fun `a value rounding to zero never keeps a minus sign`() {
        assertEquals("0.00", ValueFormat.of(listOf(-1e-14, 50.0)).format(-1e-14))
        assertEquals("0", (-1e-14).displayExact())
    }

    @Test
    fun `displayExact keeps what the cell holds, so an untouched edit is a no-op`() {
        // The grid rounds; the edit dialog must not. Seeding the field with
        // "3.100" would mean opening a cell and pressing Set wrote a different
        // number than was there — so what comes back must land inside the same
        // threshold the editor calls "unchanged".
        listOf(3.10009765625, 79.98907470703125, 2500.046295166, 4.405364961712621e-13)
            .forEach { value ->
                val text = value.displayExact()
                val readBack = text.toDouble()
                assertTrue("$value round-tripped as $text", abs(readBack - value) <= CHANGE_EPSILON)
            }
        assertEquals("3.10009765625", 3.10009765625.displayExact())
        assertEquals("2400", 2400.0.displayExact())
        // A residue smaller than the threshold needs no digits to satisfy it.
        assertEquals("0", 4.405364961712621e-13.displayExact())
    }

    @Test
    fun `a delta always carries its sign`() {
        val format = ValueFormat.of(listOf(-0.25, 0.5))
        assertEquals("-0.2500", format.formatSigned(-0.25))
        assertEquals("+0.5000", format.formatSigned(0.5))
        assertEquals("+0.0000", format.formatSigned(0.0))
    }

    @Test
    fun `the sign toggle flips a typed number both ways`() {
        assertEquals("-3.25", "3.25".withFlippedSign())
        assertEquals("3.25", "-3.25".withFlippedSign())
        assertEquals("-0.05", "0.05".withFlippedSign())
        // Round trip: two presses put back exactly what was there.
        listOf("3.25", "-3.25", "0", "1400").forEach { typed ->
            assertEquals(typed, typed.withFlippedSign().withFlippedSign())
        }
    }

    @Test
    fun `the sign toggle works on text that is not a number yet`() {
        // The control has to be usable mid-type, which is most of the time it
        // will be reached for: press ± first, then key the digits.
        assertEquals("-", "".withFlippedSign())
        assertEquals("", "-".withFlippedSign())
        assertEquals("-3.", "3.".withFlippedSign())
        // Leading whitespace is the person's text, not ours to tidy.
        assertEquals("  -12", "  12".withFlippedSign())
        assertEquals("  12", "  -12".withFlippedSign())
    }

    @Test
    fun `a flipped value parses back as its own negation`() {
        // The property that matters downstream: the dialog only enables Set when
        // `toDoubleOrNull` succeeds, so a flip that produced unparseable text
        // would silently disable the button instead of entering a negative.
        listOf("3.25", "0.05", "1400", "0").forEach { typed ->
            val flipped = typed.withFlippedSign().toDoubleOrNull()
            assertEquals(-typed.toDouble(), flipped!!, 0.0)
        }
    }

    @Test
    fun `formatting uses a dot whatever the device locale is`() {
        // Same reason as the boost editor's: these strings are read back by
        // `toDoubleOrNull`, which only accepts `.`.
        val previous = Locale.getDefault()
        try {
            Locale.setDefault(Locale.GERMANY)
            assertEquals("3.100", ValueFormat.of(realWorldSets[1]).format(3.10009765625))
            assertEquals("3.10009765625", 3.10009765625.displayExact())
        } finally {
            Locale.setDefault(previous)
        }
    }

    @Test
    fun `an empty or all-zero set formats without inventing decimals`() {
        assertEquals("0", ValueFormat.of(emptyList<Double>()).format(0.0))
        assertEquals("0", ValueFormat.of(listOf(0.0, 0.0)).format(0.0))
    }

    @Test
    fun `a non-finite value is named rather than printed as Infinity`() {
        val format = ValueFormat.of(listOf(1.0))
        assertEquals("not a number", format.format(Double.NaN))
        assertEquals("infinite", format.format(Double.POSITIVE_INFINITY))
        assertEquals("-infinite", format.format(Double.NEGATIVE_INFINITY))
    }
}
