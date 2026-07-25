package com.simoscal.quickedit

import android.app.Application
import android.net.Uri
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import org.json.JSONArray
import org.json.JSONObject

/**
 * Drives the Quick Edit flow: import → preflight → session → edit → build → share.
 *
 * All calibration judgment lives in the engine. This class only sequences bridge
 * calls, maps their results into [QuickEditUiState], and persists just enough to
 * recover after a process kill. It never decides whether a bin is safe, never
 * computes a value, and never writes a byte of the bin.
 */
class QuickEditViewModel(application: Application) : AndroidViewModel(application) {

    private val bridge = BridgeClient(application)
    private val imports = ImportStore(application)
    private val recovery = RecoveryStore(application)

    private val _state = MutableStateFlow(QuickEditUiState())
    val state: StateFlow<QuickEditUiState> = _state.asStateFlow()

    private val _recoverable = MutableStateFlow<RecoveryPointer?>(null)

    /** A previous session found on disk, offered on the landing screen. */
    val recoverable: StateFlow<RecoveryPointer?> = _recoverable.asStateFlow()

    init {
        viewModelScope.launch {
            _recoverable.value = recovery.load()
        }
    }

    // ----------------------------------------------------------------- inputs

    fun onModeChanged(mode: Mode) = _state.update { it.copy(mode = mode) }

    fun dismissError() = _state.update { it.copy(error = null) }

    /**
     * Copy a picked file in and attach it to the flow.
     *
     * Runs off the main thread through the view-model scope; the copy is what
     * hashes the bytes, so nothing downstream ever refers to the picker's URI.
     */
    fun onFilePicked(uri: Uri, kind: InputKind) {
        viewModelScope.launch {
            _state.update { it.copy(busy = true, error = null) }
            val imported = runCatching { imports.importFile(uri, kind) }
            _state.update { current ->
                imported.fold(
                    onSuccess = { file ->
                        when (kind) {
                            InputKind.BIN -> current.withBin(file)
                            InputKind.XDF -> current.withXdf(file)
                            InputKind.SWITCH_PATCH_XDF -> current.withSwitchPatchXdf(file)
                        }.copy(busy = false)
                    },
                    onFailure = { error ->
                        current.copy(
                            busy = false,
                            error = UserFacingError(
                                code = "IMPORT_FAILED",
                                message = (error as? ImportFailure)?.reason
                                    ?: "That file could not be imported.",
                                advanced = error.toString(),
                            ),
                        )
                    },
                )
            }
        }
    }

    /** Drop the optional switch-patch XDF (and with it the Boost destination). */
    fun clearSwitchPatchXdf() = _state.update { it.withSwitchPatchXdf(null) }

    /**
     * Clear a blocked verdict, returning to the un-checked state.
     *
     * This is what makes the blocker a dead end rather than a *trap*: the dialog
     * is deliberately non-dismissible, so without a way to retract the verdict
     * the person would be left staring at it with the file pickers unreachable
     * underneath. Retracting is safe precisely because it grants nothing —
     * `canOpenSession` requires [PreflightState.Passed], so the only things
     * reachable from here are choosing another file or re-running the check.
     * It never converts a blocked bin into an editable one.
     */
    fun dismissBlocker() = _state.update { it.retractingBlocker() }

    // -------------------------------------------------------------- preflight

    fun runPreflight() {
        val current = _state.value
        val bin = current.bin ?: return
        val xdf = current.xdf ?: return
        if (!current.canRunPreflight) return

        viewModelScope.launch {
            _state.update { it.copy(busy = true, preflight = PreflightState.Running, error = null) }
            val outcome = bridge.call(
                op = "preflight",
                params = params {
                    putVerified("bin", bin)
                    putVerified("xdf", xdf)
                    current.switchPatchXdf?.let { putVerified("switch_patch_xdf", it) }
                },
            )
            _state.update { state ->
                when (outcome) {
                    is BridgeOutcome.Ok -> state.copy(
                        busy = false,
                        preflight = outcome.result.toPreflightState(),
                    )
                    // A failure to *reach* a verdict is not a verdict. It shows as
                    // an error and leaves preflight un-run, so nothing downstream
                    // can mistake it for a pass.
                    is BridgeOutcome.Failed -> state.copy(
                        busy = false,
                        preflight = PreflightState.NotRun,
                        error = outcome.toUserFacing(),
                    )
                }
            }
        }
    }

    // ---------------------------------------------------------------- session

