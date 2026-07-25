package com.simoscal.quickedit

import org.json.JSONException
import org.json.JSONObject

/**
 * The app's half of the V6 bridge envelope — pure, Android-free, JVM-testable.
 *
 * Everything the UI knows about the engine passes through here as JSON text.
 * No Python object, numpy value, traceback, or calibration decision crosses
 * this line; a response is either a parsed [JSONObject] result or a stable
 * error code.
 *
 * The engine's contract (`simoscal/bridge.py`): a response is *always* a valid
 * envelope carrying `ok`, and on failure an `error.code` from a closed set. So
 * a parse failure here means something outside the contract happened (the
 * runtime died mid-call, a truncated string), and is reported as such rather
 * than being mistaken for an engine-level rejection.
 */
const val BRIDGE_VERSION: Int = 1

/** Error codes this layer synthesizes; engine-issued codes come through verbatim. */
object AppErrorCode {
    /** The response was not a parseable bridge envelope at all. */
    const val MALFORMED_RESPONSE = "MALFORMED_RESPONSE"

    /** The response was well-formed but answered a different call than we made. */
    const val RESPONSE_MISMATCH = "RESPONSE_MISMATCH"

    /** The embedded Python runtime could not start or load. */
    const val ENGINE_UNAVAILABLE = "ENGINE_UNAVAILABLE"
}

/** The three things that can come back from a bridge call. */
sealed interface BridgeOutcome {
    data class Ok(val result: JSONObject) : BridgeOutcome

    /**
     * The call was rejected. [message] is plain-language and safe to show;
     * [advanced] is the detail shown only in Advanced mode.
     */
    data class Failed(
        val code: String,
        val message: String,
        val advanced: String,
    ) : BridgeOutcome
}

object BridgeProtocol {

    /** Build one request envelope. `request_id` is echoed back and checked on parse. */
    fun request(op: String, requestId: String, params: JSONObject = JSONObject()): String =
        JSONObject()
            .put("bridge_version", BRIDGE_VERSION)
            .put("op", op)
            .put("request_id", requestId)
            .put("params", params)
            .toString()

    /**
     * Parse a response envelope, verifying it answers the call we actually made.
     *
     * The `op`/`request_id` check is not ceremony: the engine serializes calls on
     * one thread and rejects a racing request with `BUSY`, so a response bearing
     * another call's identity means the app's own plumbing crossed wires. Better
     * to surface that than to route a foreign result into session state.
     */
    fun parse(op: String, requestId: String, responseJson: String): BridgeOutcome {
        val envelope = try {
            JSONObject(responseJson)
        } catch (error: JSONException) {
            return malformed("the engine's reply could not be read", error.messageOrType())
        }

        val version = envelope.opt("bridge_version")
        if (version != BRIDGE_VERSION) {
            return BridgeOutcome.Failed(
                code = "VERSION_MISMATCH",
                message = "The app and the calibration engine speak different versions.",
                advanced = "response bridge_version=$version, app=$BRIDGE_VERSION",
            )
        }

        val replyOp = envelope.optString("op", "")
        val replyId = envelope.opt("request_id")
        // The engine answers a request it could not parse with an envelope bearing
        // neither op nor request_id. That reply *is* about our call — it is the
        // only reply we will get — so it must be delivered with its real reason
        // rather than discarded as a mismatch.
        val engineCouldNotIdentifyCall = replyOp.isEmpty() && replyId == null
        val identityMatches = engineCouldNotIdentifyCall || (replyOp == op && replyId == requestId)
        if (!identityMatches) {
            return BridgeOutcome.Failed(
                code = AppErrorCode.RESPONSE_MISMATCH,
                message = "The engine's reply did not match the request.",
                advanced = "sent op=$op id=$requestId; got op=$replyOp id=$replyId",
            )
        }

        if (envelope.optBoolean("ok", false)) {
            val result = envelope.optJSONObject("result")
                ?: return malformed("the engine reported success without a result", "op=$op")
            return BridgeOutcome.Ok(result)
        }

        val error = envelope.optJSONObject("error")
            ?: return malformed("the engine reported a failure without a reason", "op=$op")
        return BridgeOutcome.Failed(
            code = error.optString("code", AppErrorCode.MALFORMED_RESPONSE),
            message = error.optString("message", "The engine rejected the request."),
            advanced = error.optString("advanced", ""),
        )
    }

    private fun malformed(message: String, advanced: String) =
        BridgeOutcome.Failed(AppErrorCode.MALFORMED_RESPONSE, message, advanced)

    private fun JSONException.messageOrType(): String = message ?: this::class.java.simpleName
}
