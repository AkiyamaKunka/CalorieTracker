#!/data/data/com.termux/files/usr/bin/bash
# Termux:Widget one-tap shortcut — stop the CalorieTracker watcher.
# A graceful TERM lets the watcher's trap reap its ping/sync subshells and
# clean up the PID file and lock; escalate only if it hangs.

PID_FILE="$HOME/.calorie_watcher.pid"
PROC_DIR="${CALORIE_PROC_DIR:-/proc}"

# After a crash or reboot the kernel can hand the recorded PID to an
# unrelated process; only trust a PID whose cmdline still mentions the
# watcher script. Unreadable cmdline means the process cannot be identified,
# so this returns failure — stop must never kill a PID it cannot identify.
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

# SIGKILL bypasses the watcher's cleanup trap, so its ping/sync subshells
# (and any in-flight uploader) survive as orphans that keep heartbeating.
# Sweep them the same way the installer does, scoped to $HOME paths so
# nothing unrelated is ever touched.
reap_watcher_orphans() {
  local sig="$1"
  local pattern pid
  for pattern in "$HOME/android_watcher.sh" "$HOME/upload_photo.py"; do
    for pid in $(ps -ef 2>/dev/null | grep -F "$pattern" | grep -v grep | awk '{print $2}'); do
      if [ "$pid" != "$$" ]; then
        kill "$sig" "$pid" 2>/dev/null
      fi
    done
  done
  return 0
}

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

if ! is_watcher_pid "$PID"; then
  echo "⚠️ PID $PID is alive but is not the watcher (PID reuse?); refusing to kill it. Clearing stale state."
  rm -f "$PID_FILE"
  rm -rf "$HOME/.calorie_watcher.lock"
  exit 0
fi

kill "$PID" 2>/dev/null
# The watcher defers the TERM trap while a foreground upload runs (30-65s on
# a slow network), so give it a generous grace period before escalating.
TRIES=0
while [ "$TRIES" -lt 20 ]; do
  is_watcher_pid "$PID" || break
  sleep 1
  TRIES=$((TRIES + 1))
done

if is_watcher_pid "$PID"; then
  kill -9 "$PID" 2>/dev/null
  # The kill -9 skipped the cleanup trap: reap the orphaned ping/sync loops
  # and any stuck uploader (TERM first, then -9 for survivors).
  reap_watcher_orphans -TERM
  sleep 1
  reap_watcher_orphans -9
  rm -f "$PID_FILE"
  rm -rf "$HOME/.calorie_watcher.lock"
  echo "⚠️ Watcher force-killed after timeout."
else
  echo "🔴 Watcher stopped."
fi
