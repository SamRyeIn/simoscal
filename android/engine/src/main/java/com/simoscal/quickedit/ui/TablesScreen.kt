package com.simoscal.quickedit.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.TextUnit
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.simoscal.quickedit.CellRef
import com.simoscal.quickedit.CHANGE_EPSILON
import com.simoscal.quickedit.HeatColor
import com.simoscal.quickedit.HeatScale
import com.simoscal.quickedit.Mode
import com.simoscal.quickedit.displayExact
import com.simoscal.quickedit.formatSigned
import com.simoscal.quickedit.QuickEditViewModel
import com.simoscal.quickedit.TableAxis
import com.simoscal.quickedit.TableDetail
import com.simoscal.quickedit.TableSummary
import com.simoscal.quickedit.ValueFormat
import com.simoscal.quickedit.rampColor
import kotlin.math.abs

/**
 * The generic calibration editor: browse the curated table set, edit one table.
 *
 * The catalog is the profile-resolved set, not the XDF's ~3,800 tables — every
 * table reachable here came through a profile map, so its description, units, and
 * guard tags are in force. A stranger table with a surprising layout is simply
 * not offered.
 *
 * Like the boost editor, changes are staged: cell edits and the batch operations
 * build a draft, and Apply sends one `paste` op so one deliberate change is one
 * journal entry and one undo point.
 */
@Composable
fun TablesScreen(viewModel: QuickEditViewModel) {
    val state by viewModel.state.collectAsStateWithLifecycle()
    val advanced = state.mode == Mode.ADVANCED
    val tables = state.tables

    LaunchedEffect(state.sessionId) {
        if (state.sessionId != null && tables.catalog.isEmpty()) viewModel.loadCatalog()
    }

    val detail = tables.detail
    if (detail == null) {
        TableBrowser(
            query = tables.query,
            loading = tables.loading,
            summaries = tables.visibleCatalog,
            binName = state.bin?.displayName,
            shortHash = state.bin?.shortHash,
            advanced = advanced,
            onQueryChanged = viewModel::onTableQueryChanged,
            onOpen = viewModel::openTable,
        )
        return
    }

    TableEditor(viewModel = viewModel, advanced = advanced)
}

