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
from datetime import datetime
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
# Heartbeats arrive every PING_INTERVAL_SECONDS (default 15 min, up from 5 —
# see android_watcher.sh), so each ping drains up to 10 queued photos instead
# of 3 to compensate. Trade-off: after connectivity returns, the drain can
# start up to 15 min later, but a day-scale backlog still clears in a few
# heartbeats.
QUEUE_BATCH_LIMIT = _env_int("QUEUE_BATCH_LIMIT", 10, 1, 100)
QUEUE_LOCK_STALE_SECONDS = _env_int("QUEUE_LOCK_STALE_SECONDS", 600, 30, 86400)
SUPPORTED_QUEUE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".heif"}
# Statuses where retrying the same file can never succeed (bad request, too
# large, unsupported type): quarantine instead of wedging the queue forever.
# Only trusted when the response body is JSON (i.e. it came from our Flask
# app); the same status from a proxy (HTML body) is treated as transient.
PERMANENT_REJECT_STATUSES = {400, 413, 415}
# The watcher documents CAMERA_DIR; honor it as a fallback so overriding the
# camera directory does not silently void the daily-sync safety net.
CAMERA_DIR = Path(
    os.environ.get("CALORIE_CAMERA_DIR")
    or os.environ.get("CAMERA_DIR")
    or "/storage/emulated/0/DCIM/Camera"
)
VPN_INTERFACE_PREFIXES = ("tun", "tap", "wg", "ppp", "tailscale", "utun")
_VPN_CACHE_TTL_SECONDS = 60
_VPN_CACHE = {"checked_at": 0.0, "status": None}

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

def _cached_vpn_status():
    """detect_vpn_status() spawns subprocesses; cache the result briefly."""
    now = time.monotonic()
    status = _VPN_CACHE["status"]
    if status is None or now - _VPN_CACHE["checked_at"] > _VPN_CACHE_TTL_SECONDS:
        status = detect_vpn_status()
        _VPN_CACHE["checked_at"] = now
        _VPN_CACHE["status"] = status
    return status

def get_status_payload():
    vpn_active, vpn_check = _cached_vpn_status()
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

def _unclobbered_destination(dest_dir, src_path):
    """Pick a name in dest_dir that does not overwrite an existing file."""
    dest_path = dest_dir / src_path.name
    if dest_path.exists():
        suffix = src_path.suffix or ".jpg"
        dest_path = dest_dir / f"{src_path.stem}_{int(time.time())}{suffix}"
    return dest_path

def queue_photo(file_path):
    """Copy a failed upload to the offline queue without clobbering another file.

    Returns True when the photo was queued, False when it could not be saved
    (so the caller can signal the watcher to retry instead of losing the photo).
    """
    src_path = Path(file_path)
    try:
        QUEUE_DIR.mkdir(exist_ok=True)
        shutil.copy2(src_path, _unclobbered_destination(QUEUE_DIR, src_path))
    except OSError as e:
        print(f"[{time.strftime('%X')}] Could not queue {src_path.name}: {e}")
        return False
    print(f"[{time.strftime('%X')}] Saved {src_path.name} to offline queue.")
    return True

def _quarantine_rejected(item, status):
    """Move a permanently-rejected photo aside so it stops wedging the queue."""
    rejected_dir = QUEUE_DIR / "rejected"
    try:
        rejected_dir.mkdir(parents=True, exist_ok=True)
        dest_path = _unclobbered_destination(rejected_dir, item)
        shutil.move(str(item), str(dest_path))
    except OSError as e:
        print(f"[{time.strftime('%X')}] Could not quarantine {item.name}: {e}")
        return
    print(
        f"[{time.strftime('%X')}] REJECTED by server (HTTP {status}): {item.name} "
        f"moved to {dest_path}. It will NOT be retried."
    )

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
            status, body_is_json = _upload_photo_status(str(item))
            processed += 1
            if status == 200:
                item.unlink()
            elif status in PERMANENT_REJECT_STATUSES and body_is_json:
                # Only the Flask app answers with JSON, so this reject is
                # authoritative: retrying the same file can never succeed.
                _quarantine_rejected(item, status)
            else:
                if status in PERMANENT_REJECT_STATUSES:
                    print(
                        f"[{time.strftime('%X')}] HTTP {status} with a non-JSON body "
                        f"(likely a proxy, not the app); keeping {item.name} queued."
                    )
                print(f"[{time.strftime('%X')}] Upload failed, pausing queue drain.")
                break
    finally:
        _release_queue_lock()

