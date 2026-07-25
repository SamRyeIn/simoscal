package com.simoscal.quickedit.ui

import androidx.compose.foundation.gestures.detectDragGestures
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.PathEffect
import androidx.compose.ui.graphics.drawscope.DrawScope
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.foundation.Canvas
import androidx.compose.material3.MaterialTheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.text.ExperimentalTextApi
import androidx.compose.ui.text.TextMeasurer
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.drawText
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.rememberTextMeasurer
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.simoscal.quickedit.BoostCurveModel
import com.simoscal.quickedit.BoostPlotGeometry
import com.simoscal.quickedit.BoostPlotScale
import kotlin.math.max
import kotlin.math.roundToInt

/**
 * The hero surface: five slot boost caps against the base ceiling, on one canvas.
 *
 * Colour runs cool→warm with slot number because the switch patch orders its
 * slots least→most boost; the ramp carries that ordering without a legend. Only
 * the active slot is drawn at full strength and only it responds to a drag — the
 * others are ghosted context, because a fingertip that could land on any of five
 * overlapping curves would sometimes move the wrong one, and the wrong one here
 * is a boost table.
 *
 * Two limits are drawn, and they are not the same limit (see [BoostCurveModel]):
 * the solid ceiling line is the per-rpm base cap, above which a slot has no
 * effect; the dashed line is the scalar value the engine outright refuses at.
 * The shaded band between the ceiling and the top of the plot is the region
 * where an edit is accepted but changes nothing.
 */
private val SlotColors: Map<Int, Color> = mapOf(
    1 to Color(0xFF2563EB), // blue
    2 to Color(0xFF0D9488), // teal
    3 to Color(0xFF65A30D), // olive
    4 to Color(0xFFD97706), // amber
    5 to Color(0xFFDC2626), // red
)

internal fun slotColor(slot: Int): Color = SlotColors[slot] ?: Color.Gray

@OptIn(ExperimentalTextApi::class)
@Composable
fun BoostCanvas(
    model: BoostCurveModel,
    activeSlot: Int,
    draft: List<Double>,
    modifier: Modifier = Modifier,
    onDragPoint: (index: Int, psi: Double) -> Unit,
    onTapPoint: (index: Int) -> Unit,
) {
    val measurer = rememberTextMeasurer()
    val onSurface = MaterialTheme.colorScheme.onSurface
    val outline = MaterialTheme.colorScheme.outline
    val errorColor = MaterialTheme.colorScheme.error
    val surface = MaterialTheme.colorScheme.surface

    val scale = remember(model, draft) { BoostPlotScale.of(model, draft) }

    Canvas(
        modifier = modifier
            .fillMaxWidth()
            .height(260.dp)
            // Two separate gesture detectors rather than one: a tap opens numeric
            // entry for a breakpoint and a drag moves it, and Compose will not
            // let one `awaitPointerEventScope` own both cleanly.
            .pointerInput(model, activeSlot) {
                detectTapGestures { position ->
                    val geometry = BoostPlotGeometry(size.width.toFloat(), size.height.toFloat())
                    onTapPoint(scale.nearestIndex(geometry, position.x))
                }
            }
            .pointerInput(model, activeSlot) {
                // The grabbed breakpoint is fixed at touch-down and held for the
                // whole gesture. Re-deriving it from x on every move would let a
                // slightly diagonal drag walk sideways across breakpoints,
                // dragging a whole run of the curve down with one finger.
                var grabbed = 0
                detectDragGestures(
                    onDragStart = { position ->
                        grabbed = scale.nearestIndex(BoostPlotGeometry(size.width.toFloat(), size.height.toFloat()), position.x)
                    },
                ) { change, _ ->
                    change.consume()
                    val geometry = BoostPlotGeometry(size.width.toFloat(), size.height.toFloat())
                    onDragPoint(grabbed, scale.psiAt(geometry, change.position.y))
                }
            },
    ) {
        val geometry = BoostPlotGeometry(size.width, size.height)

        drawFrameAndGrid(geometry, scale, outline, onSurface, measurer)

        // The region the base ceiling swallows. Drawn first so every curve sits
        // on top of it — a curve hidden behind its own explanation helps nobody.
        drawCappedBand(model, scale, geometry, errorColor.copy(alpha = 0.10f))

        // Base ceiling: solid, neutral, and clearly not one of the slots.
        drawPolyline(
            points = model.rpmAxis.indices.map { index ->
                Offset(
                    scale.x(geometry, model.rpmAxis[index]),
                    scale.y(geometry, model.baseCeilingPsi.getOrElse(index) { 0.0 }),
                )
            },
            color = onSurface.copy(alpha = 0.65f),
            width = 2.5f,
        )

        // The engine's hard refusal, dashed because nothing may cross it.
        val refusalY = scale.y(geometry, model.refusalCeilingPsi)
        if (refusalY >= geometry.top) {
            drawLine(
                color = errorColor,
                start = Offset(geometry.left, refusalY),
                end = Offset(geometry.right, refusalY),
                strokeWidth = 2f,
                pathEffect = PathEffect.dashPathEffect(floatArrayOf(12f, 10f)),
            )
        }

        // Ghosted slots first, active slot last so it is never overdrawn.
        val labels = mutableListOf<Triple<Int, Offset, Color>>()
        model.slots.sortedBy { it.slot == activeSlot }.forEach { curve ->
            val active = curve.slot == activeSlot
            val values = if (active) draft else curve.psi
            val points = model.rpmAxis.indices.map { index ->
                Offset(
                    scale.x(geometry, model.rpmAxis[index]),
                    scale.y(geometry, values.getOrElse(index) { 0.0 }),
                )
            }
            val color = slotColor(curve.slot).let { if (active) it else it.copy(alpha = 0.30f) }
            drawPolyline(points, color, if (active) 3.5f else 2f)
            if (active) {
                points.forEach { point ->
                    drawCircle(surface, radius = 7f, center = point)
                    drawCircle(color, radius = 7f, center = point, style = Stroke(width = 2.5f))
                }
            }
            points.lastOrNull()?.let { labels += Triple(curve.slot, it, color) }
        }

        drawSlotLabels(labels, geometry, measurer, activeSlot)
    }
}

