package com.simoscal.quickedit.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.RowScope
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.AssistChipDefaults
import androidx.compose.material3.Button
import androidx.compose.material3.ChipColors
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChipDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.SelectableChipColors
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.withStyle
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.TextUnit
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

/**
 * The handful of drawn elements the promo video is actually made of.
 *
 * The video has no widgets — it has a wordmark, a tracked orange kicker over each
 * beat, hairline-bordered panels on a near-black ground, and monospace figures.
 * Those are collected here so every screen reaches for the same four things
 * rather than each re-deriving the look out of Material defaults.
 */

// ------------------------------------------------------------------- wordmark

/**
 * `simos` in text, `cal` in accent, monospace — the video's opening and closing
 * frame, and the only place the product signs its own name.
 *
 * Split exactly where the video splits it. The two halves are one [Text] rather
 * than a [androidx.compose.foundation.layout.Row] of two so the pair can never
 * wrap or space apart at a different font scale.
 */
@Composable
fun Wordmark(modifier: Modifier = Modifier, fontSize: TextUnit = 22.sp) {
    Text(
        buildAnnotatedString {
            withStyle(SpanStyle(color = PromoPalette.Text)) { append("simos") }
            withStyle(SpanStyle(color = PromoPalette.Accent)) { append("cal") }
        },
        fontFamily = FontFamily.Monospace,
        fontSize = fontSize,
        fontWeight = FontWeight.Medium,
        modifier = modifier,
    )
}

// --------------------------------------------------------------------- kicker

/**
 * The tracked uppercase label the video puts over every beat.
 *
 * Uppercasing happens here rather than at the call site so the strings stay
 * readable in source and in any future translation — the caps are styling, not
 * content.
 */
@Composable
fun Kicker(
    text: String,
    modifier: Modifier = Modifier,
    color: Color = PromoPalette.Accent,
) {
    Text(
        text.uppercase(),
        style = PromoType.kicker,
        color = color,
        modifier = modifier,
    )
}

/**
 * Kicker over screen title — the standing header on every destination, matching
 * how each beat of the video announces itself.
 */
@Composable
fun ScreenHeader(kicker: String, title: String, modifier: Modifier = Modifier) {
    Column(modifier = modifier, verticalArrangement = Arrangement.spacedBy(6.dp)) {
        Kicker(kicker)
        Text(title, style = MaterialTheme.typography.headlineSmall)
    }
}

// ---------------------------------------------------------------------- rules

/**
 * A hairline, the video's only separator.
 *
 * Material's `Divider` is 1 dp of `outlineVariant` inset from the edges; this is
 * the same weight in [PromoPalette.Rule] and full-bleed, which is what the video
 * draws under its wordmark and between its panels.
 */
@Composable
fun HairRule(modifier: Modifier = Modifier, color: Color = PromoPalette.Rule) {
    Box(
        modifier
            .fillMaxWidth()
            .height(1.dp)
            .background(color)
    )
}

// --------------------------------------------------------------------- panels

/**
 * What a panel is saying, which decides its border and its title colour.
 *
 * Tone is carried on the *edge* and in the title, never as a wash of colour
 * across the fill: the video keeps every panel on the same near-black so that a
 * coloured thing on screen is always a coloured *number*, not decoration. The
 * containers are dark enough to read as tinted black rather than as a card.
 */
enum class PanelTone {
    /** Ordinary content: a hairline and nothing else. */
    Neutral,

    /** The changed thing — an applied edit, a staged proposal. */
    Accent,

    /** A check that passed. */
    Good,

    /** Worth reading before continuing. */
    Warn,

    /** A refusal, a failure, a limit. */
    Danger,
    ;

    internal val fill: Color
        get() = when (this) {
            Neutral -> PromoPalette.BgAlt
            Accent -> PromoPalette.AccentContainer
            Good -> PromoPalette.GoodContainer
            Warn -> PromoPalette.WarnContainer
            Danger -> PromoPalette.DangerContainer
        }

    internal val edge: Color
        get() = when (this) {
            Neutral -> PromoPalette.Rule
            Accent -> PromoPalette.Accent.copy(alpha = 0.45f)
            Good -> PromoPalette.Good.copy(alpha = 0.45f)
            Warn -> PromoPalette.Warn.copy(alpha = 0.45f)
            Danger -> PromoPalette.Danger.copy(alpha = 0.55f)
        }

