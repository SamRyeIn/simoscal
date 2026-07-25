package com.simoscal.quickedit

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The boost editor's arithmetic, pinned on the JVM.
 *
 * These are the rules a fingertip runs into, and boost is the overboost domain —
 * so they are tested as pure functions rather than only through a canvas. A
 * screen test could prove a line was drawn in the right place while the value
 * sent to the engine was wrong; these prove the value.
 *
 * The fixture mirrors the real switch-patch geometry: a 12-point shared rpm
 * axis, five slots, and a base full-load row on its own coarser axis whose
 * maximum is the engine's refusal ceiling.
 */
class BoostCurveTest {

    private val rpmAxis = listOf(
        1000.0, 1500.0, 2000.0, 2500.0, 3000.0, 3500.0,
        4000.0, 4500.0, 5000.0, 5500.0, 6000.0, 6500.0,
    )

    /** Rises, peaks mid-range at 21.00 psi, then tapers — the shape of a real ceiling. */
    private val baseCeilingOwnPsi = listOf(8.00, 15.00, 21.00, 20.00, 18.00, 16.00)

    /** The same ceiling interpolated onto the finer slot axis. */
    private val baseCeilingPsi = listOf(
        8.00, 12.00, 15.00, 18.00, 21.00, 20.50,
        20.00, 19.00, 18.00, 17.00, 16.00, 16.00,
    )

    private fun flat(slot: Int, psi: Double) = SlotCurve(slot, List(12) { psi })

    private val model = BoostCurveModel(
        rpmAxis = rpmAxis,
        slots = listOf(
            flat(1, 10.0),
            flat(2, 14.0),
            flat(3, 17.0),
            flat(4, 19.0),
            SlotCurve(5, listOf(8.0, 12.0, 16.0, 18.0, 19.0, 19.0, 19.0, 18.5, 18.0, 17.5, 17.0, 16.5)),
        ),
        baseCeilingPsi = baseCeilingPsi,
        baseRpmAxis = listOf(1000.0, 2000.0, 3000.0, 4000.0, 5000.0, 6000.0),
        baseCeilingOwnPsi = baseCeilingOwnPsi,
    )

    // ------------------------------------------------------------- the ceilings

    @Test
    fun `the refusal ceiling is the base row's maximum, not its local value`() {
        // The engine's guard compares against max(base_put[-1]) — one scalar for
        // the whole curve. A per-rpm refusal limit would reject edits the engine
        // accepts at every rpm where the base dips below its own peak.
        assertEquals(21.00, model.refusalCeilingPsi, 1e-9)
    }

    @Test
    fun `the highest settable value sits strictly below refusal`() {
        assertTrue(model.maxSettablePsi < model.refusalCeilingPsi)
        // Strictly below, but not needlessly far below: one step, not a haircut.
        assertEquals(20.99, model.maxSettablePsi, 1e-6)
        assertNull(model.rejectTypedPsi(model.maxSettablePsi))
    }

    @Test
    fun `a non-round ceiling still yields a value strictly below it`() {
        // A decoded base row is physical units, not tidy integers, so the back-off
        // must hold for a ceiling that is not on the psi step at all.
        listOf(21.005, 20.999, 17.3333, 0.05).forEach { ceiling ->
            val odd = model.copy(baseCeilingOwnPsi = listOf(1.0, ceiling))
            assertTrue(
                "ceiling $ceiling produced ${odd.maxSettablePsi}",
                odd.maxSettablePsi < odd.refusalCeilingPsi,
            )
            assertTrue(odd.maxSettablePsi >= odd.refusalCeilingPsi - 2 * PSI_STEP)
        }
    }

    // ----------------------------------------------------------- typed vs drag

    @Test
    fun `a typed cap at or above the ceiling is refused, never clamped`() {
        assertNotNull(model.rejectTypedPsi(21.00))
        assertNotNull(model.rejectTypedPsi(22.00))
        assertNotNull(model.rejectTypedPsi(-0.5))
        assertNotNull(model.rejectTypedPsi(Double.NaN))
        assertNull(model.rejectTypedPsi(20.0))
        assertNull(model.rejectTypedPsi(0.0))
    }

    @Test
    fun `the refusal message names both the value and the ceiling`() {
        val message = model.rejectTypedPsi(22.0)
        assertNotNull(message)
        assertTrue(message!!.contains("22.00"))
        assertTrue(message.contains("21.00"))
        assertTrue(message.contains("IP_PUT_SP"))
    }

    @Test
    fun `formatted numbers always use a dot, whatever the device locale is`() {
        // The text fields parse with `toDoubleOrNull`, which only accepts `.`. On a
        // comma-decimal locale, default formatting would render a value the app
        // itself cannot read back.
        val previous = java.util.Locale.getDefault()
        try {
            java.util.Locale.setDefault(java.util.Locale.GERMANY)
            assertEquals("10.50", 10.5.display("%.2f"))
            assertTrue(model.rejectTypedPsi(22.0)!!.contains("22.00"))
            assertNotNull("10.50".toDoubleOrNull())
        } finally {
            java.util.Locale.setDefault(previous)
        }
    }

    @Test
    fun `a dragged value is snapped into range rather than refused`() {
        assertEquals(20.99, model.clampDraggedPsi(25.0), 1e-6)
        assertEquals(20.99, model.clampDraggedPsi(21.0), 1e-6)
        assertEquals(0.0, model.clampDraggedPsi(-4.0), 1e-6)
        assertEquals(14.32, model.clampDraggedPsi(14.3172), 1e-6)
    }

