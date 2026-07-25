package com.simoscal.quickedit

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The staged-edit discipline of the boost editor.
 *
 * The point of every rule here is that a drag is a *proposal* until Apply, and
 * that no proposal is ever silently lost, silently altered, or silently sent.
 */
class BoostUiStateTest {

    private val model = BoostCurveModel(
        rpmAxis = List(12) { 1000.0 + it * 500.0 },
        slots = SLOT_IDS.map { slot -> SlotCurve(slot, List(12) { 8.0 + slot }) },
        baseCeilingPsi = List(12) { 18.0 },
        baseRpmAxis = listOf(1000.0, 6500.0),
        baseCeilingOwnPsi = listOf(18.0, 18.0),
    )

    private fun loaded() = BoostUiState().withModel(model)

    @Test
    fun `loading a model starts a clean draft on the active slot`() {
        val state = loaded()
        assertEquals(model.curve(1)!!.psi, state.draft)
        assertFalse(state.dirty)
        assertFalse(state.canApply)
    }

    @Test
    fun `a drag makes the draft dirty without touching the model`() {
        val state = loaded().withDraggedPoint(3, 12.0)
        assertTrue(state.dirty)
        assertTrue(state.canApply)
        assertEquals(12.0, state.draft[3], 1e-9)
        // The engine's copy is untouched — nothing was sent.
        assertEquals(model.curve(1)!!.psi, state.committed)
    }

    @Test
    fun `switching slots is refused while a draft is unapplied`() {
        val dirty = loaded().withDraggedPoint(0, 12.0)
        val attempted = dirty.selectingSlot(3)
        assertEquals(1, attempted.activeSlot)
        assertNotNull(attempted.notice)
        assertTrue(attempted.notice!!.contains("slot 1"))
        // The draft survives the refusal — it is not quietly dropped.
        assertEquals(12.0, attempted.draft[0], 1e-9)
    }

    @Test
    fun `switching slots works once the draft is discarded`() {
        val state = loaded().withDraggedPoint(0, 12.0).discardingDraft().selectingSlot(3)
        assertEquals(3, state.activeSlot)
        assertEquals(model.curve(3)!!.psi, state.draft)
        assertNull(state.notice)
        assertFalse(state.dirty)
    }

    @Test
    fun `a typed value above the ceiling leaves the draft untouched`() {
        val state = loaded().withTypedPoint(2, 25.0)
        assertNotNull(state.notice)
        assertFalse("a refused entry must not become an edit", state.dirty)
    }

    @Test
    fun `a dragged value above the ceiling is snapped instead`() {
        val state = loaded().withDraggedPoint(2, 25.0)
        assertNull(state.notice)
        assertEquals(model.maxSettablePsi, state.draft[2], 1e-9)
    }

    @Test
    fun `a flat cap is validated, not clamped`() {
        assertNotNull(loaded().withFlatCap(30.0).notice)
        assertFalse(loaded().withFlatCap(30.0).dirty)
        val ok = loaded().withFlatCap(12.0)
        assertTrue(ok.draft.all { it == 12.0 })
        assertTrue(ok.dirty)
    }

    @Test
    fun `copying pulls another slot's curve into the draft`() {
        val state = loaded().copyingFrom(5)
        assertEquals(model.curve(5)!!.psi, state.draft)
        assertTrue(state.dirty)
        assertEquals("the destination is still slot 1", 1, state.activeSlot)
    }

    @Test
    fun `copying from the active slot says so rather than doing nothing`() {
        val state = loaded().copyingFrom(1)
        assertNotNull(state.notice)
        assertFalse(state.dirty)
    }

    @Test
    fun `applying folds the encoded values in, not the requested ones`() {
        val requested = List(12) { 10.0 }
        val encoded = List(12) { 9.99 } // the psi floor bit
        val state = loaded()
            .withFlatCap(10.0)
            .applied(BoostEditReceipt(slot = 1, requestedPsi = requested, encodedPsi = encoded, floored = true))

        assertEquals(encoded, state.model!!.curve(1)!!.psi)
        assertEquals(encoded, state.draft)
        // Nothing left to apply: the draft now matches what the bin holds.
        assertFalse(state.dirty)
        assertTrue(state.lastEdit!!.floored)
    }

    @Test
    fun `applying another slot's edit does not disturb the open draft`() {
        val state = loaded()
            .withDraggedPoint(0, 12.0)
            .applied(BoostEditReceipt(slot = 4, requestedPsi = emptyList(), encodedPsi = List(12) { 15.0 }, floored = false))
        assertEquals(12.0, state.draft[0], 1e-9)
        assertEquals(List(12) { 15.0 }, state.model!!.curve(4)!!.psi)
    }

    @Test
    fun `breakpoints above the base ceiling are counted for the draft`() {
        val state = loaded().withDraggedPoint(0, 17.0).withDraggedPoint(1, 17.5)
        assertTrue(state.draftCappedByBase.isEmpty())
        // The base ceiling here is a flat 18.0, and the refusal ceiling is the same
        // value — so the only reachable "capped" region is between them, which is
        // empty. Give the model a dip and the count appears.
        val dipped = state.copy(
            model = model.copy(baseCeilingPsi = model.baseCeilingPsi.toMutableList().also { it[0] = 10.0 })
        )
        assertEquals(listOf(0), dipped.draftCappedByBase)
    }

    @Test
    fun `a draft of the wrong length can never be applied`() {
        val mangled = loaded().copy(draft = listOf(1.0, 2.0))
        assertFalse(mangled.dirty)
        assertFalse(mangled.canApply)
    }
}