    /** The colour a [PanelTitle] takes inside this panel. */
    internal val ink: Color
        get() = when (this) {
            Neutral -> PromoPalette.Text
            Accent -> PromoPalette.Accent
            Good -> PromoPalette.Good
            Warn -> PromoPalette.Warn
            Danger -> PromoPalette.Danger
        }
}

/**
 * A hairline-bordered block on the app's own ground — the video's panel, and this
 * app's replacement for a Material `Card`.
 *
 * Flat by design: no elevation and no shadow. A raised surface implies a stack of
 * paper, and every one of these panels is a *readout* — what the bin holds, what
 * a gate found — which is a thing to be read off a face, not picked up.
 */
@Composable
fun Panel(
    modifier: Modifier = Modifier,
    tone: PanelTone = PanelTone.Neutral,
    padding: Dp = 16.dp,
    spacing: Dp = 4.dp,
    onClick: (() -> Unit)? = null,
    content: @Composable ColumnScope.() -> Unit,
) {
    val shape = MaterialTheme.shapes.large
    Column(
        modifier = modifier
            .fillMaxWidth()
            .background(tone.fill, shape)
            .border(1.dp, tone.edge, shape)
            .then(if (onClick != null) Modifier.clickable(onClick = onClick) else Modifier)
            .padding(padding),
        verticalArrangement = Arrangement.spacedBy(spacing),
        content = content,
    )
}

/** A panel's heading, in that panel's tone. */
@Composable
fun PanelTitle(text: String, tone: PanelTone = PanelTone.Neutral, modifier: Modifier = Modifier) {
    Text(
        text,
        style = MaterialTheme.typography.titleSmall,
        color = tone.ink,
        modifier = modifier,
    )
}

/** Dim supporting text — the video's caption line under a plot. */
@Composable
fun Caption(text: String, modifier: Modifier = Modifier, color: Color = PromoPalette.TextDim) {
    Text(text, style = MaterialTheme.typography.bodySmall, color = color, modifier = modifier)
}

/** Monospace, for anything that identifies a file, a table, or a hash. */
@Composable
fun Identifier(text: String, modifier: Modifier = Modifier, color: Color = PromoPalette.Text) {
    Text(text, style = PromoType.identifier, color = color, modifier = modifier)
}

// -------------------------------------------------------------------- buttons

/**
 * The app's buttons, squared off.
 *
 * Material draws both of these as full pills, and the theme cannot change that:
 * their shape comes from the `CornerFull` token, which is not one of the five
 * entries in [androidx.compose.material3.Shapes], so [PromoShapes] never reaches
 * it. A pill is the one shape left on screen that reads as a consumer app rather
 * than an instrument, hence the wrappers — every button in the app goes through
 * them, which is also the only way this stays consistent as screens are added.
 *
 * `TextButton` is deliberately not wrapped: it has no container to shape.
 */
@Composable
fun PromoButton(
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
    enabled: Boolean = true,
    content: @Composable RowScope.() -> Unit,
) {
    Button(
        onClick = onClick,
        modifier = modifier,
        enabled = enabled,
        shape = MaterialTheme.shapes.small,
        content = content,
    )
}

@Composable
fun PromoOutlinedButton(
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
    enabled: Boolean = true,
    content: @Composable RowScope.() -> Unit,
) {
    OutlinedButton(
        onClick = onClick,
        modifier = modifier,
        enabled = enabled,
        shape = MaterialTheme.shapes.small,
        content = content,
    )
}

// ---------------------------------------------------------------------- chips

/**
 * Selected chips take the accent, not Material's secondary container.
 *
 * Slot selection and the Simple/Advanced toggle are the two things on screen that
 * say *which thing you are about to edit*, so they get the colour the rest of the
 * app reserves for the live one.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun promoFilterChipColors(): SelectableChipColors = FilterChipDefaults.filterChipColors(
    containerColor = PromoPalette.BgAlt,
    labelColor = PromoPalette.TextDim,
    selectedContainerColor = PromoPalette.AccentContainer,
    selectedLabelColor = PromoPalette.Accent,
)

@Composable
fun promoAssistChipColors(): ChipColors = AssistChipDefaults.assistChipColors(
    containerColor = PromoPalette.BgAlt,
    labelColor = PromoPalette.TextDim,
)