RECOMPRESS_MAX_EDGE = int(os.environ.get("CALORIE_RECOMPRESS_MAX_EDGE", "1600"))
RECOMPRESS_JPEG_QUALITY = int(os.environ.get("CALORIE_RECOMPRESS_JPEG_QUALITY", "80"))
# Decode budget: a W*H image inflates to roughly W*H*3 bytes of pixel data.
# Past this many pixels (~600MB decoded) skip recompression and upload the
# original bytes instead — the server-side downscale still protects Gemini.
RECOMPRESS_MAX_PIXELS = 200_000_000


def _recompress_for_upload(original_bytes):
    """Best-effort downscale/re-encode before upload; None keeps the original.

    Gemini needs nowhere near a 10-25MB camera file, and cellular+VPN pays
    for every byte. Pillow may be missing in Termux and HEIC may not decode
    there — any failure, or a result that isn't actually smaller, falls back
    to the original bytes.

    Decode memory stays bounded: the pixel budget is checked on the header
    (before any pixel data is decoded), JPEGs are decoded at reduced scale
    via draft(), and the EXIF orientation transpose runs on the already
    shrunken image so no full-resolution copy is ever duplicated.
    """
    if RECOMPRESS_MAX_EDGE <= 0:
        return None
    try:
        import io as _io

        from PIL import Image, ImageOps

        img = Image.open(_io.BytesIO(original_bytes))
        # Header-only size check: no pixel data has been decoded yet.
        if img.size[0] * img.size[1] > RECOMPRESS_MAX_PIXELS:
            return None
        # JPEG decodes straight from the codec at 1/2..1/8 scale; a harmless
        # no-op for every other format.
        img.draft("RGB", (RECOMPRESS_MAX_EDGE, RECOMPRESS_MAX_EDGE))
        img.thumbnail((RECOMPRESS_MAX_EDGE, RECOMPRESS_MAX_EDGE))
        # Transpose AFTER shrinking: the EXIF orientation tag survives on the
        # Image object, and transposing the small image avoids holding two
        # full-resolution copies in memory at once.
        img = ImageOps.exif_transpose(img)
        out = _io.BytesIO()
        img.convert("RGB").save(out, format="JPEG", quality=RECOMPRESS_JPEG_QUALITY)
        recompressed = out.getvalue()
    except Exception:
        return None
    if len(recompressed) >= len(original_bytes):
        return None
    return recompressed


_FILENAME_TS_RE = re.compile(r"(?:^|[^0-9])(\d{8})_(\d{6})")


def _captured_at_from_filename(name):
    """Extract device-local capture time from a camera filename.

    Honor/most Androids name photos IMG_YYYYMMDD_HHMMSS.jpg in local wall
    time. Returns 'YYYY-MM-DD HH:MM:SS' or None when the name has no valid
    timestamp (the server then falls back to upload-time dating).
    """
    match = _FILENAME_TS_RE.search(name or "")
    if not match:
        return None
    try:
        captured = datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M%S")
    except ValueError:
        return None
    return captured.strftime("%Y-%m-%d %H:%M:%S")


def _upload_photo_status(file_path):
    """Upload a photo and return (status, body_is_json).

    status is the HTTP status code, or None on network/config error.
    body_is_json is True when the response body parses as JSON — our Flask app
    always answers with JSON, while an intermediary (e.g. a default nginx with
    a 1MB client_max_body_size) rejects with HTML. Callers use it to tell an
    app-origin permanent reject apart from a proxy-origin one.

    The server keys dedup and /reconcile on the ORIGINAL file's hash, so it
    is declared via the additive original_hash form field even when the
    uploaded bytes are recompressed.
    """
    try:
        _require_api_key()
        with open(file_path, "rb") as f:
            original_bytes = f.read()
        original_hash = hashlib.md5(original_bytes).hexdigest()
        upload_bytes = _recompress_for_upload(original_bytes)
        upload_name = Path(file_path).name
        if upload_bytes is None:
            upload_bytes = original_bytes
        else:
            upload_name = Path(file_path).stem + ".jpg"
            print(f"[{time.strftime('%X')}] Recompressed {Path(file_path).name}: "
                  f"{len(original_bytes)} -> {len(upload_bytes)} bytes")
        form_data = {"original_hash": original_hash}
        captured_at = _captured_at_from_filename(Path(file_path).name)
        if captured_at:
            # Backfilled photos (nightly sync, offline-queue drain) must land
            # on the day they were TAKEN, not the day the upload succeeded.
            form_data["captured_at"] = captured_at
        response = requests.post(
            f"{SERVER_URL}/upload",
            headers=get_headers(),
            files={"photo": (upload_name, upload_bytes)},
            data=form_data,
            timeout=30,
        )
    except (requests.exceptions.RequestException, RuntimeError, OSError) as e:
        print(f"[{time.strftime('%X')}] Network/config error: {e}")
        return None, False

    try:
        response.json()
        body_is_json = True
    except ValueError:
        body_is_json = False

    if response.status_code == 200:
        print(f"[{time.strftime('%X')}] Successfully uploaded to cloud!")
    else:
        print(f"[{time.strftime('%X')}] Upload failed with status {response.status_code}: {response.text}")
    return response.status_code, body_is_json

