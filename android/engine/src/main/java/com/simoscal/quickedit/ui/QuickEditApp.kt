package com.simoscal.quickedit.ui

import android.net.Uri
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Build
import androidx.compose.material.icons.filled.List
import androidx.compose.material.icons.filled.KeyboardArrowUp
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Icon
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Snackbar
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.window.DialogProperties
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.navigation.NavDestination.Companion.hierarchy
import androidx.navigation.NavGraph.Companion.findStartDestination
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.currentBackStackEntryAsState
import androidx.navigation.compose.rememberNavController
import com.simoscal.engine.R
import com.simoscal.quickedit.Destination
import com.simoscal.quickedit.InputKind
import com.simoscal.quickedit.Mode
import com.simoscal.quickedit.PreflightState
import com.simoscal.quickedit.QuickEditViewModel

/**
 * The app's whole navigation shell.
 *
 * Everything that gates *where the person can go* lives here, not scattered
 * across screens: the navigation bar (and therefore Tables/Boost/Build) only
 * exists once a session is open, and a [PreflightState.Blocked] dialog is
 * rendered above every route because there is no route it is safe to let
 * someone tap past — see [BlockedDialog].
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun QuickEditApp(viewModel: QuickEditViewModel) {
    val state by viewModel.state.collectAsStateWithLifecycle()
    val recoverable by viewModel.recoverable.collectAsStateWithLifecycle()
    val navController = rememberNavController()
    val snackbarHostState = remember { SnackbarHostState() }

    // Hoisted to the shell because the blocker dialog is drawn above every route
    // and needs its own way to reach the picker — the one on the import screen is
    // behind the dialog and therefore untappable while a blocked verdict stands.
    val blockedBinPicker = rememberLauncherForActivityResult(
        ActivityResultContracts.OpenDocument()
    ) { uri: Uri? ->
        uri?.let { viewModel.onFilePicked(it, InputKind.BIN) }
    }

    // The session is the only thing that should ever move the person between
    // "import" and the workspace. Driving navigation off `sessionOpen` (rather
    // than, say, a button's own onClick) means recovery and a fresh
    // openSession() land in the same place, and a session that closes for any
    // reason always drops back to a screen where re-import is possible.
    LaunchedEffect(state.sessionOpen) {
        if (state.sessionOpen) {
            navController.navigate("tables") {
                popUpTo("import") { inclusive = false }
            }
        } else {
            navController.navigate("import") {
                popUpTo(0)
            }
        }
    }

    val error = state.error
    LaunchedEffect(error) {
        if (error != null) {
            val message = if (state.mode == Mode.ADVANCED && error.advanced.isNotBlank()) {
                "${error.message}\n${error.advanced}"
            } else {
                error.message
            }
            snackbarHostState.showSnackbar(message)
            viewModel.dismissError()
        }
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text(stringResource(R.string.app_name)) },
                actions = {
                    ModeToggle(mode = state.mode, onModeChanged = viewModel::onModeChanged)
                },
            )
        },
        bottomBar = {
            // The bar itself is the gate that keeps Tables/Boost/Build reachable
            // only from a live session — it does not exist otherwise, rather than
            // existing-but-disabled, so there is nothing to tap toward at all.
            if (state.sessionOpen) {
                val backStackEntry by navController.currentBackStackEntryAsState()
                val currentRoute = backStackEntry?.destination?.hierarchy?.firstOrNull()?.route
                NavigationBar {
                    destinationItems().forEach { item ->
                        NavigationBarItem(
                            selected = currentRoute == item.route,
                            enabled = state.destinationEnabled(item.destination),
                            onClick = {
                                navController.navigate(item.route) {
                                    popUpTo(navController.graph.findStartDestination().id) {
                                        saveState = true
                                    }
                                    launchSingleTop = true
                                    restoreState = true
                                }
                            },
                            icon = { Icon(item.icon, contentDescription = item.label) },
                            label = { Text(item.label) },
                        )
                    }
                }
            }
        },
        snackbarHost = {
            SnackbarHost(snackbarHostState) { data ->
                Snackbar { Text(data.visuals.message) }
            }
        },
    ) { padding ->
        Column(modifier = Modifier.padding(padding)) {
            // Busy always shows, on every screen, so a person can never mistake a
            // slow bridge call for the app doing nothing.
            if (state.busy) {
                LinearProgressIndicator(modifier = Modifier.fillMaxWidth())
            }
            NavHost(navController = navController, startDestination = "import") {
                composable("import") { ImportScreen(viewModel = viewModel, recoverable = recoverable) }
                composable("tables") { TablesScreen(viewModel = viewModel) }
                composable("boost") { BoostScreen(viewModel = viewModel) }
                composable("build") { BuildScreen(viewModel = viewModel) }
            }
        }
    }

    // Rendered last so it draws above the Scaffold's own content. A blocked
    // preflight is a dead end by design (see PreflightState.Blocked) — the
    // dialog is the only thing on screen with `onDismissRequest = {}` and no
    // outside-tap or back-press dismissal, because "continue anyway" is exactly
    // what this app must never offer.
    state.blocker?.let { blocker ->
        BlockedDialog(
            blocker = blocker,
            onChooseAnotherBin = {
                // Retract the verdict *and* open the picker in one action. Both
                // halves are needed: the dialog cannot be dismissed, so leaving
                // the verdict in place would keep it covering the very picker the
                // button is named after. Picking a bin then resets preflight
                // again via QuickEditUiState.withBin.
                viewModel.dismissBlocker()
                blockedBinPicker.launch(InputKind.BIN.mimeTypes)
            },
            onCancel = {
                // Cancel returns to the import screen with no session and no
                // verdict — it is a retraction, never a way past the refusal.
                // `canOpenSession` still requires a *passed* preflight, so
                // nothing here can lead to an edit on the blocked bin.
                viewModel.dismissBlocker()
                navController.navigate("import") { popUpTo(0) }
            },
        )
    }
}

private data class DestinationItem(
    val destination: Destination,
    val route: String,
    val label: String,
    val icon: ImageVector,
)

private fun destinationItems(): List<DestinationItem> = listOf(
    DestinationItem(Destination.TABLES, "tables", "Tables", Icons.Filled.List),
    DestinationItem(Destination.BOOST, "boost", "Boost", Icons.Filled.KeyboardArrowUp),
    DestinationItem(Destination.BUILD, "build", "Build", Icons.Filled.Build),
)

/**
 * Simple/Advanced changes what is *visible*, never what is *permitted*.
 *
 * This composable only calls [QuickEditViewModel.onModeChanged], which flips a
 * display flag in state — every `canX`/`enabled` value in the app is derived
 * from other fields and never reads [Mode] at all, so there is no code path by
 * which flipping this toggle could unlock an action Simple forbade.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun ModeToggle(mode: Mode, onModeChanged: (Mode) -> Unit) {
    FilterChip(
        selected = mode == Mode.ADVANCED,
        onClick = { onModeChanged(if (mode == Mode.ADVANCED) Mode.SIMPLE else Mode.ADVANCED) },
        label = { Text(if (mode == Mode.ADVANCED) "Advanced" else "Simple") },
    )
}

/**
 * The non-dismissible dead end for a bin that cannot be safely edited.
 *
 * No "continue anyway" exists anywhere in this dialog on purpose: a blocked
 * preflight means the engine itself would refuse to open a session over this
 * bin, so offering a third button here would just be a slower way to hit the
 * same refusal one screen later — with the difference that it would look like
 * a choice. The two real ways out are choosing a different bin or backing off
 * to the import screen; neither is a "proceed" and neither may be dismissed by
 * tapping outside or pressing back.
 */
@Composable
private fun BlockedDialog(
    blocker: PreflightState.Blocked,
    onChooseAnotherBin: () -> Unit,
    onCancel: () -> Unit,
) {
    AlertDialog(
        onDismissRequest = {},
        properties = DialogProperties(dismissOnBackPress = false, dismissOnClickOutside = false),
        title = { Text(stringResource(R.string.preflight_blocked_title)) },
        text = {
            Column {
                Text(blocker.summary, style = MaterialTheme.typography.bodyMedium)
                blocker.reasons.forEach { reason ->
                    Text("• $reason", style = MaterialTheme.typography.bodySmall)
                }
            }
        },
        confirmButton = {
            TextButton(onClick = onChooseAnotherBin) {
                Text(stringResource(R.string.choose_another_bin))
            }
        },
        dismissButton = {
            TextButton(onClick = onCancel) {
                Text(stringResource(R.string.cancel))
            }
        },
    )
}
