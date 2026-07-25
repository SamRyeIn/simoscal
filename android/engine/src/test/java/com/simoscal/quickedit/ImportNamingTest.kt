package com.simoscal.quickedit

import com.simoscal.quickedit.ImportStore.Companion.toHexString
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.security.MessageDigest

/**
 * The naming and hashing rules behind the import copy.
 *
 * The streaming copy itself needs a device (it is a `ContentResolver` away), but
 * the parts that decide *what a file is called* and *what its hash reads as* are
 * pure, and they are the parts everything downstream trusts as provenance.
 */
class ImportNamingTest {

    @Test
    fun `hex encoding is lowercase, zero-padded and full width`() {
        val digest = MessageDigest.getInstance("SHA-256").digest("hello".toByteArray())
        val hex = digest.toHexString()

        assertEquals(64, hex.length)
        assertEquals("2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824", hex)
    }

    @Test
    fun `a zero byte encodes as two characters, not one`() {
        // A naive Integer.toHexString would render 0x0a as "a" and silently
        // shorten the hash — which would still look like a hash to a person.
        assertEquals("000a10ff", byteArrayOf(0, 10, 16, -1).toHexString())
    }

    @Test
    fun `names are content-addressed with the full hash`() {
        val hash = "d61a6e297b3ac1d25f60ec8cb3bb504ff47f2db603a960a56e6a6e34074ad69b"
        assertEquals("$hash.bin", ImportStore.contentAddressedName(hash, InputKind.BIN))
        assertEquals("$hash.xdf", ImportStore.contentAddressedName(hash, InputKind.XDF))
        assertEquals("$hash.patch.xdf", ImportStore.contentAddressedName(hash, InputKind.SWITCH_PATCH_XDF))
    }

    @Test
    fun `the same bytes imported as different kinds do not collide`() {
        val hash = "a".repeat(64)
        assertNotEquals(
            ImportStore.contentAddressedName(hash, InputKind.XDF),
            ImportStore.contentAddressedName(hash, InputKind.SWITCH_PATCH_XDF),
        )
    }

    @Test
    fun `a name carries nothing the picker supplied`() {
        // The display name is chosen by whatever app served the picker, so it must
        // never reach the filesystem: a name like "../../evil" would otherwise be
        // a path, not a label.
        val hash = "b".repeat(64)
        val name = ImportStore.contentAddressedName(hash, InputKind.BIN)
        assertTrue(name.all { char -> char.isLetterOrDigit() || char == '.' })
    }

    @Test
    fun `the short hash is a prefix of the full hash`() {
        val file = ImportedFile(
            path = "/files/imports/x.bin",
            sha256 = "d61a6e297b3ac1d25f60ec8cb3bb504ff47f2db603a960a56e6a6e34074ad69b",
            displayName = "5G0906259L__0002.bin",
            sizeBytes = 4_194_304,
        )
        assertEquals("d61a6e297b3a", file.shortHash)
        assertTrue(file.sha256.startsWith(file.shortHash))
    }
}
