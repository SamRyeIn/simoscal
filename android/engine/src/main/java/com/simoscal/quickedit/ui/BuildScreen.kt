package com.simoscal.quickedit.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
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
        ScreenHeader(kicker = "Verify, then you flash it", title = "Build")

        OutlinedTextField(
            value = revision,
            onValueChange = { revision = it },
            label = { Text("Revision") },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )

        PromoButton(
            onClick = { viewModel.build(revision) },
            enabled = state.canBuild && revision.isNotBlank(),
            modifier = Modifier.fillMaxWidth(),
        ) {
            Text("Build")
        }

        when (val build = state.build) {
            is BuildState.NotBuilt -> {
                Caption("Nothing built yet.")
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
                PromoButton(
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

        HairRule()
        Text(
            stringResource(R.string.build_disclaimer),
            style = MaterialTheme.typography.bodySmall,
            fontWeight = FontWeight.Bold,
        )
    }
}

@Composable
private fun FailedCard(build: BuildState.Failed) {
    Panel(tone = PanelTone.Danger) {
        PanelTitle("Build failed", tone = PanelTone.Danger)
        Text(build.summary)
        build.reasons.forEach { reason ->
            Text("• $reason")
        }
    }
}

@Composable
private fun VerifiedCard(build: BuildState.Verified, advanced: Boolean) {
    Panel(tone = PanelTone.Good, spacing = 8.dp) {
        PanelTitle("Verified", tone = PanelTone.Good)
        Text("Revision ${build.revision}")
        // The built file's name, monospace: it is the string that gets picked out
        // of a share sheet later, and one wrong revision digit is the whole point
        // of naming it at all.
        Identifier(build.binName)

        HairRule(color = PromoPalette.Good.copy(alpha = 0.3f))
        Kicker("Changed tables", color = PromoPalette.TextFaint)
        if (build.changedTables.isEmpty()) {
            Caption("No tables were changed.")
        } else {
            build.changedTables.forEach { label ->
                Text("• $label", style = MaterialTheme.typography.bodyMedium)
            }
        }

        HairRule(color = PromoPalette.Good.copy(alpha = 0.3f))
        Kicker("Gates", color = PromoPalette.TextFaint)
        build.gates.forEach { gate -> GateRow(gate, advanced) }
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
    // Straight off the palette's own vocabulary: `good` is a check that passed,
    // `danger` is a refusal, and a gate that never ran is faint — it is not a
    // verdict at all and must not be coloured like one.
    val (label, color) = when {
        !gate.ran -> "DID NOT RUN" to PromoPalette.TextFaint
        gate.passed -> "PASSED" to PromoPalette.Good
        else -> "FAILED" to PromoPalette.Danger
    }
    Column(modifier = Modifier.padding(vertical = 4.dp)) {
        Row(
            horizontalArrangement = Arrangement.spacedBy(8.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Kicker(label, color = color)
            Text(gate.name, style = MaterialTheme.typography.bodyMedium)
        }
        if (advanced && gate.detail.isNotBlank()) {
            Caption(gate.detail)
        }
    }
}
