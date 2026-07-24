package com.simoscal.engine

import android.os.Build
import android.util.Log
import androidx.test.platform.app.InstrumentationRegistry
import androidx.test.ext.junit.runners.AndroidJUnit4
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File

/**
 * The V0 go/no-go, run on-device.
 *
 * This does not decide parity by itself — it produces the device half of the
 * comparison and asserts the properties that are checkable without the host
 * report. The verdict comes from pulling `v0_device_report.json` and running
 * `android/parity/run_host_parity.py --compare` against it, because parity is a
 * statement about two runs and only the host side holds the other one.
 *
 * Fixtures are the real SC8S50 files. They are **not** committed (the bin and
 * XDFs are Sam's own, and the repo gitignores `*.bin`), so they are pushed to
 * the device before the run:
 *
 * ```
 * adb push Code/xdf/SC8S50.V1.0.xdf /data/local/tmp/v0/
 * adb push Code/bin/5G0906259L__0002.bin /data/local/tmp/v0/
 * ```
 *
 * Absent fixtures **skip** rather than fail, matching the repo-wide convention
 * for tests that touch the real bin/XDF.
 */
@RunWith(AndroidJUnit4::class)
class V0ParityTest {

    private val tag = "V0Parity"

    private val fixtureDir: File
        get() {
            val arg = InstrumentationRegistry.getArguments().getString("fixtureDir")
            return File(arg ?: "/data/local/tmp/v0")
        }

    @Test
    fun engineProducesParityReport() {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        SimoscalEngine.start(context)

        val xdf = File(fixtureDir, "SC8S50.V1.0.xdf")
        val bin = File(fixtureDir, "5G0906259L__0002.bin")
        assumeTrue(
            "V0 fixtures not present at $fixtureDir — push the real XDF/bin first",
            xdf.isFile && bin.isFile,
        )

        // The switch-patch leg is optional; when its fixtures are absent the
        // payload records SKIPPED *in the compared report*, so a host golden that
        // ran the leg cannot silently match a device run that did not.
        val patchXdf = File(fixtureDir, "S50 Switch Patch.29.33.V2.xdf")
        val patchedBin = File(fixtureDir, "CB_HSL_SP2933_5G0906259L_0002_BasicsGuide_R04.bin")
        val havePatch = patchXdf.isFile && patchedBin.isFile

        val workDir = File(context.filesDir, "v0_parity").apply { mkdirs() }

        val coldStartMs = measure {
            SimoscalEngine.start(context)
            Log.i(tag, SimoscalEngine.describeRuntime())
        }

        val started = System.nanoTime()
        val json = SimoscalEngine.runParity(
            xdfPath = xdf.absolutePath,
            binPath = bin.absolutePath,
            workDir = workDir.absolutePath,
            patchXdfPath = if (havePatch) patchXdf.absolutePath else null,
            patchedBinPath = if (havePatch) patchedBin.absolutePath else null,
            decodeAll = true,
        )
        val elapsedMs = (System.nanoTime() - started) / 1_000_000

        // Write where `adb pull` can reach it without root.
        val out = File(context.getExternalFilesDir(null), "v0_device_report.json")
        out.writeText(json)

        val report = JSONObject(json)
        val compared = report.getJSONObject("compared")
        val steps = compared.getJSONObject("steps")

        // --- properties checkable device-side, without the host report -------- //
        val parse = steps.getJSONObject("parse")
        assertTrue("no tables parsed", parse.getInt("table_count") > 0)

        // The source bin must be byte-identical after the run: the engine edits a
        // copy-on-write buffer, never the file it was handed. This is the
        // immutable-source rule, checked on the device that will hold the user's
        // only good bin.
        assertEquals(
            "source bin was modified by the parity run",
            parse.getString("bin_sha256"),
            sha256(bin),
        )

        // Checksums must verify clean after correction, or nothing downstream is
        // flashable.
        val readbackChecksums = steps.getJSONObject("readback").getJSONArray("checksums")
        for (i in 0 until readbackChecksums.length()) {
            val c = readbackChecksums.getJSONObject(i)
            if (c.getBoolean("can_verify")) {
                assertFalse(
                    "${c.getString("name")} stale after correction",
                    c.getBoolean("is_stale"),
                )
            }
        }

        // The edited cell must read back off the saved file as it was encoded.
        val edit = steps.getJSONObject("edit_and_save")
        assertEquals(
            "readback disagrees with the encoded value",
            edit.getString("after_cell"),
            steps.getJSONObject("readback").getString("readback_cell"),
        )

        // psi must floor, never round up — a cap asked as N psi cannot encode above N.
        val probes = steps.getJSONObject("psi_floor").getJSONObject("probes")
        for (key in probes.keys()) {
            assertTrue(
                "psi floor rounded up at $key",
                probes.getJSONObject(key).getBoolean("not_above_request"),
            )
        }

        Log.i(tag, "digest=${report.getString("digest")}")
        Log.i(tag, "report=${out.absolutePath}")
        Log.i(tag, "abi=${Build.SUPPORTED_ABIS.joinToString(",")}")
        Log.i(tag, "coldStartMs=$coldStartMs runParityMs=$elapsedMs")
        Log.i(tag, "timings=${report.getJSONObject("timings")}")
        Log.i(tag, "environment=${report.getJSONObject("environment")}")
        Log.i(tag, "boostLegRan=$havePatch")
    }

    private fun measure(block: () -> Unit): Long {
        val started = System.nanoTime()
        block()
        return (System.nanoTime() - started) / 1_000_000
    }

    private fun sha256(file: File): String {
        val digest = java.security.MessageDigest.getInstance("SHA-256")
        file.inputStream().use { stream ->
            val buffer = ByteArray(1 shl 20)
            while (true) {
                val read = stream.read(buffer)
                if (read <= 0) break
                digest.update(buffer, 0, read)
            }
        }
        return digest.digest().joinToString("") { "%02x".format(it) }
    }
}
