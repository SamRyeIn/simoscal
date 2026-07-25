package com.simoscal.quickedit

import kotlin.math.abs
import org.json.JSONArray
import org.json.JSONObject

/**
 * The generic calibration editor's state and transitions — pure and JVM-testable.
 *
 * Same staging discipline as the boost editor: cell edits accumulate in a
 * [TablesUiState.draft] and Apply sends **one** `paste` op, so one deliberate
 * change is one journal entry and one undo point. The batch operations (fill,
 * add, multiply, interpolate) run on the draft here rather than as separate
 * engine ops for the same reason — they are ways of *composing* a proposal, not
 * separate things done to the bin.
 *
 * Restore is the exception and is not implemented here: only the journal knows
 * what a table held at session start, so restoring is a real `restore` op sent
 * to the engine (see [QuickEditViewModel.restoreTable]).
 */

/** One decoded breakpoint axis, as the bridge reports it. */
data class TableAxis(val units: String, val values: List<Double>)

/** A read-only description of one editable table. Mirrors `simoscal.tune.catalog.TableInfo`. */
data class TableSummary(
    val space: String,
    val name: String,
    val symbol: String?,
    val title: String?,
    val description: String,
    val uniqueidHex: String,
    val units: String,
    val rows: Int,
    val cols: Int,
    val ndim: Int,
    val reversible: Boolean,
    val isAxis: Boolean,
    val categories: List<String>,
) {

    /**
     * ``ID — Description`` — the naming form this project mandates everywhere.
     *
     * Never the ID alone and never the description alone: a bare `IP_PUT_SP` says
     * nothing to someone reading a change list, and a bare description does not
     * identify which of several similar tables was touched.
     */
    val idAndDescription: String
        get() {
            val id = symbol ?: uniqueidHex
            val text = description.ifBlank { title ?: "(no description)" }
            return "$id — $text"
        }

    companion object {
        fun fromJson(json: JSONObject): TableSummary {
            val shape = json.optJSONArray("shape")
            return TableSummary(
                space = json.optString("space", "base"),
                name = json.optString("name"),
                symbol = json.optStringOrNull("symbol"),
                title = json.optStringOrNull("title"),
                description = json.optString("description", ""),
                uniqueidHex = json.optString("uniqueid_hex", ""),
                units = json.optString("units", ""),
                rows = shape?.optInt(0) ?: 1,
                cols = shape?.optInt(1) ?: 1,
                ndim = json.optInt("ndim", 0),
                reversible = json.optBoolean("reversible", false),
                isAxis = json.optBoolean("is_axis", false),
                categories = json.stringList("categories"),
            )
        }
    }
}

/** A table plus its current decoded values and axes. */
data class TableDetail(
    val summary: TableSummary,
    val values: List<List<Double>>,
    val xAxis: TableAxis?,
    val yAxis: TableAxis?,
) {
    companion object {
        fun fromJson(json: JSONObject): TableDetail = TableDetail(
            summary = TableSummary.fromJson(json),
            values = json.grid("values"),
            xAxis = json.axis("x_axis"),
            yAxis = json.axis("y_axis"),
        )
    }
}

/** One cell of a table grid. */
data class CellRef(val row: Int, val col: Int)

/** What the engine encoded for a committed table edit. */
data class TableEditReceipt(
    val label: String,
    val quantized: Boolean,
    val maxAbsQuantization: Double,
    val warning: String,
    val encoded: List<List<Double>>,
)

data class TablesUiState(
    val catalog: List<TableSummary> = emptyList(),
    val query: String = "",
    val detail: TableDetail? = null,
    val draft: List<List<Double>> = emptyList(),
    val selection: Set<CellRef> = emptySet(),
    val loading: Boolean = false,
    val lastEdit: TableEditReceipt? = null,
    val notice: String? = null,
) {

    /** The catalog narrowed by [query], matched against ID, description, and units. */
    val visibleCatalog: List<TableSummary>
        get() {
            val needle = query.trim().lowercase()
            if (needle.isEmpty()) return catalog
            return catalog.filter { summary ->
                summary.idAndDescription.lowercase().contains(needle) ||
                    summary.name.lowercase().contains(needle) ||
                    summary.categories.any { it.lowercase().contains(needle) }
            }
        }

    val committed: List<List<Double>>
        get() = detail?.values.orEmpty()

    val dirty: Boolean
        get() = draft.size == committed.size &&
            draft.indices.any { row ->
                draft[row].size == committed[row].size &&
                    draft[row].indices.any { col -> abs(draft[row][col] - committed[row][col]) > 1e-12 }
            }

    /**
     * Apply is gated on reversibility, not only on dirtiness.
     *
     * A non-linear or raw-only table cannot be written back from physical units,
     * and the engine refuses such an edit outright. The editor therefore presents
     * it read-only rather than letting someone compose a proposal that can only
     * end in a refusal.
     */
    val canApply: Boolean
        get() = detail?.summary?.reversible == true && dirty

    /** The cells whose proposed value differs from the committed one. */
    val changedCells: List<CellRef>
        get() = draft.indices.flatMap { row ->
            val committedRow = committed.getOrNull(row).orEmpty()
            draft[row].indices
                .filter { col ->
                    val before = committedRow.getOrNull(col)
                    before != null && abs(draft[row][col] - before) > 1e-12
                }
                .map { col -> CellRef(row, col) }
        }

    /** Proposed minus committed for one cell, or null when either is missing. */
    fun delta(cell: CellRef): Double? {
        val proposed = draft.getOrNull(cell.row)?.getOrNull(cell.col) ?: return null
        val before = committed.getOrNull(cell.row)?.getOrNull(cell.col) ?: return null
        return proposed - before
    }
}

