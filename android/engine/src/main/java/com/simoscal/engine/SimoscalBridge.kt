package com.simoscal.engine

import android.content.Context
import com.chaquo.python.Python
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import java.util.concurrent.Future

/**
 * The Android side of Quick Edit's versioned Python bridge.
 *
 * Kotlin owns lifecycle and scheduling only. Requests and responses stay JSON
 * strings serialized inside Python, so no PyObject, numpy value, traceback, or
 * calibration decision crosses into the UI layer.
 */
object SimoscalBridge {

    sealed interface Result {
        data class Response(val json: String) : Result
        data class EngineUnavailable(val message: String) : Result
    }

    private val dispatcher: ExecutorService = Executors.newSingleThreadExecutor { runnable ->
        Thread(runnable, "simoscal-engine").apply { isDaemon = true }
    }

    /**
     * Queue one bridge call on the engine's single background thread.
     *
     * Calls cannot race, and Compose never waits on Python. The callback runs on
     * the engine thread; V7 posts the resulting immutable JSON model into UI
     * state on its own lifecycle-aware scope.
     */
    fun dispatch(
        context: Context,
        requestJson: String,
        callback: (Result) -> Unit,
    ): Future<*> = dispatcher.submit {
        callback(dispatchBlocking(context.applicationContext, requestJson))
    }

    /**
     * Blocking core used only from the background dispatcher and instrumentation.
     */
    internal fun dispatchBlocking(context: Context, requestJson: String): Result {
        return try {
            SimoscalEngine.start(context)
            val response = Python.getInstance()
                .getModule("simoscal.bridge")
                .callAttr("dispatch", requestJson)
                .toString()
            Result.Response(response)
        } catch (error: Exception) {
            // Python's bridge maps operation failures into JSON itself. Reaching
            // here means the embedded runtime could not start or load at all;
            // keep exception details out of the UI boundary.
            Result.EngineUnavailable("The embedded calibration engine could not start.")
        }
    }
}
