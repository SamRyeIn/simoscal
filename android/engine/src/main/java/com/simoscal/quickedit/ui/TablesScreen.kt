package com.simoscal.quickedit.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.simoscal.quickedit.Mode
import com.simoscal.quickedit.QuickEditViewModel

/**
 * Table editing, honestly deferred.
 *
 * V8 fills this in with the real calibration editors. Faking a table grid here
 * would be worse than an empty screen: it would look editable before the
 * engine plumbing behind it exists, and a person could believe they changed a
 * value that was never touched. This screen only shows what is *actually*
 * true right now — provenance and history controls — plus a plain statement
 * of what is missing.
 */
@Composable
fun TablesScreen(viewModel: QuickEditViewModel) {
    val state by viewModel.state.collectAsStateWithLifecycle()
    val advanced = state.mode == Mode.ADVANCED

    Column(
        modifier = Modifier
            .fillMaxWidth()
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        Text("Tables", style = MaterialTheme.typography.headlineSmall, fontWeight = FontWeight.Bold)

        SessionProvenanceCard(binName = state.bin?.displayName, shortHash = state.bin?.shortHash, advanced = advanced)

        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            OutlinedButton(onClick = { viewModel.undo() }, enabled = state.canUndo) {
                Text("Undo")
            }
            OutlinedButton(onClick = { viewModel.redo() }, enabled = state.canRedo) {
                Text("Redo")
            }
        }

        Card {
            Column(modifier = Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                Text("Table editors arrive in V8", style = MaterialTheme.typography.titleSmall)
                Text(
                    "This screen only tracks session provenance and history for now. " +
                        "No calibration table is editable here yet.",
                    style = MaterialTheme.typography.bodyMedium,
                )
            }
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
