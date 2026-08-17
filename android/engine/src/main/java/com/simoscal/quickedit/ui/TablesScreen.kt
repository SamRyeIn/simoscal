package com.simoscal.quickedit.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.clickable
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
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.simoscal.quickedit.CellRef
import com.simoscal.quickedit.HeatColor
import com.simoscal.quickedit.HeatScale
import com.simoscal.quickedit.Mode
import com.simoscal.quickedit.display
import com.simoscal.quickedit.QuickEditViewModel
import com.simoscal.quickedit.TableSummary
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
        Text("Tables", style = MaterialTheme.typography.headlineSmall, fontWeight = FontWeight.Bold)

        SessionProvenanceCard(binName = binName, shortHash = shortHash, advanced = advanced)

        OutlinedTextField(
            value = query,
            onValueChange = onQueryChanged,
            label = { Text("Search by ID, description, or category") },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )

        if (loading && summaries.isEmpty()) {
            Text("Reading the table catalog…", style = MaterialTheme.typography.bodyMedium)
        }

        LazyColumn(verticalArrangement = Arrangement.spacedBy(8.dp)) {
            items(summaries, key = { "${it.space}/${it.name}" }) { summary ->
                Card(
                    modifier = Modifier
                        .fillMaxWidth()
                        .clickable { onOpen(summary) },
                ) {
                    Column(modifier = Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(2.dp)) {
                        // ID — Description, always both: an ID alone means nothing
                        // in a change list, and a description alone does not say
                        // which of several similar tables was touched.
                        Text(summary.idAndDescription, style = MaterialTheme.typography.bodyMedium)
                        Text(
                            buildString {
                                append("${summary.rows}×${summary.cols}")
                                if (summary.units.isNotBlank()) append(" · ${summary.units}")
                                if (summary.space != "base") append(" · ${summary.space}")
                                if (summary.isAxis) append(" · axis")
                                if (!summary.reversible) append(" · read-only")
                            },
                            style = MaterialTheme.typography.bodySmall,
                        )
                    }
                }
            }
        }
    }
}

