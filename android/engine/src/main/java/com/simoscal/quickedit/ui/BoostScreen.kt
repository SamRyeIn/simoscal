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
 * Boost/switch-patch editing, honestly deferred.
 *
 * Reachable at all only when a switch-patch XDF was imported — see
 * [com.simoscal.quickedit.QuickEditUiState.destinationEnabled] — so by the
 * time this screen renders, `switchPatchXdf` is normally non-null. It is
 * still checked here rather than assumed: a screen that renders nothing when
 * its input is missing teaches a person nothing, and the check costs one `if`.
 * Clearing the switch-patch XDF also closes the session outright (see
 * `withSwitchPatchXdf`), so in practice this branch is a safety net rather than
 * a state the navigation bar can currently reach.
 */
@Composable
fun BoostScreen(viewModel: QuickEditViewModel) {
    val state by viewModel.state.collectAsStateWithLifecycle()
    val advanced = state.mode == Mode.ADVANCED

    Column(
        modifier = Modifier
            .fillMaxWidth()
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        Text("Boost", style = MaterialTheme.typography.headlineSmall, fontWeight = FontWeight.Bold)

        SessionProvenanceCard(binName = state.bin?.displayName, shortHash = state.bin?.shortHash, advanced = advanced)

        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            OutlinedButton(onClick = { viewModel.undo() }, enabled = state.canUndo) {
                Text("Undo")
            }
            OutlinedButton(onClick = { viewModel.redo() }, enabled = state.canRedo) {
                Text("Redo")
            }
        }

        if (state.switchPatchXdf == null) {
            Card {
                Column(modifier = Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                    Text("Switch-patch XDF needed", style = MaterialTheme.typography.titleSmall)
                    Text(
                        "The Boost editor needs a switch-patch XDF, imported from the landing " +
                            "screen in Advanced mode, before it has anything to edit.",
                        style = MaterialTheme.typography.bodyMedium,
                    )
                }
            }
        }

        Card {
            Column(modifier = Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                Text("Boost editors arrive in V8", style = MaterialTheme.typography.titleSmall)
                Text(
                    "This screen only tracks session provenance and history for now. " +
                        "No switch-patch table is editable here yet.",
                    style = MaterialTheme.typography.bodyMedium,
                )
            }
        }
    }
}
