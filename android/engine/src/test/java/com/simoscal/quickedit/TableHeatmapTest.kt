package com.simoscal.quickedit

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The grid's value → colour mapping.
 *
 * Worth testing on its own for the same reason [BoostPlotTest] is: this decides
 * what a person believes a table's shape to be. The cases that matter are the
 * ones where a wrong answer still looks plausible on screen — a flat table shaded
 * as if it varied, a NaN dragging the scale to nonsense, or ink chosen so badly
 * the numerals under it cannot be read.
 */
class TableHeatmapTest {

    private val grid = listOf(
        listOf(0.0, 5.0),
        listOf(10.0, 20.0),
    )

    @Test
    fun `the scale spans the grid's own values`() {
        val scale = HeatScale.of(grid)
        assertEquals(0.0, scale.min, 1e-12)
        assertEquals(20.0, scale.max, 1e-12)
        assertFalse(scale.flat)
    }

    @Test
    fun `fraction places the endpoints and the middle`() {
        val scale = HeatScale.of(grid)
        assertEquals(0.0, scale.fraction(0.0), 1e-12)
        assertEquals(1.0, scale.fraction(20.0), 1e-12)
        assertEquals(0.5, scale.fraction(10.0), 1e-12)
    }

    @Test
    fun `fraction clamps values from outside the scale`() {
        // The draft can hold a value outside the committed range mid-edit; it must
        // shade as the extreme rather than run off the end of the ramp.
        val scale = HeatScale.of(grid)
        assertEquals(0.0, scale.fraction(-50.0), 1e-12)
        assertEquals(1.0, scale.fraction(1e9), 1e-12)
    }

    @Test
    fun `fraction rises with value`() {
        val scale = HeatScale.of(grid)
        val samples = listOf(0.0, 1.0, 5.0, 9.9, 10.0, 15.0, 20.0)
        samples.zipWithNext().forEach { (lower, higher) ->
            assertTrue(
                "fraction must not fall from $lower to $higher",
                scale.fraction(lower) <= scale.fraction(higher),
            )
        }
    }

    @Test
    fun `a constant table is flat and gets no colour`() {
        val scale = HeatScale.of(listOf(listOf(7.0, 7.0), listOf(7.0, 7.0)))
        assertTrue(scale.flat)
        assertNull(scale.colorFor(7.0))
    }

    @Test
    fun `a single-cell table is flat`() {
        assertTrue(HeatScale.of(listOf(listOf(3.0))).flat)
    }

    @Test
    fun `an empty table is flat rather than throwing`() {
        assertTrue(HeatScale.of(emptyList()).flat)
        assertTrue(HeatScale.of(listOf(emptyList())).flat)
    }

    @Test
    fun `a table differing only far below display precision is flat`() {
        // Otherwise a table that is constant for every practical purpose gets a
        // full-range rainbow that reads as real structure.
        val scale = HeatScale.of(listOf(listOf(1.0, 1.0 + 1e-15)))
        assertTrue(scale.flat)
    }

    @Test
    fun `non-finite cells do not poison the scale`() {
        val scale = HeatScale.of(listOf(listOf(0.0, Double.NaN), listOf(10.0, Double.POSITIVE_INFINITY)))
        assertEquals(0.0, scale.min, 1e-12)
        assertEquals(10.0, scale.max, 1e-12)
        assertFalse(scale.flat)
        assertNull(scale.colorFor(Double.NaN))
        assertNotNull(scale.colorFor(5.0))
    }

    @Test
    fun `an all-NaN table is flat rather than colouring everything`() {
        assertTrue(HeatScale.of(listOf(listOf(Double.NaN, Double.NaN))).flat)
    }

    @Test
    fun `the ramp runs blue at the bottom to red at the top`() {
        val low = rampColor(0.0)
        val high = rampColor(1.0)
        assertTrue("low end should be blue-dominant", low.blue > low.red)
        assertTrue("high end should be red-dominant", high.red > high.blue)
    }

    @Test
    fun `the ramp clamps outside zero to one`() {
        assertEquals(rampColor(0.0), rampColor(-1.0))
        assertEquals(rampColor(1.0), rampColor(2.0))
    }

    @Test
    fun `the ramp is continuous across its stops`() {
        // A visible seam at a stop boundary would read as a threshold in the data
        // that does not exist.
        var previous = rampColor(0.0)
        var step = 0.005
        while (step <= 1.0) {
            val current = rampColor(step)
            val jump = maxOf(
                kotlin.math.abs(current.red - previous.red),
                kotlin.math.abs(current.green - previous.green),
                kotlin.math.abs(current.blue - previous.blue),
            )
            assertTrue("channel jumped $jump at $step", jump <= 12)
            previous = current
            step += 0.005
        }
    }

    @Test
    fun `ink choice flips somewhere along the ramp`() {
        // The point of computing luminance: a single fixed ink colour cannot stay
        // readable from the dark blue end to the amber.
        val darkInk = (0..100).map { rampColor(it / 100.0) }.count { it.prefersDarkInk }
        assertTrue("some of the ramp needs dark ink", darkInk > 0)
        assertTrue("some of the ramp needs light ink", darkInk < 101)
    }

    @Test
    fun `the amber band takes dark ink and the blue end takes light ink`() {
        assertTrue(rampColor(0.75).prefersDarkInk)
        assertFalse(rampColor(0.0).prefersDarkInk)
    }

    @Test
    fun `luminance stays inside zero to one`() {
        (0..100).forEach { step ->
            val luminance = rampColor(step / 100.0).luminance
            assertTrue(luminance in 0.0..1.0)
        }
    }

    @Test
    fun `a descending table still shades by value not by position`() {
        // Guards against ever scaling by cell index: the first cell here is the
        // hottest, and must colour as the top of the ramp.
        val scale = HeatScale.of(listOf(listOf(20.0, 10.0, 0.0)))
        assertEquals(1.0, scale.fraction(20.0), 1e-12)
        assertEquals(0.0, scale.fraction(0.0), 1e-12)
    }

    @Test
    fun `negative ranges scale normally`() {
        // Timing tables run negative; the scale must not assume zero is the floor.
        val scale = HeatScale.of(listOf(listOf(-10.0, -5.0, 0.0)))
        assertFalse(scale.flat)
        assertEquals(0.0, scale.fraction(-10.0), 1e-12)
        assertEquals(0.5, scale.fraction(-5.0), 1e-12)
        assertEquals(1.0, scale.fraction(0.0), 1e-12)
    }
}