@Composable
private fun TableEditor(viewModel: QuickEditViewModel, advanced: Boolean) {
    val state by viewModel.state.collectAsStateWithLifecycle()
    val tables = state.tables
    val detail = tables.detail ?: return
    val summary = detail.summary

    var editingCell by remember { mutableStateOf<CellRef?>(null) }
    var batch by remember { mutableStateOf<BatchOperation?>(null) }
    var intent by remember { mutableStateOf("") }

    Column(
        modifier = Modifier
            .fillMaxWidth()
            .verticalScroll(rememberScrollState())
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            OutlinedButton(onClick = viewModel::onTableClosed) { Text("Back") }
            Text(
                summary.units.ifBlank { "no units" },
                style = MaterialTheme.typography.labelLarge,
                modifier = Modifier.padding(top = 12.dp),
            )
        }

        Text(summary.idAndDescription, style = MaterialTheme.typography.titleMedium)

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
            xAxisValues = detail.xAxis?.values.orEmpty(),
            yAxisValues = detail.yAxis?.values.orEmpty(),
            editable = tables.writable,
            onCellLongPress = { cell -> viewModel.onCellToggled(cell) },
            onCellTap = { cell -> if (tables.writable) editingCell = cell else Unit },
        )

        Text(
            "Tap a cell to type a value · long-press to select it for a batch operation. " +
                "Fill shades low to high across this table; a selected cell is outlined " +
                "in blue and a changed one in its accent, with its old value beneath. " +
                "${tables.selection.size} selected, ${tables.changedCells.size} changed.",
            style = MaterialTheme.typography.bodySmall,
        )

        if (tables.writable) {
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.fillMaxWidth()) {
                OutlinedButton(onClick = viewModel::onSelectAllCells) { Text("All") }
                OutlinedButton(onClick = viewModel::onClearSelection) { Text("None") }
                OutlinedButton(onClick = viewModel::onInterpolateSelection) { Text("Ramp") }
            }
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.fillMaxWidth()) {
                BatchOperation.values().forEach { operation ->
                    OutlinedButton(onClick = { batch = operation }) { Text(operation.label) }
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
                Button(
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
                OutlinedButton(onClick = viewModel::onTableDiscard, enabled = tables.dirty) { Text("Discard") }
                if (advanced) {
                    // Restore goes to the engine, not to a local copy: only the
                    // journal knows what this table held when the session opened.
                    // Disabled while the grid is dirty — it would overwrite the
                    // staged proposal with the session-start values.
                    OutlinedButton(
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
            Card {
                Column(modifier = Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                    Text("Applied", style = MaterialTheme.typography.titleSmall)
                    Text(
                        if (receipt.quantized) {
                            "Quantized: the encoding moved a value by up to " +
                                "${receipt.maxAbsQuantization.display()} ${summary.units}"
                        } else {
                            "Stored exactly as requested."
                        },
                        style = MaterialTheme.typography.bodyMedium,
                    )
                    if (receipt.warning.isNotBlank()) {
                        Text(receipt.warning, style = MaterialTheme.typography.bodySmall)
                    }
                }
            }
        }

        // Both re-read this grid from the engine on success, so they are refused
        // while a proposal is staged rather than silently replacing it.
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            OutlinedButton(
                onClick = viewModel::undo,
                enabled = state.canUndo && state.canMutateSession,
            ) { Text("Undo") }
            OutlinedButton(
                onClick = viewModel::redo,
                enabled = state.canRedo && state.canMutateSession,
            ) { Text("Redo") }
        }

        state.dirtyDraftRefusal?.let { reason ->
            Text(reason, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.error)
        }
    }

    editingCell?.let { cell ->
        NumericEntryDialog(
            title = "Row ${cell.row + 1}, column ${cell.col + 1}",
            supporting = buildString {
                append(summary.units.ifBlank { "physical units" })
                val before = tables.committed.getOrNull(cell.row)?.getOrNull(cell.col)
                if (before != null) append(" · currently ${before.display()}")
            },
            initial = tables.draft.getOrNull(cell.row)?.getOrNull(cell.col)?.let { it.display() } ?: "",
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
            supporting = operation.supporting,
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

private enum class BatchOperation(val label: String, val supporting: String, val initial: String) {
    FILL("Fill", "Set every selected cell to this value.", ""),
    OFFSET("Offset", "Add this signed amount to every selected cell.", "0"),
    SCALE("Scale", "Multiply every selected cell by this factor.", "1.0"),
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
    xAxisValues: List<Double>,
    yAxisValues: List<Double>,
    editable: Boolean,
    onCellTap: (CellRef) -> Unit,
    onCellLongPress: (CellRef) -> Unit,
) {
    val selectedBorder = MaterialTheme.colorScheme.primary
    val changedBorder = MaterialTheme.colorScheme.tertiary
    val plainBorder = MaterialTheme.colorScheme.outlineVariant
    val surface = MaterialTheme.colorScheme.surface
    val onSurface = MaterialTheme.colorScheme.onSurface

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
                    Row {
                        // Corner spacer, so the x-axis header lines up with the grid.
                        if (yAxisValues.isNotEmpty()) AxisCell("")
                        xAxisValues.forEach { value -> AxisCell(value.display()) }
                    }
                }
                values.indices.forEach { row ->
                    Row {
                        if (yAxisValues.isNotEmpty()) {
                            AxisCell(yAxisValues.getOrNull(row)?.let { it.display() } ?: "")
                        }
                        values[row].indices.forEach { col ->
                            val cell = CellRef(row, col)
                            val proposed = values[row][col]
                            val before = committed.getOrNull(row)?.getOrNull(col)
                            val changed = before != null && abs(proposed - before) > 1e-12
                            val selected = cell in selection
                            val heat = scale.colorFor(proposed)
                            GridCell(
                                proposed = proposed,
                                before = if (changed) before else null,
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

        HeatLegend(scale)
    }
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
private fun HeatLegend(scale: HeatScale) {
    if (scale.flat) {
        Text(
            "Every cell holds the same value, so there is no shape to colour.",
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
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
            scale.min.display(),
            style = MaterialTheme.typography.labelSmall,
            fontFamily = FontFamily.Monospace,
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
            scale.max.display(),
            style = MaterialTheme.typography.labelSmall,
            fontFamily = FontFamily.Monospace,
        )
    }
}

@Composable
private fun AxisCell(text: String) {
    Text(
        text,
        style = MaterialTheme.typography.labelSmall,
        fontFamily = FontFamily.Monospace,
        textAlign = TextAlign.Center,
        modifier = Modifier
            .width(72.dp)
            .padding(4.dp),
    )
}

@OptIn(ExperimentalFoundationApi::class)
@Composable
private fun GridCell(
    proposed: Double,
    before: Double?,
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
            .width(72.dp)
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
        Text(
            proposed.display(),
            style = MaterialTheme.typography.labelSmall,
            fontFamily = FontFamily.Monospace,
            color = ink,
        )
        if (before != null) {
            Text(
                // Same ink, dimmed: the fill under it can be any colour on the
                // ramp, so onSurfaceVariant is not reliably legible here.
                "was ${before.display()}",
                style = MaterialTheme.typography.labelSmall,
                fontFamily = FontFamily.Monospace,
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

    Card(colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceVariant)) {
        Column(modifier = Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(2.dp)) {
            Text("Proposed change", style = MaterialTheme.typography.titleSmall)
            Text(
                "${tables.changedCells.size} cell(s), largest delta " +
                    "${largest.display("%+.6g")} ${tables.detail?.summary?.units.orEmpty()}",
                style = MaterialTheme.typography.bodyMedium,
            )
            Text(
                "Applying sends the whole grid as one paste op — one journal entry, " +
                    "one undo point.",
                style = MaterialTheme.typography.bodySmall,
            )
        }
    }
}

/**
 * Shared provenance block for Tables and Boost: which bin the live session
 * came from, so a person mid-edit is never left guessing which file they are
 * actually working against.
 */
@Composable
internal fun SessionProvenanceCard(binName: String?, shortHash: String?, advanced: Boolean) {
    Card {
        Column(modifier = Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Text("Session bin", style = MaterialTheme.typography.titleSmall)
            Text(binName ?: "Unknown", style = MaterialTheme.typography.bodyMedium)
            if (advanced && shortHash != null) {
                Text("SHA-256 $shortHash", style = MaterialTheme.typography.bodySmall)
            }
        }
    }
}
