#!/data/data/com.termux/files/usr/bin/bash
# Termux:Widget one-tap shortcut — start the CalorieTracker watcher.
# The watcher's own PID/lock check makes a double-tap harmless.

if [ ! -f "$HOME/android_watcher.sh" ]; then
  echo "❌ ~/android_watcher.sh not found — run the installer first."
  exit 1
fi

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

termux-wake-lock 2>/dev/null || true
nohup bash "$HOME/android_watcher.sh" >> "$HOME/watcher.log" 2>&1 &

# The watcher may need a moment to write its PID file (longer when it first
# has to clear stale lock state), so poll briefly instead of trusting one
# fixed sleep that misreports a slow start as a failure.
PID_FILE="$HOME/.calorie_watcher.pid"
STARTED=""
TRIES=0
while [ "$TRIES" -lt 10 ]; do
  PID="$(cat "$PID_FILE" 2>/dev/null)"
  if [ -n "$PID" ]; then
    if is_watcher_pid "$PID"; then
      STARTED=1
      break
    fi
    # Alive but /proc identity unavailable (e.g. no /proc): report running.
    if kill -0 "$PID" 2>/dev/null && [ ! -r "$PROC_DIR/$PID/cmdline" ]; then
      STARTED=1
      break
    fi
  fi
  TRIES=$((TRIES + 1))
  sleep 1
done

if [ -n "$STARTED" ]; then
  echo "🟢 Watcher running (PID $PID)."
else
  echo "❌ Watcher did not start — check ~/watcher.log"
  exit 1
fi