@Composable
private fun TableBrowser(
    query: String,
    loading: Boolean,
    summaries: List<TableSummary>,
    binName: String?,
    shortHash: String?,
    advanced: Boolean,
    onQueryChanged: (String) -> Unit,
    onOpen: (TableSummary) -> Unit,
) {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        ScreenHeader(kicker = "Every table, in physical units", title = "Tables")

        SessionProvenanceCard(binName = binName, shortHash = shortHash, advanced = advanced)

        OutlinedTextField(
            value = query,
            onValueChange = onQueryChanged,
            label = { Text("Search by ID, description, or category") },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )

        if (loading && summaries.isEmpty()) {
            Caption("Reading the table catalog…")
        }

        LazyColumn(verticalArrangement = Arrangement.spacedBy(8.dp)) {
            items(summaries, key = { "${it.space}/${it.name}" }) { summary ->
                Panel(padding = 12.dp, spacing = 2.dp, onClick = { onOpen(summary) }) {
                    // ID and description, always both: an ID alone means nothing
                    // in a change list, and a description alone does not say which
                    // of several similar tables was touched. Set the way the video
                    // sets them — monospace ID over the description in prose.
                    TableIdentity(id = summary.id, describedAs = summary.describedAs)
                    // What the table is, in units: the line that separates
                    // two similarly-named maps before one is opened.
                    Caption(summary.signature.ifBlank { summary.unitsText })
                    Text(
                        buildString {
                            append("${summary.rows}×${summary.cols}")
                            if (summary.space != "base") append(" · ${summary.space}")
                            if (summary.isAxis) append(" · axis")
                            if (!summary.reversible) append(" · read-only")
                        },
                        style = PromoType.figureSmall,
                        color = PromoPalette.TextFaint,
                    )
                }
            }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun TableEditor(viewModel: QuickEditViewModel, advanced: Boolean) {
    val state by viewModel.state.collectAsStateWithLifecycle()
    val tables = state.tables
    val detail = tables.detail ?: return
    val summary = detail.summary

    var editingCell by remember { mutableStateOf<CellRef?>(null) }
    var batch by remember { mutableStateOf<BatchOperation?>(null) }
    var intent by remember { mutableStateOf("") }

    // One precision for every cell of this table, drawn from the proposal *and*
    // what it replaces: a cell shows its old value underneath, and the two have
    // to be rounded the same way or the comparison the reader is making is
    // against two differently-rounded numbers.
    val cellFormat = remember(tables.draft, tables.committed) {
        ValueFormat.of(tables.draft.flatten(), tables.committed.flatten())
    }

    Column(
        modifier = Modifier
            .fillMaxWidth()
            .verticalScroll(rememberScrollState())
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            PromoOutlinedButton(onClick = viewModel::onTableClosed) { Text("Back") }
            Box(modifier = Modifier.weight(1f))
            // Same FilterChip idiom as the Simple/Advanced toggle in the top bar.
            FilterChip(
                selected = state.heatmap,
                onClick = { viewModel.onHeatmapChanged(!state.heatmap) },
                label = { Text(if (state.heatmap) "Colour on" else "Colour off") },
                colors = promoFilterChipColors(),
            )
        }

        TableIdentity(id = summary.id, describedAs = summary.describedAs, idSize = 20.sp)

        // What the table is, under what it is called. The title says which
        // calibration this is; this says what its numbers mean and what they are
        // scheduled against — "hPa vs. Engine speed [rpm] and Airmass per stroke
        // [mg/stk]". Without it a grid of numbers is only a grid of numbers.
        Caption(summary.signature.ifBlank { summary.unitsText })

        if (!tables.writable) {
            NoticeCard(
                title = "Read-only",
                body = if (summary.owner.isNotEmpty()) {
                    "This table is written only through ${summary.owner}. It carries " +
                        "structural rules a generic grid edit cannot honour, so the " +
                        "engine refuses one — it is shown here for reference only."
                } else {
                    "This table's scaling is non-linear or has no embedded data, so a " +
                        "physical-unit write cannot round-trip. The engine refuses generic " +
                        "edits to it, and it is shown here for reference only."
                },
            )
        }

        TableGrid(
            values = tables.draft,
            committed = tables.committed,
            selection = tables.selection,
            xAxis = detail.xAxis,
            yAxis = detail.yAxis,
            cellFormat = cellFormat,
            editable = tables.writable,
            heatmap = state.heatmap,
            onCellLongPress = { cell -> viewModel.onCellToggled(cell) },
            onCellTap = { cell -> if (tables.writable) editingCell = cell else Unit },
        )

        Caption(
            buildString {
                append("Tap a cell to type a value · long-press to select it for a batch operation. ")
                if (state.heatmap) append("Fill shades low to high across this table; a ")
                else append("A ")
                append("selected cell is outlined in blue and a changed one in orange, ")
                append("with its old value beneath. ")
                append("${tables.selection.size} selected, ${tables.changedCells.size} changed.")
            }
        )

        if (tables.writable) {
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.fillMaxWidth()) {
                PromoOutlinedButton(onClick = viewModel::onSelectAllCells) { Text("All") }
                PromoOutlinedButton(onClick = viewModel::onClearSelection) { Text("None") }
                PromoOutlinedButton(onClick = viewModel::onInterpolateSelection) { Text("Ramp") }
            }
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.fillMaxWidth()) {
                BatchOperation.values().forEach { operation ->
                    PromoOutlinedButton(onClick = { batch = operation }) { Text(operation.label) }
                }
            }

            ChangeSummaryCard(viewModel)

            OutlinedTextField(
                value = intent,
                onValueChange = { intent = it },
                label = { Text("Why this change (recorded in the journal)") },
                modifier = Modifier.fillMaxWidth(),
            )

            Row(horizontalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.fillMaxWidth()) {
                PromoButton(
                    onClick = {
                        viewModel.applyTableDraft(
                            intent.ifBlank { "edit ${summary.idAndDescription} from Quick Edit" }
                        )
                        intent = ""
                    },
                    enabled = tables.canApply && !state.busy,
                ) {
                    Text(if (tables.dirty) "Apply" else "No change")
                }
                PromoOutlinedButton(onClick = viewModel::onTableDiscard, enabled = tables.dirty) { Text("Discard") }
                if (advanced) {
                    // Restore goes to the engine, not to a local copy: only the
                    // journal knows what this table held when the session opened.
                    // Disabled while the grid is dirty — it would overwrite the
                    // staged proposal with the session-start values.
                    PromoOutlinedButton(
                        onClick = {
                            viewModel.restoreTable("restore ${summary.idAndDescription} to its session-start values")
                        },
                        enabled = state.canMutateSession,
                    ) { Text("Restore") }
                }
            }
        }

        tables.notice?.let { notice -> NoticeCard(title = "Not applied", body = notice, emphasise = true) }

        tables.lastEdit?.let { receipt ->
            Panel(tone = PanelTone.Accent) {
                PanelTitle("Applied", tone = PanelTone.Accent)
                Text(
                    if (receipt.quantized) {
                        "Quantized: the encoding moved a value by up to " +
                            "${receipt.maxAbsQuantization.displayExact()} ${summary.unitsText}"
                    } else {
                        "Stored exactly as requested."
                    },
                    style = MaterialTheme.typography.bodyMedium,
                )
                if (receipt.warning.isNotBlank()) {
                    Caption(receipt.warning)
                }
            }
        }

        // Both re-read this grid from the engine on success, so they are refused
        // while a proposal is staged rather than silently replacing it.
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            PromoOutlinedButton(
                onClick = viewModel::undo,
                enabled = state.canUndo && state.canMutateSession,
            ) { Text("Undo") }
            PromoOutlinedButton(
                onClick = viewModel::redo,
                enabled = state.canRedo && state.canMutateSession,
            ) { Text("Redo") }
        }

        state.dirtyDraftRefusal?.let { reason ->
            Caption(reason, color = PromoPalette.Danger)
        }
    }

    editingCell?.let { cell ->
        NumericEntryDialog(
            title = "Row ${cell.row + 1}, column ${cell.col + 1}",
            supporting = buildString {
                // Where this cell sits, in the quantities the table is scheduled
                // against. Row and column indices identify a cell; only the
                // breakpoints say what operating point it governs, which is the
                // thing being decided when a number is typed.
                breakpointDescription(cell, detail)?.let { appendLine(it) }
                append("Value in ${summary.unitsText}")
                val before = tables.committed.getOrNull(cell.row)?.getOrNull(cell.col)
                if (before != null) append(" · currently ${before.displayExact()}")
            },
            // Full precision, unlike the grid: seeding the field with the
            // rounded text would mean opening a cell and pressing Set — changing
            // nothing — silently wrote a different number than it held.
            initial = tables.draft.getOrNull(cell.row)?.getOrNull(cell.col)?.displayExact() ?: "",
            onDismiss = { editingCell = null },
            onConfirm = { value ->
                viewModel.onCellTyped(cell, value)
                editingCell = null
            },
        )
    }

    batch?.let { operation ->
        NumericEntryDialog(
            title = "${operation.label} ${tables.selection.size} selected cell(s)",
            supporting = if (operation.inTableUnits) {
                "${operation.supporting} In ${summary.unitsText}."
            } else {
                operation.supporting
            },
            initial = operation.initial,
            onDismiss = { batch = null },
            onConfirm = { value ->
                when (operation) {
                    BatchOperation.FILL -> viewModel.onFillSelection(value)
                    BatchOperation.OFFSET -> viewModel.onOffsetSelection(value)
                    BatchOperation.SCALE -> viewModel.onScaleSelection(value)
                }
                batch = null
            },
        )
    }
}