// ----------------------------------------------------------------- transitions

fun TablesUiState.withCatalog(tables: List<TableSummary>): TablesUiState =
    copy(catalog = tables, loading = false, notice = null)

/** Open a table: the draft starts as an exact copy of what the engine holds. */
fun TablesUiState.withDetail(loaded: TableDetail): TablesUiState = copy(
    detail = loaded,
    draft = loaded.values.map { it.toList() },
    selection = emptySet(),
    loading = false,
    lastEdit = null,
    notice = null,
)

fun TablesUiState.closingDetail(): TablesUiState =
    copy(detail = null, draft = emptyList(), selection = emptySet(), notice = null)

fun TablesUiState.togglingCell(cell: CellRef): TablesUiState =
    copy(selection = if (cell in selection) selection - cell else selection + cell, notice = null)

fun TablesUiState.selectingAll(): TablesUiState {
    val cells = buildSet {
        draft.indices.forEach { row -> draft[row].indices.forEach { col -> add(CellRef(row, col)) } }
    }
    return copy(selection = cells, notice = null)
}

fun TablesUiState.clearingSelection(): TablesUiState = copy(selection = emptySet(), notice = null)

fun TablesUiState.discardingDraft(): TablesUiState =
    copy(draft = committed.map { it.toList() }, notice = null)

/**
 * Set one cell from typed input.
 *
 * Validated, never coerced — the same rule the boost editor follows, for the same
 * reason: a stated number that is silently replaced by a different one is the
 * failure mode this whole library is built to avoid.
 */
fun TablesUiState.withTypedCell(cell: CellRef, value: Double): TablesUiState {
    val refusal = rejectValue(value) ?: monotonicRefusal(cell, value)
    if (refusal != null) return copy(notice = refusal)
    return copy(draft = draft.replacing(cell, value), notice = null)
}

/** Set every selected cell to one value. */
fun TablesUiState.fillingSelection(value: Double): TablesUiState =
    transformSelection("Fill") { value }

/** Add a signed offset to every selected cell. */
fun TablesUiState.offsettingSelection(delta: Double): TablesUiState =
    transformSelection("Offset") { it + delta }

/** Scale every selected cell. */
fun TablesUiState.scalingSelection(factor: Double): TablesUiState =
    transformSelection("Scale") { it * factor }

/**
 * Ramp the selected run linearly between its endpoints.
 *
 * Mirrors the engine's `interpolate`: the selection must lie on a single row or a
 * single column and be contiguous, and the two endpoints keep their values. The
 * rule is duplicated here rather than deferred to the engine so the refusal
 * arrives while the selection is on screen and can still be corrected.
 */
fun TablesUiState.interpolatingSelection(): TablesUiState {
    if (selection.size < 3) {
        return copy(notice = "Interpolate needs at least three selected cells.")
    }
    val rows = selection.map { it.row }.distinct()
    val cols = selection.map { it.col }.distinct()
    val onOneRow = rows.size == 1
    val onOneCol = cols.size == 1
    if (!onOneRow && !onOneCol) {
        return copy(notice = "Interpolate needs a selection on one row or one column.")
    }

    val indices = (if (onOneRow) selection.map { it.col } else selection.map { it.row }).sorted()
    if (indices.last() - indices.first() + 1 != indices.size) {
        return copy(notice = "Interpolate needs a contiguous run of cells.")
    }

    val cellAt: (Int) -> CellRef =
        if (onOneRow) { i -> CellRef(rows.first(), i) } else { i -> CellRef(i, cols.first()) }
    val start = draft.at(cellAt(indices.first())) ?: return this
    val end = draft.at(cellAt(indices.last())) ?: return this
    val span = indices.size - 1

    var grid = draft
    indices.forEachIndexed { step, index ->
        grid = grid.replacing(cellAt(index), start + (end - start) * step / span)
    }
    return copy(draft = grid, notice = null)
}

