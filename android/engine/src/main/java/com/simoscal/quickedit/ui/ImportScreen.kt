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
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
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
        // The landing screen is the video's opening frame: the wordmark, a rule
        // under it, and the one sentence that says what the thing does. It is the
        // only screen with room for it, and the only one where a person has not
        // yet been told what they are holding.
        Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Wordmark(fontSize = 40.sp)
            HairRule()
            Caption("Edit in physical units. Checksum-verified .bin out. You flash it.")
        }

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

        PromoButton(
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
            PromoButton(
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
    // Accent, the colour of the live edit everywhere else in the app: this panel
    // is unfinished work on a real bin, and it is offered above the pickers.
    Panel(tone = PanelTone.Accent, spacing = 8.dp) {
        PanelTitle("Resume previous session?", tone = PanelTone.Accent)
        Identifier(pointer.bin.displayName)
        Caption("Saved ${DateFormat.getDateTimeInstance().format(Date(pointer.savedAtMillis))}")
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            PromoButton(onClick = onResume) { Text("Resume session") }
            PromoOutlinedButton(onClick = onDiscard) { Text("Discard") }
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
    Panel {
        // The label is a kicker rather than a heading: it names the slot, and the
        // filename under it is the content — same order the video uses over every
        // plot it draws.
        Kicker(label, color = PromoPalette.TextFaint)
        if (file != null) {
            // Monospace, because a filename is an identifier: `..._R14.bin` and
            // `..._R15.bin` differ by one glyph, and in a proportional face that
            // glyph is the easiest one on screen to misread.
            Identifier(file.displayName)
            Caption(formatSize(file.sizeBytes))
            if (advanced) {
                Text(
                    "SHA-256 ${file.shortHash}",
                    style = PromoType.figureSmall,
                    color = PromoPalette.TextFaint,
                )
            }
        } else {
            Caption("No file chosen", color = PromoPalette.TextFaint)
        }
        PromoOutlinedButton(onClick = onChoose) {
            Text(if (file == null) "Choose" else "Replace")
        }
    }
}

@Composable
private fun SwitchPatchRow(file: ImportedFile?, onChoose: () -> Unit, onClear: () -> Unit) {
    Panel {
        Kicker("Switch-patch XDF (optional)", color = PromoPalette.TextFaint)
        Caption("Only needed to reach the Boost and Slots destinations.")
        if (file != null) {
            Identifier(file.displayName)
            Text(
                "SHA-256 ${file.shortHash}",
                style = PromoType.figureSmall,
                color = PromoPalette.TextFaint,
            )
        } else {
            Caption("No file chosen", color = PromoPalette.TextFaint)
        }
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            PromoOutlinedButton(onClick = onChoose) {
                Text(if (file == null) "Choose" else "Replace")
            }
            if (file != null) {
                TextButton(onClick = onClear) { Text("Clear") }
            }
        }
    }
}

@Composable
private fun PassedCard(passed: PreflightState.Passed) {
    // `good` — the palette's checks-pass green, and the only screen-level verdict
    // in the app that earns it before a build.
    Panel(tone = PanelTone.Good) {
        PanelTitle("This bin can be edited", tone = PanelTone.Good)
        Text(passed.summary, style = MaterialTheme.typography.bodyMedium)
        if (passed.reasons.isNotEmpty()) {
            HairRule(color = PromoPalette.Good.copy(alpha = 0.3f))
            passed.reasons.forEach { reason ->
                Caption("• $reason")
            }
        }
    }
}

private fun formatSize(bytes: Long): String = when {
    bytes >= 1_048_576 -> "%.1f MB".format(bytes / 1_048_576.0)
    bytes >= 1_024 -> "%.1f KB".format(bytes / 1_024.0)
    else -> "$bytes B"
}