// -------------------------------------------------------------------- drawing

private fun DrawScope.drawPolyline(points: List<Offset>, color: Color, width: Float) {
    if (points.size < 2) return
    val path = Path().apply {
        moveTo(points.first().x, points.first().y)
        points.drop(1).forEach { lineTo(it.x, it.y) }
    }
    drawPath(path, color, style = Stroke(width = width))
}

/**
 * Shade from the base ceiling up to the top of the plot.
 *
 * Everything in this band is accepted by the engine and then ignored by the ECU,
 * because `min(base, slot)` picks the base there. Left undrawn, a person would
 * raise a slot into it, see the edit succeed, and find nothing changed on a log.
 */
private fun DrawScope.drawCappedBand(
    model: BoostCurveModel,
    scale: BoostPlotScale,
    geometry: BoostPlotGeometry,
    color: Color,
) {
    if (model.rpmAxis.size < 2) return
    val path = Path().apply {
        moveTo(geometry.left, geometry.top)
        model.rpmAxis.indices.forEach { index ->
            lineTo(
                scale.x(geometry, model.rpmAxis[index]),
                scale.y(geometry, model.baseCeilingPsi.getOrElse(index) { 0.0 }),
            )
        }
        lineTo(geometry.right, geometry.top)
        close()
    }
    drawPath(path, color)
}

@OptIn(ExperimentalTextApi::class)
private fun DrawScope.drawFrameAndGrid(
    geometry: BoostPlotGeometry,
    scale: BoostPlotScale,
    outline: Color,
    onSurface: Color,
    measurer: TextMeasurer,
) {
    val labelStyle = TextStyle(fontSize = 9.sp, color = onSurface.copy(alpha = 0.75f))

    // Horizontal gridlines every 5 psi — a tuner's natural step.
    var psi = 0.0
    while (psi <= scale.psiMax) {
        val py = scale.y(geometry, psi)
        drawLine(outline.copy(alpha = 0.25f), Offset(geometry.left, py), Offset(geometry.right, py), 1f)
        drawText(
            textMeasurer = measurer,
            text = "${psi.roundToInt()}",
            topLeft = Offset(geometry.left - 30f, py - 12f),
            style = labelStyle,
        )
        psi += 5.0
    }

    // Vertical gridlines at the first, middle, and last breakpoint only: twelve
    // rpm labels on a phone-width plot is unreadable noise.
    listOfNotNull(
        scale.rpmAxis.firstOrNull(),
        scale.rpmAxis.getOrNull(scale.rpmAxis.size / 2),
        scale.rpmAxis.lastOrNull(),
    ).distinct().forEach { rpm ->
        val px = scale.x(geometry, rpm)
        drawLine(outline.copy(alpha = 0.25f), Offset(px, geometry.top), Offset(px, geometry.bottom), 1f)
        drawText(
            textMeasurer = measurer,
            text = "${rpm.roundToInt()}",
            topLeft = Offset(px - 16f, geometry.bottom + 6f),
            style = labelStyle,
        )
    }

    drawLine(outline, Offset(geometry.left, geometry.top), Offset(geometry.left, geometry.bottom), 1.5f)
    drawLine(outline, Offset(geometry.left, geometry.bottom), Offset(geometry.right, geometry.bottom), 1.5f)

    drawText(
        textMeasurer = measurer,
        text = "psi",
        topLeft = Offset(geometry.left - 46f, geometry.top - 2f),
        style = labelStyle,
    )
    drawText(
        textMeasurer = measurer,
        text = "rpm",
        topLeft = Offset(geometry.right - 26f, geometry.bottom + 6f),
        style = labelStyle,
    )
}

/**
 * Label each curve at its right-hand end, nudged apart where they converge.
 *
 * Direct labels rather than a legend: five curves and a legend means five
 * colour-matching glances, and on a phone in daylight that is exactly the sort of
 * small friction that leads to editing the wrong slot.
 */
@OptIn(ExperimentalTextApi::class)
private fun DrawScope.drawSlotLabels(
    labels: List<Triple<Int, Offset, Color>>,
    geometry: BoostPlotGeometry,
    measurer: TextMeasurer,
    activeSlot: Int,
) {
    val minimumGap = 26f
    var previousY = Float.NEGATIVE_INFINITY
    labels.sortedBy { it.second.y }.forEach { (slot, point, color) ->
        val y = max(point.y - 8f, previousY + minimumGap)
        previousY = y
        drawText(
            textMeasurer = measurer,
            text = "$slot",
            topLeft = Offset(geometry.right + 8f, y),
            style = TextStyle(
                fontSize = 11.sp,
                color = color,
                fontWeight = if (slot == activeSlot) FontWeight.Bold else FontWeight.Normal,
            ),
        )
    }
}

