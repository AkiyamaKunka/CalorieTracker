#!/data/data/com.termux/files/usr/bin/bash
# Termux:Widget one-tap shortcut — install the latest scripts pushed to
# /sdcard/Download (e.g. via `adb push` from the dev machine) and restart
# the watcher. Pairs with the installer's pre-flight: if the payload is
# missing it aborts before touching the running watcher.

SRC="${CALORIE_INSTALL_SRC:-/sdcard/Download}"
if [ ! -f "$SRC/install_and_start.sh" ]; then
  echo "❌ $SRC/install_and_start.sh not found — push the scripts first."
  exit 1
fi
# The installer's own pre-flight cannot protect against a truncated copy of
# the installer itself (an interrupted adb push): it would kill the watcher,
# then hit EOF before restarting it. Only the caller can catch that, so
# syntax-check the installer here before handing off.
if ! bash -n "$SRC/install_and_start.sh" 2>/dev/null; then
  echo "❌ $SRC/install_and_start.sh failed a syntax check (truncated push?) — re-push it; the running watcher was left untouched."
  exit 1
fi
exec bash "$SRC/install_and_start.sh"
