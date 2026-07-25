package com.simoscal.quickedit

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The generic calibration editor's rules.
 *
 * Same two invariants as the boost editor, tested the same way: a typed value is
 * never quietly altered, and a table the engine cannot write from physical units
 * is never made to look editable.
 */
class TablesUiStateTest {

    private fun summary(
        name: String = "pressure_quotient_max",
        reversible: Boolean = true,
        isAxis: Boolean = false,
        rows: Int = 2,
        cols: Int = 3,
    ) = TableSummary(
        space = "base",
        name = name,
        symbol = "KFPQMAX",
        title = "Maximum pressure quotient",
        description = "Maximum pressure quotient",
        uniqueidHex = "0x1234",
        units = "-",
        rows = rows,
        cols = cols,
        ndim = 2,
        reversible = reversible,
        isAxis = isAxis,
        categories = listOf("boost"),
    )

    private fun detail(
        summary: TableSummary = summary(),
        values: List<List<Double>> = listOf(listOf(1.0, 2.0, 3.0), listOf(4.0, 5.0, 6.0)),
    ) = TableDetail(summary = summary, values = values, xAxis = null, yAxis = null)

    private fun open(detail: TableDetail = detail()) = TablesUiState().withDetail(detail)

    // ------------------------------------------------------------------ naming

    @Test
    fun `a table always presents as ID plus description`() {
        assertEquals("KFPQMAX — Maximum pressure quotient", summary().idAndDescription)
        // No symbol: the unique id stands in, but the description still appears.
        val anonymous = summary().copy(symbol = null)
        assertEquals("0x1234 — Maximum pressure quotient", anonymous.idAndDescription)
    }

    // ------------------------------------------------------------------ drafts

    @Test
    fun `opening a table starts an identical, clean draft`() {
        val state = open()
        assertEquals(state.committed, state.draft)
        assertFalse(state.dirty)
        assertFalse(state.canApply)
        assertTrue(state.changedCells.isEmpty())
    }

    @Test
    fun `a typed cell shows up as a change with a delta`() {
        val state = open().withTypedCell(CellRef(1, 2), 6.5)
        assertTrue(state.dirty)
        assertEquals(listOf(CellRef(1, 2)), state.changedCells)
        assertEquals(0.5, state.delta(CellRef(1, 2))!!, 1e-9)
        // The committed copy is untouched until Apply.
        assertEquals(6.0, state.committed[1][2], 1e-9)
    }

    @Test
    fun `a non-finite entry is refused, not stored`() {
        val state = open().withTypedCell(CellRef(0, 0), Double.NaN)
        assertNotNull(state.notice)
        assertFalse(state.dirty)
    }

    @Test
    fun `a non-reversible table can never be applied`() {
        val state = open(detail(summary(reversible = false))).withTypedCell(CellRef(0, 0), 9.0)
        assertTrue("the draft may still be composed", state.dirty)
        assertFalse("but it can never be sent", state.canApply)
    }

    @Test
    fun `discarding returns the draft to what the engine holds`() {
        val state = open().withTypedCell(CellRef(0, 0), 99.0).discardingDraft()
        assertFalse(state.dirty)
        assertEquals(1.0, state.draft[0][0], 1e-9)
    }

    // ---------------------------------------------------------------- batching

    @Test
    fun `fill, offset, and scale act only on the selection`() {
        val selected = open().togglingCell(CellRef(0, 0)).togglingCell(CellRef(0, 1))

        val filled = selected.fillingSelection(7.0)
        assertEquals(listOf(7.0, 7.0, 3.0), filled.draft[0])
        assertEquals(listOf(4.0, 5.0, 6.0), filled.draft[1])

        val offset = selected.offsettingSelection(-0.5)
        assertEquals(listOf(0.5, 1.5, 3.0), offset.draft[0])

        val scaled = selected.scalingSelection(2.0)
        assertEquals(listOf(2.0, 4.0, 3.0), scaled.draft[0])
    }

    @Test
    fun `a batch operation with nothing selected says so`() {
        val state = open().fillingSelection(7.0)
        assertNotNull(state.notice)
        assertFalse(state.dirty)
    }

    @Test
    fun `toggling a cell twice deselects it`() {
        val state = open().togglingCell(CellRef(0, 0)).togglingCell(CellRef(0, 0))
        assertTrue(state.selection.isEmpty())
    }

    @Test
    fun `select all covers every cell of the grid`() {
        assertEquals(6, open().selectingAll().selection.size)
    }

    // ------------------------------------------------------------- interpolate

    @Test
    fun `ramping a row fills the interior linearly and holds the endpoints`() {
        val wide = detail(summary(cols = 5), listOf(listOf(10.0, 0.0, 0.0, 0.0, 20.0)))
        val state = open(wide)
            .togglingCell(CellRef(0, 0)).togglingCell(CellRef(0, 1)).togglingCell(CellRef(0, 2))
            .togglingCell(CellRef(0, 3)).togglingCell(CellRef(0, 4))
            .interpolatingSelection()
        assertEquals(listOf(10.0, 12.5, 15.0, 17.5, 20.0), state.draft[0])
    }

