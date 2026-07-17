#!/usr/bin/env bash
# Real end-to-end iOS simulator run: boots the sim, resets app state,
# pre-grants photo permission, injects the test food photos, then drives
# app/integration_test/e2e_test.dart against LIVE Gemini.
#
# Usage: scripts/run_e2e_ios.sh <env-file>
#   <env-file> is a --dart-define-from-file .env with E2E_GEMINI_KEY=...
#   It lives OUTSIDE the repo; no key material is stored or printed here.
#   The two test photos (pizza.jpg, burger.jpg) are expected next to the
#   env file, or in $E2E_PHOTOS_DIR if set.
set -euo pipefail

ENV_FILE="${1:?usage: run_e2e_ios.sh <env-file with E2E_GEMINI_KEY=...>}"
ENV_FILE="$(cd "$(dirname "$ENV_FILE")" && pwd)/$(basename "$ENV_FILE")"
[[ -f "$ENV_FILE" ]] || { echo "env file not found: $ENV_FILE" >&2; exit 1; }

UDID="${E2E_SIM_UDID:-6A13AADC-0B09-4AE2-8F8E-BAAC14352C99}"
BUNDLE_ID="dev.calorietracker.calorieTracker"
PHOTOS_DIR="${E2E_PHOTOS_DIR:-$(dirname "$ENV_FILE")}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

export DEVELOPER_DIR="${DEVELOPER_DIR:-/Applications/Xcode.app/Contents/Developer}"

for f in "$PHOTOS_DIR/pizza.jpg" "$PHOTOS_DIR/burger.jpg"; do
  [[ -f "$f" ]] || { echo "test photo not found: $f" >&2; exit 1; }
done

echo "==> Booting simulator $UDID"
xcrun simctl boot "$UDID" 2>/dev/null || true # tolerate already-booted
xcrun simctl bootstatus "$UDID" -b

echo "==> Resetting app state (uninstall $BUNDLE_ID + keychain reset)"
xcrun simctl uninstall "$UDID" "$BUNDLE_ID" 2>/dev/null || true
# flutter_secure_storage lives in the simulator keychain and SURVIVES app
# uninstall — reset it so the onboarding (no-key) path is really exercised.
xcrun simctl keychain "$UDID" reset 2>/dev/null || true

echo "==> Pre-granting photo permissions (no dialog during the test)"
xcrun simctl privacy "$UDID" grant photos "$BUNDLE_ID"
xcrun simctl privacy "$UDID" grant media-library "$BUNDLE_ID" 2>/dev/null || true

# The TCC grant is wiped when `flutter test` (re)installs the app, a
# `simctl privacy` call while the app runs KILLS it, and any concurrent
# simctl call in the install->launch window can wedge the launch. So the
# re-grant after install must avoid CoreSimulator entirely: poll the
# filesystem for the fresh app container, then write the TCC rows straight
# into the simulator's TCC.db with sqlite3 (row shape identical to what
# `simctl privacy grant` produces on this runtime).
SIM_DATA="$HOME/Library/Developer/CoreSimulator/Devices/$UDID/data"
TCC_DB="$SIM_DATA/Library/TCC/TCC.db"
(
  # Wait for the app PROCESS (install-time TCC reset is already done by
  # then), detected via the process table only — no CoreSimulator calls.
  for _ in $(seq 1 6000); do # up to ~10 min of build time
    if pgrep -f "$UDID.*Runner.app/Runner" >/dev/null 2>&1; then
      # Keep re-asserting the rows for a while: the first in-app photo
      # permission request comes ~10s after launch and must find them.
      # NOTE: this iOS 26 runtime requires auth_version=2 for
      # kTCCServicePhotos (tccd ignores v1 rows for Photos — the reason a
      # plain `simctl privacy grant photos`, which writes v1, never sticks),
      # while kTCCServiceMediaLibrary still uses v1.
      for _ in $(seq 1 200); do
        sqlite3 "$TCC_DB" "INSERT OR REPLACE INTO access
          (service, client, client_type, auth_value, auth_reason,
           auth_version, indirect_object_identifier, flags)
          VALUES ('kTCCServicePhotos', '$BUNDLE_ID', 0, 2, 4, 2, 'UNUSED', 0);
          INSERT OR REPLACE INTO access
          (service, client, client_type, auth_value, auth_reason,
           auth_version, indirect_object_identifier, flags)
          VALUES ('kTCCServiceMediaLibrary', '$BUNDLE_ID', 0, 2, 4, 1,
                  'UNUSED', 0);" 2>/dev/null || true
        sleep 0.2
      done
      echo "==> (granter) photo permission TCC rows asserted post-launch"
      exit 0
    fi
    sleep 0.1
  done
) &
GRANTER_PID=$!
trap 'kill "$GRANTER_PID" 2>/dev/null || true' EXIT

echo "==> Injecting test food photos into the simulator gallery"
xcrun simctl addmedia "$UDID" "$PHOTOS_DIR/pizza.jpg" "$PHOTOS_DIR/burger.jpg"

echo "==> Running the integration test (LIVE Gemini)"
(cd "$REPO_ROOT/app" && flutter test integration_test/e2e_test.dart \
  -d "$UDID" \
  --dart-define-from-file="$ENV_FILE")
