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
# A file that never stabilizes (e.g. permanently 0-byte) would otherwise force
# a full rescan plus a ~30s stability stall on EVERY tick forever; give up on
# it after this many stability failures (tracked in "$FAIL_KEY.unstable").
MAX_STABILITY_ATTEMPTS=5
SCAN_LIST="$HOME/.calorie_watcher_scan.$$"
NEW_LIST="$HOME/.calorie_watcher_new.$$"
EXPECTED_KEYS="$HOME/.calorie_watcher_keys.$$"
# Heartbeat interval (seconds). 15 min instead of 5: fewer radio wakeups and
# less log churn. Trade-off: after connectivity returns, the offline-queue
# drain can start up to 15 min later — compensated by the uploader draining a
# bigger batch per heartbeat (QUEUE_BATCH_LIMIT default 10, see
# upload_photo.py). The bot only warns about a stale heartbeat after 2h, so
# 15-min pings stay far inside that threshold.
PING_INTERVAL_SECONDS="${PING_INTERVAL_SECONDS:-900}"
# Daily housekeeping (history pruning + stale fail-counter cleanup) runs once
# per this many poll iterations; the default is one day of 5-second polls.
CALORIE_HOUSEKEEP_POLLS="${CALORIE_HOUSEKEEP_POLLS:-17280}"

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

# Rotate watcher.log once it exceeds 1MB, keeping a single previous
# generation (~2MB total bound). Every writer appends per-write (>> / tee -a),
# so renaming the live file is safe: the next append recreates a fresh log.
rotate_log_if_needed() {
  local size
  size="$(stat -c %s "$LOG_FILE" 2>/dev/null || stat -f %z "$LOG_FILE" 2>/dev/null || echo 0)"
  if [ "$size" -gt 1048576 ] 2>/dev/null; then
    mv "$LOG_FILE" "$LOG_FILE.1"
    echo "[$(date)] Rotated watcher.log ($size bytes) to watcher.log.1." | tee -a "$LOG_FILE"
  fi
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
  rm -f "$PID_FILE" "$SCAN_LIST" "$NEW_LIST" "$EXPECTED_KEYS" "$HOME/.history.tmp"
  rmdir "$LOCK_DIR" 2>/dev/null
  return 0
}
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT

# Acquire a wake-lock so Android doesn't kill this process
termux-wake-lock

rotate_log_if_needed
echo "[$(date)] Starting CalorieTracker Android Watcher..." | tee -a "$LOG_FILE"

# Start background heartbeat ping (see PING_INTERVAL_SECONDS above)
(
  while true; do
    python3 ~/upload_photo.py --ping >> "$LOG_FILE" 2>&1
    sleep "$PING_INTERVAL_SECONDS"
  done
) &
PING_PID=$!