    @Test
    fun `every drag result is a value the engine would accept`() {
        // The property that matters, stated as a property: no reachable fingertip
        // position can produce a cap the guard refuses.
        generateSequence(-10.0) { it + 0.137 }.takeWhile { it < 40.0 }.forEach { raw ->
            assertNull(
                "drag to $raw produced a refused cap",
                model.rejectTypedPsi(model.clampDraggedPsi(raw)),
            )
        }
    }

    // ------------------------------------------------------------- the min() band

    @Test
    fun `effective boost is the pointwise minimum of slot and base`() {
        val effective = model.effectivePsi(4) // flat 19.0 against a base that dips to 16
        assertEquals(12, effective.size)
        assertEquals(8.0, effective[0], 1e-9)   // base 8.00 wins
        assertEquals(19.0, effective[4], 1e-9)  // base 21.00, slot wins
        assertEquals(16.0, effective[11], 1e-9) // base 16.00 wins again
    }

    @Test
    fun `points the base swallows are reported as capped, not as errors`() {
        // 19.0 flat is under the 21.00 refusal ceiling everywhere — a legal edit —
        // yet the base still limits it wherever the base row sits below 19.
        val capped = model.cappedByBase(4)
        assertTrue(capped.contains(0))
        assertFalse(capped.contains(4))
        assertTrue(capped.contains(11))
        // Slot 1's flat 10.0 still pokes above the base's 8.00 at 1000 rpm, which
        // is the point: "capped" is per-breakpoint, not a property of the curve.
        assertEquals(listOf(0), model.cappedByBase(1))
        // A slot under the base at every breakpoint is capped nowhere.
        val low = model.copy(slots = model.slots + flat(6, 7.0))
        assertTrue(low.cappedByBase(6).isEmpty())
    }

    // ---------------------------------------------------------- batch operations

    @Test
    fun `a flat cap fills every breakpoint`() {
        val flat = model.flatCap(15.0)
        assertEquals(12, flat.size)
        assertTrue(flat.all { it == 15.0 })
        assertTrue(SlotCurve(1, flat).isFlat)
    }

    @Test
    fun `a flat cap above the ceiling lands at the highest settable value`() {
        assertTrue(model.flatCap(30.0).all { it == model.maxSettablePsi })
    }

    @Test
    fun `copying a slot reproduces its curve, and a missing slot copies nothing`() {
        assertEquals(model.curve(5)!!.psi, model.copySlot(5))
        assertNull(model.copySlot(9))
    }

    @Test
    fun `smoothing holds the endpoints and averages the interior`() {
        val spiky = listOf(10.0, 10.0, 20.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0)
        val smoothed = model.smooth(spiky)
        assertEquals(10.0, smoothed.first(), 1e-9)
        assertEquals(10.0, smoothed.last(), 1e-9)
        assertEquals(13.33, smoothed[2], 0.01)
        assertTrue("the spike must come down", smoothed[2] < spiky[2])
    }

    @Test
    fun `smoothing never produces a refused value`() {
        val atTheLimit = List(12) { model.maxSettablePsi }
        model.smooth(atTheLimit).forEach { assertNull(model.rejectTypedPsi(it)) }
    }

    @Test
    fun `dragging one point leaves its neighbours alone`() {
        val before = model.curve(3)!!.psi
        val after = model.withDraggedPoint(before, index = 6, psi = 12.5)
        assertEquals(12.5, after[6], 1e-6)
        assertEquals(before.filterIndexed { i, _ -> i != 6 }, after.filterIndexed { i, _ -> i != 6 })
        // An index off the end is a no-op, not a crash or an appended point.
        assertEquals(before, model.withDraggedPoint(before, index = 99, psi = 12.5))
    }

    // --------------------------------------------------------------- parsing

    @Test
    fun `the bridge payload round-trips into the model`() {
        val payload = JSONObject(
            """
            {
              "rpm_axis": [1000, 2000, 3000],
              "slots": [
                {"slot": 1, "psi": [8.0, 9.0, 10.0]},
                {"slot": 2, "psi": [11.0, 12.0, 13.0]}
              ],
              "base_ceiling_psi": [14.0, 18.0, 21.0],
              "base_rpm_axis": [1000, 3000],
              "base_ceiling_own_psi": [14.0, 21.0]
            }
            """.trimIndent()
        )
        val parsed = BoostCurveModel.fromJson(payload)
        assertEquals(listOf(1000.0, 2000.0, 3000.0), parsed.rpmAxis)
        assertEquals(2, parsed.slots.size)
        assertEquals(listOf(11.0, 12.0, 13.0), parsed.curve(2)!!.psi)
        assertEquals(21.0, parsed.refusalCeilingPsi, 1e-9)
    }

    @Test
    fun `an empty payload parses to an empty model rather than throwing`() {
        // The screen decides what to show when there is nothing to draw; the parser
        // must not be the thing that crashes on a session with no patch space.
        val parsed = BoostCurveModel.fromJson(JSONObject("{}"))
        assertTrue(parsed.rpmAxis.isEmpty())
        assertTrue(parsed.slots.isEmpty())
        assertNull(parsed.curve(1))
        assertEquals(0.0, parsed.refusalCeilingPsi, 1e-9)
    }
}
