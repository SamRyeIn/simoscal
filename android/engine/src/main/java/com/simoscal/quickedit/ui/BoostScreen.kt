package com.simoscal.quickedit.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.AssistChip
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.foundation.text.KeyboardOptions
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.simoscal.quickedit.Mode
import com.simoscal.quickedit.display
import com.simoscal.quickedit.QuickEditViewModel
import com.simoscal.quickedit.SLOT_IDS
import kotlin.math.abs

/**
 * The boost editor: the switch patch's five map slots against the base ceiling.
 *
 * Edits are *staged*. A drag and the batch controls all move a local draft, and
 * only **Apply** sends it to the engine as one journaled op. That is why the
 * Apply button carries an intent line: every calibration change in this project
 * records why it was made, and the phone is no exception.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun BoostScreen(viewModel: QuickEditViewModel) {
    val state by viewModel.state.collectAsStateWithLifecycle()
    val advanced = state.mode == Mode.ADVANCED
    val boost = state.boost

    // Read once per session rather than on every visit: the model is only stale
    // after an edit, and every path that edits already re-reads it.
    LaunchedEffect(state.sessionId) {
        if (state.sessionId != null && boost.model == null) viewModel.loadBoostCurve()
    }

    var editingIndex by remember { mutableStateOf<Int?>(null) }
    var flatCapOpen by remember { mutableStateOf(false) }
    var axisOpen by remember { mutableStateOf(false) }
    var intent by remember { mutableStateOf("") }

    // Hoisted above the layout because the dialogs below it need the same model,
    // and the alternative is re-reaching into `boost` with a second null check.
    val model = boost.model

    Column(
        modifier = Modifier
            .fillMaxWidth()
            .verticalScroll(rememberScrollState())
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        Text("Boost", style = MaterialTheme.typography.headlineSmall, fontWeight = FontWeight.Bold)

        SessionProvenanceCard(binName = state.bin?.displayName, shortHash = state.bin?.shortHash, advanced = advanced)

        if (state.switchPatchXdf == null) {
            NoticeCard(
                title = "Switch-patch XDF needed",
                body = "The Boost editor needs a switch-patch XDF, imported from the landing " +
                    "screen in Advanced mode, before it has anything to edit.",
            )
            return@Column
        }

        if (model == null) {
            NoticeCard(
                title = if (boost.loading) "Reading the slot curves…" else "No boost model",
                body = boost.unavailable
                    ?: "The five map-slot boost caps are being read from the session.",
            )
            return@Column
        }

        SlotChips(
            activeSlot = boost.activeSlot,
            dirty = boost.dirty,
            onSelect = viewModel::onBoostSlotSelected,
        )

        BoostCanvas(
            model = model,
            activeSlot = boost.activeSlot,
            draft = boost.draft,
            onDragPoint = viewModel::onBoostPointDragged,
            onTapPoint = { index -> editingIndex = index },
        )

        CeilingLegend(
            refusalPsi = model.refusalCeilingPsi,
            cappedCount = boost.draftCappedByBase.size,
        )

        // Batch controls. Copy is offered per source slot rather than as a menu so
        // the destination is never ambiguous: whatever is copied lands on the
        // slot the chips say is active.
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.fillMaxWidth()) {
            OutlinedButton(onClick = { flatCapOpen = true }) { Text("Flat cap") }
            OutlinedButton(onClick = viewModel::onBoostSmooth) { Text("Smooth") }
            OutlinedButton(onClick = viewModel::onBoostDiscard, enabled = boost.dirty) { Text("Discard") }
        }

        Row(horizontalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.fillMaxWidth()) {
            Text(
                "Copy from",
                style = MaterialTheme.typography.labelLarge,
                modifier = Modifier.padding(top = 8.dp),
            )
            SLOT_IDS.filter { it != boost.activeSlot }.forEach { slot ->
                AssistChip(
                    onClick = { viewModel.onBoostCopyFrom(slot) },
                    label = { Text("$slot") },
                )
            }
        }

        OutlinedTextField(
            value = intent,
            onValueChange = { intent = it },
            label = { Text("Why this change (recorded in the journal)") },
            singleLine = false,
            modifier = Modifier.fillMaxWidth(),
        )

        Button(
            onClick = {
                viewModel.applyBoostDraft(
                    intent.ifBlank { "cap map slot ${boost.activeSlot} from the Quick Edit boost editor" }
                )
                intent = ""
            },
            enabled = boost.canApply && !state.busy,
            modifier = Modifier.fillMaxWidth(),
        ) {
            Text(if (boost.dirty) "Apply to slot ${boost.activeSlot}" else "No change to apply")
        }

        boost.notice?.let { notice ->
            NoticeCard(title = "Not applied", body = notice, emphasise = true)
        }

        boost.lastEdit?.let { receipt -> EncodedReceiptCard(receipt.slot, receipt.requestedPsi, receipt.encodedPsi, receipt.floored) }

        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            OutlinedButton(onClick = viewModel::undo, enabled = state.canUndo) { Text("Undo") }
            OutlinedButton(onClick = viewModel::redo, enabled = state.canRedo) { Text("Redo") }
        }

        if (advanced) {
            OutlinedButton(onClick = { axisOpen = true }, modifier = Modifier.fillMaxWidth()) {
                Text("Edit shared rpm breakpoints")
            }
            Text(
                "One rpm axis serves all five slots — moving it re-interprets every " +
                    "slot curve at once, which is why it lives behind Advanced.",
                style = MaterialTheme.typography.bodySmall,
            )
        }
    }

    // ------------------------------------------------------------------ dialogs

    editingIndex?.let { index ->
        NumericEntryDialog(
            title = "Slot ${boost.activeSlot} at ${model?.rpmAxis?.getOrNull(index)?.toInt() ?: 0} rpm",
            supporting = "psi gauge. Values at or above the base ceiling are refused, not rounded down.",
            initial = boost.draft.getOrNull(index)?.let { it.display("%.2f") } ?: "",
            onDismiss = { editingIndex = null },
            onConfirm = { value ->
                viewModel.onBoostPointTyped(index, value)
                editingIndex = null
            },
        )
    }

    if (flatCapOpen) {
        NumericEntryDialog(
            title = "Flat cap for slot ${boost.activeSlot}",
            supporting = "One psi figure across every rpm breakpoint.",
            initial = boost.draft.firstOrNull()?.let { it.display("%.2f") } ?: "",
            onDismiss = { flatCapOpen = false },
            onConfirm = { value ->
                viewModel.onBoostFlatCap(value)
                flatCapOpen = false
            },
        )
    }

    if (axisOpen) {
        val current = model?.rpmAxis.orEmpty()
        RpmAxisDialog(
            current = current,
            onDismiss = { axisOpen = false },
            onConfirm = { breakpoints ->
                viewModel.applySlotRpmAxis(breakpoints, "re-breakpoint the shared slot rpm axis")
                axisOpen = false
            },
        )
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun SlotChips(activeSlot: Int, dirty: Boolean, onSelect: (Int) -> Unit) {
    Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            SLOT_IDS.forEach { slot ->
                FilterChip(
                    selected = slot == activeSlot,
                    onClick = { onSelect(slot) },
                    label = { Text("Slot $slot") },
                )
            }
        }
        if (dirty) {
            Text(
                "Slot $activeSlot has an unapplied change. Apply or discard it before " +
                    "switching slots.",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.error,
            )
        }
    }
}

/**
 * Names the two ceilings in words, because a shaded band alone does not say which
 * limit refuses an edit and which merely swallows it.
 */
