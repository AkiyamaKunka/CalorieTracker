#!/data/data/com.termux/files/usr/bin/bash

# Configuration
CAMERA_DIR="${CALORIE_CAMERA_DIR:-${CAMERA_DIR:-/storage/emulated/0/DCIM/Camera/}}"
LOG_FILE="$HOME/watcher.log"
PID_FILE="$HOME/.calorie_watcher.pid"
LOCK_DIR="$HOME/.calorie_watcher.lock"
PROC_DIR="${CALORIE_PROC_DIR:-/proc}"
HISTORY_FILE="$HOME/uploaded_files.log"
FAIL_COUNT_DIR="$HOME/.calorie_upload_failures"
MAX_UPLOAD_ATTEMPTS=3
SCAN_LIST="$HOME/.calorie_watcher_scan.$$"
NEW_LIST="$HOME/.calorie_watcher_new.$$"

# A recorded PID only counts as a live watcher if its cmdline still mentions
# this script: after a SIGKILL or reboot the kernel can hand the same PID to
# an unrelated process. Unreadable/missing cmdline is treated as stale.
is_watcher_running() {
  local pid="$1"
  local cmdline
  [ -n "$pid" ] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  cmdline="$(tr '\0' ' ' < "$PROC_DIR/$pid/cmdline" 2>/dev/null || true)"
  case "$cmdline" in
    *android_watcher*) return 0 ;;
    *) return 1 ;;
  esac
}

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  EXISTING_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
  if is_watcher_running "$EXISTING_PID"; then
    echo "[$(date)] CalorieTracker watcher is already running with PID $EXISTING_PID." | tee -a "$LOG_FILE"
    exit 0
  fi
  rm -rf "$LOCK_DIR"
  mkdir "$LOCK_DIR" 2>/dev/null || exit 0
fi

EXISTING_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
if [ -n "$EXISTING_PID" ] && [ "$EXISTING_PID" != "$$" ] && is_watcher_running "$EXISTING_PID"; then
  echo "[$(date)] CalorieTracker watcher is already running with PID $EXISTING_PID." | tee -a "$LOG_FILE"
  rmdir "$LOCK_DIR" 2>/dev/null || true
  exit 0
fi

echo $$ > "$PID_FILE"
cleanup() {
  [ -n "$PING_PID" ] && kill "$PING_PID" 2>/dev/null
  [ -n "$SYNC_PID" ] && kill "$SYNC_PID" 2>/dev/null
  rm -f "$PID_FILE" "$SCAN_LIST" "$NEW_LIST"
  rmdir "$LOCK_DIR" 2>/dev/null
  return 0
}
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT

# Acquire a wake-lock so Android doesn't kill this process
termux-wake-lock

echo "[$(date)] Starting CalorieTracker Android Watcher..." | tee -a "$LOG_FILE"

# Start background ping every 5 minutes
(
  while true; do
    python3 ~/upload_photo.py --ping >> "$LOG_FILE" 2>&1
    sleep 300
  done
) &
PING_PID=$!

# Start Daily Sync Reconciliation (11:00 PM)
(
  while true; do
    current_hour=$(date +%H)
    if [ "$current_hour" == "23" ]; then
      echo "[$(date)] Triggering Daily Sync..." | tee -a "$LOG_FILE"
      python3 ~/upload_photo.py --sync >> "$LOG_FILE" 2>&1
      # Sleep for 2 hours so it doesn't trigger again today
      sleep 7200
    else
      # Check again in 30 minutes
      sleep 1800
    fi
  done
) &
SYNC_PID=$!

