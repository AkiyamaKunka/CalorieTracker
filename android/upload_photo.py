#!/usr/bin/env python3

import os
import sys
import json
import time
import requests
from pathlib import Path

# --- Configuration ---
API_KEY = "termux-super-secret-key-9988"
SERVER_URL = "http://SERVER-IP-REDACTED"
QUEUE_DIR = Path.home() / ".offline_queue"

def get_headers():
    return {"X-API-Key": API_KEY}

def process_queue():
    """Upload any photos that were saved while offline."""
    if not QUEUE_DIR.exists():
        return
        
    for item in QUEUE_DIR.glob("*.jpg"):
        print(f"[{time.strftime('%X')}] Attempting queued upload: {item.name}")
        success = upload_photo(str(item))
        if success:
            item.unlink()
        else:
            print(f"[{time.strftime('%X')}] Still offline, keeping in queue.")

def upload_photo(file_path):
    """Upload photo to the Cloud Bot API."""
    try:
        with open(file_path, "rb") as f:
            files = {"photo": f}
            response = requests.post(f"{SERVER_URL}/upload", headers=get_headers(), files=files, timeout=30)
            
        if response.status_code == 200:
            print(f"[{time.strftime('%X')}] Successfully uploaded to cloud!")
            return True
        else:
            print(f"[{time.strftime('%X')}] Upload failed with status {response.status_code}: {response.text}")
            return False
    except requests.exceptions.RequestException as e:
        print(f"[{time.strftime('%X')}] Network error: {e}")
        return False

def ping_server():
    """Send heartbeat ping."""
    try:
        response = requests.post(f"{SERVER_URL}/ping", headers=get_headers(), timeout=10)
        if response.status_code == 200:
            print(f"[{time.strftime('%X')}] Ping successful.")
            process_queue()  # If we're online, try to empty the queue!
    except requests.exceptions.RequestException:
        print(f"[{time.strftime('%X')}] Ping failed. Offline.")

def sync_photos():
    """Daily sync: Scan today's photos, hash them, and upload missing ones."""
    print(f"[{time.strftime('%X')}] Starting Daily Sync Reconciliation...")
    camera_dir = Path("/storage/emulated/0/DCIM/Camera")
    if not camera_dir.exists():
        return

    today_str = time.strftime("%Y%m%d")
    photo_hashes = {}
    
    # Hash all photos taken today
    for item in camera_dir.glob("*"):
        if item.is_file() and item.suffix.lower() in [".jpg", ".jpeg", ".png"]:
            # Check modification time to see if it's from today
            mtime = time.localtime(item.stat().st_mtime)
            if time.strftime("%Y%m%d", mtime) == today_str:
                try:
                    with open(item, "rb") as f:
                        img_hash = hashlib.md5(f.read()).hexdigest()
                        photo_hashes[img_hash] = item
                except Exception as e:
                    print(f"[{time.strftime('%X')}] Error hashing {item.name}: {e}")

    if not photo_hashes:
        print(f"[{time.strftime('%X')}] No photos taken today to sync.")
        return

    # Ask the server which hashes are missing
    try:
        response = requests.post(
            f"{SERVER_URL}/reconcile",
            json={"hashes": list(photo_hashes.keys())},
            headers=get_headers(),
            timeout=20
        )
        if response.status_code == 200:
            missing_hashes = response.json().get("missing_hashes", [])
            print(f"[{time.strftime('%X')}] Server is missing {len(missing_hashes)} photos.")
            
            for m_hash in missing_hashes:
                file_path = photo_hashes[m_hash]
                print(f"[{time.strftime('%X')}] Uploading missed photo: {file_path.name}")
                success = upload_photo(str(file_path))
                if not success:
                    # Queue it if offline
                    QUEUE_DIR.mkdir(exist_ok=True)
                    dst = QUEUE_DIR / file_path.name
                    with open(file_path, "rb") as src, open(dst, "wb") as dst_f:
                        dst_f.write(src.read())
        else:
            print(f"[{time.strftime('%X')}] Reconcile failed: {response.text}")
    except requests.exceptions.RequestException as e:
        print(f"[{time.strftime('%X')}] Reconcile network error: {e}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 upload_photo.py <path_to_photo> | --ping | --sync")
        sys.exit(1)
        
    arg = sys.argv[1]
    
    if arg == "--ping":
        ping_server()
    elif arg == "--sync":
        sync_photos()
    else:
        process_queue()
        success = upload_photo(arg)
        if not success:
            QUEUE_DIR.mkdir(exist_ok=True)
            filename = os.path.basename(arg)
            queued_path = QUEUE_DIR / filename
            with open(arg, "rb") as src, open(queued_path, "wb") as dst:
                dst.write(src.read())
            print(f"[{time.strftime('%X')}] Saved {filename} to offline queue.")
