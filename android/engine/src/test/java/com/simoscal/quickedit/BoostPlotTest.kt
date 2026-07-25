package com.simoscal.quickedit

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The canvas coordinate math.
 *
 * Worth testing on its own because this is the step where a fingertip becomes a
 * number: if [BoostPlotScale.psiAt] were not the exact inverse of
 * [BoostPlotScale.y], the value written to the bin would differ from the height
 * the curve was drawn at, and nothing on screen would reveal it.
 */
class BoostPlotTest {

    private val model = BoostCurveModel(
        // Deliberately non-uniform: real slot breakpoints are not evenly spaced.
        rpmAxis = listOf(1000.0, 1200.0, 1600.0, 2400.0, 3000.0, 4000.0, 5000.0, 6500.0),
        slots = listOf(SlotCurve(1, List(8) { 12.0 })),
        baseCeilingPsi = List(8) { 20.0 },
        baseRpmAxis = listOf(1000.0, 6500.0),
        baseCeilingOwnPsi = listOf(20.0, 20.0),
    )

    private val scale = BoostPlotScale.of(model, model.slots.first().psi)
    private val geometry = BoostPlotGeometry(canvasWidth = 800f, canvasHeight = 400f)

    @Test
    fun `psiAt inverts y exactly`() {
        listOf(0.0, 5.0, 12.34, 20.0, scale.psiMax).forEach { psi ->
            assertEquals(psi, scale.psiAt(geometry, scale.y(geometry, psi)), 1e-3)
        }
    }

    @Test
    fun `the plot spans the canvas minus its label gutters`() {
        assertEquals(56f, geometry.left, 1e-3f)
        assertEquals(800f - 56f - 40f, geometry.width, 1e-3f)
        assertEquals(400f - 14f - 34f, geometry.height, 1e-3f)
        assertTrue(geometry.right < 800f)
        assertTrue(geometry.bottom < 400f)
    }

    @Test
    fun `rpm maps by value, so uneven breakpoints are not drawn evenly spaced`() {
        val first = scale.x(geometry, 1000.0)
        val second = scale.x(geometry, 1200.0)
        val last = scale.x(geometry, 6500.0)
        assertEquals(geometry.left, first, 1e-3f)
        assertEquals(geometry.right, last, 1e-3f)
        // 1200 rpm is 200/5500 of the way along — nowhere near 1/7 of the width,
        // which is where index-spacing would have put it.
        assertEquals(geometry.left + geometry.width * (200f / 5500f), second, 0.5f)
    }

    @Test
    fun `the nearest breakpoint is the one a finger is closest to`() {
        model.rpmAxis.forEachIndexed { index, rpm ->
            assertEquals(index, scale.nearestIndex(geometry, scale.x(geometry, rpm)))
        }
        // Just left of the last breakpoint still grabs the last breakpoint.
        assertEquals(model.rpmAxis.lastIndex, scale.nearestIndex(geometry, geometry.right - 2f))
    }

    @Test
    fun `the y range leaves headroom above the highest line`() {
        assertTrue("the refusal ceiling must not sit on the frame", scale.psiMax > model.refusalCeilingPsi)
        assertTrue(scale.y(geometry, model.refusalCeilingPsi) > geometry.top)
    }

    @Test
    fun `an empty model produces a usable scale rather than dividing by zero`() {
        val empty = BoostCurveModel(emptyList(), emptyList(), emptyList(), emptyList(), emptyList())
        val emptyScale = BoostPlotScale.of(empty, emptyList())
        assertTrue(emptyScale.psiMax > 0.0)
        assertEquals(0, emptyScale.nearestIndex(geometry, 100f))
        assertTrue(emptyScale.x(geometry, 0.0).isFinite())
        assertTrue(emptyScale.y(geometry, 0.0).isFinite())
    }

    @Test
    fun `a zero-size canvas does not produce infinities`() {
        val tiny = BoostPlotGeometry(canvasWidth = 0f, canvasHeight = 0f)
        assertTrue(tiny.width > 0f)
        assertTrue(tiny.height > 0f)
        assertTrue(scale.psiAt(tiny, 0f).isFinite())
    }
}