# Startup catch-up sync: photos taken while the watcher was OFF would
# otherwise wait for the 23:00 sync (or age past the lookback window).
# Termux:Boot starts the watcher on boot, so this recovers an outage's
# backlog within a minute of the phone coming back. The 30s delay lets
# the initial ping/queue-drain settle first; server-side dedup makes a
# sync that overlaps the seeding harmless.
(
  sleep 30
  echo "[$(date)] Startup catch-up sync (watcher was just started)..." | tee -a "$LOG_FILE"
  python3 ~/upload_photo.py --sync >> "$LOG_FILE" 2>&1
) &

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
# The scan pipeline is line-based (sort/comm/history), so a filename
# containing a newline would shatter into phantom entries; the awk guard
# drops any line that isn't a full camera-dir path with a photo extension
# (the nightly sync iterates in Python and still covers such files).
list_photos() {
  find "$CAMERA_DIR" -maxdepth 1 -type f \
    ! -name '.pending-*' \
    \( -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' -o -iname '*.heic' -o -iname '*.heif' \) \
    2>/dev/null \
    | LC_ALL=C awk -v dir="$CAMERA_DIR" \
        'index($0, dir) == 1 && tolower($0) ~ /\.(jpg|jpeg|png|heic|heif)$/' \
    | LC_ALL=C sort
}

# History stays sorted so each poll can diff it against the scan with comm.
remember_uploaded() {
  echo "$1" >> "$HISTORY_FILE"
  LC_ALL=C sort -u "$HISTORY_FILE" -o "$HISTORY_FILE"
}

# Camera-dir mtime for the poll gate. Read this BEFORE each full scan: a photo
# that lands mid-scan bumps the directory mtime past the cached value, so the
# next tick sees a change and rescans (TOCTOU-safe ordering). "unknown" (stat
# failed, e.g. storage unmounted) never matches, so those ticks always scan.
camera_dir_mtime() {
  stat -c %Y "$CAMERA_DIR" 2>/dev/null || stat -f %m "$CAMERA_DIR" 2>/dev/null || echo unknown
}

# True when at least one upload fail-counter exists; those files force a full
# scan every tick because retries must not wait on a camera-dir mtime change.
fail_counters_pending() {
  local entry
  for entry in "$FAIL_COUNT_DIR"/*; do
    [ -e "$entry" ] && return 0
  done
  return 1
}

# Any pending fail counter (plain or .unstable) forces a full scan every tick,
# and daily housekeeping only expires counters after 7 days — so a counter
# whose photo was deleted would disable mtime gating for up to a week. Each
# scan, drop counters whose source photo is no longer in the scan. The key is
# a one-way cksum of the path, so recompute the expected key set from the
# scan itself (same derivation as FAIL_KEY below).
prune_orphan_fail_counters() {
  local entry base path
  fail_counters_pending || return 0
  while IFS= read -r path; do
    [ -n "$path" ] || continue
    printf '%s' "$path" | cksum | tr -s ' \t' '_'
  done < "$SCAN_LIST" > "$EXPECTED_KEYS"
  for entry in "$FAIL_COUNT_DIR"/*; do
    [ -f "$entry" ] || continue
    base="${entry##*/}"
    base="${base%.unstable}"
    if ! grep -qxF "$base" "$EXPECTED_KEYS" 2>/dev/null; then
      rm -f "$entry"
    fi
  done
  rm -f "$EXPECTED_KEYS"
  return 0
}

