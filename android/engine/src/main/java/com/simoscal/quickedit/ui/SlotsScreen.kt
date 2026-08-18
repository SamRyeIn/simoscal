package com.simoscal.quickedit.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.simoscal.quickedit.QuickEditViewModel
import com.simoscal.quickedit.SettingKind
import com.simoscal.quickedit.SlotSetting
import com.simoscal.quickedit.ValueFormat

/**
 * The per-slot switchboard: every setting that differs between map slots, on one
 * screen, five slots across.
 *
 * The switch patch is one shared tune plus a per-slot decision about which
 * features are on. That decision is comparative — "which slots have launch
 * control enabled" — and the generic table editor answers it badly: five tables
 * opened in turn, and the one you skipped is the one that surprises you in the
 * car. Here every slot is visible at once and a tap is one journaled edit.
 *
 * Four of the sixteen settings are read-only, and they are shown anyway. A
 * screen that hid them would be claiming the patch has twelve per-slot settings,
 * which is false; showing them greyed with the reason attached says what is
 * actually true — we can read these and have no business writing them yet.
 */
@Composable
fun SlotsScreen(viewModel: QuickEditViewModel) {
    val state by viewModel.state.collectAsStateWithLifecycle()
    val slots = state.slots

    LaunchedEffect(state.sessionId) {
        if (state.sessionId != null && !slots.loaded) viewModel.loadSlotSettings()
    }

    Column(
        modifier = Modifier
            .fillMaxWidth()
            .verticalScroll(rememberScrollState())
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        ScreenHeader(kicker = "What each slot turns on", title = "Slots")

        SessionProvenanceCard(
            binName = state.bin?.displayName,
            shortHash = state.bin?.shortHash,
            advanced = state.mode == com.simoscal.quickedit.Mode.ADVANCED,
        )

        if (!slots.loaded) {
            NoticeCard(
                title = if (slots.loading) "Reading the slot settings…" else "No slot settings",
                body = slots.notice
                    ?: "The switch patch's per-slot settings are being read from the session.",
            )
            return@Column
        }

        Caption(
            "Everything the switch patch lets you set per map slot. The features " +
                "themselves are tuned once and shared by every slot — what changes " +
                "here is which slots turn them on. Each tap is one journaled edit " +
                "and its own undo point."
        )

        slots.groups.forEach { (group, rows) ->
            Kicker(
                group.ifBlank { "Other" },
                modifier = Modifier.padding(top = 8.dp),
            )
            SlotHeaderRow(slots.slots)
            rows.forEach { setting ->
                SettingRow(
                    setting = setting,
                    slots = slots.slots,
                    expanded = slots.expanded == setting.key,
                    busy = setting.key in slots.pending,
                    onExpand = { viewModel.onSlotSettingExpanded(setting.key) },
                    onToggle = { slot ->
                        viewModel.setSlotFlag(
                            key = setting.key,
                            slot = slot,
                            on = !setting.isOn(slot),
                            intent = "${if (setting.isOn(slot)) "disable" else "enable"} " +
                                "${setting.title} on slot $slot from Quick Edit",
                        )
                    },
                )
            }
        }

        slots.notice?.let { notice ->
            NoticeCard(title = "Not applied", body = notice, emphasise = true)
        }

        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            PromoOutlinedButton(
                onClick = viewModel::undo,
                enabled = state.canUndo && state.canMutateSession,
            ) { Text("Undo") }
            PromoOutlinedButton(
                onClick = viewModel::redo,
                enabled = state.canRedo && state.canMutateSession,
            ) { Text("Redo") }
        }
    }
}

private val LABEL_WIDTH = 168.dp
private val SLOT_WIDTH = 60.dp

@Composable
private fun SlotHeaderRow(slots: List<Int>) {
    Row(verticalAlignment = Alignment.CenterVertically) {
        Box(Modifier.width(LABEL_WIDTH))
        slots.forEach { slot ->
            Text(
                "$slot",
                style = MaterialTheme.typography.labelMedium,
                fontWeight = FontWeight.Bold,
                textAlign = TextAlign.Center,
                // Each column headed in its own slot colour — the same five the
                // boost canvas draws its curves in, so "slot 4" means one thing
                // across the whole app.
                color = slotColor(slot),
                modifier = Modifier.width(SLOT_WIDTH),
            )
        }
    }
}

/**
 * One setting across all five slots, with its detail one tap away.
 *
 * The label is the tap target for the explanation rather than an "i" button: on
 * a screen of sixteen near-identical rows, the thing a person wants to know is
 * "what is this one", and making the name itself answer that is fewer targets
 * to hit and fewer to miss.
 */
