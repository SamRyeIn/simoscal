#!/usr/bin/env bash
# V0 device-parity helper. Two modes:
#
#   ./push_fixtures_and_compare.sh push      # copy the real XDF/bin onto the device
#   ./push_fixtures_and_compare.sh compare   # pull the device report, diff vs host golden
#
# Run `push` once (before the test), then run the instrumentation test from
# Android Studio, then run `compare`. Nothing here flashes anything.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
CODE="$REPO_ROOT/Code"
ADB="/opt/homebrew/share/android-commandlinetools/platform-tools/adb"
DEVICE_DIR="/data/local/tmp/v0"
PY313="$CODE/.venv313/bin/python"

case "${1:-}" in
  push)
    "$ADB" shell mkdir -p "$DEVICE_DIR"
    "$ADB" push "$CODE/xdf/SC8S50.V1.0.xdf" "$DEVICE_DIR/"
    "$ADB" push "$CODE/bin/5G0906259L__0002.bin" "$DEVICE_DIR/"
    # Optional switch-patch fixtures — enable the boost-curve leg if present.
    PATCH_XDF="$REPO_ROOT/BinToolz-main/definitions/S50 Switch Patch.29.33.V2.xdf"
    PATCH_BIN="$REPO_ROOT/Tunes/TuningBasicsGuide/BinToolz-patched/CB_HSL_SP2933_5G0906259L_0002_BasicsGuide_R04.bin"
    [ -f "$PATCH_XDF" ] && "$ADB" push "$PATCH_XDF" "$DEVICE_DIR/" || echo "note: switch-patch XDF absent — boost leg will be SKIPPED"
    [ -f "$PATCH_BIN" ] && "$ADB" push "$PATCH_BIN" "$DEVICE_DIR/" || echo "note: patched bin absent — boost leg will be SKIPPED"
    echo "OK: fixtures pushed to $DEVICE_DIR"
    ;;
  compare)
    OUT="$CODE/android/parity/v0_device_report.json"
    # The test writes to targetContext's external files dir. On an application
    # module that is the app package; on a self-instrumenting library it is the
    # .test package. Try both so this works regardless.
    "$ADB" pull /sdcard/Android/data/com.simoscal.engine/files/v0_device_report.json "$OUT" 2>/dev/null \
      || "$ADB" pull /sdcard/Android/data/com.simoscal.engine.test/files/v0_device_report.json "$OUT"
    echo "pulled: $OUT"
    "$PY313" "$CODE/android/parity/run_host_parity.py" --compare "$OUT"
    ;;
  *)
    echo "usage: $0 {push|compare}" >&2
    exit 2
    ;;
esac
