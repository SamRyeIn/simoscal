package com.simoscal.quickedit.ui

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
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.simoscal.engine.R
import com.simoscal.quickedit.BuildState
import com.simoscal.quickedit.GateResult
import com.simoscal.quickedit.Mode
import com.simoscal.quickedit.QuickEditViewModel
import com.simoscal.quickedit.ShareBin

/**
 * The build/verify/share screen — the only place in the app an export
 * affordance can appear, and only in the [BuildState.Verified] branch below.
 *
 * The disclaimer (`R.string.build_disclaimer`) is shown regardless of which
 * build-state branch is active, because it is not a caveat about *this* build
 * — it is the standing boundary of what any of these gates can ever prove:
 * file integrity, not mechanical safety.
 */
@Composable
fun BuildScreen(viewModel: QuickEditViewModel) {
    val state by viewModel.state.collectAsStateWithLifecycle()
    val advanced = state.mode == Mode.ADVANCED
    val context = LocalContext.current

    var revision by rememberSaveable { mutableStateOf("R00") }

    Column(
        modifier = Modifier
            .fillMaxWidth()
            .verticalScroll(rememberScrollState())
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        Text("Build", style = MaterialTheme.typography.headlineSmall, fontWeight = FontWeight.Bold)

        OutlinedTextField(
            value = revision,
            onValueChange = { revision = it },
            label = { Text("Revision") },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )

        Button(
            onClick = { viewModel.build(revision) },
            enabled = state.canBuild && revision.isNotBlank(),
            modifier = Modifier.fillMaxWidth(),
        ) {
            Text("Build")
        }

        when (val build = state.build) {
            is BuildState.NotBuilt -> {
                Text("Nothing built yet.", style = MaterialTheme.typography.bodyMedium)
            }
            is BuildState.Running -> {
                Row {
                    CircularProgressIndicator(modifier = Modifier.padding(end = 8.dp))
                    Text("Building...")
                }
            }
            is BuildState.Failed -> {
                FailedCard(build)
            }
            is BuildState.Verified -> {
                VerifiedCard(build, advanced)
                // A share that cannot be set up must report itself, not crash the
                // app: FileProvider throws if the staged bin ever falls outside
                // the declared paths, and losing the whole screen would take the
                // build report down with it.
                var shareError by remember { mutableStateOf<String?>(null) }
                Button(
                    onClick = {
                        shareError = runCatching {
                            context.startActivity(ShareBin.intentFor(context, build))
                        }.exceptionOrNull()?.let { "This bin could not be shared: ${it.message}" }
                    },
                    modifier = Modifier.fillMaxWidth(),
                ) {
                    Text("Export / Share")
                }
                shareError?.let { message ->
                    Text(message, color = MaterialTheme.colorScheme.error)
                }
            }
        }

        Divider()
        Text(
            stringResource(R.string.build_disclaimer),
            style = MaterialTheme.typography.bodySmall,
            fontWeight = FontWeight.Bold,
        )
    }
}

@Composable
private fun FailedCard(build: BuildState.Failed) {
    Card(colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.errorContainer)) {
        Column(modifier = Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Text(
                "Build failed",
                style = MaterialTheme.typography.titleMedium,
                color = MaterialTheme.colorScheme.onErrorContainer,
            )
            Text(build.summary, color = MaterialTheme.colorScheme.onErrorContainer)
            build.reasons.forEach { reason ->
                Text("• $reason", color = MaterialTheme.colorScheme.onErrorContainer)
            }
        }
    }
}

@Composable
private fun VerifiedCard(build: BuildState.Verified, advanced: Boolean) {
    Card(colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.secondaryContainer)) {
        Column(modifier = Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text("Verified", style = MaterialTheme.typography.titleMedium)
            Text("Revision ${build.revision}")
            Text(build.binName)

            Divider()
            Text("Changed tables", style = MaterialTheme.typography.titleSmall)
            if (build.changedTables.isEmpty()) {
                Text("No tables were changed.", style = MaterialTheme.typography.bodyMedium)
            } else {
                build.changedTables.forEach { label ->
                    Text("• $label", style = MaterialTheme.typography.bodyMedium)
                }
            }

            Divider()
            Text("Gates", style = MaterialTheme.typography.titleSmall)
            build.gates.forEach { gate -> GateRow(gate, advanced) }
        }
    }
}

/**
 * Three distinct visual states, not two: a gate that never ran must not read
 * as a pass. `ran == false` gets its own label and its own color regardless
 * of [GateResult.passed] — a byte audit with no reference bin, for instance,
 * defaults to `passed = false` upstream, but even if it did not, "did not
 * run" has to stay visually distinct from both pass and fail so nobody mistakes
 * an unexercised gate for evidence of anything.
 */
@Composable
private fun GateRow(gate: GateResult, advanced: Boolean) {
    val (label, color) = when {
        !gate.ran -> "DID NOT RUN" to MaterialTheme.colorScheme.onSurfaceVariant
        gate.passed -> "PASSED" to MaterialTheme.colorScheme.primary
        else -> "FAILED" to MaterialTheme.colorScheme.error
    }
    Column(modifier = Modifier.padding(vertical = 4.dp)) {
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Text(label, color = color, fontWeight = FontWeight.Bold)
            Text(gate.name)
        }
        if (advanced && gate.detail.isNotBlank()) {
            Text(gate.detail, style = MaterialTheme.typography.bodySmall)
        }
    }
}