@Composable
private fun CeilingLegend(refusalPsi: Double, cappedCount: Int) {
    Card(colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceVariant)) {
        Column(modifier = Modifier.padding(12.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Text(
                "Solid line: the base `IP_PUT_SP` — Pressure up throttle setpoint " +
                    "full-load ceiling. The ECU targets min(base, slot), so a slot " +
                    "above it changes nothing at that rpm.",
                style = MaterialTheme.typography.bodySmall,
            )
            Text(
                "Dashed line: ${refusalPsi.display("%.2f")} psi. The engine refuses any cap that reaches it.",
                style = MaterialTheme.typography.bodySmall,
            )
            if (cappedCount > 0) {
                Text(
                    "$cappedCount breakpoint${if (cappedCount == 1) "" else "s"} of this " +
                        "draft sit above the base ceiling and will have no effect there.",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.error,
                )
            }
        }
    }
}

/**
 * Requested vs encoded, always shown after an apply.
 *
 * psi is floored on its way to stored hPa, so a cap asked for as 10.00 lands a
 * hair under it. Showing both numbers is the difference between a tool that
 * reports what the bin holds and one that reports what you hoped it held.
 */
@Composable
private fun EncodedReceiptCard(
    slot: Int,
    requested: List<Double>,
    encoded: List<Double>,
    floored: Boolean,
) {
    Card {
        Column(modifier = Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Text("Slot $slot applied", style = MaterialTheme.typography.titleSmall)
            val worst = requested.indices
                .maxByOrNull { abs(requested[it] - encoded.getOrElse(it) { requested[it] }) }
            if (floored && worst != null) {
                Text(
                    "Floored: asked ${requested[worst].display("%.2f")} psi, stored " +
                        "${encoded.getOrElse(worst) { requested[worst] }.display("%.2f")} psi at the widest point.",
                    style = MaterialTheme.typography.bodyMedium,
                )
            } else {
                Text("Stored exactly as requested.", style = MaterialTheme.typography.bodyMedium)
            }
            Text(
                encoded.joinToString(" · ") { it.display("%.1f") },
                style = MaterialTheme.typography.bodySmall,
            )
        }
    }
}