/**
 * The operating point one cell governs: `Engine speed [rpm] 4000 · Airmass per
 * stroke [mg/stk] 250`, or null for a table with no breakpoint axes.
 */
private fun breakpointDescription(cell: CellRef, detail: TableDetail): String? {
    val parts = listOfNotNull(
        detail.xAxis?.at(cell.col),
        detail.yAxis?.at(cell.row),
    )
    return parts.ifEmpty { null }?.joinToString(" · ")
}

private fun TableAxis.at(index: Int): String? {
    val value = values.getOrNull(index) ?: return null
    val name = label.ifBlank { symbol ?: "Axis" }
    return "$name ${ValueFormat.of(values).format(value)}"
}

private enum class BatchOperation(
    val label: String,
    val supporting: String,
    val initial: String,
    /** Whether the number typed carries the table's unit — a factor does not. */
    val inTableUnits: Boolean,
) {
    FILL("Fill", "Set every selected cell to this value.", "", true),
    OFFSET("Offset", "Add this signed amount to every selected cell.", "0", true),
    SCALE("Scale", "Multiply every selected cell by this factor.", "1.0", false),
}

/**
 * A table's ID over its description, the way the video sets a table: the symbol
 * in a monospace face because it is an identifier, the English under it in prose
 * because it is a sentence.
 *
 * Both halves always, per the project's naming rule — this only decides how the
 * two are typeset, never whether one can be dropped.
 */
