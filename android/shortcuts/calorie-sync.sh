#!/data/data/com.termux/files/usr/bin/bash
# Termux:Widget one-tap shortcut — reconcile recent photos with the server
# right now instead of waiting for the nightly 23:00 sync.

echo "🔄 Reconciling today's and yesterday's photos with the server..."
python3 "$HOME/upload_photo.py" --sync
