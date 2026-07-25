package com.simoscal.quickedit

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The rules in [QuickEditUiState] are the ones a person's engine depends on, so
 * they are tested here rather than only through a screen: a Compose test proves
 * a button was drawn, but these prove the state that button reads can never say
 * "share this" about a bin that was not verified.
 */
class QuickEditStateTest {

    private fun file(name: String, hash: String) = ImportedFile(
        path = "/data/user/0/com.simoscal.engine/files/imports/$hash.bin",
        sha256 = hash,
        displayName = name,
        sizeBytes = 4 * 1024 * 1024,
    )

    private val bin = file("5G0906259L__0002.bin", "d61a6e297b3ac1d25f60ec8cb3bb504f")
    private val xdf = file("SC8S50.V1.0.xdf", "aa11bb22cc33dd44ee55ff6600778899")
    private val patch = file("S50 Switch Patch.29.33.V2.xdf", "0102030405060708090a0b0c0d0e0f10")

    private val verified = BuildState.Verified(
        revision = "R00",
        sharePath = "/data/user/0/com.simoscal.engine/files/staging/candidate.bin",
        binName = "candidate.bin",
        changedTables = listOf("IP_PUT_SP — Pressure up throttle setpoint"),
        gates = listOf(GateResult("checksums", passed = true, ran = true, detail = "")),
    )

    private fun openSession() = QuickEditUiState(
        bin = bin,
        xdf = xdf,
        preflight = PreflightState.Passed("Looks like an SC8S50 bin.", emptyList(), null),
        sessionId = "abc123",
    )

    // ------------------------------------------------------------------ export

    @Test
    fun `export is invisible until a build is verified`() {
        val session = openSession()
        assertFalse(session.exportVisible)
        assertFalse(session.copy(build = BuildState.Running).exportVisible)
        assertFalse(session.copy(build = BuildState.Failed("gate failed", listOf("audit"))).exportVisible)
        assertTrue(session.copy(build = verified).exportVisible)
    }

    @Test
    fun `a failed build offers nothing to share`() {
        val failed = openSession().copy(build = BuildState.Failed("byte audit failed", listOf("3 unexplained bytes")))
        assertFalse(failed.exportVisible)
        assertNull(failed.verifiedSharePath)
    }

    @Test
    fun `editing after a verified build withdraws the share affordance`() {
        val built = openSession().copy(build = verified)
        assertTrue(built.exportVisible)

        // This is the invariant that keeps a stale candidate bin from being shared
        // as though it contained the edit that was just made.
        val afterEdit = built.invalidatingBuild()
        assertFalse(afterEdit.exportVisible)
        assertNull(afterEdit.verifiedSharePath)
        assertEquals(BuildState.NotBuilt, afterEdit.build)
    }

    @Test
    fun `invalidating an unbuilt state leaves it untouched`() {
        val session = openSession()
        assertTrue(session === session.invalidatingBuild())
    }

    // --------------------------------------------------------------- preflight

    @Test
    fun `a session cannot be opened before preflight passes`() {
        val ready = QuickEditUiState(bin = bin, xdf = xdf)
        assertFalse(ready.canOpenSession)
        assertFalse(ready.copy(preflight = PreflightState.Running).canOpenSession)
        assertFalse(ready.copy(preflight = PreflightState.Blocked("no", listOf("wrong bin"))).canOpenSession)
        assertTrue(ready.copy(preflight = PreflightState.Passed("ok", emptyList(), null)).canOpenSession)
    }

    @Test
    fun `a blocked preflight exposes a blocker and no way forward`() {
        val blocked = QuickEditUiState(
            bin = bin,
            xdf = xdf,
            preflight = PreflightState.Blocked("This is not an SC8S50 bin.", listOf("size mismatch")),
        )
        assertNotNull(blocked.blocker)
        assertFalse(blocked.canOpenSession)
        assertFalse(blocked.sessionOpen)
        assertEquals(listOf("size mismatch"), blocked.blocker?.reasons)
    }

    /**
     * The blocker must be a dead end, not a trap.
     *
     * The dialog that renders a blocked verdict cannot be dismissed, so if
     * nothing could retract the verdict the person would be stuck behind it with
     * the file pickers unreachable underneath. Retracting must still leave them
     * unable to edit the bin that was refused.
     */
    @Test
    fun `retracting a blocker returns to un-checked without granting anything`() {
        val blocked = QuickEditUiState(
            bin = bin,
            xdf = xdf,
            preflight = PreflightState.Blocked("This is not an SC8S50 bin.", listOf("size mismatch")),
        )

        val retracted = blocked.retractingBlocker()
        assertNull(retracted.blocker)
        assertEquals(PreflightState.NotRun, retracted.preflight)
        // The bin that was refused is still not editable — only re-checkable.
        assertFalse(retracted.canOpenSession)
        assertFalse(retracted.sessionOpen)
        assertFalse(retracted.exportVisible)
        assertTrue(retracted.canRunPreflight)
    }

    @Test
    fun `retracting cannot erase a passed or running verdict`() {
        val passed = QuickEditUiState(
            bin = bin, xdf = xdf,
            preflight = PreflightState.Passed("ok", emptyList(), null),
        )
        assertTrue(passed === passed.retractingBlocker())

        val running = passed.copy(preflight = PreflightState.Running)
        assertTrue(running === running.retractingBlocker())
    }