@Composable
internal fun TableIdentity(id: String, describedAs: String, idSize: TextUnit = 15.sp) {
    Column(verticalArrangement = Arrangement.spacedBy(2.dp)) {
        Text(
            id,
            style = PromoType.identifier.copy(fontSize = idSize),
            color = PromoPalette.Text,
        )
        Text(
            describedAs,
            style = MaterialTheme.typography.bodySmall,
            fontStyle = FontStyle.Italic,
            color = PromoPalette.TextDim,
        )
    }
}

/** [HeatColor] as a Compose colour. */
private fun HeatColor.compose(): Color = Color(red, green, blue)

/**
 * The grid, colour-coded by value, with the source value under every changed cell.
 *
 * Showing before-and-after in place rather than as a separate diff view is
 * deliberate: the decision a person is making is "is this new number right for
 * this cell", and that question is unanswerable without the old number beside it.
 *
 * Three things compete for a cell's appearance — its value, whether it is
 * selected, and whether it has been changed — and they are given separate
 * channels rather than fighting over one. Value owns the *fill*, because it is
 * the property every cell has and the one the shape is read from. Selection and
 * change own the *border*, because they are sparse and transient. Before the
 * heatmap, all three shared the fill, so shading a table by value would have
 * silently cost the ability to see what you had just edited.
 */
