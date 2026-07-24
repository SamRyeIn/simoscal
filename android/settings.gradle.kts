pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}

dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
    }
}

rootProject.name = "simoscal-android"

// V0 is the feasibility gate, so the tree deliberately holds one module: the
// Chaquopy engine. The Compose app shell arrives with V7 — there is no point
// building UI against a runtime that has not yet proven byte parity.
include(":engine")
