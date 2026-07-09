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
exec bash "$SRC/install_and_start.sh"
