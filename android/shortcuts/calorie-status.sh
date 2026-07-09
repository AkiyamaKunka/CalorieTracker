#!/data/data/com.termux/files/usr/bin/bash
# Termux:Widget one-tap shortcut — watcher status at a glance.

PID_FILE="$HOME/.calorie_watcher.pid"
if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "🟢 Watcher running (PID $(cat "$PID_FILE"))"
else
  echo "🔴 Watcher stopped"
fi

QUEUE_DIR="$HOME/.offline_queue"
if [ -d "$QUEUE_DIR" ]; then
  COUNT="$(find "$QUEUE_DIR" -maxdepth 1 -type f 2>/dev/null | wc -l | tr -d ' ')"
  echo "📦 Offline queue: $COUNT photo(s) waiting"
fi

echo "🕒 Recent activity:"
tail -n 3 "$HOME/watcher.log" 2>/dev/null || echo "  (no log yet)"
