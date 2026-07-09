#!/data/data/com.termux/files/usr/bin/bash
# Termux:Widget one-tap shortcut — stop the CalorieTracker watcher.
# A graceful TERM lets the watcher's trap reap its ping/sync subshells and
# clean up the PID file and lock; escalate only if it hangs.

PID_FILE="$HOME/.calorie_watcher.pid"

if [ ! -f "$PID_FILE" ]; then
  echo "ℹ️ Watcher is not running."
  exit 0
fi

PID="$(cat "$PID_FILE")"
if ! kill -0 "$PID" 2>/dev/null; then
  echo "ℹ️ Watcher already stopped; clearing stale state."
  rm -f "$PID_FILE"
  rm -rf "$HOME/.calorie_watcher.lock"
  exit 0
fi

kill "$PID" 2>/dev/null
for _ in 1 2 3 4 5 6 7 8 9 10; do
  kill -0 "$PID" 2>/dev/null || break
  sleep 1
done

if kill -0 "$PID" 2>/dev/null; then
  kill -9 "$PID" 2>/dev/null
  rm -f "$PID_FILE"
  rm -rf "$HOME/.calorie_watcher.lock"
  echo "⚠️ Watcher force-killed after timeout."
else
  echo "🔴 Watcher stopped."
fi
