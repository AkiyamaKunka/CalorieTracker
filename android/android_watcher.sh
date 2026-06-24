#!/data/data/com.termux/files/usr/bin/bash

# Configuration
CAMERA_DIR="/storage/emulated/0/DCIM/Camera/"
LOG_FILE="$HOME/watcher.log"

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

# Seed the history file with the absolute newest file on startup so it doesn't upload a backlog
INITIAL_FILE=$(ls -t "$CAMERA_DIR" 2>/dev/null | grep -iE '\.(jpg|jpeg|png|heic)$' | head -n 1)
if [ -n "$INITIAL_FILE" ]; then
  echo "$CAMERA_DIR/$INITIAL_FILE" >> "$HISTORY_FILE"
fi

while true; do
  # Find the newest media file in the camera directory
  NEWEST_BASENAME=$(ls -t "$CAMERA_DIR" 2>/dev/null | grep -iE '\.(jpg|jpeg|png|heic)$' | head -n 1)
  
  if [ -n "$NEWEST_BASENAME" ]; then
    # Ensure there is a trailing slash if missing
    if [[ "$CAMERA_DIR" != */ ]]; then
      NEWEST_FILE="$CAMERA_DIR/$NEWEST_BASENAME"
    else
      NEWEST_FILE="$CAMERA_DIR$NEWEST_BASENAME"
    fi
    
    # Check if we have already uploaded this exact file
    if ! grep -Fxq "$NEWEST_FILE" "$HISTORY_FILE"; then
      echo "[$(date)] New photo detected via polling: $NEWEST_FILE" | tee -a $LOG_FILE
      
      # Add it to history so we NEVER upload it again, even if newer photos are deleted
      echo "$NEWEST_FILE" >> "$HISTORY_FILE"
      
      # Let the file finish saving completely
      sleep 2
      
      # Pass it to the python script for offline queueing and upload
      python3 ~/upload_photo.py "$NEWEST_FILE" >> $LOG_FILE 2>&1
    fi
  fi
  sleep 5
done
