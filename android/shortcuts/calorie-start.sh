#!/data/data/com.termux/files/usr/bin/bash
# Termux:Widget one-tap shortcut — start the CalorieTracker watcher.
# The watcher's own PID/lock check makes a double-tap harmless.

if [ ! -f "$HOME/android_watcher.sh" ]; then
  echo "❌ ~/android_watcher.sh not found — run the installer first."
  exit 1
fi

termux-wake-lock 2>/dev/null || true
nohup bash "$HOME/android_watcher.sh" >> "$HOME/watcher.log" 2>&1 &

sleep 2
PID_FILE="$HOME/.calorie_watcher.pid"
if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "🟢 Watcher running (PID $(cat "$PID_FILE"))."
else
  echo "❌ Watcher did not start — check ~/watcher.log"
  exit 1
fi