    @Test
    fun `preflight needs both inputs`() {
        assertFalse(QuickEditUiState(bin = bin).canRunPreflight)
        assertFalse(QuickEditUiState(xdf = xdf).canRunPreflight)
        assertTrue(QuickEditUiState(bin = bin, xdf = xdf).canRunPreflight)
    }

    @Test
    fun `nothing runs while the engine is busy`() {
        val busy = QuickEditUiState(bin = bin, xdf = xdf, busy = true)
        assertFalse(busy.canRunPreflight)
        assertFalse(busy.copy(preflight = PreflightState.Passed("ok", emptyList(), null)).canOpenSession)
        assertFalse(busy.copy(sessionId = "abc").canBuild)
    }

    // ----------------------------------------------------------- input changes

    @Test
    fun `choosing a different bin discards every verdict about the old one`() {
        val built = openSession().copy(build = verified, canUndo = true, canRedo = true)
        val replaced = built.withBin(file("other.bin", "ffffffffffffffffffffffffffffffff"))

        assertEquals(PreflightState.NotRun, replaced.preflight)
        assertNull(replaced.sessionId)
        assertEquals(BuildState.NotBuilt, replaced.build)
        assertFalse(replaced.canUndo)
        assertFalse(replaced.canRedo)
        assertFalse(replaced.exportVisible)
    }

    @Test
    fun `choosing a different xdf discards every verdict too`() {
        val built = openSession().copy(build = verified)
        val replaced = built.withXdf(file("other.xdf", "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"))

        assertEquals(PreflightState.NotRun, replaced.preflight)
        assertNull(replaced.sessionId)
        assertEquals(BuildState.NotBuilt, replaced.build)
    }

    @Test
    fun `dropping the switch-patch xdf closes the session it was opened with`() {
        val withPatch = openSession().copy(switchPatchXdf = patch)
        assertTrue(withPatch.destinationEnabled(Destination.BOOST))

        val dropped = withPatch.withSwitchPatchXdf(null)
        assertNull(dropped.switchPatchXdf)
        assertNull(dropped.sessionId)
        assertFalse(dropped.destinationEnabled(Destination.BOOST))
    }

    // ------------------------------------------------------------ destinations

    @Test
    fun `the workspace is unreachable without a session`() {
        val noSession = QuickEditUiState(bin = bin, xdf = xdf, switchPatchXdf = patch)
        Destination.values().forEach { destination ->
            assertFalse("$destination should be disabled", noSession.destinationEnabled(destination))
        }
    }

    @Test
    fun `boost needs the switch-patch xdf, the others do not`() {
        val session = openSession()
        assertTrue(session.destinationEnabled(Destination.TABLES))
        assertTrue(session.destinationEnabled(Destination.BUILD))
        assertFalse(session.destinationEnabled(Destination.BOOST))
        assertTrue(session.copy(switchPatchXdf = patch).destinationEnabled(Destination.BOOST))
    }

    // -------------------------------------------------------------------- mode

    /**
     * Advanced reveals detail; it must never unlock an action.
     *
     * Written as a sweep over every interesting state rather than one example,
     * because the failure this guards against is someone later adding a *new*
     * gate that quietly consults `mode`.
     */
    @Test
    fun `advanced mode changes no gate anywhere in the state space`() {
        val bases = listOf(
            QuickEditUiState(),
            QuickEditUiState(bin = bin),
            QuickEditUiState(bin = bin, xdf = xdf),
            QuickEditUiState(bin = bin, xdf = xdf, preflight = PreflightState.Running),
            QuickEditUiState(bin = bin, xdf = xdf, preflight = PreflightState.Blocked("no", listOf("r"))),
            openSession(),
            openSession().copy(switchPatchXdf = patch),
            openSession().copy(build = BuildState.Running),
            openSession().copy(build = BuildState.Failed("no", listOf("r"))),
            openSession().copy(build = verified),
            openSession().copy(busy = true),
        )

        bases.forEach { base ->
            val simple = base.copy(mode = Mode.SIMPLE)
            val advanced = base.copy(mode = Mode.ADVANCED)
            val label = "state=$base"

            assertEquals("$label canRunPreflight", simple.canRunPreflight, advanced.canRunPreflight)
            assertEquals("$label canOpenSession", simple.canOpenSession, advanced.canOpenSession)
            assertEquals("$label canBuild", simple.canBuild, advanced.canBuild)
            assertEquals("$label exportVisible", simple.exportVisible, advanced.exportVisible)
            assertEquals("$label sessionOpen", simple.sessionOpen, advanced.sessionOpen)
            assertEquals("$label blocker", simple.blocker, advanced.blocker)
            assertEquals("$label sharePath", simple.verifiedSharePath, advanced.verifiedSharePath)
            Destination.values().forEach { destination ->
                assertEquals(
                    "$label $destination",
                    simple.destinationEnabled(destination),
                    advanced.destinationEnabled(destination),
                )
            }
        }
    }

    // ------------------------------------------------------------------ edits

    @Test
    fun `hasEdits follows the engine's journal, not a local count`() {
        val session = openSession()
        assertFalse(session.hasEdits)
        assertTrue(session.copy(canUndo = true).hasEdits)
        // Undone back to the start: redo is available, but there is nothing to save.
        assertFalse(session.copy(canUndo = false, canRedo = true).hasEdits)
    }
}
