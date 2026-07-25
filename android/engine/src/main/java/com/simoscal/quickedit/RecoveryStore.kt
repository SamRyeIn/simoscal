package com.simoscal.quickedit

import android.content.Context
import androidx.datastore.core.DataStore
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import kotlinx.coroutines.flow.first
import org.json.JSONObject

/**
 * What the app must remember to rebuild a session after the process dies.
 *
 * The engine already owns the hard half — `session_serialize` emits a record
 * that `session_recover` turns back into an equivalent live `Tune`. The app's
 * job is only to persist that record alongside *verified pointers* to the input
 * files, because recovery re-hashes each one and refuses a source that changed.
 */
data class RecoveryPointer(
    /** The engine's own serialized session record, stored verbatim. */
    val record: String,
    val bin: ImportedFile,
    val xdf: ImportedFile,
    val switchPatchXdf: ImportedFile?,
    val savedAtMillis: Long,
)

private val Context.recoveryDataStore: DataStore<Preferences> by preferencesDataStore(name = "quickedit_recovery")

/**
 * Durable storage for the single most recent session.
 *
 * DataStore rather than Room: there is exactly one record with no relations and
 * no queries, so a schema, a DAO, and an annotation processor would all be
 * ceremony around a key/value write. If projects and revision lineage arrive in
 * Phase 2, that is the point to reconsider — not before.
 */
class RecoveryStore(context: Context) {

    private val store = context.applicationContext.recoveryDataStore

    suspend fun save(pointer: RecoveryPointer) {
        val encoded = JSONObject()
            .put(KEY_RECORD, pointer.record)
            .put(KEY_BIN, pointer.bin.encode())
            .put(KEY_XDF, pointer.xdf.encode())
            .put(KEY_SAVED_AT, pointer.savedAtMillis)
            .apply { pointer.switchPatchXdf?.let { put(KEY_PATCH, it.encode()) } }
            .toString()
        store.edit { prefs -> prefs[POINTER] = encoded }
    }

    /**
     * Read back the saved session, or null if there is none.
     *
     * A record that cannot be decoded is treated as absent rather than as an
     * error: the only cost is losing an unfinished session, and the alternative
     * — surfacing a parse failure the person can do nothing about — is worse.
     * The engine, not this class, is what guarantees the record is *sound*.
     */
    suspend fun load(): RecoveryPointer? {
        val encoded = store.data.first()[POINTER] ?: return null
        return runCatching {
            val json = JSONObject(encoded)
            RecoveryPointer(
                record = json.getString(KEY_RECORD),
                bin = json.getJSONObject(KEY_BIN).decodeImportedFile(),
                xdf = json.getJSONObject(KEY_XDF).decodeImportedFile(),
                switchPatchXdf = json.optJSONObject(KEY_PATCH)?.decodeImportedFile(),
                savedAtMillis = json.optLong(KEY_SAVED_AT),
            )
        }.getOrNull()
    }

    suspend fun clear() {
        store.edit { prefs -> prefs.remove(POINTER) }
    }

    private companion object {
        val POINTER = stringPreferencesKey("pointer")
        const val KEY_RECORD = "record"
        const val KEY_BIN = "bin"
        const val KEY_XDF = "xdf"
        const val KEY_PATCH = "switch_patch_xdf"
        const val KEY_SAVED_AT = "saved_at"
    }
}

/** Pure encode/decode, kept next to the store so the pair stays in sync. */
fun ImportedFile.encode(): JSONObject = JSONObject()
    .put("path", path)
    .put("sha256", sha256)
    .put("display_name", displayName)
    .put("size_bytes", sizeBytes)

fun JSONObject.decodeImportedFile(): ImportedFile = ImportedFile(
    path = getString("path"),
    sha256 = getString("sha256"),
    displayName = optString("display_name", ""),
    sizeBytes = optLong("size_bytes"),
)