def upload_photo(file_path):
    """Upload photo to the Cloud Bot API."""
    status, _ = _upload_photo_status(file_path)
    return status == 200

def ping_server():
    """Send heartbeat ping. The ping itself is the reachability probe: the
    first URL that answers 200 becomes SERVER_URL for the queue drain."""
    global SERVER_URL
    payload = get_status_payload()
    try:
        _require_api_key()
    except RuntimeError as e:
        print(f"[{time.strftime('%X')}] Ping failed. Offline or misconfigured: {e}")
        return

    last_error = "no server URLs configured"
    for url in SERVER_URLS:
        try:
            response = requests.post(
                f"{url}/ping",
                headers=get_headers(payload),
                json=payload,
                timeout=10,
            )
        except requests.exceptions.RequestException as e:
            last_error = str(e)
            continue
        if response.status_code == 200:
            SERVER_URL = url
            vpn_label = "on" if payload["vpn_active"] else "off"
            print(f"[{time.strftime('%X')}] Ping successful. VPN: {vpn_label} ({payload['vpn_check']}).")
            process_queue()  # If we're online, try to empty the queue!
            return
        last_error = f"HTTP {response.status_code}"
    print(f"[{time.strftime('%X')}] Ping failed. Offline or misconfigured: {last_error}")

def sync_photos():
    """Daily sync: Scan recent photos, hash them, and upload missing ones."""
    print(f"[{time.strftime('%X')}] Starting Daily Sync Reconciliation...")
    camera_dir = CAMERA_DIR
    if not camera_dir.exists():
        return

    # Photos taken after yesterday's 23:00 sync (e.g. while the watcher was
    # down) would otherwise never be reconciled; server-side dedup makes
    # re-offering yesterday's hashes idempotent.
    valid_days = {
        time.strftime("%Y%m%d"),
        time.strftime("%Y%m%d", time.localtime(time.time() - 86400)),
    }
    photo_hashes = {}

    # Hash all photos taken today or yesterday
    for item in camera_dir.glob("*"):
        if item.is_file() and item.suffix.lower() in SUPPORTED_QUEUE_EXTENSIONS:
            try:
                mtime = time.localtime(item.stat().st_mtime)
            except OSError:
                continue
            if time.strftime("%Y%m%d", mtime) in valid_days:
                try:
                    with open(item, "rb") as f:
                        img_hash = hashlib.md5(f.read()).hexdigest()
                        photo_hashes[img_hash] = item
                except Exception as e:
                    print(f"[{time.strftime('%X')}] Error hashing {item.name}: {e}")

    if not photo_hashes:
        print(f"[{time.strftime('%X')}] No photos from the last two days to sync.")
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

def main(argv=None):
    """CLI entry point. Returns the process exit code.

    Exit 0 means the photo was uploaded or safely queued; exit 1 means it was
    neither, so the watcher must keep retrying instead of marking it done.
    """
    argv = sys.argv if argv is None else argv
    if len(argv) < 2:
        print("Usage: python3 upload_photo.py <path_to_photo> | --ping | --sync")
        return 1

    arg = argv[1]

    if arg == "--ping":
        ping_server()
    elif arg == "--sync":
        sync_photos()
    else:
        # Try the current server first. Any failure — network-level (None) or
        # HTTP-level (a foreign port-80 listener can answer 404/502) — triggers
        # a re-probe; retry when the probe was inconclusive (status None) or it
        # actually moved us to a different URL.
        status, _ = _upload_photo_status(arg)
        if status != 200:
            failed_url = SERVER_URL
            refresh_server_url()
            if status is None or SERVER_URL != failed_url:
                status, _ = _upload_photo_status(arg)
        if status == 200:
            process_queue(max_items=1)
        elif not queue_photo(arg):
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
