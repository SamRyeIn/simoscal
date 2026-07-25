package com.simoscal.quickedit

import android.content.Context
import android.content.Intent
import androidx.core.content.FileProvider
import java.io.File

/**
 * Hands one verified bin to another app (in practice, SimosTools) to flash.
 *
 * This is the only path by which anything leaves Quick Edit, so it is
 * deliberately narrow:
 *
 * - it takes a [BuildState.Verified] and nothing else, so an unverified or
 *   stale build has no way to reach the share sheet even by a caller's mistake;
 * - the URI is a per-share FileProvider grant against the staging directory,
 *   not a file path and not world-readable storage;
 * - the imports directory is not exposed by the provider at all, so the source
 *   bin and XDFs cannot be shared through this route.
 *
 * Quick Edit does not flash and never will. This is where its responsibility
 * for the file ends.
 */
object ShareBin {

    fun intentFor(context: Context, verified: BuildState.Verified): Intent {
        val file = File(verified.sharePath)
        val uri = FileProvider.getUriForFile(
            context,
            "${context.packageName}.fileprovider",
            file,
        )

        val send = Intent(Intent.ACTION_SEND).apply {
            type = "application/octet-stream"
            putExtra(Intent.EXTRA_STREAM, uri)
            putExtra(Intent.EXTRA_SUBJECT, file.name)
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        }

        return Intent.createChooser(send, "Send ${file.name}").apply {
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        }
    }
}
