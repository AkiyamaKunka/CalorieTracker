#!/usr/bin/env bash
# Build the APK we hand to other people.
#
# --split-per-abi, deliberately: the default fat APK carries THREE copies
# of the Flutter engine (arm64 + armeabi-v7a + x86_64 ≈ 58 MB of the
# 60 MB). Every real phone is arm64; x86_64 exists only for emulators.
# The arm64 split is ~21 MB — a 65% smaller file to send over WeChat.
set -euo pipefail
cd "$(dirname "$0")/.."/app
export JAVA_HOME="${JAVA_HOME:-/opt/homebrew/opt/openjdk@17}"
export DEVELOPER_DIR="${DEVELOPER_DIR:-/Applications/Xcode.app/Contents/Developer}"

flutter build apk --release --split-per-abi
OUT="build/app/outputs/flutter-apk/app-arm64-v8a-release.apk"
VERSION="$(grep '^version:' pubspec.yaml | awk '{print $2}' | cut -d+ -f1)"
DEST="$HOME/Desktop/Bitewise-share/Bitewise-$VERSION.apk"
mkdir -p "$(dirname "$DEST")"
cp "$OUT" "$DEST"
ls -lh "$DEST"
echo "arm64 build ready: $DEST"
echo "NOTE: arm64 only — correct for every phone since ~2017. Emulators"
echo "and ancient 32-bit devices need app-armeabi-v7a-release.apk."