    fun openSession() {
        val current = _state.value
        if (!current.canOpenSession) return
        val bin = current.bin ?: return
        val xdf = current.xdf ?: return

        viewModelScope.launch {
            _state.update { it.copy(busy = true, error = null) }
            val outcome = bridge.call(
                op = "session_create",
                params = params {
                    putVerified("bin", bin)
                    putVerified("xdf", xdf)
                    current.switchPatchXdf?.let { putVerified("switch_patch_xdf", it) }
                },
            )
            _state.update { state ->
                when (outcome) {
                    is BridgeOutcome.Ok -> state.copy(
                        busy = false,
                        sessionId = outcome.result.optString("session_id").takeIf { it.isNotEmpty() },
                        canUndo = outcome.result.optBoolean("can_undo", false),
                        canRedo = outcome.result.optBoolean("can_redo", false),
                    )
                    is BridgeOutcome.Failed -> state.copy(
                        busy = false,
                        // The engine re-runs preflight inside session_create, so a
                        // PREFLIGHT_BLOCKED here is a real verdict, not a transport
                        // failure — show it as the blocker it is.
                        preflight = if (outcome.code == "PREFLIGHT_BLOCKED") {
                            PreflightState.Blocked(outcome.message, listOfNotNull(outcome.advanced.ifEmpty { null }))
                        } else {
                            state.preflight
                        },
                        error = if (outcome.code == "PREFLIGHT_BLOCKED") null else outcome.toUserFacing(),
                    )
                }
            }
            persistRecovery()
        }
    }

    /** Restore the session found on disk, re-verifying every input's hash. */
    fun recoverSession(pointer: RecoveryPointer) {
        viewModelScope.launch {
            _state.update { it.copy(busy = true, error = null) }
            val xdfPaths = JSONObject().apply {
                put("base", pointer.xdf.encode())
                pointer.switchPatchXdf?.let { put(PATCH_SPACE, it.encode()) }
            }
            val outcome = bridge.call(
                op = "session_recover",
                params = params {
                    put("record", JSONObject(pointer.record))
                    putVerified("source_bin", pointer.bin)
                    put("xdf_paths", xdfPaths)
                },
            )
            _state.update { state ->
                when (outcome) {
                    is BridgeOutcome.Ok -> state.copy(
                        busy = false,
                        bin = pointer.bin,
                        xdf = pointer.xdf,
                        switchPatchXdf = pointer.switchPatchXdf,
                        // Recovery only ever restores a session that passed preflight
                        // when it was created; the engine re-verifies the source hash.
                        preflight = PreflightState.Passed(
                            summary = "Restored from a saved session.",
                            reasons = emptyList(),
                            switchPatchPresent = pointer.switchPatchXdf != null,
                        ),
                        sessionId = outcome.result.optString("session_id").takeIf { it.isNotEmpty() },
                        canUndo = outcome.result.optBoolean("can_undo", false),
                        canRedo = outcome.result.optBoolean("can_redo", false),
                        build = BuildState.NotBuilt,
                    )
                    is BridgeOutcome.Failed -> state.copy(
                        busy = false,
                        error = outcome.toUserFacing(),
                    )
                }
            }
            if (outcome is BridgeOutcome.Failed) {
                // A record that will not restore is worse than none: it would offer
                // the same dead end on every launch.
                recovery.clear()
                _recoverable.value = null
            }
        }
    }

    fun discardRecoverable() {
        viewModelScope.launch {
            recovery.clear()
            _recoverable.value = null
        }
    }

    // ------------------------------------------------------------ undo / redo

    fun undo() = historyOp("undo")

    fun redo() = historyOp("redo")

    private fun historyOp(op: String) {
        val sessionId = _state.value.sessionId ?: return
        viewModelScope.launch {
            _state.update { it.copy(busy = true, error = null) }
            val outcome = bridge.call(op, params { put("session_id", sessionId) })
            _state.update { state ->
                when (outcome) {
                    is BridgeOutcome.Ok -> state.copy(
                        busy = false,
                        canUndo = outcome.result.optBoolean("can_undo", false),
                        canRedo = outcome.result.optBoolean("can_redo", false),
                    ).invalidatingBuild()
                    is BridgeOutcome.Failed -> state.copy(busy = false, error = outcome.toUserFacing())
                }
            }
            persistRecovery()
        }
    }

    // ------------------------------------------------------------------ build

