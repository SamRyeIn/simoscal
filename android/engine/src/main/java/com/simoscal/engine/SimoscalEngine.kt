package com.simoscal.engine

import android.content.Context
import com.chaquo.python.Kwarg
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform

/**
 * The V0 slice of the engine facade: start the embedded interpreter and run the
 * parity payload.
 *
 * Deliberately thin. V0 exists to answer one question — does the Python engine
 * produce identical results here? — so this exposes exactly the call that
 * answers it and nothing else. The real operation surface (preflight, sessions,
 * table catalog, edits, build) is V6's versioned bridge, and building it before
 * the runtime is proven would be building on an unverified foundation.
 *
 * No bin math happens in Kotlin, here or later. Kotlin owns lifecycle and file
 * paths; Python owns every byte decision.
 */
object SimoscalEngine {

    /** Starts the embedded interpreter once per process. Safe to call repeatedly. */
    fun start(context: Context) {
        if (!Python.isStarted()) {
            Python.start(AndroidPlatform(context.applicationContext))
        }
    }

    /**
     * Runs the parity payload and returns its report as a JSON string.
     *
     * The report is serialized **inside Python** rather than returned as an
     * object graph: the comparison must be over exactly the values the host
     * runner sees, and marshalling floats through Kotlin on the way out would
     * introduce a conversion the host side never performs.
     *
     * @param xdfPath absolute path to the SC8S50 XDF.
     * @param binPath absolute path to the source bin. Never written to.
     * @param workDir writable directory for the saved candidate bin.
     * @param patchXdfPath switch-patch XDF, or null to record the boost leg skipped.
     * @param patchedBinPath already-patched bin, or null to record the boost leg skipped.
     * @param decodeAll whether to decode every table (the full parse-time sweep).
     */
    fun runParity(
        xdfPath: String,
        binPath: String,
        workDir: String,
        patchXdfPath: String? = null,
        patchedBinPath: String? = null,
        decodeAll: Boolean = true,
    ): String {
        val py = Python.getInstance()
        val parity = py.getModule("simoscal_v0_parity")

        val report = parity.callAttr(
            "run_parity",
            Kwarg("xdf_path", xdfPath),
            Kwarg("bin_path", binPath),
            Kwarg("work_dir", workDir),
            Kwarg("patch_xdf_path", patchXdfPath),
            Kwarg("patched_bin_path", patchedBinPath),
            Kwarg("decode_all", decodeAll),
        )

        return py.getModule("json").callAttr(
            "dumps", report, Kwarg("sort_keys", true), Kwarg("indent", 2),
        ).toString()
    }

    /** The engine's own view of where it is running — used by the V0 report header. */
    fun describeRuntime(): String {
        val py = Python.getInstance()
        val sys = py.getModule("sys")
        return "python=${sys["version"]} abi=${android.os.Build.SUPPORTED_ABIS.joinToString(",")}"
    }
}
