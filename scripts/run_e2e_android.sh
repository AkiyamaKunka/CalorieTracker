#!/bin/bash
# Android-emulator E2E runner (mirror of run_e2e_ios.sh).
# Usage: scripts/run_e2e_android.sh <env-file with E2E_GEMINI_KEY=...> [device-id]
# Assumes the emulator/device is already booted and adb-visible.
set -euo pipefail
ENV_FILE="$1"; DEV="${2:-emulator-5554}"
APP_ID=dev.calorietracker.calorie_tracker
# On a REAL phone use PHOTO_DIR=/sdcard/Pictures: the Termux watcher ships
# everything in DCIM/Camera to the production server — test photos must
# never enter that pipeline. The app's picker sees all MediaStore images.
PHOTO_DIR="${PHOTO_DIR:-/sdcard/DCIM/Camera}"
SCRATCH="$(dirname "$ENV_FILE")"
export JAVA_HOME=/opt/homebrew/opt/openjdk@17

adb -s "$DEV" uninstall "$APP_ID" >/dev/null 2>&1 || true
# Inject the test photos with camera-style names so captured_at derivation
# has real material, then trigger a media scan for each.
STAMP=$(date +%Y%m%d_%H%M%S)
adb -s "$DEV" push "$SCRATCH/pizza.jpg" "$PHOTO_DIR/IMG_${STAMP}_p.jpg" >/dev/null
adb -s "$DEV" push "$SCRATCH/burger.jpg" "$PHOTO_DIR/IMG_${STAMP}_b.jpg" >/dev/null
for f in "IMG_${STAMP}_p.jpg" "IMG_${STAMP}_b.jpg"; do
  adb -s "$DEV" shell am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE \
    -d "file://$PHOTO_DIR/$f" >/dev/null
done

cd "$(dirname "$0")/../app"
# flutter test installs the APK itself; grant the photo permission right after
# install via a background waiter (pm grant needs the package present).
(
  for i in $(seq 1 120); do
    if adb -s "$DEV" shell pm list packages 2>/dev/null | grep -q "$APP_ID"; then
      adb -s "$DEV" shell pm grant "$APP_ID" android.permission.READ_MEDIA_IMAGES 2>/dev/null || true
      adb -s "$DEV" shell pm grant "$APP_ID" android.permission.READ_EXTERNAL_STORAGE 2>/dev/null || true
      adb -s "$DEV" shell pm grant "$APP_ID" android.permission.POST_NOTIFICATIONS 2>/dev/null || true
      exit 0
    fi
    sleep 2
  done
) &
GRANTER=$!
# E2E_MODE=profile drives an AOT + R8-minified build — the same crash
# surface as release (Flutter Driver cannot attach to true release builds;
# profile is the closest shippable-variant proxy). Default stays debug.
if [ "${E2E_MODE:-debug}" = "profile" ]; then
  flutter drive --driver=test_driver/integration_test.dart \
    --target=integration_test/e2e_test.dart -d "$DEV" --profile \
    --dart-define-from-file="$ENV_FILE"
else
  flutter test integration_test/e2e_test.dart -d "$DEV" --dart-define-from-file="$ENV_FILE"
fi
RC=$?
kill $GRANTER 2>/dev/null || true
exit $RC
