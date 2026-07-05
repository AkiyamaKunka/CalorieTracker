#!/data/data/com.termux/files/usr/bin/bash

# Configuration
CAMERA_DIR="/storage/emulated/0/DCIM/Camera/"
LOG_FILE="$HOME/watcher.log"
PID_FILE="$HOME/.calorie_watcher.pid"
LOCK_DIR="$HOME/.calorie_watcher.lock"

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  EXISTING_PID="$(cat "$PID_FILE" 2>/dev/null || echo unknown)"
  if [ "$EXISTING_PID" != "unknown" ] && ! kill -0 "$EXISTING_PID" 2>/dev/null; then
    rm -rf "$LOCK_DIR"
    mkdir "$LOCK_DIR" 2>/dev/null || exit 0
  else
    echo "[$(date)] CalorieTracker watcher is already running with PID $EXISTING_PID." | tee -a "$LOG_FILE"
    exit 0
  fi
fi

if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null && [ "$(cat "$PID_FILE")" != "$$" ]; then
  echo "[$(date)] CalorieTracker watcher is already running with PID $(cat "$PID_FILE")." | tee -a "$LOG_FILE"
  rmdir "$LOCK_DIR" 2>/dev/null || true
  exit 0
fi

echo $$ > "$PID_FILE"
cleanup() {
  rm -f "$PID_FILE"
  rmdir "$LOCK_DIR" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# Acquire a wake-lock so Android doesn't kill this process
termux-wake-lock

echo "[$(date)] Starting CalorieTracker Android Watcher..." | tee -a $LOG_FILE

# Start background ping every 5 minutes
(
  while true; do
    python3 ~/upload_photo.py --ping >> $LOG_FILE 2>&1
    sleep 300
  done
) &

# Start Daily Sync Reconciliation (11:00 PM)
(
  while true; do
    current_hour=$(date +%H)
    if [ "$current_hour" == "23" ]; then
      echo "[$(date)] Triggering Daily Sync..." | tee -a $LOG_FILE
      python3 ~/upload_photo.py --sync >> $LOG_FILE 2>&1
      # Sleep for 2 hours so it doesn't trigger again today
      sleep 7200
    else
      # Check again in 30 minutes
      sleep 1800
    fi
  done
) &


# Monitor the camera directory using a polling loop (bypasses Android emulated storage bug)
echo "[$(date)] Started polling loop for $CAMERA_DIR" | tee -a $LOG_FILE
HISTORY_FILE="$HOME/uploaded_files.log"
touch "$HISTORY_FILE"

# Seed the history with every photo already present at startup so restarts do not upload a backlog.
# The daily reconciliation job still catches today's missed photos at 11 PM.
find "$CAMERA_DIR" -maxdepth 1 -type f \
  \( -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' -o -iname '*.heic' \) \
  2>/dev/null >> "$HISTORY_FILE"
sort -u "$HISTORY_FILE" -o "$HISTORY_FILE" 2>/dev/null || true

while true; do
  find "$CAMERA_DIR" -maxdepth 1 -type f \
    \( -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' -o -iname '*.heic' \) \
    2>/dev/null | sort | while IFS= read -r FILE_PATH; do
    # Check if we have already uploaded this exact file
    if ! grep -Fxq "$FILE_PATH" "$HISTORY_FILE"; then
      echo "[$(date)] New photo detected via polling: $FILE_PATH" | tee -a $LOG_FILE
      
      # Add it to history so we NEVER upload it again
      echo "$FILE_PATH" >> "$HISTORY_FILE"
      
      # Let the file finish saving completely
      sleep 2
      
      # Pass it to the python script for offline queueing and upload
      python3 ~/upload_photo.py "$FILE_PATH"
    fi
  done
  
  sleep 5
done
