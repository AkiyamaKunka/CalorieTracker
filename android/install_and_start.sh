#!/data/data/com.termux/files/usr/bin/bash
set -e

SRC_DIR="${CALORIE_INSTALL_SRC:-/sdcard/Download}"

echo "[$(date)] Installing CalorieTracker Android scripts..."

# Verify the payload exists BEFORE killing the running watcher, so a botched
# copy never leaves the phone with no watcher at all.
for name in upload_photo.py android_watcher.sh; do
  if [ ! -f "$SRC_DIR/$name" ]; then
    echo "ERROR: $SRC_DIR/$name not found. Copy it there (or set CALORIE_INSTALL_SRC) and rerun." >&2
    exit 1
  fi
done

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

cp "$SRC_DIR/upload_photo.py" "$HOME/upload_photo.py"
cp "$SRC_DIR/android_watcher.sh" "$HOME/android_watcher.sh"
chmod +x "$HOME/upload_photo.py" "$HOME/android_watcher.sh"

# Optional: one-tap home-screen buttons via the Termux:Widget app. Shortcuts
# ship in a shortcuts/ folder next to the payload; absence is not an error.
if [ -d "$SRC_DIR/shortcuts" ]; then
  mkdir -p "$HOME/.shortcuts"
  cp "$SRC_DIR"/shortcuts/calorie-*.sh "$HOME/.shortcuts/" 2>/dev/null || true
  chmod +x "$HOME"/.shortcuts/calorie-*.sh 2>/dev/null || true
  echo "Installed Termux:Widget shortcuts: start / stop / status / sync."
fi

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

# Keep any pending offline uploads across reinstalls; just clear a stale
# queue lock (safe: every uploader process was killed above). The --ping
# below drains whatever is queued.
mkdir -p "$HOME/.offline_queue"
rm -rf "$HOME/.offline_queue/.process_lock"

python3 "$HOME/upload_photo.py" --ping

termux-wake-lock || true
nohup bash "$HOME/android_watcher.sh" >> "$HOME/watcher.log" 2>&1 &

echo "[$(date)] CalorieTracker watcher started."
