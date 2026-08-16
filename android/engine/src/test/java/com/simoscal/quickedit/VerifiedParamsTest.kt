package com.simoscal.quickedit

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The file-naming half of the params contract.
 *
 * [putVerified] is the *only* way a file reaches the engine, and the engine
 * resolves one from two keys — `<name>_path` and `<name>_sha256`
 * (`simoscal.bridge._verified_path()`). A missing `<name>_path` is not a
 * degraded request there, it is a hard `BAD_PARAMS` refusal, because the bridge
 * will not open a file it cannot also hash-verify.
 *
 * That makes this a contract no host-side test crossed before: the envelope
 * tests check the wrapper, the state tests check the gates, and neither looks at
 * the key names inside `params`. The app shipped putting the path under the bare
 * `<name>`, so every op that names a file — `preflight`, `session_create`,
 * `session_recover`, `build` — failed on a real device with
 * `missing required parameter 'bin_path'` while all 93 unit tests stayed green.
 *
 * The names in [ENGINE_FILE_PARAMS] are mirrored from the `_verified_path`
 * call sites in `simoscal/bridge.py`; adding one there means adding it here.
 */
class VerifiedParamsTest {

    private val file = ImportedFile(
        path = "/data/user/0/com.simoscal.engine/files/imports/abc123.bin",
        sha256 = "d61a6e297b3ac1d25f60ec8cb3bb504ff47f2db603a960a56e6a6e34074ad69b",
        displayName = "5G0906259L__0002.bin",
        sizeBytes = 4_194_304,
    )

    private companion object {
        /** Every `name` the engine passes to `_verified_path`. */
        val ENGINE_FILE_PARAMS = listOf(
            "bin",
            "xdf",
            "switch_patch_xdf",
            "source_bin",
            "reference_bin",
        )
    }

    @Test
    fun `the path goes under name_path, not under the bare name`() {
        val params = params { putVerified("bin", file) }

        assertEquals(file.path, params.getString("bin_path"))
        // The regression itself: a bare `bin` key is what the engine refuses.
        assertFalse("path must not be sent under the bare name", params.has("bin"))
    }

    @Test
    fun `the hash goes under name_sha256`() {
        val params = params { putVerified("bin", file) }

        assertEquals(file.sha256, params.getString("bin_sha256"))
    }

    @Test
    fun `a verified file contributes exactly the two keys the engine reads`() {
        val params = params { putVerified("xdf", file) }

        assertEquals(2, params.length())
        assertEquals(setOf("xdf_path", "xdf_sha256"), params.keys().asSequence().toSet())
    }

    @Test
    fun `every engine file param survives the suffixing rule`() {
        for (name in ENGINE_FILE_PARAMS) {
            val params = params { putVerified(name, file) }

            assertTrue("$name: expected ${name}_path", params.has("${name}_path"))
            assertTrue("$name: expected ${name}_sha256", params.has("${name}_sha256"))
            assertFalse("$name: bare key must not be present", params.has(name))
        }
    }

    @Test
    fun `several files coexist without colliding`() {
        val xdf = file.copy(path = "/imports/def456.xdf", displayName = "SC8S50.V1.0.xdf")
        val params = params {
            putVerified("bin", file)
            putVerified("xdf", xdf)
        }

        assertEquals(4, params.length())
        assertEquals(file.path, params.getString("bin_path"))
        assertEquals(xdf.path, params.getString("xdf_path"))
    }
}
