package com.simoscal.quickedit.ui

import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Shapes
import androidx.compose.material3.Surface
import androidx.compose.material3.Typography
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

/**
 * The promo palette, transcribed from `Docs/promo/config.py` — one RGB triple per
 * entry, same names, same order.
 *
 * The video and the app are the same product seen twice, so they are painted from
 * one list of colours rather than two that drift. Anything added here must be
 * added there first: the video is built from the library's real output and is the
 * older of the two, which makes it the source.
 *
 * The roles the names carry are load-bearing, not decorative:
 * [Accent] is boost and heat — the thing that changed; [Accent2] is verification;
 * [Good], [Warn], [Danger] are pass, caution, and refusal.
 */
object PromoPalette {
    /** `bg` — near-black, slightly blue. */
    val Bg = Color(0xFF0B0E14)

    /** `bg_alt` — panel fill. */
    val BgAlt = Color(0xFF121720)

    /** `rule` — hairlines and borders. */
    val Rule = Color(0xFF2C3646)

    /** `text` — primary display text. */
    val Text = Color(0xFFECF0F6)

    /** `text_dim` — secondary text and captions. */
    val TextDim = Color(0xFF96A2B4)

    /** `text_faint` — tertiary text, axis labels, units. */
    val TextFaint = Color(0xFF5E697A)

    /** `accent` — simoscal orange: boost, heat, the changed thing. */
    val Accent = Color(0xFFFF8A2E)

    /** `accent_2` — cool blue: verification. */
    val Accent2 = Color(0xFF56BEFF)

    /** `good` — checks pass. */
    val Good = Color(0xFF62D68C)

    /** `warn` — knock, caution. */
    val Warn = Color(0xFFFFCD5C)

    /** `danger` — limits, refusals, the flash gate. */
    val Danger = Color(0xFFFF6060)

    // Container fills. The video has no filled containers — it separates panels
    // with a hairline on the same near-black — so these are derived rather than
    // transcribed: each is `Bg` carried a little way toward its accent, dark
    // enough that the accent itself stays the brightest thing in the panel.
    val AccentContainer = Color(0xFF33210F)
    val Accent2Container = Color(0xFF0F2433)
    val GoodContainer = Color(0xFF11291C)
    val WarnContainer = Color(0xFF2E2611)
    val DangerContainer = Color(0xFF33161A)

    /** A border a shade below [Rule], for the grid's own cell edges. */
    val RuleFaint = Color(0xFF1E2634)
}

/**
 * One scheme, always dark — the app looks like the promo video or it does not.
 *
 * There is no light variant and no `dynamicColorScheme`. Material You would paint
 * this app in whatever the wallpaper is, which is the opposite of the intent:
 * [PromoPalette.Accent] means *boost and heat* here, [PromoPalette.Danger] means
 * *the engine refused*, and a scheme that reassigns those hues per device makes
 * the one screen someone reads in a hurry look different on every phone.
 *
 * `error` stays the loudest thing in the scheme, as it was before the repaint: a
 * blocked preflight and a failed gate are the two moments this app most needs to
 * be unmistakable.
 */
private val PromoColors = darkColorScheme(
    primary = PromoPalette.Accent,
    onPrimary = PromoPalette.Bg,
    primaryContainer = PromoPalette.AccentContainer,
    onPrimaryContainer = PromoPalette.Accent,
    inversePrimary = PromoPalette.Accent,

    secondary = PromoPalette.Accent2,
    onSecondary = PromoPalette.Bg,
    secondaryContainer = PromoPalette.Accent2Container,
    onSecondaryContainer = PromoPalette.Accent2,

    tertiary = PromoPalette.Good,
    onTertiary = PromoPalette.Bg,
    tertiaryContainer = PromoPalette.GoodContainer,
    onTertiaryContainer = PromoPalette.Good,

    background = PromoPalette.Bg,
    onBackground = PromoPalette.Text,
    surface = PromoPalette.Bg,
    onSurface = PromoPalette.Text,
    surfaceVariant = PromoPalette.BgAlt,
    onSurfaceVariant = PromoPalette.TextDim,
    surfaceTint = PromoPalette.Accent,
    inverseSurface = PromoPalette.Text,
    inverseOnSurface = PromoPalette.Bg,

    error = PromoPalette.Danger,
    onError = PromoPalette.Bg,
    errorContainer = PromoPalette.DangerContainer,
    onErrorContainer = PromoPalette.Danger,

    outline = PromoPalette.Rule,
    outlineVariant = PromoPalette.RuleFaint,
    scrim = Color(0xFF000000),
)