@Composable
internal fun NoticeCard(title: String, body: String, emphasise: Boolean = false) {
    Card(
        colors = if (emphasise) {
            CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.errorContainer)
        } else {
            CardDefaults.cardColors()
        },
    ) {
        Column(modifier = Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Text(title, style = MaterialTheme.typography.titleSmall)
            Text(body, style = MaterialTheme.typography.bodyMedium)
        }
    }
}

/**
 * Typed numeric entry.
 *
 * Confirm is disabled until the text parses. A dialog that accepted "1O.5" and
 * quietly did nothing would be worse than one that refuses it visibly.
 */
@Composable
internal fun NumericEntryDialog(
    title: String,
    supporting: String,
    initial: String,
    onDismiss: () -> Unit,
    onConfirm: (Double) -> Unit,
) {
    var text by remember { mutableStateOf(initial) }
    val parsed = text.trim().toDoubleOrNull()

    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text(title) },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedTextField(
                    value = text,
                    onValueChange = { text = it },
                    singleLine = true,
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
                )
                Text(supporting, style = MaterialTheme.typography.bodySmall)
            }
        },
        confirmButton = {
            TextButton(onClick = { parsed?.let(onConfirm) }, enabled = parsed != null) {
                Text("Set")
            }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Cancel") } },
    )
}

/**
 * The shared rpm axis, edited as one comma-separated line.
 *
 * A twelve-field form on a phone is worse than a single line someone can read
 * end to end and sanity-check for order. The engine enforces strictly-increasing
 * breakpoints and the axis-length header regardless; this only refuses the
 * obvious cases early.
 */
@Composable
private fun RpmAxisDialog(
    current: List<Double>,
    onDismiss: () -> Unit,
    onConfirm: (List<Double>) -> Unit,
) {
    var text by remember { mutableStateOf(current.joinToString(", ") { it.toInt().toString() }) }
    val parsed = text.split(",").mapNotNull { it.trim().toDoubleOrNull() }
    val rightLength = parsed.size == current.size
    val increasing = parsed.zipWithNext().all { (a, b) -> b > a }
    val valid = rightLength && increasing && parsed.isNotEmpty()

    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("Shared slot rpm axis") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedTextField(
                    value = text,
                    onValueChange = { text = it },
                    label = { Text("${current.size} breakpoints, comma separated") },
                    modifier = Modifier.fillMaxWidth(),
                )
                Text(
                    when {
                        !rightLength -> "Needs exactly ${current.size} values; ${parsed.size} parsed."
                        !increasing -> "Breakpoints must strictly increase."
                        else -> "This axis is shared by all five slots. Every slot curve " +
                            "is re-interpreted against it — the stored grids do not move."
                    },
                    style = MaterialTheme.typography.bodySmall,
                    textAlign = TextAlign.Start,
                )
            }
        },
        confirmButton = {
            TextButton(onClick = { onConfirm(parsed) }, enabled = valid) { Text("Apply axis") }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Cancel") } },
    )
}
