package com.simoscal.quickedit

import android.app.Application
import android.net.Uri
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import java.io.File
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
     * [ImportStore.importFile] moves itself to an IO dispatcher, so the copy —
     * which is also what hashes the bytes, so nothing downstream ever refers to
     * the picker's URI — never blocks composition, and the busy state set here
     * can actually render while a slow provider streams.
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
        if (refusingWhileDirty()) return
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
            // Undo moves the session's values out from under whatever is on screen.
            // Re-reading is not a refresh nicety: a grid still showing the undone
            // values would let someone Apply a draft built on numbers the session
            // no longer holds, and the diff they reviewed would not be the diff
            // they made.
            if (outcome is BridgeOutcome.Ok) refreshOpenViews()
            persistRecovery()
        }
    }

    /**
     * Refuse an engine-mutating action while an editor holds an unapplied draft.
     *
     * Returns true when the action was refused, having placed the reason in the
     * editor that is blocking. A confirmation dialog was the alternative; a
     * refusal is better here because there is nothing to decide — Apply and
     * Discard are both already on screen, one tap away, and neither loses work
     * (CR-20260813-04).
     */
    private fun refusingWhileDirty(): Boolean {
        val reason = _state.value.dirtyDraftRefusal ?: return false
        _state.update { state ->
            when (state.dirtyDraft) {
                DirtyDraft.BOOST -> state.copy(boost = state.boost.copy(notice = reason))
                DirtyDraft.TABLE -> state.copy(tables = state.tables.copy(notice = reason))
                null -> state
            }
        }
        return true
    }

    /** Re-read whichever editor surfaces are currently showing engine values. */
    private fun refreshOpenViews() {
        val current = _state.value
        if (current.boost.model != null) loadBoostCurve()
        current.tables.detail?.summary?.let { openTable(it) }
    }

    // ------------------------------------------------------------------ build

    /**
     * Run the full gate chain over the live session.
     *
     * The imported bin is both the byte-audit reference and the source, which is
     * what makes the audit meaningful: every changed byte must be explained by
     * the journal, against the exact bytes that were imported.
     *
     * The candidate's *file name* is the engine's to choose. The imported bin's
     * display name is whatever a document provider reported, which is untrusted
     * text — passing it on as a name once let `../escaped.bin` place a candidate
     * outside the shared staging tree (CR-20260813-05). It stays display-only,
     * shown on the build screen; the file on disk is named from the revision.
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
                    putVerified("reference_bin", bin)
                    putVerified("source_bin", bin)
                },
            )
            _state.update { state ->
                when (outcome) {
                    is BridgeOutcome.Ok -> state.copy(
                        busy = false,
                        build = outcome.result.optJSONObject("report")
                            ?.toBuildState(revision)
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

    // ------------------------------------------------------------------ boost

    /** Pure draft manipulation — no engine call, nothing committed. */
    fun onBoostSlotSelected(slot: Int) = _state.update { it.copy(boost = it.boost.selectingSlot(slot)) }

    fun onBoostPointDragged(index: Int, psi: Double) =
        _state.update { it.copy(boost = it.boost.withDraggedPoint(index, psi)) }

    fun onBoostPointTyped(index: Int, psi: Double) =
        _state.update { it.copy(boost = it.boost.withTypedPoint(index, psi)) }

    fun onBoostFlatCap(psi: Double) = _state.update { it.copy(boost = it.boost.withFlatCap(psi)) }

    fun onBoostSmooth() = _state.update { it.copy(boost = it.boost.smoothed()) }

    fun onBoostCopyFrom(slot: Int) = _state.update { it.copy(boost = it.boost.copyingFrom(slot)) }

    fun onBoostDiscard() = _state.update { it.copy(boost = it.boost.discardingDraft()) }

    fun dismissBoostNotice() = _state.update { it.copy(boost = it.boost.copy(notice = null)) }

    /**
     * Read the whole boost model: rpm axis, five slot curves, base ceiling.
     *
     * A `TUNE_ERROR` here means the session has no switch-patch space, which is a
     * *state* of this session rather than a failure of the call — so it lands in
     * [BoostUiState.unavailable] and the screen explains it, instead of flashing
     * an error snackbar the person can do nothing about.
     */
    fun loadBoostCurve() {
        val sessionId = _state.value.sessionId ?: return
        viewModelScope.launch {
            _state.update { it.copy(boost = it.boost.copy(loading = true, notice = null)) }
            val outcome = bridge.call("boost_curve", params { put("session_id", sessionId) })
            _state.update { state ->
                when (outcome) {
                    is BridgeOutcome.Ok -> {
                        val payload = outcome.result.optJSONObject("boost_curve")
                        if (payload == null) {
                            state.copy(
                                boost = state.boost.copy(
                                    loading = false,
                                    unavailable = "The engine returned no boost model.",
                                ),
                            )
                        } else {
                            state.copy(boost = state.boost.withModel(BoostCurveModel.fromJson(payload)))
                        }
                    }
                    is BridgeOutcome.Failed -> state.copy(
                        boost = state.boost.copy(loading = false, unavailable = outcome.message),
                        error = if (outcome.code == "TUNE_ERROR") null else outcome.toUserFacing(),
                    )
                }
            }
        }
    }

    /**
     * Commit the active slot's draft as one journaled boost edit.
     *
     * The whole curve goes in a single op even when one breakpoint moved: the
     * engine writes the slot grid atomically and journals one entry, so one
     * deliberate change stays one undo point.
     */
    fun applyBoostDraft(intent: String) {
        val current = _state.value
        val sessionId = current.sessionId ?: return
        val boost = current.boost
        if (!boost.canApply) return

        viewModelScope.launch {
            _state.update { it.copy(busy = true, error = null) }
            val outcome = bridge.call(
                op = "boost_edit",
                params = params {
                    put("session_id", sessionId)
                    put("slot", boost.activeSlot)
                    put("psi", boost.draft.toJsonArray())
                    put("intent", intent)
                },
            )
            _state.update { state ->
                when (outcome) {
                    is BridgeOutcome.Ok -> {
                        val receipt = BoostEditReceipt(
                            slot = outcome.result.optInt("slot", boost.activeSlot),
                            requestedPsi = outcome.result.doubleList("requested_psi"),
                            encodedPsi = outcome.result.doubleList("encoded_psi"),
                            floored = outcome.result.optBoolean("floored", false),
                        )
                        state.copy(
                            busy = false,
                            canUndo = outcome.result.optBoolean("can_undo", state.canUndo),
                            canRedo = outcome.result.optBoolean("can_redo", state.canRedo),
                            boost = state.boost.applied(receipt),
                        ).invalidatingBuild()
                    }
                    // A guard refusal is the editor's business, not a global error:
                    // it names a value the person can still change, and it belongs
                    // beside the curve rather than in a snackbar that scrolls away.
                    is BridgeOutcome.Failed -> state.copy(
                        busy = false,
                        boost = state.boost.copy(notice = outcome.message),
                        error = if (outcome.code == "EDIT_REJECTED") null else outcome.toUserFacing(),
                    )
                }
            }
            persistRecovery()
        }
    }

    /**
     * Re-breakpoint the rpm axis shared by all five slots (Advanced).
     *
     * The model is re-read rather than patched locally afterwards, because moving
     * the axis re-interpolates the base ceiling onto it — and that interpolation
     * is the engine's, not something the app should reproduce.
     */
    fun applySlotRpmAxis(breakpoints: List<Double>, intent: String) {
        val sessionId = _state.value.sessionId ?: return
        // The axis apply re-reads the whole boost model on success, so a staged
        // slot curve would vanish into it.
        if (refusingWhileDirty()) return
        viewModelScope.launch {
            _state.update { it.copy(busy = true, error = null) }
            val outcome = bridge.call(
                op = "boost_rpm_axis",
                params = params {
                    put("session_id", sessionId)
                    put("breakpoints", breakpoints.toJsonArray())
                    put("intent", intent)
                },
            )
            _state.update { state ->
                when (outcome) {
                    is BridgeOutcome.Ok -> state.copy(
                        busy = false,
                        canUndo = outcome.result.optBoolean("can_undo", state.canUndo),
                        canRedo = outcome.result.optBoolean("can_redo", state.canRedo),
                    ).invalidatingBuild()
                    is BridgeOutcome.Failed -> state.copy(
                        busy = false,
                        boost = state.boost.copy(notice = outcome.message),
                        error = if (outcome.code == "EDIT_REJECTED") null else outcome.toUserFacing(),
                    )
                }
            }
            if (outcome is BridgeOutcome.Ok) loadBoostCurve()
            persistRecovery()
        }
    }

    // ----------------------------------------------------------------- tables

    fun onTableQueryChanged(query: String) =
        _state.update { it.copy(tables = it.tables.copy(query = query)) }

    fun onTableClosed() = _state.update { it.copy(tables = it.tables.closingDetail()) }

    fun onCellToggled(cell: CellRef) = _state.update { it.copy(tables = it.tables.togglingCell(cell)) }

    fun onSelectAllCells() = _state.update { it.copy(tables = it.tables.selectingAll()) }

    fun onClearSelection() = _state.update { it.copy(tables = it.tables.clearingSelection()) }

    fun onCellTyped(cell: CellRef, value: Double) =
        _state.update { it.copy(tables = it.tables.withTypedCell(cell, value)) }

    fun onFillSelection(value: Double) = _state.update { it.copy(tables = it.tables.fillingSelection(value)) }

    fun onOffsetSelection(delta: Double) = _state.update { it.copy(tables = it.tables.offsettingSelection(delta)) }

    fun onScaleSelection(factor: Double) = _state.update { it.copy(tables = it.tables.scalingSelection(factor)) }

    fun onInterpolateSelection() = _state.update { it.copy(tables = it.tables.interpolatingSelection()) }

    fun onTableDiscard() = _state.update { it.copy(tables = it.tables.discardingDraft()) }

    fun dismissTableNotice() = _state.update { it.copy(tables = it.tables.copy(notice = null)) }

    /** List every table the profiles resolve — the curated set, not the whole XDF. */
    fun loadCatalog() {
        val sessionId = _state.value.sessionId ?: return
        viewModelScope.launch {
            _state.update { it.copy(tables = it.tables.copy(loading = true)) }
            val outcome = bridge.call("catalog", params { put("session_id", sessionId) })
            _state.update { state ->
                when (outcome) {
                    is BridgeOutcome.Ok -> {
                        val array = outcome.result.optJSONArray("tables")
                        val tables = (0 until (array?.length() ?: 0)).mapNotNull { index ->
                            array?.optJSONObject(index)?.let(TableSummary::fromJson)
                        }
                        state.copy(tables = state.tables.withCatalog(tables))
                    }
                    is BridgeOutcome.Failed -> state.copy(
                        tables = state.tables.copy(loading = false),
                        error = outcome.toUserFacing(),
                    )
                }
            }
        }
    }

    fun openTable(summary: TableSummary) {
        val sessionId = _state.value.sessionId ?: return
        viewModelScope.launch {
            _state.update { it.copy(tables = it.tables.copy(loading = true, notice = null)) }
            val outcome = bridge.call(
                op = "table_detail",
                params = params {
                    put("session_id", sessionId)
                    put("name", summary.name)
                    put("space", summary.space)
                },
            )
            _state.update { state ->
                when (outcome) {
                    is BridgeOutcome.Ok -> {
                        val payload = outcome.result.optJSONObject("table")
                        if (payload == null) {
                            state.copy(tables = state.tables.copy(loading = false, notice = "The engine returned no table."))
                        } else {
                            state.copy(tables = state.tables.withDetail(TableDetail.fromJson(payload)))
                        }
                    }
                    is BridgeOutcome.Failed -> state.copy(
                        tables = state.tables.copy(loading = false),
                        error = outcome.toUserFacing(),
                    )
                }
            }
        }
    }

    /** Commit the open table's draft as a single `paste` op over the whole grid. */
    fun applyTableDraft(intent: String) {
        val current = _state.value
        val sessionId = current.sessionId ?: return
        val summary = current.tables.detail?.summary ?: return
        if (!current.tables.canApply) return
        val proposed = current.tables.draft

        commitTableEdit(sessionId, summary, intent) {
            put("op", "paste")
            put("selection", JSONObject().put("kind", "all"))
            put("array", proposed.toJsonArray())
        }
    }

    /**
     * Put a table back to the values it held when the session opened.
     *
     * A real engine op rather than a local reset: only the journal knows what the
     * imported bin held, and after several edits the app's own copy no longer
     * does. It is also journaled like any other change, which is what keeps the
     * build's byte audit able to explain it.
     */
    fun restoreTable(intent: String) {
        val current = _state.value
        val sessionId = current.sessionId ?: return
        val summary = current.tables.detail?.summary ?: return
        // Restore is a generic `restore` op, so it is bound by the same two rules
        // as any other generic write: reversible, and not domain-owned.
        if (!current.tables.writable) return
        // It also rewrites the grid from the journal, which would swallow a
        // staged proposal exactly as Undo would.
        if (refusingWhileDirty()) return

        commitTableEdit(sessionId, summary, intent) {
            put("op", "restore")
            put("selection", JSONObject().put("kind", "all"))
        }
    }

    private fun commitTableEdit(
        sessionId: String,
        summary: TableSummary,
        intent: String,
        addOp: JSONObject.() -> Unit,
    ) {
        viewModelScope.launch {
            _state.update { it.copy(busy = true, error = null) }
            val outcome = bridge.call(
                op = "edit",
                params = params {
                    put("session_id", sessionId)
                    put("name", summary.name)
                    put("space", summary.space)
                    put("intent", intent)
                    addOp()
                },
            )
            _state.update { state ->
                when (outcome) {
                    is BridgeOutcome.Ok -> {
                        val receipt = TableEditReceipt(
                            label = summary.idAndDescription,
                            quantized = outcome.result.optBoolean("quantized", false),
                            maxAbsQuantization = outcome.result.optDouble("max_abs_quantization", 0.0),
                            warning = outcome.result.optString("warning", ""),
                            encoded = outcome.result.grid("encoded"),
                        )
                        state.copy(
                            busy = false,
                            canUndo = outcome.result.optBoolean("can_undo", state.canUndo),
                            canRedo = outcome.result.optBoolean("can_redo", state.canRedo),
                            tables = state.tables.applied(receipt),
                        ).invalidatingBuild()
                    }
                    is BridgeOutcome.Failed -> state.copy(
                        busy = false,
                        tables = state.tables.copy(notice = outcome.message),
                        error = if (outcome.code == "EDIT_REJECTED") null else outcome.toUserFacing(),
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

private fun JSONObject.toBuildState(revision: String): BuildState {
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
            // The candidate's own file name, read off the path the engine
            // returned — not the imported bin's display name. They are different
            // files, and this card names the one about to be handed to SimosTools.
            binName = File(sharePath).name,
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
