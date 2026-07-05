#!/usr/bin/env python3

import os
import sys
import json
import time
import requests
import hashlib
import re
import shutil
import subprocess
from pathlib import Path

# --- Configuration ---
CONFIG_PATH = Path(os.environ.get("CALORIE_TRACKER_ANDROID_CONFIG", Path.home() / ".calorie_tracker_upload.json"))


def _load_local_config():
    try:
        return json.loads(CONFIG_PATH.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as e:
        print(f"[{time.strftime('%X')}] Could not read config {CONFIG_PATH}: {e}")
        return {}


def _split_urls(value):
    if not value:
        return []
    if isinstance(value, list):
        return [str(item).strip().rstrip("/") for item in value if str(item).strip()]
    return [item.strip().rstrip("/") for item in str(value).split(",") if item.strip()]


def _env_int(name, default, min_value=None, max_value=None):
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        print(f"[{time.strftime('%X')}] Invalid {name}; using {default}.")
        value = default
    if min_value is not None:
        value = max(min_value, value)
    if max_value is not None:
        value = min(max_value, value)
    return value


LOCAL_CONFIG = _load_local_config()
API_KEY = os.environ.get("ANDROID_API_KEY") or LOCAL_CONFIG.get("ANDROID_API_KEY") or LOCAL_CONFIG.get("api_key") or ""
SERVER_URLS = (
    _split_urls(os.environ.get("CALORIE_TRACKER_SERVER_URLS"))
    or _split_urls(LOCAL_CONFIG.get("SERVER_URLS") or LOCAL_CONFIG.get("server_urls"))
    or _split_urls(LOCAL_CONFIG.get("SERVER_URL") or LOCAL_CONFIG.get("server_url"))
    or ["http://YOUR_GCP_IP"]
)
QUEUE_DIR = Path.home() / ".offline_queue"
QUEUE_BATCH_LIMIT = _env_int("QUEUE_BATCH_LIMIT", 3, 1, 100)
QUEUE_LOCK_STALE_SECONDS = _env_int("QUEUE_LOCK_STALE_SECONDS", 600, 30, 86400)
SUPPORTED_QUEUE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".heif"}
VPN_INTERFACE_PREFIXES = ("tun", "tap", "wg", "ppp", "tailscale", "utun")

SERVER_URL = SERVER_URLS[0]

def _require_api_key():
    if not API_KEY:
        raise RuntimeError(
            f"ANDROID_API_KEY is not configured. Set it in the environment or {CONFIG_PATH}."
        )
    return API_KEY

def _looks_like_vpn_interface(name):
    return name.lower().startswith(VPN_INTERFACE_PREFIXES)

def _command_output(args):
    try:
        return subprocess.check_output(
            args,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        )
    except Exception:
        return ""

def _network_interfaces_from_sysfs():
    try:
        return [item.name for item in Path("/sys/class/net").iterdir()]
    except OSError:
        return []

def detect_vpn_status():
    """Best-effort VPN detection for Android/Termux."""
    override = os.environ.get("ANDROID_VPN_ACTIVE")
    if override:
        active = override.strip().lower() in {"1", "true", "yes", "on"}
        return active, "env_override"

    candidates = []
    route_text = _command_output(["ip", "route"]) or _command_output(["/system/bin/ip", "route"])
    addr_text = _command_output(["ip", "addr"]) or _command_output(["/system/bin/ip", "addr"])

    candidates.extend(re.findall(r"\bdev\s+([^\s]+)", route_text))
    candidates.extend(re.findall(r"^\d+:\s+([^:@]+)", addr_text, flags=re.MULTILINE))
    candidates.extend(_network_interfaces_from_sysfs())

    proc_route = ""
    try:
        proc_route = Path("/proc/net/route").read_text()
        for line in proc_route.splitlines()[1:]:
            parts = line.split()
            if parts:
                candidates.append(parts[0])
    except OSError:
        pass

    vpn_interfaces = sorted({name for name in candidates if _looks_like_vpn_interface(name)})
    if vpn_interfaces:
        return True, ",".join(vpn_interfaces)[:80]

    if route_text or addr_text or proc_route:
        return False, "no_vpn_interface"

    return False, "vpn_check_unavailable"

def vpn_check_reliable(vpn_active, vpn_check):
    """Whether the local Android check is strong enough to warn on by itself."""
    if vpn_active:
        return True
    return vpn_check == "env_override"

def get_status_payload():
    vpn_active, vpn_check = detect_vpn_status()
    return {
        "timezone": time.strftime("%z") or "+0800",
        "vpn_active": vpn_active,
        "vpn_check": vpn_check,
        "vpn_check_reliable": vpn_check_reliable(vpn_active, vpn_check),
    }

def get_headers(status_payload=None):
    if status_payload is None:
        status_payload = get_status_payload()
    vpn_active = status_payload["vpn_active"]
    vpn_check = status_payload["vpn_check"]
    return {
        "X-API-Key": _require_api_key(),
        "X-VPN-Active": "true" if vpn_active else "false",
        "X-VPN-Check": vpn_check,
        "X-VPN-Check-Reliable": "true" if status_payload["vpn_check_reliable"] else "false",
    }