    /**
     * Run the full gate chain over the live session.
     *
     * The imported bin is both the byte-audit reference and the source, which is
     * what makes the audit meaningful: every changed byte must be explained by
     * the journal, against the exact bytes that were imported.
     */
    fun build(revision: String) {
        val current = _state.value
        val sessionId = current.sessionId ?: return
        val bin = current.bin ?: return
        if (!current.canBuild) return

        viewModelScope.launch {
            _state.update { it.copy(busy = true, build = BuildState.Running, error = null) }
            val outcome = bridge.call(
                op = "build",
                params = params {
                    put("session_id", sessionId)
                    put("revision", revision)
                    put("staging_dir", imports.stagingDir().absolutePath)
                    put("bin_name", bin.displayName)
                    putVerified("reference_bin", bin)
                    putVerified("source_bin", bin)
                },
            )
            _state.update { state ->
                when (outcome) {
                    is BridgeOutcome.Ok -> state.copy(
                        busy = false,
                        build = outcome.result.optJSONObject("report")
                            ?.toBuildState(revision, bin.displayName)
                            ?: BuildState.Failed("The build produced no report.", emptyList()),
                    )
                    is BridgeOutcome.Failed -> state.copy(
                        busy = false,
                        build = BuildState.NotBuilt,
                        error = outcome.toUserFacing(),
                    )
                }
            }
            persistRecovery()
        }
    }

    // ----------------------------------------------------------- persistence

    /**
     * Snapshot the live session to disk.
     *
     * Called after every state-changing op rather than on a timer: the failure
     * this guards against is an abrupt process death, which gives no warning.
     * A snapshot that fails is silent — losing recoverability is not worth
     * interrupting the person mid-edit, and nothing about the bin is at risk.
     */
    private suspend fun persistRecovery() {
        val current = _state.value
        val sessionId = current.sessionId ?: return
        val bin = current.bin ?: return
        val xdf = current.xdf ?: return

        val outcome = bridge.call("session_serialize", params { put("session_id", sessionId) })
        if (outcome !is BridgeOutcome.Ok) return
        val record = outcome.result.optJSONObject("record") ?: return

        val pointer = RecoveryPointer(
            record = record.toString(),
            bin = bin,
            xdf = xdf,
            switchPatchXdf = current.switchPatchXdf,
            savedAtMillis = System.currentTimeMillis(),
        )
        runCatching { recovery.save(pointer) }
    }

    companion object {
        /** Space name the engine opens the switch-patch XDF under. */
        const val PATCH_SPACE = "patch"
    }
}

// --------------------------------------------------------------- result mapping

private fun BridgeOutcome.Failed.toUserFacing() = UserFacingError(code, message, advanced)

private fun JSONObject.toStringList(key: String): List<String> {
    val array: JSONArray = optJSONArray(key) ?: return emptyList()
    return (0 until array.length()).map { index -> array.optString(index) }
}

private fun JSONObject.toPreflightState(): PreflightState {
    val summary = optString("summary", "")
    val reasons = toStringList("reasons")
    val patchPresent = if (isNull("switch_patch_present")) null else optBoolean("switch_patch_present")
    return if (optBoolean("ok_to_edit", false)) {
        PreflightState.Passed(summary, reasons, patchPresent)
    } else {
        PreflightState.Blocked(summary, reasons)
    }
}

private fun JSONObject.toBuildState(revision: String, binName: String): BuildState {
    val gatesArray = optJSONArray("gates")
    val gates = (0 until (gatesArray?.length() ?: 0)).mapNotNull { index ->
        gatesArray?.optJSONObject(index)?.let { gate ->
            GateResult(
                name = gate.optString("name"),
                passed = gate.optBoolean("passed", false),
                ran = gate.optBoolean("ran", false),
                detail = gate.optString("detail"),
            )
        }
    }

    val tablesArray = optJSONArray("changed_tables")
    val changedTables = (0 until (tablesArray?.length() ?: 0)).mapNotNull { index ->
        tablesArray?.optJSONObject(index)?.optString("label")
    }

    val sharePath = if (isNull("share_path")) null else optString("share_path").ifEmpty { null }

    // `verified` and a non-null share_path must agree. If they ever disagree the
    // build is treated as failed: the whole point of the verified state is that
    // it is the only state with something to share.
    return if (optBoolean("verified", false) && sharePath != null) {
        BuildState.Verified(
            revision = optString("revision", revision),
            sharePath = sharePath,
            binName = binName,
            changedTables = changedTables,
            gates = gates,
        )
    } else {
        BuildState.Failed(
            summary = optString("summary", "The build did not pass its checks."),
            reasons = toStringList("problems"),
        )
    }
}
