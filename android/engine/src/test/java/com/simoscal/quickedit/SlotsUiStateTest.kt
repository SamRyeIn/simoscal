package com.simoscal.quickedit

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The per-slot switchboard's rules.
 *
 * One invariant carries the screen: **nothing is toggleable unless the engine
 * said so**. The patch exposes sixteen per-slot scalars and only twelve are
 * flags this library writes; the other four are read, described, and refused.
 * A row that moved when it should not is not a cosmetic bug — `Manual AFU` is a
 * 0–1 fraction stored `/128`, so "toggling" it writes 128× what anyone meant.
 */
class SlotsUiStateTest {

    private fun row(
        key: String = "enable_sl_tc",
        title: String = "Enable SL TC",
        kind: String = "flag",
        writable: Boolean = true,
        readonly: String = "",
        caution: String = "",
        units: String = "",
        group: String = "Traction",
        values: List<Double> = listOf(0.0, 0.0, 0.0, 0.0, 0.0),
    ) = SlotSetting.fromJson(
        JSONObject()
            .put("key", key)
            .put("title", title)
            .put("description", "a description")
            .put("kind", kind)
            .put("units", units)
            .put("group", group)
            .put("caution", caution)
            .put("readonly", readonly)
            .put("writable", writable)
            .put("values", values.toJsonArray())
            .put("slots", listOf(1.0, 2.0, 3.0, 4.0, 5.0).toJsonArray())
    )

    private fun state(vararg rows: SlotSetting) = SlotsUiState().withSettings(rows.toList())

    @Test
    fun `a flag reports which slots it is on for`() {
        val setting = row(values = listOf(1.0, 0.0, 1.0, 0.0, 0.0))

        assertTrue(setting.isOn(1))
        assertFalse(setting.isOn(2))
        assertTrue(setting.isOn(3))
        assertEquals(2, setting.onCount)
    }

    @Test
    fun `a read-only setting is never toggleable`() {
        val setting = row(
            key = "rpm_limiter", title = "RPM limiter", kind = "number",
            writable = false, readonly = "not characterised", units = "rpm",
        )

        assertFalse(setting.toggleable)
        assertFalse(state(setting).canToggle("rpm_limiter"))
    }

    @Test
    fun `a writable non-flag is still not toggleable`() {
        // The trap this guards: `Manual AFU` is writable-looking and is a 0–1
        // fraction stored /128. Kind, not just the writable bit, decides whether
        // there is an on/off to set at all.
        val setting = row(key = "manual_afu", kind = "number", writable = true)

        assertFalse(setting.toggleable)
        assertFalse(state(setting).canToggle("manual_afu"))
    }

    @Test
    fun `an unrecognised kind is never treated as a flag`() {
        // Newer engine, older app. Guessing FLAG here would write 0/1 over
        // something this build knows nothing about.
        val setting = row(kind = "trinary")

        assertEquals(SettingKind.UNKNOWN, setting.kind)
        assertFalse(setting.toggleable)
    }

    @Test
    fun `a row already in flight cannot be sent twice`() {
        // Two taps on the same cell would otherwise be two writes and two undo
        // points for one intent.
        val before = state(row())
        assertTrue(before.canToggle("enable_sl_tc"))

        assertFalse(before.sending("enable_sl_tc").canToggle("enable_sl_tc"))
    }

    @Test
    fun `a refusal clears the in-flight mark and leaves the values alone`() {
        val before = state(row(values = listOf(0.0, 0.0, 0.0, 0.0, 0.0)))
        val after = before.sending("enable_sl_tc").refused("enable_sl_tc", "engine said no")

        assertEquals("engine said no", after.notice)
        assertFalse("enable_sl_tc" in after.pending)
        // Nothing optimistically flipped: a refused write must not have looked,
        // even for a frame, like it worked.
        assertEquals(0, after.setting("enable_sl_tc")!!.onCount)
        assertTrue(after.canToggle("enable_sl_tc"))
    }

    @Test
    fun `reloading the settings clears anything left in flight`() {
        val stuck = state(row()).sending("enable_sl_tc")

        assertTrue(stuck.withSettings(listOf(row())).pending.isEmpty())
    }

    @Test
    fun `re-reading after an undo replaces the values rather than merging them`() {
        // Undo moves the session's bytes out from under the screen. This grid's
        // whole job is to say which slots have a feature on, so a flag left
        // reading "on" after its edit was undone is the screen being wrong about
        // the only thing it claims to know.
        val before = state(row(values = listOf(0.0, 1.0, 0.0, 0.0, 1.0)))
        assertEquals(2, before.setting("enable_sl_tc")!!.onCount)

        val afterUndo = before.withSettings(listOf(row(values = listOf(0.0, 0.0, 0.0, 0.0, 0.0))))
        assertEquals(0, afterUndo.setting("enable_sl_tc")!!.onCount)
    }

    @Test
    fun `rows keep the engine's order and grouping`() {
        val state = state(
            row(key = "enable_sl_tc", group = "Traction"),
            row(key = "disable_oem_tc", title = "Disable OEM TC", group = "Traction"),
            row(key = "enable_lc", title = "Enable LC", group = "Features"),
        )

        assertEquals(listOf("Traction", "Features"), state.groups.map { it.first })
        assertEquals(2, state.groups.first().second.size)
    }

    @Test
    fun `slot columns come from the engine rather than being assumed`() {
        assertEquals(listOf(1, 2, 3, 4, 5), state(row()).slots)
        assertTrue(SlotsUiState().slots.isEmpty())
    }

    @Test
    fun `expanding the same row twice closes it`() {
        val opened = SlotsUiState().expanding("enable_lc")
        assertEquals("enable_lc", opened.expanded)
        assertNull(opened.expanding("enable_lc").expanded)
        assertEquals("pops_enable", opened.expanding("pops_enable").expanded)
    }

    @Test
    fun `a bridge payload carries the reason a row will not move`() {
        val payload = JSONObject(
            """
            {
              "settings": [
                {"key": "enable_sl_tc", "title": "Enable SL TC", "description": "d",
                 "kind": "flag", "units": "", "group": "Traction",
                 "caution": "turns off a driver-safety system", "readonly": "",
                 "writable": true, "values": [1,0,0,0,0], "slots": [1,2,3,4,5]},
                {"key": "gauge_settings", "title": "Gauge settings (bitmask)",
                 "description": "d", "kind": "opaque", "units": "", "group": "Display",
                 "caution": "", "readonly": "no source says what any bit means",
                 "writable": false, "values": [0,0,0,0,0], "slots": [1,2,3,4,5]}
              ]
            }
            """.trimIndent()
        )
        val state = SlotsUiState().withSettings(payload.slotSettings())

        assertEquals(2, state.settings.size)
        assertTrue(state.canToggle("enable_sl_tc"))
        assertEquals(
            "turns off a driver-safety system",
            state.setting("enable_sl_tc")!!.caution,
        )
        assertFalse(state.canToggle("gauge_settings"))
        assertEquals(
            "no source says what any bit means",
            state.setting("gauge_settings")!!.readonly,
        )
    }
}