    @Test
    fun `ramping refuses a selection that is not one contiguous row or column`() {
        val scattered = open()
            .togglingCell(CellRef(0, 0)).togglingCell(CellRef(1, 1)).togglingCell(CellRef(0, 2))
            .interpolatingSelection()
        assertNotNull(scattered.notice)
        assertFalse(scattered.dirty)

        val gapped = open(detail(summary(cols = 5), listOf(listOf(1.0, 2.0, 3.0, 4.0, 5.0))))
            .togglingCell(CellRef(0, 0)).togglingCell(CellRef(0, 1)).togglingCell(CellRef(0, 4))
            .interpolatingSelection()
        assertNotNull(gapped.notice)
        assertFalse(gapped.dirty)
    }

    @Test
    fun `ramping needs at least three cells`() {
        val state = open().togglingCell(CellRef(0, 0)).togglingCell(CellRef(0, 1)).interpolatingSelection()
        assertNotNull(state.notice)
    }

    // -------------------------------------------------------------------- axes

    @Test
    fun `an axis edit that breaks monotonicity is refused at the keystroke`() {
        val axis = detail(summary(isAxis = true, rows = 1, cols = 4), listOf(listOf(1000.0, 2000.0, 3000.0, 4000.0)))
        val state = open(axis).withTypedCell(CellRef(0, 2), 1500.0)
        assertNotNull(state.notice)
        assertTrue(state.notice!!.contains("strictly increase"))
        assertFalse(state.dirty)
        // A value that keeps the order is accepted.
        assertTrue(open(axis).withTypedCell(CellRef(0, 2), 2500.0).dirty)
    }

    @Test
    fun `a batch operation that breaks an axis is refused whole`() {
        val axis = detail(summary(isAxis = true, rows = 1, cols = 4), listOf(listOf(1000.0, 2000.0, 3000.0, 4000.0)))
        val state = open(axis).togglingCell(CellRef(0, 1)).fillingSelection(3500.0)
        assertNotNull(state.notice)
        assertFalse("no partial application", state.dirty)
    }

    // ------------------------------------------------------------------ applied

    @Test
    fun `applying folds the encoded grid back in`() {
        val encoded = listOf(listOf(1.0, 2.0, 3.0), listOf(4.0, 5.0, 6.49))
        val state = open()
            .withTypedCell(CellRef(1, 2), 6.5)
            .applied(TableEditReceipt("KFPQMAX — Maximum pressure quotient", true, 0.01, "", encoded))
        assertEquals(encoded, state.committed)
        assertEquals(encoded, state.draft)
        assertFalse(state.dirty)
        assertTrue(state.lastEdit!!.quantized)
    }

    // ------------------------------------------------------------------ search

    @Test
    fun `search matches the ID, the description, and the category`() {
        val state = TablesUiState(catalog = listOf(summary(), summary(name = "put_setpoint").copy(
            symbol = "IP_PUT_SP", description = "Pressure up throttle setpoint", categories = listOf("boost"),
        )))
        assertEquals(2, state.copy(query = "boost").visibleCatalog.size)
        assertEquals(1, state.copy(query = "IP_PUT_SP").visibleCatalog.size)
        assertEquals(1, state.copy(query = "throttle").visibleCatalog.size)
        assertEquals(2, state.copy(query = "  ").visibleCatalog.size)
        assertEquals(0, state.copy(query = "ignition").visibleCatalog.size)
    }

    // ----------------------------------------------------------------- parsing

    @Test
    fun `a bridge table payload parses, including a flat one-dimensional grid`() {
        val payload = JSONObject(
            """
            {
              "space": "base", "name": "put_setpoint_rpm_axis", "symbol": "IP_PUT_SP_RPM",
              "title": "Pressure up throttle setpoint rpm axis",
              "description": "Pressure up throttle setpoint rpm axis",
              "uniqueid_hex": "0xabcd", "units": "rpm", "shape": [1, 3], "ndim": 1,
              "reversible": true, "is_axis": true, "categories": ["boost"],
              "values": [1000.0, 2000.0, 3000.0],
              "x_axis": {"units": "rpm", "values": [1000.0, 2000.0, 3000.0]},
              "y_axis": null
            }
            """.trimIndent()
        )
        val parsed = TableDetail.fromJson(payload)
        assertTrue(parsed.summary.isAxis)
        // A flat list normalizes to one row, so the editor's [row][col] holds.
        assertEquals(1, parsed.values.size)
        assertEquals(listOf(1000.0, 2000.0, 3000.0), parsed.values[0])
        assertEquals("rpm", parsed.xAxis!!.units)
        assertNull(parsed.yAxis)
    }

    @Test
    fun `a two-dimensional grid keeps its rows`() {
        val payload = JSONObject("""{"values": [[1.0, 2.0], [3.0, 4.0]], "shape": [2, 2]}""")
        val parsed = TableDetail.fromJson(payload)
        assertEquals(2, parsed.values.size)
        assertEquals(listOf(3.0, 4.0), parsed.values[1])
    }
}
