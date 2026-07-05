#!/data/data/com.termux/files/usr/bin/bash
set -e

echo "[$(date)] Installing CalorieTracker Android scripts..."

for pattern in android_watcher.sh upload_photo.py; do
  for pid in $(ps -ef 2>/dev/null | grep "$pattern" | grep -v grep | awk '{print $2}'); do
    if [ "$pid" != "$$" ]; then
      kill "$pid" 2>/dev/null || true
    fi
  done
done
sleep 1
for pattern in android_watcher.sh upload_photo.py; do
  for pid in $(ps -ef 2>/dev/null | grep "$pattern" | grep -v grep | awk '{print $2}'); do
    if [ "$pid" != "$$" ]; then
      kill -9 "$pid" 2>/dev/null || true
    fi
  done
done

rm -f "$HOME/.calorie_watcher.pid"
rm -rf "$HOME/.calorie_watcher.lock"

cp /sdcard/Download/upload_photo.py "$HOME/upload_photo.py"
cp /sdcard/Download/android_watcher.sh "$HOME/android_watcher.sh"
chmod +x "$HOME/upload_photo.py" "$HOME/android_watcher.sh"

CONFIG_FILE="${CALORIE_TRACKER_ANDROID_CONFIG:-$HOME/.calorie_tracker_upload.json}"
if [ ! -f "$CONFIG_FILE" ]; then
  cat > "$CONFIG_FILE" <<'JSON'
{
  "ANDROID_API_KEY": "replace-with-the-same-random-value-as-server",
  "SERVER_URLS": [
    "http://YOUR_GCP_IP",
    "http://YOUR_GCP_IP:5000"
  ]
}
JSON
  chmod 600 "$CONFIG_FILE"
  echo "Created $CONFIG_FILE."
  echo "Edit it with your real server URL and ANDROID_API_KEY, then rerun this installer."
  exit 1
fi
chmod 600 "$CONFIG_FILE"

if [ -d "$HOME/.offline_queue" ]; then
  mv "$HOME/.offline_queue" "$HOME/.offline_queue.backup.$(date +%Y%m%d_%H%M%S)"
fi
mkdir -p "$HOME/.offline_queue"

python3 "$HOME/upload_photo.py" --ping

termux-wake-lock || true
nohup bash "$HOME/android_watcher.sh" >> "$HOME/watcher.log" 2>&1 &

echo "[$(date)] CalorieTracker watcher started."