# MediaStore writes in-progress captures as '.pending-*'; skip those.
list_photos() {
  find "$CAMERA_DIR" -maxdepth 1 -type f \
    ! -name '.pending-*' \
    \( -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' -o -iname '*.heic' -o -iname '*.heif' \) \
    2>/dev/null | LC_ALL=C sort
}

# History stays sorted so each poll can diff it against the scan with comm.
remember_uploaded() {
  echo "$1" >> "$HISTORY_FILE"
  LC_ALL=C sort -u "$HISTORY_FILE" -o "$HISTORY_FILE"
}

# Bounded wait until the file stops growing and is non-empty (the camera app
# may still be writing it). Returns 1 if the file vanished / is unreadable, or
# if it never stabilized at a non-zero size (a 0-byte or still-growing file
# must not be uploaded; the caller skips it and retries on the next poll).
wait_for_stable_file() {
  local path="$1"
  local last_size="-1"
  local size
  local tries=0
  while [ "$tries" -lt 15 ]; do
    size="$(stat -c %s "$path" 2>/dev/null || stat -f %z "$path" 2>/dev/null || true)"
    [ -n "$size" ] || return 1
    if [ "$size" = "$last_size" ] && [ "$size" -gt 0 ] 2>/dev/null; then
      return 0
    fi
    last_size="$size"
    tries=$((tries + 1))
    sleep 2
  done
  [ "$last_size" -gt 0 ] 2>/dev/null && return 0
  return 1
}

# Monitor the camera directory using a polling loop (bypasses Android emulated storage bug)
touch "$HISTORY_FILE"
mkdir -p "$FAIL_COUNT_DIR"

# Seed the history with photos already present at startup so restarts do not
# upload a whole camera backlog — except photos taken within the last
# SEED_FRESH_MINUTES (default 15): a meal snapped just before starting the
# watcher should upload now, not wait for the 11 PM reconciliation.
SEED_FRESH_MINUTES="${SEED_FRESH_MINUTES:-15}"
if [ "$SEED_FRESH_MINUTES" -gt 0 ] 2>/dev/null; then
  find "$CAMERA_DIR" -maxdepth 1 -type f \
    ! -name '.pending-*' \
    \( -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' -o -iname '*.heic' -o -iname '*.heif' \) \
    -mmin "+${SEED_FRESH_MINUTES}" \
    2>/dev/null | LC_ALL=C sort >> "$HISTORY_FILE"
else
  list_photos >> "$HISTORY_FILE"
fi
LC_ALL=C sort -u "$HISTORY_FILE" -o "$HISTORY_FILE" 2>/dev/null || true

echo "[$(date)] Started polling loop for $CAMERA_DIR" | tee -a "$LOG_FILE"

while true; do
  list_photos > "$SCAN_LIST"
  if [ -s "$HISTORY_FILE" ]; then
    LC_ALL=C comm -23 "$SCAN_LIST" "$HISTORY_FILE" > "$NEW_LIST"
  else
    cp "$SCAN_LIST" "$NEW_LIST"
  fi

  while IFS= read -r FILE_PATH; do
    [ -n "$FILE_PATH" ] || continue
    echo "[$(date)] New photo detected via polling: $FILE_PATH" | tee -a "$LOG_FILE"

    FAIL_KEY="$FAIL_COUNT_DIR/$(printf '%s' "$FILE_PATH" | cksum | tr -s ' \t' '_')"
    # A pending retry means the file already passed the stability check once.
    if [ ! -f "$FAIL_KEY" ] && ! wait_for_stable_file "$FILE_PATH"; then
      echo "[$(date)] Skipping $FILE_PATH (unreadable or never stabilized; will retry next poll)." | tee -a "$LOG_FILE"
      continue
    fi
    # Record to history only after the uploader handled the file (exit 0), so
    # a crash mid-upload gets retried on the next poll instead of stranding it.
    if python3 ~/upload_photo.py "$FILE_PATH" >> "$LOG_FILE" 2>&1; then
      remember_uploaded "$FILE_PATH"
      rm -f "$FAIL_KEY"
    else
      FAILS=$(( $(cat "$FAIL_KEY" 2>/dev/null || echo 0) + 1 ))
      echo "$FAILS" > "$FAIL_KEY"
      if [ "$FAILS" -ge "$MAX_UPLOAD_ATTEMPTS" ]; then
        echo "[$(date)] GIVING UP on $FILE_PATH after $FAILS failed attempts; recording it so it stops retrying. Daily sync may still catch it." | tee -a "$LOG_FILE"
        remember_uploaded "$FILE_PATH"
        rm -f "$FAIL_KEY"
      else
        echo "[$(date)] Upload attempt $FAILS failed for $FILE_PATH; will retry on the next poll." | tee -a "$LOG_FILE"
      fi
    fi
  done < "$NEW_LIST"

  sleep 5
done
