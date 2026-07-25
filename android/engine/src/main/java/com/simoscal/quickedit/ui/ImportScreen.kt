package com.simoscal.quickedit.ui

import android.net.Uri
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Divider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.simoscal.quickedit.ImportedFile
import com.simoscal.quickedit.InputKind
import com.simoscal.quickedit.Mode
import com.simoscal.quickedit.PreflightState
import com.simoscal.quickedit.QuickEditViewModel
import com.simoscal.quickedit.RecoveryPointer
import java.text.DateFormat
import java.util.Date

/**
 * The landing screen: choose a bin and an XDF, run the safety check, open a
 * session.
 *
 * This is deliberately the only place in the app that can create the inputs a
 * session is built from. Everything about a bin's safety — the preflight
 * verdict, the [PreflightState.Blocked] dead end handled up in [QuickEditApp] —
 * traces back to files chosen here, so this screen never lets a person skip
 * ahead: "Open session" only exists once [PreflightState.Passed] is on state.
 */
@Composable
fun ImportScreen(viewModel: QuickEditViewModel, recoverable: RecoveryPointer?) {
    val state by viewModel.state.collectAsStateWithLifecycle()
    val advanced = state.mode == Mode.ADVANCED

    val binPicker = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocument()) { uri: Uri? ->
        uri?.let { viewModel.onFilePicked(it, InputKind.BIN) }
    }
    val xdfPicker = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocument()) { uri: Uri? ->
        uri?.let { viewModel.onFilePicked(it, InputKind.XDF) }
    }
    val switchPatchPicker = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocument()) { uri: Uri? ->
        uri?.let { viewModel.onFilePicked(it, InputKind.SWITCH_PATCH_XDF) }
    }

    Column(
        modifier = Modifier
            .fillMaxWidth()
            .verticalScroll(rememberScrollState())
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        Text("Quick Edit", style = MaterialTheme.typography.headlineMedium, fontWeight = FontWeight.Bold)

        // Offered first, above the ordinary bin/XDF pickers: a recoverable
        // session already has a passed preflight behind it (recoverSession()
        // restores PreflightState.Passed), so this is a faster path back into
        // the workspace than re-importing and re-checking from scratch.
        if (recoverable != null) {
            RecoverableCard(
                pointer = recoverable,
                onResume = { viewModel.recoverSession(recoverable) },
                onDiscard = { viewModel.discardRecoverable() },
            )
        }

        InputRow(
            label = "Calibration bin",
            file = state.bin,
            advanced = advanced,
            onChoose = { binPicker.launch(InputKind.BIN.mimeTypes) },
        )
        InputRow(
            label = "XDF definition",
            file = state.xdf,
            advanced = advanced,
            onChoose = { xdfPicker.launch(InputKind.XDF.mimeTypes) },
        )

        // Advanced-only: this file only unlocks the Boost destination's extra
        // switch-patch space, it is never required to run preflight or open a
        // session, so Simple mode has no reason to show it.
        if (advanced) {
            SwitchPatchRow(
                file = state.switchPatchXdf,
                onChoose = { switchPatchPicker.launch(InputKind.SWITCH_PATCH_XDF.mimeTypes) },
                onClear = { viewModel.clearSwitchPatchXdf() },
            )
        }

        Button(
            onClick = { viewModel.runPreflight() },
            enabled = state.canRunPreflight,
            modifier = Modifier.fillMaxWidth(),
        ) {
            Text("Check this bin")
        }

        if (state.preflight is PreflightState.Running) {
            Row(verticalAlignment = androidx.compose.ui.Alignment.CenterVertically) {
                CircularProgressIndicator(modifier = Modifier.padding(end = 8.dp))
                Text("Checking...")
            }
        }

        val passed = state.preflight as? PreflightState.Passed
        if (passed != null) {
            PassedCard(passed = passed)
            Button(
                onClick = { viewModel.openSession() },
                enabled = state.canOpenSession,
                modifier = Modifier.fillMaxWidth(),
            ) {
                Text("Open session")
            }
        }

        // A Blocked verdict is handled by the non-dismissible dialog in
        // QuickEditApp — nothing about it is rendered inline here, so this
        // screen never offers a second, quieter way past the same dead end.
    }
}

@Composable
private fun RecoverableCard(pointer: RecoveryPointer, onResume: () -> Unit, onDiscard: () -> Unit) {
    Card {
        Column(modifier = Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text("Resume previous session?", style = MaterialTheme.typography.titleMedium)
            Text(pointer.bin.displayName, style = MaterialTheme.typography.bodyMedium)
            Text(
                "Saved ${DateFormat.getDateTimeInstance().format(Date(pointer.savedAtMillis))}",
                style = MaterialTheme.typography.bodySmall,
            )
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Button(onClick = onResume) { Text("Resume session") }
                OutlinedButton(onClick = onDiscard) { Text("Discard") }
            }
        }
    }
}

/**
 * One replaceable input row.
 *
 * Bin and XDF are order-independent: whichever import lands second is what
 * flips `canRunPreflight` to true (see [com.simoscal.quickedit.QuickEditUiState]),
 * so this composable never assumes the other file is already chosen.
 */
@Composable
private fun InputRow(label: String, file: ImportedFile?, advanced: Boolean, onChoose: () -> Unit) {
    Card {
        Column(modifier = Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Text(label, style = MaterialTheme.typography.titleSmall)
            if (file != null) {
                Text(file.displayName, style = MaterialTheme.typography.bodyMedium)
                Text("${formatSize(file.sizeBytes)}", style = MaterialTheme.typography.bodySmall)
                if (advanced) {
                    Text("SHA-256 ${file.shortHash}", style = MaterialTheme.typography.bodySmall)
                }
            } else {
                Text("No file chosen", style = MaterialTheme.typography.bodyMedium)
            }
            OutlinedButton(onClick = onChoose) {
                Text(if (file == null) "Choose" else "Replace")
            }
        }
    }
}

@Composable
private fun SwitchPatchRow(file: ImportedFile?, onChoose: () -> Unit, onClear: () -> Unit) {
    Card {
        Column(modifier = Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Text("Switch-patch XDF (optional)", style = MaterialTheme.typography.titleSmall)
            Text(
                "Only needed to reach the Boost destination.",
                style = MaterialTheme.typography.bodySmall,
            )
            if (file != null) {
                Text(file.displayName, style = MaterialTheme.typography.bodyMedium)
                Text("SHA-256 ${file.shortHash}", style = MaterialTheme.typography.bodySmall)
            } else {
                Text("No file chosen", style = MaterialTheme.typography.bodyMedium)
            }
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedButton(onClick = onChoose) {
                    Text(if (file == null) "Choose" else "Replace")
                }
                if (file != null) {
                    TextButton(onClick = onClear) { Text("Clear") }
                }
            }
        }
    }
}

@Composable
private fun PassedCard(passed: PreflightState.Passed) {
    Card(colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.secondaryContainer)) {
        Column(modifier = Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Text("This bin can be edited", style = MaterialTheme.typography.titleMedium)
            Text(passed.summary, style = MaterialTheme.typography.bodyMedium)
            if (passed.reasons.isNotEmpty()) {
                Divider()
                passed.reasons.forEach { reason ->
                    Text("• $reason", style = MaterialTheme.typography.bodySmall)
                }
            }
        }
    }
}

private fun formatSize(bytes: Long): String = when {
    bytes >= 1_048_576 -> "%.1f MB".format(bytes / 1_048_576.0)
    bytes >= 1_024 -> "%.1f KB".format(bytes / 1_024.0)
    else -> "$bytes B"
}