/** Fold a committed edit's encoded values back in, so the grid shows the bin's truth. */
fun TablesUiState.applied(receipt: TableEditReceipt): TablesUiState {
    val current = detail ?: return this
    val encoded = receipt.encoded.map { it.toList() }
    return copy(
        detail = current.copy(values = encoded),
        draft = encoded,
        lastEdit = receipt,
        notice = null,
    )
}

// --------------------------------------------------------------------- helpers

private fun TablesUiState.transformSelection(
    label: String,
    transform: (Double) -> Double,
): TablesUiState {
    if (selection.isEmpty()) return copy(notice = "$label needs a selection.")
    var grid = draft
    selection.sortedWith(compareBy({ it.row }, { it.col })).forEach { cell ->
        val before = grid.at(cell) ?: return@forEach
        val after = transform(before)
        rejectValue(after)?.let { return copy(notice = it) }
        grid = grid.replacing(cell, after)
    }
    val monotonic = monotonicRefusalFor(grid)
    if (monotonic != null) return copy(notice = monotonic)
    return copy(draft = grid, notice = null)
}

/** The value rules that hold for every table, matching the engine's own checks. */
private fun TablesUiState.rejectValue(value: Double): String? = when {
    value.isNaN() || value.isInfinite() -> "Enter a finite number."
    detail == null -> "No table is open."
    else -> null
}

private fun TablesUiState.monotonicRefusal(cell: CellRef, value: Double): String? =
    monotonicRefusalFor(draft.replacing(cell, value))

/**
 * An axis whose breakpoints do not strictly increase is refused here as well as
 * by the engine. Duplicated deliberately: the engine's refusal is the one that
 * protects the bin, and this one only makes it visible a keystroke earlier.
 */
private fun TablesUiState.monotonicRefusalFor(grid: List<List<Double>>): String? {
    if (detail?.summary?.isAxis != true) return null
    val flat = grid.flatten()
    val breaks = (1 until flat.size).any { flat[it] <= flat[it - 1] }
    return if (breaks) "Axis breakpoints must strictly increase." else null
}

private fun List<List<Double>>.at(cell: CellRef): Double? =
    getOrNull(cell.row)?.getOrNull(cell.col)

private fun List<List<Double>>.replacing(cell: CellRef, value: Double): List<List<Double>> =
    mapIndexed { row, cells ->
        if (row != cell.row) cells
        else cells.mapIndexed { col, existing -> if (col == cell.col) value else existing }
    }

internal fun JSONObject.doubleList(key: String): List<Double> {
    val array = optJSONArray(key) ?: return emptyList()
    return (0 until array.length()).map { array.optDouble(it) }
}

internal fun List<Double>.toJsonArray(): JSONArray = JSONArray().also { array ->
    forEach { array.put(it) }
}

@JvmName("gridToJsonArray")
internal fun List<List<Double>>.toJsonArray(): JSONArray = JSONArray().also { array ->
    forEach { row -> array.put(row.toJsonArray()) }
}

internal fun JSONObject.optStringOrNull(key: String): String? =
    if (isNull(key)) null else optString(key).ifEmpty { null }

internal fun JSONObject.stringList(key: String): List<String> {
    val array: JSONArray = optJSONArray(key) ?: return emptyList()
    return (0 until array.length()).map { array.optString(it) }
}

/**
 * Read `values` as a grid, normalizing the 1-D case.
 *
 * A vector or scalar table serializes as a flat list, and every consumer here
 * wants `[row][col]`. Normalizing once at the boundary keeps that shape
 * assumption out of the editor, where a stray `values[0][0]` on a flat list
 * would be a crash rather than a wrong number.
 */
internal fun JSONObject.grid(key: String): List<List<Double>> {
    val array = optJSONArray(key) ?: return emptyList()
    val rows = (0 until array.length()).map { index ->
        when (val row = array.opt(index)) {
            is JSONArray -> (0 until row.length()).map { row.optDouble(it) }
            else -> null
        }
    }
    if (rows.all { it != null }) return rows.filterNotNull()
    return listOf((0 until array.length()).map { array.optDouble(it) })
}

internal fun JSONObject.axis(key: String): TableAxis? {
    val node = optJSONObject(key) ?: return null
    val values = node.optJSONArray("values") ?: return null
    return TableAxis(
        units = node.optString("units", ""),
        values = (0 until values.length()).map { values.optDouble(it) },
    )
}
