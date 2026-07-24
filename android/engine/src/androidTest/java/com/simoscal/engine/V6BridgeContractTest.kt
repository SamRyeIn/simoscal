package com.simoscal.engine

import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class V6BridgeContractTest {

    @Test
    fun bridgeInfoCrossesAsJsonWithoutPyObjects() {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val request = """{"bridge_version":1,"op":"bridge_info","params":{}}"""
        val result = SimoscalBridge.dispatchBlocking(context, request)
        assertTrue(result is SimoscalBridge.Result.Response)
        val json = (result as SimoscalBridge.Result.Response).json
        assertTrue(json.contains(""""ok":true"""))
        assertTrue(json.contains(""""bridge_version":1"""))
    }
}
