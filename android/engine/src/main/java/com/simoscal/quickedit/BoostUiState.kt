package com.simoscal.quickedit

import kotlin.math.abs

/**
 * The boost editor's screen state and every transition it can make — pure.
 *
 * The editor is deliberately *staged*: a drag moves a [draft], and only Apply
 * turns that draft into one journaled engine op. A fingertip sliding across
 * twelve breakpoints would otherwise commit dozens of edits, and an undo history
 * where one gesture is fifty entries is a history nobody can reason about — on a
 * bin that gets flashed to a real ECU.
 *
 * Everything here is a plain function of state, so the rules can be pinned on the
 * JVM. The view model adds only the bridge calls.
 */

/** What the engine actually encoded for a committed slot edit. */
data class BoostEditReceipt(
    val slot: Int,
    val requestedPsi: List<Double>,
    val encodedPsi: List<Double>,
    /** The psi floor moved at least one point below what was asked for. */
    val floored: Boolean,
)

data class BoostUiState(
    val model: BoostCurveModel? = null,
    val activeSlot: Int = SLOT_IDS.first(),
    /** The working curve for [activeSlot]. Empty until a model is loaded. */
    val draft: List<Double> = emptyList(),
    val loading: Boolean = false,
    /** Why there is nothing to edit, when that is the case. */
    val unavailable: String? = null,
    val lastEdit: BoostEditReceipt? = null,
    /** A transient inline message — a refused entry, a blocked slot switch. */
    val notice: String? = null,
) {

    /** The committed curve for [activeSlot], as the engine last reported it. */
    val committed: List<Double>
        get() = model?.curve(activeSlot)?.psi ?: emptyList()

    /**
     * Whether the draft differs from what the engine holds.
     *
     * A size mismatch reads as *not* dirty, which also makes [canApply] false —
     * the safe direction. A draft whose length does not match the slot grid is a
     * bug somewhere upstream, and the response to that is to refuse to send it,
     * not to send it and let the engine's shape check be the first thing to notice.
     */
    val dirty: Boolean
        get() = draft.size == committed.size &&
            draft.indices.any { abs(draft[it] - committed[it]) > 1e-9 }

    val canApply: Boolean
        get() = model != null && dirty

    /** Breakpoints of the *draft* the base ceiling would swallow. */
    val draftCappedByBase: List<Int>
        get() {
            val ceiling = model?.baseCeilingPsi ?: return emptyList()
            return draft.indices.filter { index ->
                draft[index] > ceiling.getOrElse(index) { Double.MAX_VALUE } + 1e-9
            }
        }
}

/** Adopt a freshly read model, starting a clean draft on the active slot. */
fun BoostUiState.withModel(loaded: BoostCurveModel): BoostUiState {
    val slot = if (loaded.curve(activeSlot) != null) activeSlot else loaded.slots.firstOrNull()?.slot ?: activeSlot
    return copy(
        model = loaded,
        activeSlot = slot,
        draft = loaded.curve(slot)?.psi.orEmpty(),
        loading = false,
        unavailable = null,
        notice = null,
    )
}

/**
 * Switch slots — refused while the draft is dirty.
 *
 * Not a nag. The alternative is dropping an edit the person made on the way to
 * another slot, and a boost cap that silently reverted is exactly the kind of
 * thing someone discovers on a datalog rather than on screen.
 */
fun BoostUiState.selectingSlot(slot: Int): BoostUiState = when {
    slot == activeSlot -> copy(notice = null)
    dirty -> copy(notice = "Apply or discard the change to slot $activeSlot first.")
    else -> copy(
        activeSlot = slot,
        draft = model?.curve(slot)?.psi.orEmpty(),
        notice = null,
    )
}

/** Move one breakpoint by drag: snapped into the legal range, never refused. */
fun BoostUiState.withDraggedPoint(index: Int, psi: Double): BoostUiState {
    val current = model ?: return this
    return copy(draft = current.withDraggedPoint(draft, index, psi), notice = null)
}

/**
 * Set one breakpoint from typed input: validated, never clamped.
 *
 * A typed number is a stated intent, and quietly storing a different one is the
 * single thing this project's safety model forbids most plainly. So an
 * out-of-range entry leaves the draft alone and says why.
 */
fun BoostUiState.withTypedPoint(index: Int, psi: Double): BoostUiState {
    val current = model ?: return this
    val refusal = current.rejectTypedPsi(psi)
    if (refusal != null) return copy(notice = refusal)
    if (index !in draft.indices) return this
    return copy(draft = draft.toMutableList().also { it[index] = psi }, notice = null)
}

/** Flatten the draft to one cap. Typed, so it is validated rather than clamped. */
fun BoostUiState.withFlatCap(psi: Double): BoostUiState {
    val current = model ?: return this
    val refusal = current.rejectTypedPsi(psi)
    if (refusal != null) return copy(notice = refusal)
    return copy(draft = List(draft.size) { psi }, notice = null)
}

fun BoostUiState.smoothed(): BoostUiState {
    val current = model ?: return this
    return copy(draft = current.smooth(draft), notice = null)
}

/** Copy another slot's committed curve into the draft. */
fun BoostUiState.copyingFrom(slot: Int): BoostUiState {
    val current = model ?: return this
    if (slot == activeSlot) return copy(notice = "Slot $slot is already the one being edited.")
    val source = current.copySlot(slot)
        ?: return copy(notice = "Slot $slot has no curve to copy.")
    return copy(draft = source, notice = null)
}

fun BoostUiState.discardingDraft(): BoostUiState = copy(draft = committed, notice = null)

/**
 * Fold a committed edit's *encoded* values back into the model.
 *
 * The encoded curve, not the requested one: psi is floored on its way to stored
 * hPa, so what the bin now holds can sit a hair under what was asked for. Showing
 * the request back would quietly overstate the bin's contents by up to a floor's
 * width, which is the same class of lie as clamping.
 */
fun BoostUiState.applied(receipt: BoostEditReceipt): BoostUiState {
    val current = model ?: return this
    val updated = current.copy(
        slots = current.slots.map { curve ->
            if (curve.slot == receipt.slot) curve.copy(psi = receipt.encodedPsi) else curve
        }
    )
    return copy(
        model = updated,
        draft = if (receipt.slot == activeSlot) receipt.encodedPsi else draft,
        lastEdit = receipt,
        notice = null,
    )
}