/**
 * Squarer than Material's default.
 *
 * The video draws instrument panels — hairline boxes with an 18 px radius on a
 * 1920 px canvas, which is barely a radius at all. Material's 12 dp cards read as
 * consumer-app pills next to that, so every step is pulled in.
 */
private val PromoShapes = Shapes(
    extraSmall = RoundedCornerShape(3.dp),
    small = RoundedCornerShape(5.dp),
    medium = RoundedCornerShape(7.dp),
    large = RoundedCornerShape(10.dp),
    extraLarge = RoundedCornerShape(14.dp),
)

/**
 * Display type, tightened toward the video's.
 *
 * The video sets its display faces in SF Pro and its figures in Menlo. Neither
 * ships with Android and bundling them would put two font files into an APK that
 * already carries Python and numpy, so the platform's own sans and monospace
 * stand in — the *structure* is what carries the look: heavy tight titles, dim
 * captions, and every number in a monospace face (see [PromoType]).
 */
private val PromoTypography = Typography().let { base ->
    base.copy(
        headlineMedium = base.headlineMedium.copy(
            fontWeight = FontWeight.Bold,
            letterSpacing = (-0.5).sp,
        ),
        headlineSmall = base.headlineSmall.copy(
            fontWeight = FontWeight.Bold,
            letterSpacing = (-0.4).sp,
        ),
        titleMedium = base.titleMedium.copy(fontWeight = FontWeight.SemiBold),
        titleSmall = base.titleSmall.copy(fontWeight = FontWeight.SemiBold),
    )
}

/**
 * The named styles the video uses that Material has no slot for.
 *
 * Font family only where it matters and no colour at all: colour is applied at the
 * call site, so one style can serve a dim caption and an accent-coloured figure.
 */
object PromoType {
    /**
     * The tracked uppercase kicker over every beat of the video ("FIVE MAPS, ONE
     * SWITCH"). Rendered through [Kicker], which also does the uppercasing.
     */
    val kicker = TextStyle(
        fontSize = 12.sp,
        fontWeight = FontWeight.Bold,
        letterSpacing = 2.4.sp,
    )

    /** Table IDs, bin names, hashes — anything that identifies rather than reads. */
    val identifier = TextStyle(
        fontFamily = FontFamily.Monospace,
        fontSize = 14.sp,
        letterSpacing = 0.sp,
    )

    /** Small monospace: grid cells, axis breakpoints, slot values. */
    val figureSmall = TextStyle(
        fontFamily = FontFamily.Monospace,
        fontSize = 11.sp,
        letterSpacing = 0.sp,
    )

    /** The big number the video hangs a beat on — 298 HP, +4.4 PSI. */
    val figureLarge = TextStyle(
        fontSize = 34.sp,
        fontWeight = FontWeight.Bold,
        letterSpacing = (-1).sp,
    )
}

/**
 * Wraps content in the promo look, on an opaque [PromoPalette.Bg] ground.
 *
 * The [Surface] is not redundant with the window background: it keeps the app's
 * own ground under every screen regardless of what the platform theme does with
 * the window, and it sets the default content colour every unstyled `Text`
 * inherits.
 */
@Composable
fun QuickEditTheme(content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = PromoColors,
        typography = PromoTypography,
        shapes = PromoShapes,
    ) {
        Surface(color = PromoPalette.Bg, contentColor = PromoPalette.Text, content = content)
    }
}