def get_server_url():
    """Try URLs and return the first reachable one."""
    if not API_KEY:
        return SERVER_URLS[0]

    for url in SERVER_URLS:
        try:
            payload = get_status_payload()
            # Increased timeout to 5s because Cellular networks can be slow to wake up
            resp = requests.post(
                f"{url}/ping",
                headers=get_headers(payload),
                json=payload,
                timeout=5,
            )
            if resp.status_code == 200:
                return url
        except requests.exceptions.RequestException:
            pass
    # If both fail (e.g. offline), default to Port 80 since Port 5000 is likely blocked anyway
    return SERVER_URLS[0]


def refresh_server_url():
    global SERVER_URL
    SERVER_URL = get_server_url()
    return SERVER_URL

def _queued_items():
    items = []
    for item in QUEUE_DIR.iterdir():
        try:
            if item.is_file() and item.suffix.lower() in SUPPORTED_QUEUE_EXTENSIONS:
                items.append((item.stat().st_mtime, item))
        except OSError:
            continue
    return [item for _, item in sorted(items, key=lambda pair: pair[0])]

def _acquire_queue_lock():
    QUEUE_DIR.mkdir(exist_ok=True)
    lock_dir = QUEUE_DIR / ".process_lock"
    try:
        lock_dir.mkdir()
        return True
    except FileExistsError:
        try:
            age = time.time() - lock_dir.stat().st_mtime
            if age > QUEUE_LOCK_STALE_SECONDS:
                print(f"[{time.strftime('%X')}] Removing stale queue lock ({int(age)}s old).")
                lock_dir.rmdir()
                lock_dir.mkdir()
                return True
        except OSError:
            pass
        return False

def _release_queue_lock():
    try:
        (QUEUE_DIR / ".process_lock").rmdir()
    except OSError:
        pass

def queue_photo(file_path):
    """Copy a failed upload to the offline queue without clobbering another file."""
    QUEUE_DIR.mkdir(exist_ok=True)
    src_path = Path(file_path)
    queued_path = QUEUE_DIR / src_path.name
    if queued_path.exists():
        suffix = src_path.suffix or ".jpg"
        queued_path = QUEUE_DIR / f"{src_path.stem}_{int(time.time())}{suffix}"

    shutil.copy2(src_path, queued_path)
    print(f"[{time.strftime('%X')}] Saved {src_path.name} to offline queue.")

def process_queue(max_items=QUEUE_BATCH_LIMIT):
    """Upload any photos that were saved while offline."""
    if not QUEUE_DIR.exists():
        return

    if not _acquire_queue_lock():
        print(f"[{time.strftime('%X')}] Queue is already being processed, skipping.")
        return
        
    try:
        processed = 0
        for item in _queued_items():
            if max_items is not None and processed >= max_items:
                break

            print(f"[{time.strftime('%X')}] Attempting queued upload: {item.name}")
            success = upload_photo(str(item))
            processed += 1
            if success:
                item.unlink()
            else:
                print(f"[{time.strftime('%X')}] Upload failed, pausing queue drain.")
                break
    finally:
        _release_queue_lock()

def upload_photo(file_path):
    """Upload photo to the Cloud Bot API."""
    try:
        _require_api_key()
        with open(file_path, "rb") as f:
            files = {"photo": f}
            response = requests.post(f"{SERVER_URL}/upload", headers=get_headers(), files=files, timeout=30)
            
        if response.status_code == 200:
            print(f"[{time.strftime('%X')}] Successfully uploaded to cloud!")
            return True
        else:
            print(f"[{time.strftime('%X')}] Upload failed with status {response.status_code}: {response.text}")
            return False
    except (requests.exceptions.RequestException, RuntimeError) as e:
        print(f"[{time.strftime('%X')}] Network/config error: {e}")
        return False

def ping_server():
    """Send heartbeat ping."""
    payload = get_status_payload()
    try:
        _require_api_key()
        refresh_server_url()
        response = requests.post(
            f"{SERVER_URL}/ping",
            headers=get_headers(payload),
            json=payload,
            timeout=10,
        )
        if response.status_code == 200:
            vpn_label = "on" if payload["vpn_active"] else "off"
            print(f"[{time.strftime('%X')}] Ping successful. VPN: {vpn_label} ({payload['vpn_check']}).")
            process_queue()  # If we're online, try to empty the queue!
    except (requests.exceptions.RequestException, RuntimeError) as e:
        print(f"[{time.strftime('%X')}] Ping failed. Offline or misconfigured: {e}")

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
        _require_api_key()
        refresh_server_url()
        payload = get_status_payload()
        response = requests.post(
            f"{SERVER_URL}/reconcile",
            json={**payload, "hashes": list(photo_hashes.keys())},
            headers=get_headers(payload),
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
                    queue_photo(str(file_path))
        else:
            print(f"[{time.strftime('%X')}] Reconcile failed: {response.text}")
    except (requests.exceptions.RequestException, RuntimeError) as e:
        print(f"[{time.strftime('%X')}] Reconcile network/config error: {e}")

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
        refresh_server_url()
        success = upload_photo(arg)
        if not success:
            queue_photo(arg)
        else:
            process_queue(max_items=1)
