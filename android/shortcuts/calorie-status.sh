#!/data/data/com.termux/files/usr/bin/bash
# Termux:Widget one-tap shortcut — watcher status at a glance.

PID_FILE="$HOME/.calorie_watcher.pid"
PROC_DIR="${CALORIE_PROC_DIR:-/proc}"

# After a crash or reboot the kernel can hand a recorded PID to an unrelated
# process; only trust a PID whose cmdline still mentions the watcher script.
is_watcher_pid() {
  local pid="$1"
  local cmdline
  [ -n "$pid" ] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  cmdline="$(tr '\0' ' ' < "$PROC_DIR/$pid/cmdline" 2>/dev/null)" || return 1
  case "$cmdline" in
    *android_watcher*) return 0 ;;
    *) return 1 ;;
  esac
}

PID="$(cat "$PID_FILE" 2>/dev/null)"
if [ -n "$PID" ] && is_watcher_pid "$PID"; then
  echo "🟢 Watcher running (PID $PID)"
elif [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null && [ ! -r "$PROC_DIR/$PID/cmdline" ]; then
  # Alive but /proc identity unavailable: report it as running rather than
  # scaring the user with a false "stopped" (status is informational only).
  echo "🟢 Watcher running (PID $PID, unverified)"
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