# Daily housekeeping: prune history entries whose photos were deleted (keeps
# the history file bounded) and expire week-old fail counters. The caller only
# invokes this when the scan saw at least one photo — an unmounted or empty
# camera dir must never wipe the history, or every photo would mass re-upload
# once storage came back.
housekeep() {
  LC_ALL=C comm -12 "$HISTORY_FILE" "$SCAN_LIST" > "$HOME/.history.tmp" \
    && mv "$HOME/.history.tmp" "$HISTORY_FILE"
  find "$FAIL_COUNT_DIR" -type f -mtime +7 -delete 2>/dev/null
  return 0
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

# mtime-gated polling state: most 5s ticks touch nothing on flash. A full
# find+sort+comm scan only runs when the camera-dir mtime changed, a retry is
# pending (fail counter or a skipped-unstable file from the previous tick),
# or every 12th tick as a 60s safety net for FUSE mtime quirks.
LAST_DIR_MTIME=""
TICKS_SINCE_SCAN=0
RETRY_PENDING=0
POLLS_SINCE_HOUSEKEEP=0

while true; do
  rotate_log_if_needed
  TICKS_SINCE_SCAN=$((TICKS_SINCE_SCAN + 1))
  POLLS_SINCE_HOUSEKEEP=$((POLLS_SINCE_HOUSEKEEP + 1))

  # Read the dir mtime BEFORE any scan work (see camera_dir_mtime).
  DIR_MTIME="$(camera_dir_mtime)"
  if [ "$RETRY_PENDING" -eq 0 ] \
      && [ "$TICKS_SINCE_SCAN" -lt 12 ] \
      && [ "$DIR_MTIME" != "unknown" ] \
      && [ "$DIR_MTIME" = "$LAST_DIR_MTIME" ] \
      && ! fail_counters_pending; then
    sleep 5
    continue
  fi
  LAST_DIR_MTIME="$DIR_MTIME"
  TICKS_SINCE_SCAN=0
  RETRY_PENDING=0

  list_photos > "$SCAN_LIST"
  if [ -s "$SCAN_LIST" ]; then
    prune_orphan_fail_counters
  fi
  if [ -s "$HISTORY_FILE" ]; then
    LC_ALL=C comm -23 "$SCAN_LIST" "$HISTORY_FILE" > "$NEW_LIST"
  else
    cp "$SCAN_LIST" "$NEW_LIST"
  fi

  if [ "$POLLS_SINCE_HOUSEKEEP" -ge "$CALORIE_HOUSEKEEP_POLLS" ] && [ -s "$SCAN_LIST" ]; then
    housekeep
    POLLS_SINCE_HOUSEKEEP=0
  fi

  while IFS= read -r FILE_PATH; do
    [ -n "$FILE_PATH" ] || continue
    echo "[$(date)] New photo detected via polling: $FILE_PATH" | tee -a "$LOG_FILE"

    FAIL_KEY="$FAIL_COUNT_DIR/$(printf '%s' "$FILE_PATH" | cksum | tr -s ' \t' '_')"
    # A pending retry means the file already passed the stability check once.
    if [ ! -f "$FAIL_KEY" ] && ! wait_for_stable_file "$FILE_PATH"; then
      UNSTABLE_FAILS=$(( $(cat "$FAIL_KEY.unstable" 2>/dev/null || echo 0) + 1 ))
      if [ "$UNSTABLE_FAILS" -ge "$MAX_STABILITY_ATTEMPTS" ]; then
        echo "[$(date)] GIVING UP on $FILE_PATH after $UNSTABLE_FAILS stability failures (still empty or unreadable); recording it so it stops forcing rescans. Nightly sync can still recover it if it ever becomes readable." | tee -a "$LOG_FILE"
        remember_uploaded "$FILE_PATH"
        rm -f "$FAIL_KEY.unstable"
      else
        echo "$UNSTABLE_FAILS" > "$FAIL_KEY.unstable"
        echo "[$(date)] Skipping $FILE_PATH (unreadable or never stabilized; stability attempt $UNSTABLE_FAILS of $MAX_STABILITY_ATTEMPTS); will retry next poll." | tee -a "$LOG_FILE"
        RETRY_PENDING=1
      fi
      continue
    fi
    # Record to history only after the uploader handled the file (exit 0), so
    # a crash mid-upload gets retried on the next poll instead of stranding it.
    if python3 ~/upload_photo.py "$FILE_PATH" >> "$LOG_FILE" 2>&1; then
      remember_uploaded "$FILE_PATH"
      rm -f "$FAIL_KEY" "$FAIL_KEY.unstable"
    else
      FAILS=$(( $(cat "$FAIL_KEY" 2>/dev/null || echo 0) + 1 ))
      echo "$FAILS" > "$FAIL_KEY"
      if [ "$FAILS" -ge "$MAX_UPLOAD_ATTEMPTS" ]; then
        echo "[$(date)] GIVING UP on $FILE_PATH after $FAILS failed attempts; recording it so it stops retrying. Daily sync may still catch it." | tee -a "$LOG_FILE"
        remember_uploaded "$FILE_PATH"
        rm -f "$FAIL_KEY" "$FAIL_KEY.unstable"
      else
        echo "[$(date)] Upload attempt $FAILS failed for $FILE_PATH; will retry on the next poll." | tee -a "$LOG_FILE"
      fi
    fi
  done < "$NEW_LIST"

  sleep 5
done
