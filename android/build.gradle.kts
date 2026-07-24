// Versions are pinned rather than floating: the V0 gate's whole output is a
// statement about a specific runtime, and a plugin that silently moved would
// invalidate the parity result it produced.
// AGP 7.4.2 + Gradle 7.6.4 (see gradle-wrapper.properties) is a deliberate,
// evidence-based pin, not a stale default. Chaquopy 17.0.0's own Python tasks
// have undeclared task-graph edges (install reads merge/asset outputs) that
// Gradle 8.0+ rejects as a hard build failure with no cycle-free workaround.
// Gradle 7.6 treats the same edges as a warning and builds. Chaquopy 17 supports
// AGP 7.3–9.2, so 7.4.2 is in range, and the *runtime under test* — Python 3.13 +
// numpy — is fixed by the Chaquopy version, unaffected by this AGP/Gradle choice.
// The public-release build tooling is revisited at the Phase 2 gate.
plugins {
    id("com.android.application") version "7.4.2" apply false
    id("org.jetbrains.kotlin.android") version "1.9.24" apply false
    id("com.chaquo.python") version "17.0.0" apply false
}
