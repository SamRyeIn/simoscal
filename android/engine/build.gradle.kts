// Application module (not a library). The engine module holds the Chaquopy
// runtime and the V0 parity harness; V7 adds the Compose UI on top. Chaquopy is
// designed and tested against application modules, and the app-module task graph
// is its well-trodden path.
plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
}

android {
    namespace = "com.simoscal.engine"
    // compileSdk 33 pairs with AGP 7.4.2 (see the root build for why the tooling
    // is pinned pre-8.0). It only governs the build; the runtime under test is
    // Python 3.13 + numpy, fixed by the Chaquopy version. minSdk 26 unchanged.
    compileSdk = 33

    defaultConfig {
        applicationId = "com.simoscal.engine"
        minSdk = 26
        targetSdk = 33
        versionCode = 1
        versionName = "0.0-v0"
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"

        ndk {
            // Both ABIs are built so the artifact is honest about what it ships.
            // Only arm64-v8a is *exercised* by the V0 gate on an Apple-silicon
            // host, since an x86_64 image cannot run natively here; the plan's
            // x86_64 leg needs an Intel host or a physical x86_64 device.
            abiFilters += listOf("arm64-v8a", "x86_64")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    // AGP 7.4 spells this `packagingOptions` (renamed to `packaging` in AGP 8).
    packagingOptions {
        resources.excludes += setOf("META-INF/*")
    }
}

chaquopy {
    defaultConfig {
        // Pinned to the Chaquopy target the V0 measurements were taken against.
        version = "3.13"

        // Chaquopy needs a host interpreter of the same minor version to resolve
        // and build pure-Python packages. Homebrew's python@3.13 is what the
        // host golden is generated with, so both halves of the parity comparison
        // come from the same minor version.
        buildPython("/opt/homebrew/opt/python@3.13/bin/python3.13")

        pip {
            // Installs simoscal from the repo working tree, so the device runs
            // *this* checkout rather than a published copy. Core deps are
            // numpy-only by design (see Code/pyproject.toml), which is what
            // keeps matplotlib and openpyxl out of the mobile closure entirely.
            install("../..")
        }
    }

    // No explicit Python sourceSet: `src/main/python` is already Chaquopy's default
    // Python source directory, so the parity payload placed there is picked up
    // automatically.
}

dependencies {
    // AndroidX test artifacts pinned to versions that compile against SDK 33.
    androidTestImplementation("androidx.test:runner:1.5.2")
    androidTestImplementation("androidx.test:rules:1.5.0")
    androidTestImplementation("androidx.test.ext:junit:1.1.5")
}
