package com.simoscal.quickedit

import android.content.Context
import com.simoscal.engine.SimoscalBridge
import java.util.UUID
import kotlin.coroutines.resume
import kotlinx.coroutines.suspendCancellableCoroutine
import org.json.JSONObject

/**
 * Suspending front door to the engine.
 *
 * [SimoscalBridge] already serializes every call onto one background thread, so
 * this adds only the coroutine shape and the envelope discipline from
 * [BridgeProtocol]. Compose never blocks on Python, and Python state is never
 * touched from two places at once.
 */
class BridgeClient(context: Context) {

    private val appContext = context.applicationContext

    /**
     * Perform one bridge op.
     *
     * Never throws for an engine-level failure: a rejected op comes back as
     * [BridgeOutcome.Failed] with a stable code, which is what the UI renders.
     * That keeps "the engine said no" and "the app crashed" from looking alike.
     */
    suspend fun call(op: String, params: JSONObject = JSONObject()): BridgeOutcome {
        val requestId = UUID.randomUUID().toString()
        val requestJson = BridgeProtocol.request(op, requestId, params)

        val raw = suspendCancellableCoroutine { continuation ->
            val future = SimoscalBridge.dispatch(appContext, requestJson) { result ->
                continuation.resume(result)
            }
            // Cancelling the coroutine (screen left, session closed) must not kill
            // an in-flight engine call: it may be mid-write on a staged bin, and a
            // half-run op is exactly what the journal cannot represent. The result
            // is simply dropped instead.
            continuation.invokeOnCancellation { future.cancel(false) }
        }

        return when (raw) {
            is SimoscalBridge.Result.Response ->
                BridgeProtocol.parse(op, requestId, raw.json)

            is SimoscalBridge.Result.EngineUnavailable -> BridgeOutcome.Failed(
                code = AppErrorCode.ENGINE_UNAVAILABLE,
                message = raw.message,
                advanced = "op=$op",
            )
        }
    }
}

/** Convenience: build a params object without a pile of local vals at each call site. */
inline fun params(build: JSONObject.() -> Unit): JSONObject = JSONObject().apply(build)

/**
 * A verified path+hash pair, the only way a file is named to the engine.
 *
 * Both keys are suffixed. `simoscal.bridge._verified_path()` resolves a file
 * from `<name>_path` + `<name>_sha256` and treats an absent `<name>_path` as a
 * bad request, so sending the path under the bare `<name>` fails every
 * file-naming op — which is exactly what shipped until `VerifiedParamsTest`
 * pinned these key names to the engine's contract.
 */
fun JSONObject.putVerified(name: String, file: ImportedFile): JSONObject = apply {
    put("${name}_path", file.path)
    put("${name}_sha256", file.sha256)
}