@Composable
private fun TableGrid(
    values: List<List<Double>>,
    committed: List<List<Double>>,
    selection: Set<CellRef>,
    xAxis: TableAxis?,
    yAxis: TableAxis?,
    cellFormat: ValueFormat,
    editable: Boolean,
    heatmap: Boolean,
    onCellTap: (CellRef) -> Unit,
    onCellLongPress: (CellRef) -> Unit,
) {
    // Blue for a selection, orange for a change — the palette's own division of
    // labour: `accent_2` is the cool "this is the one you picked" and `accent` is
    // reserved throughout the app and the video for the thing that moved.
    val selectedBorder = PromoPalette.Accent2
    val changedBorder = PromoPalette.Accent
    val plainBorder = PromoPalette.RuleFaint
    val surface = PromoPalette.BgAlt
    val onSurface = PromoPalette.Text

    val xAxisValues = xAxis?.values.orEmpty()
    val yAxisValues = yAxis?.values.orEmpty()

    // Each axis is its own quantity, so each picks its own precision — there is
    // no reason for an rpm ladder to carry a lambda grid's decimals, and one
    // shared precision would round whichever axis lost.
    val xFormat = remember(xAxisValues) { ValueFormat.of(xAxisValues) }
    val yFormat = remember(yAxisValues) { ValueFormat.of(yAxisValues) }

    // Scaled against the draft, not the committed values: the colours should show
    // the shape of the table being proposed, which is the thing under review.
    val scale = remember(values) { HeatScale.of(values) }

    // No inner vertical scroll: the screen already scrolls vertically, and nesting
    // a second one on the same axis makes which container reacts to a swipe a
    // coin toss. Horizontal scrolling is a different axis and composes cleanly.
    Column(modifier = Modifier.fillMaxWidth()) {
        Row(modifier = Modifier.horizontalScroll(rememberScrollState())) {
            Column {
                if (xAxisValues.isNotEmpty()) {
                    // The x-axis label sits over the breakpoints it names, so
                    // which quantity runs across is answered where the question
                    // is asked rather than in prose further up the screen.
                    Row {
                        if (yAxisValues.isNotEmpty()) Box(Modifier.width(HEADER_WIDTH))
                        AxisTitle(
                            text = xAxis?.label.orEmpty(),
                            width = CELL_WIDTH * xAxisValues.size,
                        )
                    }
                    Row {
                        // The corner carries the y-axis label, beside the rows it
                        // names, for the same reason.
                        if (yAxisValues.isNotEmpty()) {
                            AxisTitle(text = yAxis?.label.orEmpty(), width = HEADER_WIDTH)
                        }
                        xAxisValues.forEach { value -> AxisCell(xFormat.format(value)) }
                    }
                }
                values.indices.forEach { row ->
                    Row {
                        if (yAxisValues.isNotEmpty()) {
                            AxisCell(
                                yAxisValues.getOrNull(row)?.let { yFormat.format(it) } ?: "",
                                width = HEADER_WIDTH,
                            )
                        }
                        values[row].indices.forEach { col ->
                            val cell = CellRef(row, col)
                            val proposed = values[row][col]
                            val before = committed.getOrNull(row)?.getOrNull(col)
                            val changed = before != null && abs(proposed - before) > CHANGE_EPSILON
                            val selected = cell in selection
                            // Selection and change keep their borders when the
                            // shading is off: they report what you did, which is
                            // not decoration to be switched away.
                            val heat = if (heatmap) scale.colorFor(proposed) else null
                            GridCell(
                                proposed = cellFormat.format(proposed),
                                before = if (changed) cellFormat.format(before!!) else null,
                                background = heat?.compose() ?: surface,
                                // Ink follows the fill it lands on, not the theme:
                                // one fixed colour cannot stay legible across a
                                // ramp that runs dark blue to amber.
                                ink = heat?.let { if (it.prefersDarkInk) Color.Black else Color.White }
                                    ?: onSurface,
                                border = when {
                                    selected -> selectedBorder
                                    changed -> changedBorder
                                    else -> plainBorder
                                },
                                borderWidth = if (selected || changed) 2.dp else 0.5.dp,
                                editable = editable,
                                onTap = { onCellTap(cell) },
                                onLongPress = { onCellLongPress(cell) },
                            )
                        }
                    }
                }
            }
        }

        if (heatmap) HeatLegend(scale, cellFormat)
    }
}

/**
 * One data cell's width, and the wider row-header column beside it.
 *
 * The header column holds the y-axis label as well as the breakpoints, and the
 * longest name in the curated set — "Compressor-inlet air temperature" — breaks
 * at its hyphen and reads as "Compressor-in / let air" in anything narrower.
 * 132dp keeps that word whole; the grid scrolls horizontally anyway, so the
 * column costs nothing a person cannot swipe past.
 */
private val CELL_WIDTH = 72.dp
private val HEADER_WIDTH = 132.dp

/** An axis's `Quantity [unit]` label, wrapped into the space it labels. */
@Composable
private fun AxisTitle(text: String, width: Dp) {
    Text(
        text,
        style = MaterialTheme.typography.labelSmall,
        fontWeight = FontWeight.Bold,
        color = PromoPalette.TextDim,
        textAlign = TextAlign.Center,
        modifier = Modifier
            .width(width)
            .padding(horizontal = 4.dp, vertical = 2.dp),
    )
}

/**
 * What the colours mean for this table.
 *
 * Without the endpoints printed, a relative scale is unreadable: the same red
 * means 2400 hPa on one table and 0.85 lambda on the next, and nothing on screen
 * would say which. The flat case is stated outright rather than left as an
 * absence, so "no colours" cannot be mistaken for a rendering failure.
 */
