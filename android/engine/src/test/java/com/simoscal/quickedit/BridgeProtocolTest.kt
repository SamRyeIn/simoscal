package com.simoscal.quickedit

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The envelope contract, exercised against the real `org.json` the app uses.
 *
 * Each case here is a shape the engine can genuinely produce (see
 * `simoscal/bridge.py`), plus the shapes that mean something went wrong *outside*
 * the contract. The distinction matters: "the engine rejected this" and "the
 * reply was not a bridge reply" must never be collapsed into one message.
 */
class BridgeProtocolTest {

    private val requestId = "11111111-2222-3333-4444-555555555555"

    // ---------------------------------------------------------------- request

    @Test
    fun `a request carries the version, op, id and params`() {
        val json = JSONObject(
            BridgeProtocol.request("preflight", requestId, JSONObject().put("bin", "/tmp/a.bin"))
        )
        assertEquals(BRIDGE_VERSION, json.getInt("bridge_version"))
        assertEquals("preflight", json.getString("op"))
        assertEquals(requestId, json.getString("request_id"))
        assertEquals("/tmp/a.bin", json.getJSONObject("params").getString("bin"))
    }

    @Test
    fun `a request with no params still carries an empty params object`() {
        val json = JSONObject(BridgeProtocol.request("bridge_info", requestId))
        assertEquals(0, json.getJSONObject("params").length())
    }

    // ----------------------------------------------------------------- parsing

    private fun okEnvelope(op: String, result: JSONObject) = JSONObject()
        .put("bridge_version", BRIDGE_VERSION)
        .put("ok", true)
        .put("op", op)
        .put("request_id", requestId)
        .put("result", result)
        .toString()

    private fun errEnvelope(op: String, code: String, message: String, advanced: String = "") = JSONObject()
        .put("bridge_version", BRIDGE_VERSION)
        .put("ok", false)
        .put("op", op)
        .put("request_id", requestId)
        .put(
            "error",
            JSONObject().put("code", code).put("message", message).put("advanced", advanced),
        )
        .toString()

    @Test
    fun `a successful envelope yields its result`() {
        val outcome = BridgeProtocol.parse(
            "preflight", requestId,
            okEnvelope("preflight", JSONObject().put("ok_to_edit", true).put("summary", "fine")),
        )
        val ok = outcome as BridgeOutcome.Ok
        assertTrue(ok.result.getBoolean("ok_to_edit"))
        assertEquals("fine", ok.result.getString("summary"))
    }

    @Test
    fun `an engine rejection comes through with its code intact`() {
        val outcome = BridgeProtocol.parse(
            "session_create", requestId,
            errEnvelope("session_create", "PREFLIGHT_BLOCKED", "This bin is not editable.", "size mismatch"),
        )
        val failed = outcome as BridgeOutcome.Failed
        assertEquals("PREFLIGHT_BLOCKED", failed.code)
        assertEquals("This bin is not editable.", failed.message)
        assertEquals("size mismatch", failed.advanced)
    }

    @Test
    fun `a version mismatch is reported as such, not as a generic failure`() {
        val stale = JSONObject()
            .put("bridge_version", BRIDGE_VERSION + 1)
            .put("ok", true)
            .put("op", "preflight")
            .put("request_id", requestId)
            .put("result", JSONObject())
            .toString()

        val failed = BridgeProtocol.parse("preflight", requestId, stale) as BridgeOutcome.Failed
        assertEquals("VERSION_MISMATCH", failed.code)
    }

    @Test
    fun `a missing version is a mismatch, never a default`() {
        val noVersion = JSONObject().put("ok", true).put("op", "preflight").toString()
        val failed = BridgeProtocol.parse("preflight", requestId, noVersion) as BridgeOutcome.Failed
        assertEquals("VERSION_MISMATCH", failed.code)
    }

    @Test
    fun `text that is not json is a malformed response`() {
        val failed = BridgeProtocol.parse("preflight", requestId, "Traceback (most recent call last):")
            as BridgeOutcome.Failed
        assertEquals(AppErrorCode.MALFORMED_RESPONSE, failed.code)
    }

    @Test
    fun `a reply for a different op is refused rather than routed`() {
        val failed = BridgeProtocol.parse(
            "build", requestId, okEnvelope("preflight", JSONObject()),
        ) as BridgeOutcome.Failed
        assertEquals(AppErrorCode.RESPONSE_MISMATCH, failed.code)
    }

    @Test
    fun `a reply for a different request id is refused`() {
        val failed = BridgeProtocol.parse(
            "preflight", "a-different-id", okEnvelope("preflight", JSONObject()),
        ) as BridgeOutcome.Failed
        assertEquals(AppErrorCode.RESPONSE_MISMATCH, failed.code)
    }

    /**
     * The engine answers unparseable JSON with an envelope carrying no `op` and
     * no `request_id`. That reply is *about* our call, so it must be delivered,
     * not discarded as a mismatch.
     */
    @Test
    fun `the engine's own bad-request envelope is delivered`() {
        val badRequest = JSONObject()
            .put("bridge_version", BRIDGE_VERSION)
            .put("ok", false)
            .put("op", "")
            .put(
                "error",
                JSONObject()
                    .put("code", "BAD_REQUEST")
                    .put("message", "the request was not valid JSON")
                    .put("advanced", ""),
            )
            .toString()

        val failed = BridgeProtocol.parse("preflight", requestId, badRequest) as BridgeOutcome.Failed
        assertEquals("BAD_REQUEST", failed.code)
        assertEquals("the request was not valid JSON", failed.message)
    }

    @Test
    fun `success without a result is malformed, not an empty success`() {
        val truncated = JSONObject()
            .put("bridge_version", BRIDGE_VERSION)
            .put("ok", true)
            .put("op", "preflight")
            .put("request_id", requestId)
            .toString()

        val failed = BridgeProtocol.parse("preflight", requestId, truncated) as BridgeOutcome.Failed
        assertEquals(AppErrorCode.MALFORMED_RESPONSE, failed.code)
    }

    @Test
    fun `failure without an error object is malformed`() {
        val truncated = JSONObject()
            .put("bridge_version", BRIDGE_VERSION)
            .put("ok", false)
            .put("op", "preflight")
            .put("request_id", requestId)
            .toString()

        val failed = BridgeProtocol.parse("preflight", requestId, truncated) as BridgeOutcome.Failed
        assertEquals(AppErrorCode.MALFORMED_RESPONSE, failed.code)
    }

    @Test
    fun `a missing ok flag is treated as failure, never as success`() {
        val noOk = JSONObject()
            .put("bridge_version", BRIDGE_VERSION)
            .put("op", "preflight")
            .put("request_id", requestId)
            .put("result", JSONObject())
            .toString()

        val outcome = BridgeProtocol.parse("preflight", requestId, noOk)
        assertTrue(outcome is BridgeOutcome.Failed)
    }
}
