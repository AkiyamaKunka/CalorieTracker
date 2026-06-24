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


# Monitor the camera directory for new files
inotifywait -m -e close_write --format "%w%f" "$CAMERA_DIR" | while read FILE
do
  # Convert filename to lowercase to handle .JPG, .PNG, .HEIC, etc.
  FILE_LOWER="${FILE,,}"
  if [[ "$FILE_LOWER" == *.jpg ]] || [[ "$FILE_LOWER" == *.jpeg ]] || [[ "$FILE_LOWER" == *.png ]] || [[ "$FILE_LOWER" == *.heic ]]; then
    echo "[$(date)] New photo detected: $FILE" | tee -a $LOG_FILE
    
    # Let the file finish saving completely
    sleep 2
    
    # Pass it to the python script for offline queueing and upload
    python3 ~/upload_photo.py "$FILE" >> $LOG_FILE 2>&1
  fi
done