@Composable
private fun SettingRow(
    setting: SlotSetting,
    slots: List<Int>,
    expanded: Boolean,
    busy: Boolean,
    onExpand: () -> Unit,
    onToggle: (Int) -> Unit,
) {
    Column {
        Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.fillMaxWidth()) {
            Column(
                modifier = Modifier
                    .width(LABEL_WIDTH)
                    .clickable(onClick = onExpand)
                    .padding(vertical = 6.dp, horizontal = 2.dp),
            ) {
                Text(
                    setting.title,
                    style = MaterialTheme.typography.bodyMedium,
                    color = if (setting.toggleable) PromoPalette.Text else PromoPalette.TextDim,
                )
                Text(
                    when {
                        !setting.writable -> "read-only · why?"
                        setting.caution.isNotBlank() -> "${setting.onCount} of ${slots.size} on · caution"
                        else -> "${setting.onCount} of ${slots.size} on"
                    },
                    style = MaterialTheme.typography.labelSmall,
                    // `warn`, not `danger`: a caution is something to read before
                    // toggling, not a refusal. The palette keeps them apart and so
                    // should the row.
                    color = if (setting.caution.isNotBlank() && setting.writable) {
                        PromoPalette.Warn
                    } else {
                        PromoPalette.TextFaint
                    },
                )
            }
            slots.forEach { slot ->
                Box(
                    modifier = Modifier.width(SLOT_WIDTH),
                    contentAlignment = Alignment.Center,
                ) {
                    SlotCell(setting = setting, slot = slot, busy = busy, onToggle = onToggle)
                }
            }
        }
        if (expanded) SettingDetail(setting)
    }
}

/**
 * One slot's value for one setting.
 *
 * A flag is a filled/empty pill rather than a Switch: five Material switches in a
 * row is 300dp of chrome for five bits, and the state that matters — how many
 * slots are on and which — reads better as a row of blocks than as a row of
 * sliders. The non-flag kinds print their value instead, because there is
 * nothing to toggle and a switch would imply there was.
 */
@Composable
private fun SlotCell(
    setting: SlotSetting,
    slot: Int,
    busy: Boolean,
    onToggle: (Int) -> Unit,
) {
    val value = setting.valueIn(slot)
    if (setting.kind != SettingKind.FLAG) {
        Text(
            value?.let { ValueFormat.of(setting.values).format(it) } ?: "—",
            style = PromoType.figureSmall,
            color = PromoPalette.TextDim,
        )
        return
    }

    val on = setting.isOn(slot)
    val enabled = setting.toggleable && !busy
    // A lit pill is the accent, an unlit one is the ground: a row of these reads
    // as an instrument's own indicator lamps, which is the question the screen
    // exists to answer at a glance — which slots have this on.
    val fill = when {
        !enabled && on -> PromoPalette.Rule
        on -> PromoPalette.Accent
        else -> Color.Transparent
    }
    Box(
        modifier = Modifier
            .size(width = 44.dp, height = 30.dp)
            .background(fill, RoundedCornerShape(4.dp))
            .border(
                1.dp,
                if (enabled) PromoPalette.Rule else PromoPalette.RuleFaint,
                RoundedCornerShape(4.dp),
            )
            .then(
                if (enabled) {
                    Modifier.clickable(
                        onClickLabel = if (on) "Turn off on slot $slot" else "Turn on on slot $slot",
                        onClick = { onToggle(slot) },
                    )
                } else {
                    Modifier
                }
            ),
        contentAlignment = Alignment.Center,
    ) {
        Text(
            if (on) "on" else "off",
            style = PromoType.figureSmall,
            color = if (on && enabled) PromoPalette.Bg else PromoPalette.TextFaint,
        )
    }
}

/** What the setting is, why it will not move, and what it does to a moving car. */
@Composable
private fun SettingDetail(setting: SlotSetting) {
    // A caution is the one thing on this screen that changes what a car does
    // without changing a number, so the panel it opens into takes the warn tone.
    val tone = if (setting.caution.isNotBlank()) PanelTone.Warn else PanelTone.Neutral
    Panel(
        tone = tone,
        padding = 12.dp,
        spacing = 6.dp,
        modifier = Modifier.padding(bottom = 8.dp),
    ) {
        Text(setting.description, style = MaterialTheme.typography.bodySmall)
        if (setting.units.isNotBlank()) {
            Text(
                "Units: ${setting.units}",
                style = PromoType.figureSmall,
                color = PromoPalette.TextFaint,
            )
        }
        if (setting.caution.isNotBlank()) {
            Caption("Caution — ${setting.caution}", color = PromoPalette.Warn)
        }
        if (setting.readonly.isNotBlank()) {
            Text(
                "Read-only here — ${setting.readonly}",
                style = MaterialTheme.typography.bodySmall,
                fontWeight = FontWeight.Medium,
            )
        }
        if (setting.kind == SettingKind.UNKNOWN) {
            Caption(
                "This engine reports a kind of setting this app does not " +
                    "recognise, so it is shown but never written."
            )
        }
    }
}