@Composable
private fun HeatLegend(scale: HeatScale, format: ValueFormat) {
    if (scale.flat) {
        Caption(
            "Every cell holds the same value, so there is no shape to colour.",
            modifier = Modifier.padding(top = 8.dp),
        )
        return
    }

    Row(
        modifier = Modifier
            .fillMaxWidth()
            .padding(top = 8.dp),
        horizontalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        Text(
            format.format(scale.min),
            style = PromoType.figureSmall,
            color = PromoPalette.TextFaint,
        )
        Box(
            modifier = Modifier
                .weight(1f)
                .height(10.dp)
                .background(
                    Brush.horizontalGradient(
                        // Sampled from the same function the cells use, so the key
                        // cannot drift from what the grid actually paints.
                        (0..10).map { rampColor(it / 10.0).compose() }
                    ),
                    RoundedCornerShape(4.dp),
                ),
        )
        Text(
            format.format(scale.max),
            style = PromoType.figureSmall,
            color = PromoPalette.TextFaint,
        )
    }
}

@Composable
private fun AxisCell(text: String, width: Dp = CELL_WIDTH) {
    Text(
        text,
        style = PromoType.figureSmall,
        color = PromoPalette.TextFaint,
        textAlign = TextAlign.Center,
        modifier = Modifier
            .width(width)
            .padding(4.dp),
    )
}

@OptIn(ExperimentalFoundationApi::class)
@Composable
private fun GridCell(
    proposed: String,
    before: String?,
    background: Color,
    ink: Color,
    border: Color,
    borderWidth: Dp,
    editable: Boolean,
    onTap: () -> Unit,
    onLongPress: () -> Unit,
) {
    Column(
        modifier = Modifier
            .width(CELL_WIDTH)
            .padding(1.dp)
            .background(background, RoundedCornerShape(4.dp))
            .border(borderWidth, border, RoundedCornerShape(4.dp))
            .then(
                if (editable) {
                    Modifier.combinedClickable(
                        onClick = onTap,
                        onClickLabel = "Edit cell",
                        onLongClick = onLongPress,
                        onLongClickLabel = "Select cell",
                    )
                } else {
                    Modifier
                }
            )
            .padding(4.dp),
    ) {
        Text(proposed, style = PromoType.figureSmall, color = ink)
        if (before != null) {
            Text(
                // Same ink, dimmed: the fill under it can be any colour on the
                // ramp, so a fixed dim grey is not reliably legible here.
                "was $before",
                style = PromoType.figureSmall,
                color = ink.copy(alpha = 0.75f),
            )
        }
    }
}

/** How far the proposal moves the table — the number reviewed before Apply. */
@Composable
private fun ChangeSummaryCard(viewModel: QuickEditViewModel) {
    val state by viewModel.state.collectAsStateWithLifecycle()
    val tables = state.tables
    if (!tables.dirty) return

    val deltas = tables.changedCells.mapNotNull { tables.delta(it) }
    val largest = deltas.maxByOrNull { abs(it) } ?: 0.0
    // The deltas' own precision, not the grid's: a change can be far smaller
    // than the values it moves, and rounding it to the grid would report the
    // most consequential number on this card as "+0".
    val deltaFormat = remember(deltas) { ValueFormat.of(deltas) }

    Panel(tone = PanelTone.Accent, padding = 12.dp, spacing = 2.dp) {
        PanelTitle("Proposed change", tone = PanelTone.Accent)
        Text(
            "${tables.changedCells.size} cell(s), largest delta " +
                "${deltaFormat.formatSigned(largest)} " +
                (tables.detail?.summary?.unitsText.orEmpty()),
            style = MaterialTheme.typography.bodyMedium,
        )
        Caption(
            "Applying sends the whole grid as one paste op — one journal entry, " +
                "one undo point."
        )
    }
}

/**
 * Shared provenance block for Tables and Boost: which bin the live session
 * came from, so a person mid-edit is never left guessing which file they are
 * actually working against.
 */
@Composable
internal fun SessionProvenanceCard(binName: String?, shortHash: String?, advanced: Boolean) {
    Panel(padding = 12.dp) {
        Kicker("Session bin", color = PromoPalette.TextFaint)
        Identifier(binName ?: "Unknown")
        if (advanced && shortHash != null) {
            Text(
                "SHA-256 $shortHash",
                style = PromoType.figureSmall,
                color = PromoPalette.TextFaint,
            )
        }
    }
}
