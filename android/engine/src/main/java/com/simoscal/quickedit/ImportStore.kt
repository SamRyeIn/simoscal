package com.simoscal.quickedit

import android.content.Context
import android.net.Uri
import android.provider.OpenableColumns
import java.io.File
import java.io.IOException
import java.security.MessageDigest

/**
 * One imported input: a bin or an XDF, copied into app-private storage.
 *
 * The engine only ever accepts a *path plus hash* it can re-verify, so this is
 * the shape every downstream bridge call needs.
 */
data class ImportedFile(
    /** Absolute path to the app-private copy. Never a content:// URI. */
    val path: String,
    val sha256: String,
    /** What the picker called it — display only, never used to locate the file. */
    val displayName: String,
    val sizeBytes: Long,
) {
    val shortHash: String get() = sha256.take(12)
}

/** Why an import could not be completed. Distinct from an engine rejection. */
class ImportFailure(val reason: String, cause: Throwable? = null) : Exception(reason, cause)

/**
 * Copies user-picked files into app-private storage, hashing as it streams.
 *
 * Two rules this class exists to enforce:
 *
 * 1. **Never edit a picked URI in place.** A `content://` URI can point at a
 *    file in Drive, on an SD card, or in another app's store; it can change or
 *    vanish between the picker and the build. Everything past this boundary
 *    works on our own copy, which the engine re-hashes on every call.
 * 2. **The hash is computed from the bytes we actually wrote**, not from the
 *    source, so a truncated or racing read cannot produce a copy whose recorded
 *    hash describes something other than the file on disk.
 *
 * The Storage Access Framework is used deliberately: it needs no broad storage
 * permission, so the manifest can stay permission-free.
 */
class ImportStore(private val context: Context) {

    private val importsDir: File
        get() = File(context.filesDir, IMPORTS_DIR).apply { mkdirs() }

    /**
     * Stream [uri] into a fresh app-private copy and return it verified.
     *
     * The copy lands at a temporary name first and is renamed to its
     * content-addressed name only after the whole stream is written, so an
     * interrupted import (process kill, storage full) can never leave a
     * truncated file sitting at a name that claims a hash it does not have.
     */
    @Throws(ImportFailure::class)
    fun importFile(uri: Uri, kind: InputKind): ImportedFile {
        val displayName = queryDisplayName(uri) ?: kind.fallbackName
        val staging = File.createTempFile("import-", ".part", importsDir)
        val digest = MessageDigest.getInstance("SHA-256")
        var written = 0L

        try {
            val input = context.contentResolver.openInputStream(uri)
                ?: throw ImportFailure("That file could not be opened for reading.")
            input.use { source ->
                staging.outputStream().use { sink ->
                    val buffer = ByteArray(BUFFER_BYTES)
                    while (true) {
                        val read = source.read(buffer)
                        if (read <= 0) break
                        sink.write(buffer, 0, read)
                        digest.update(buffer, 0, read)
                        written += read
                    }
                    sink.flush()
                }
            }
        } catch (error: IOException) {
            staging.delete()
            throw ImportFailure("That file could not be copied into the app.", error)
        } catch (error: SecurityException) {
            staging.delete()
            throw ImportFailure("The app was not granted access to that file.", error)
        }

        if (written == 0L) {
            staging.delete()
            throw ImportFailure("That file is empty.")
        }

        val sha256 = digest.digest().toHexString()
        val destination = File(importsDir, contentAddressedName(sha256, kind))
        // A byte-identical re-import is a no-op, not an error: same bytes, same name.
        if (destination.exists()) {
            staging.delete()
        } else if (!staging.renameTo(destination)) {
            staging.delete()
            throw ImportFailure("The imported copy could not be saved.")
        }

        return ImportedFile(
            path = destination.absolutePath,
            sha256 = sha256,
            displayName = displayName,
            sizeBytes = written,
        )
    }

    /** Where a build stages its candidate bin. App-private; shared only via FileProvider. */
    fun stagingDir(): File = File(context.filesDir, STAGING_DIR).apply { mkdirs() }

    private fun queryDisplayName(uri: Uri): String? =
        runCatching {
            context.contentResolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)
                ?.use { cursor ->
                    if (cursor.moveToFirst() && !cursor.isNull(0)) cursor.getString(0) else null
                }
        }.getOrNull()

    companion object {
        private const val IMPORTS_DIR = "imports"
        private const val STAGING_DIR = "staging"
        private const val BUFFER_BYTES = 64 * 1024

        /**
         * Content-addressed filename. Pure so it can be tested without Android.
         *
         * Full hash, not a prefix: these files are the provenance of everything
         * downstream, and a 12-hex-char name would be a needless collision surface.
         */
        fun contentAddressedName(sha256: String, kind: InputKind): String =
            "$sha256${kind.extension}"

        fun ByteArray.toHexString(): String =
            joinToString("") { byte -> "%02x".format(byte) }
    }
}

/** The kinds of file Quick Edit imports. Drives both the picker filter and the copy's name. */
enum class InputKind(val extension: String, val fallbackName: String, val mimeTypes: Array<String>) {
    BIN(".bin", "imported.bin", arrayOf("*/*")),
    XDF(".xdf", "imported.xdf", arrayOf("*/*")),
    SWITCH_PATCH_XDF(".patch.xdf", "switch-patch.xdf", arrayOf("*/*")),
}
