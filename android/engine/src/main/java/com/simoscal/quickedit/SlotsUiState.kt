package com.simoscal.quickedit

import org.json.JSONObject

/**
 * The per-slot switchboard: sixteen scalars against five map slots.
 *
 * The switch patch's whole proposition is one shared tune plus a per-slot
 * decision about which features are on. That decision is *comparative* — "which
 * slots have launch control enabled" — and a table-at-a-time editor answers it
 * badly: you open five tables, and the one you did not open is the one that
 * surprises you in the car. So the engine reports every setting against every
 * slot in one read, and this renders it as a grid.
 *
 * Unlike the table and boost editors, edits here are **not staged**. A flag is
 * one byte with two states and no shape to review before committing; a draft
 * would add an Apply step that reviews nothing. Each toggle is its own bridge
 * call, its own journal entry, and its own undo point.
 */

/** A setting's shape, which decides how a row is rendered and whether it moves. */
enum class SettingKind {
    /** 0/1 — the only kind this app writes. */
    FLAG,

    /** A scalar with a unit and a real range. */
    NUMBER,

    /** Packed bits nobody has documented. */
    OPAQUE,

    /** A kind this build does not know — newer engine, older app. */
    UNKNOWN;

    companion object {
        fun parse(raw: String): SettingKind = when (raw) {
            "flag" -> FLAG
            "number" -> NUMBER
            "opaque" -> OPAQUE
            // Never guess FLAG: an unknown kind rendered as a toggle is an
            // invitation to write 0/1 over something that is neither.
            else -> UNKNOWN
        }
    }
}

/**
 * One row of the switchboard: a setting and its value in each of the five slots.
 *
 * [readonly] is the load-bearing field. Empty means writable; anything else is
 * the *reason* the engine will not write it, carried from the profile to the
 * screen so a row that does not toggle says why rather than looking broken.
 */
data class SlotSetting(
    val key: String,
    val title: String,
    val description: String,
    val kind: SettingKind,
    val units: String,
    val group: String,
    val caution: String,
    val readonly: String,
    val writable: Boolean,
    val values: List<Double>,
    val slots: List<Int>,
) {

    /** Whether this row can be toggled — a flag the engine agrees to write. */
    val toggleable: Boolean
        get() = writable && kind == SettingKind.FLAG

    /** Is the flag on in [slot]? Meaningless for the non-flag kinds. */
    fun isOn(slot: Int): Boolean = valueIn(slot)?.let { it >= 0.5 } == true

    fun valueIn(slot: Int): Double? = slots.indexOf(slot).takeIf { it >= 0 }?.let { values.getOrNull(it) }

    /** How many slots have this flag on — the "3 of 5" a row leads with. */
    val onCount: Int
        get() = slots.count { isOn(it) }

    companion object {
        fun fromJson(json: JSONObject): SlotSetting {
            val slots = json.optJSONArray("slots")
            return SlotSetting(
                key = json.optString("key"),
                title = json.optString("title"),
                description = json.optString("description"),
                kind = SettingKind.parse(json.optString("kind")),
                units = json.optString("units", ""),
                group = json.optString("group", ""),
                caution = json.optString("caution", ""),
                readonly = json.optString("readonly", ""),
                writable = json.optBoolean("writable", false),
                values = json.doubleList("values"),
                slots = (0 until (slots?.length() ?: 0)).map { slots!!.optInt(it) },
            )
        }
    }
}

data class SlotsUiState(
    val settings: List<SlotSetting> = emptyList(),
    val loading: Boolean = false,
    val notice: String? = null,
    /** The row whose detail sheet is open, by key. */
    val expanded: String? = null,
    /** Keys currently in flight, so a double-tap cannot send two writes. */
    val pending: Set<String> = emptySet(),
) {

    val loaded: Boolean
        get() = settings.isNotEmpty()

    /** The slot columns, taken from the engine rather than assumed to be 1..5. */
    val slots: List<Int>
        get() = settings.firstOrNull()?.slots.orEmpty()

    /** Rows in the engine's order, grouped by the heading they sit under. */
    val groups: List<Pair<String, List<SlotSetting>>>
        get() = settings.groupBy { it.group }.toList()

    fun setting(key: String): SlotSetting? = settings.firstOrNull { it.key == key }

    /** Whether a specific cell may be tapped right now. */
    fun canToggle(key: String): Boolean =
        setting(key)?.toggleable == true && key !in pending
}

/** The `settings` array of a `slot_settings` / `slot_flag` reply. */
internal fun JSONObject.slotSettings(): List<SlotSetting> {
    val array = optJSONArray("settings") ?: return emptyList()
    return (0 until array.length()).mapNotNull { index ->
        array.optJSONObject(index)?.let { SlotSetting.fromJson(it) }
    }
}

// ----------------------------------------------------------------- transitions

fun SlotsUiState.loadingSettings(): SlotsUiState = copy(loading = true, notice = null)

fun SlotsUiState.withSettings(rows: List<SlotSetting>): SlotsUiState =
    copy(settings = rows, loading = false, pending = emptySet(), notice = null)

fun SlotsUiState.expanding(key: String?): SlotsUiState =
    copy(expanded = if (key == expanded) null else key)

fun SlotsUiState.sending(key: String): SlotsUiState =
    copy(pending = pending + key, notice = null)

/**
 * A refused write leaves the grid exactly as it was.
 *
 * The engine is the only thing that decides whether a flag moved, so nothing
 * here optimistically flips a switch and waits to be corrected. A toggle that
 * was refused must not have looked, even for a frame, like it worked.
 */
fun SlotsUiState.refused(key: String, reason: String): SlotsUiState =
    copy(pending = pending - key, notice = reason)
