package com.simoscal.quickedit

import java.util.Locale

/**
 * Format a number for display, always with a `.` decimal separator.
 *
 * Not cosmetic. `String.format` uses the *default* locale, so on a comma-decimal
 * phone a value shown as "10,50" is fed straight back into a text field whose
 * only parser is `toDoubleOrNull` — which rejects it, leaving the Set button
 * permanently disabled. Every number that can round-trip through the UI, and
 * every number quoted in a refusal message next to one, goes through here.
 */
internal fun Double.display(pattern: String = "%.6g"): String =
    String.format(Locale.US, pattern, this)
