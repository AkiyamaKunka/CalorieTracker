import sys
import os
import hashlib
import subprocess
import time
import requests
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Add android dir to path to import upload_photo
ANDROID_DIR = Path(__file__).resolve().parent.parent / "android"
sys.path.append(str(ANDROID_DIR))
import upload_photo

@pytest.fixture(autouse=True)
def reset_vpn_cache():
    # The VPN status cache is module-level; keep tests independent.
    upload_photo._VPN_CACHE["checked_at"] = 0.0
    upload_photo._VPN_CACHE["status"] = None
    yield
    upload_photo._VPN_CACHE["checked_at"] = 0.0
    upload_photo._VPN_CACHE["status"] = None

@pytest.fixture
def mock_queue_dir(tmp_path):
    # Override QUEUE_DIR to point to a temporary pytest directory
    old_queue = upload_photo.QUEUE_DIR
    upload_photo.QUEUE_DIR = tmp_path / ".offline_queue"
    yield upload_photo.QUEUE_DIR
    upload_photo.QUEUE_DIR = old_queue

@pytest.fixture
def mock_env():
    old_urls = upload_photo.SERVER_URLS
    old_server_url = upload_photo.SERVER_URL
    old_api_key = upload_photo.API_KEY
    # Use dummy URLs for testing
    upload_photo.SERVER_URLS = ["http://mock-cellular.test", "http://mock-wifi.test:5000"]
    upload_photo.SERVER_URL = "http://mock-cellular.test"
    upload_photo.API_KEY = "test-upload-key"
    yield
    upload_photo.SERVER_URLS = old_urls
    upload_photo.SERVER_URL = old_server_url
    upload_photo.API_KEY = old_api_key

@patch('upload_photo.requests.post')
def test_a1_complete_offline_mode(mock_post, mock_queue_dir, tmp_path, mock_env):
    """Test Case A1: Complete Offline Mode"""
    # Simulate completely offline: requests.post throws ConnectionError
    mock_post.side_effect = requests.exceptions.ConnectionError("Offline")

    # Create a dummy image
    img_path = tmp_path / "test_photo.jpg"
    img_path.write_bytes(b"dummy image data")

    # upload_photo() should return False
    assert upload_photo.upload_photo(str(img_path)) is False

    # Queueing is done by the caller; exercise the real helper so we also pin
    # that queue_photo() auto-creates QUEUE_DIR and reports success.
    assert not upload_photo.QUEUE_DIR.exists()
    assert upload_photo.queue_photo(str(img_path)) is True

    # Queue entries are named <md5-12>__<original-name> so identical content
    # dedups while the original name still drives captured_at derivation.
    entry_name = hashlib.md5(b"dummy image data").hexdigest()[:12] + "__test_photo.jpg"
    assert (upload_photo.QUEUE_DIR / entry_name).read_bytes() == b"dummy image data"

@patch('upload_photo.requests.post')
def test_a2_cellular_fallback(mock_post, mock_env, monkeypatch):
    """Test Case A2: Cellular Fallback (first URL blocked, second wins)"""
    monkeypatch.setattr(
        upload_photo, "SERVER_URLS",
        ["http://mock-wifi.test:5000", "http://mock-cellular.test"],
    )

    def mock_ping(url, **kwargs):
        if "5000" in url:
            raise requests.exceptions.Timeout("Port 5000 Blocked")
        resp = MagicMock()
        resp.status_code = 200
        return resp

    mock_post.side_effect = mock_ping

    # The Timeout URL is first, so success proves the fallback actually ran.
    url = upload_photo.get_server_url()
    assert url == "http://mock-cellular.test"
    assert mock_post.call_count == 2

@patch('upload_photo.requests.post')
def test_a2_all_urls_down_defaults_to_first(mock_post, mock_env):
    mock_post.side_effect = requests.exceptions.ConnectionError("Offline everywhere")

    assert upload_photo.get_server_url() == upload_photo.SERVER_URLS[0]
    assert mock_post.call_count == len(upload_photo.SERVER_URLS)

@patch('upload_photo.requests.post')
def test_a3_a7_process_queue_on_reconnect(mock_post, mock_queue_dir, tmp_path, mock_env):
    """Test Case A3 & A7: Flush queue on reconnect and delete queued files"""
    # Create some offline queued files
    mock_queue_dir.mkdir(exist_ok=True)
    (mock_queue_dir / "offline1.jpg").write_bytes(b"data1")
    (mock_queue_dir / "offline2.jpg").write_bytes(b"data2")

    # Simulate Server coming back online (returns 200 OK)
    resp = MagicMock()
    resp.status_code = 200
    mock_post.return_value = resp

    # Process the queue
    upload_photo.SERVER_URL = "http://mock-cellular.test"
    upload_photo.process_queue()

    # Verify post was called twice
    assert mock_post.call_count == 2

    # Verify the queue is now empty (Test Case A7)
    assert not (mock_queue_dir / "offline1.jpg").exists()
    assert not (mock_queue_dir / "offline2.jpg").exists()

@patch('upload_photo.requests.post')
def test_a9_api_key_rejection(mock_post, mock_queue_dir, tmp_path, mock_env):
    """Test Case A9: API Key Rejection doesn't delete file but correctly fails"""
    # Simulate 401 Unauthorized
    resp = MagicMock()
    resp.status_code = 401
    resp.text = "Unauthorized"
    mock_post.return_value = resp

    img_path = tmp_path / "test_photo.jpg"
    img_path.write_bytes(b"dummy")

    assert upload_photo.upload_photo(str(img_path)) is False

def test_a10_missing_queue_dir(mock_queue_dir):
    """Test Case A10: Process queue when directory doesn't exist"""
    # Ensure it doesn't crash if QUEUE_DIR is missing
    if mock_queue_dir.exists():
        mock_queue_dir.rmdir()

    # Should safely return without exception
    upload_photo.process_queue()

def test_vpn_check_reliable_only_for_positive_or_override():
    assert upload_photo.vpn_check_reliable(True, "tun0") is True
    assert upload_photo.vpn_check_reliable(False, "env_override") is True
    assert upload_photo.vpn_check_reliable(False, "no_vpn_interface") is False
    assert upload_photo.vpn_check_reliable(False, "vpn_check_unavailable") is False

def test_a11_storage_full_skips_queueing(mock_queue_dir, tmp_path, monkeypatch, capsys):
    """Test Case A11: storage-full OSError from copy2 is caught, not raised"""
    img_path = tmp_path / "test_photo.jpg"
    img_path.write_bytes(b"dummy")

    def fail_copy(src, dst):
        raise OSError("No space left on device")

    monkeypatch.setattr(upload_photo.shutil, "copy2", fail_copy)

    # Must not raise, and must report failure so the CLI can exit non-zero.
    assert upload_photo.queue_photo(str(img_path)) is False

    assert "Could not queue test_photo.jpg" in capsys.readouterr().out
    assert not (mock_queue_dir / "test_photo.jpg").exists()


def test_queue_photo_vanished_source_is_skipped(mock_queue_dir, tmp_path, capsys):
    assert upload_photo.queue_photo(str(tmp_path / "gone.jpg")) is False  # must not raise

    assert "Could not queue gone.jpg" in capsys.readouterr().out


def test_upload_photo_vanished_file_returns_false(mock_env, tmp_path):
    # open() raises FileNotFoundError before any network call happens
    assert upload_photo.upload_photo(str(tmp_path / "gone.jpg")) is False


def test_queue_photo_avoids_clobbering_existing_file(mock_queue_dir, tmp_path):
    """Same name, different bytes (e.g. a pre-upgrade queue entry): both
    copies must survive — content-hash entry names keep them apart."""
    img_path = tmp_path / "photo.jpg"
    img_path.write_bytes(b"new")
    mock_queue_dir.mkdir(exist_ok=True)
    (mock_queue_dir / "photo.jpg").write_bytes(b"old")

    upload_photo.queue_photo(str(img_path))

    assert (mock_queue_dir / "photo.jpg").read_bytes() == b"old"
    hashed_name = hashlib.md5(b"new").hexdigest()[:12] + "__photo.jpg"
    assert (mock_queue_dir / hashed_name).read_bytes() == b"new"


def test_queue_photo_same_content_is_noop(mock_queue_dir, tmp_path):
    """Re-queueing identical bytes must not create a second copy and must
    still report success (the photo IS safely queued)."""
    img_path = tmp_path / "meal.jpg"
    img_path.write_bytes(b"same meal bytes")

    assert upload_photo.queue_photo(str(img_path)) is True
    assert upload_photo.queue_photo(str(img_path)) is True

    assert len([p for p in mock_queue_dir.iterdir() if p.is_file()]) == 1


def test_captured_at_survives_hash_prefixed_queue_name():
    """The drain derives captured_at from the queued filename; the <md5-12>__
    prefix must not break the timestamp regex — including the worst case of
    an all-digit hash prefix before a name that starts with the date."""
    assert upload_photo._captured_at_from_filename(
        "a3f9c2d81b04__IMG_20260715_193042.jpg"
    ) == "2026-07-15 19:30:42"
    assert upload_photo._captured_at_from_filename(
        "123456789012__20260715_193042.jpg"
    ) == "2026-07-15 19:30:42"


def test_process_queue_honors_batch_limit(mock_queue_dir, monkeypatch):
    mock_queue_dir.mkdir(exist_ok=True)
    for index in range(3):
        (mock_queue_dir / f"queued_{index}.jpg").write_bytes(f"data{index}".encode())

    uploaded = []
    monkeypatch.setattr(upload_photo, "_upload_photo_status", lambda path: uploaded.append(path) or (200, True))

    upload_photo.process_queue(max_items=2)

    assert len(uploaded) == 2
    assert len([item for item in mock_queue_dir.iterdir() if item.suffix == ".jpg"]) == 1


@pytest.mark.parametrize(
    "result",
    [(None, False), (500, True), (413, False)],
    ids=["network-error", "server-500", "proxy-413-non-json"],
)
def test_process_queue_keeps_failed_item_and_stops(result, mock_queue_dir, monkeypatch):
    """Transient failures — including a proxy-shaped 413 whose body is not
    JSON (e.g. nginx client_max_body_size HTML page) — must keep the file
    queued and pause the drain rather than quarantine it."""
    mock_queue_dir.mkdir(exist_ok=True)
    first = mock_queue_dir / "queued_1.jpg"
    second = mock_queue_dir / "queued_2.jpg"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    now = time.time()
    os.utime(first, (now - 100, now - 100))  # first is drained first
    os.utime(second, (now - 50, now - 50))

    calls = []
    monkeypatch.setattr(upload_photo, "_upload_photo_status", lambda path: calls.append(path) or result)

    upload_photo.process_queue(max_items=3)

    assert len(calls) == 1
    assert first.exists()
    assert second.exists()
    assert not (mock_queue_dir / "rejected").exists()


def test_process_queue_quarantines_rejected_photo_and_continues(mock_queue_dir, monkeypatch):
    """An app-origin 413 (JSON body) must quarantine, not wedge the queue."""
    mock_queue_dir.mkdir(exist_ok=True)
    big = mock_queue_dir / "big.jpg"
    ok = mock_queue_dir / "ok.jpg"
    big.write_bytes(b"way too big")
    ok.write_bytes(b"fine")
    now = time.time()
    os.utime(big, (now - 100, now - 100))  # big is drained first
    os.utime(ok, (now - 50, now - 50))

    statuses = {"big.jpg": (413, True), "ok.jpg": (200, True)}
    monkeypatch.setattr(upload_photo, "_upload_photo_status", lambda path: statuses[Path(path).name])

    upload_photo.process_queue(max_items=5)

    assert not big.exists()
    assert (mock_queue_dir / "rejected" / "big.jpg").read_bytes() == b"way too big"
    # The drain continued past the reject and uploaded (then removed) ok.jpg
    assert not ok.exists()


def test_quarantine_does_not_clobber_existing_reject(mock_queue_dir, monkeypatch):
    mock_queue_dir.mkdir(exist_ok=True)
    rejected_dir = mock_queue_dir / "rejected"
    rejected_dir.mkdir()
    (rejected_dir / "dup.jpg").write_bytes(b"old reject")
    (mock_queue_dir / "dup.jpg").write_bytes(b"new reject")

    monkeypatch.setattr(upload_photo, "_upload_photo_status", lambda path: (413, True))
    monkeypatch.setattr(upload_photo.time, "time", lambda: 1234567890)

    upload_photo.process_queue(max_items=1)

    assert (rejected_dir / "dup.jpg").read_bytes() == b"old reject"
    assert (rejected_dir / "dup_1234567890.jpg").read_bytes() == b"new reject"


def test_process_queue_recovers_stale_lock(mock_queue_dir, monkeypatch):
    mock_queue_dir.mkdir(exist_ok=True)
    (mock_queue_dir / "queued.jpg").write_bytes(b"data")
    lock_dir = mock_queue_dir / ".process_lock"
    lock_dir.mkdir()
    monkeypatch.setattr(upload_photo, "QUEUE_LOCK_STALE_SECONDS", 30)
    monkeypatch.setattr(upload_photo.time, "time", lambda: lock_dir.stat().st_mtime + 60)
    uploaded = []
    monkeypatch.setattr(upload_photo, "_upload_photo_status", lambda path: uploaded.append(path) or (200, True))

    upload_photo.process_queue(max_items=1)

    assert len(uploaded) == 1
    assert not lock_dir.exists()


@patch('upload_photo.requests.post')
def test_ping_server_single_probe_sets_url_and_drains(mock_post, mock_queue_dir, mock_env, monkeypatch):
    """The ping IS the probe: one round-trip, then the queue drain."""
    resp = MagicMock()
    resp.status_code = 200
    mock_post.return_value = resp
    drains = []
    monkeypatch.setattr(upload_photo, "process_queue", lambda *a, **k: drains.append(True))

    upload_photo.ping_server()

    assert mock_post.call_count == 1
    assert mock_post.call_args[0][0] == "http://mock-cellular.test/ping"
    assert upload_photo.SERVER_URL == "http://mock-cellular.test"
    assert drains == [True]


@patch('upload_photo.requests.post')
def test_ping_server_falls_back_to_second_url(mock_post, mock_queue_dir, mock_env, monkeypatch):
    def mock_ping(url, **kwargs):
        if url.startswith("http://mock-cellular.test"):
            raise requests.exceptions.ConnectionError("down")
        resp = MagicMock()
        resp.status_code = 200
        return resp

    mock_post.side_effect = mock_ping
    monkeypatch.setattr(upload_photo, "process_queue", lambda *a, **k: None)

    upload_photo.ping_server()

    assert mock_post.call_count == 2
    assert upload_photo.SERVER_URL == "http://mock-wifi.test:5000"


@patch('upload_photo.requests.post')
def test_ping_server_all_down_skips_queue_drain(mock_post, mock_queue_dir, mock_env, monkeypatch):
    mock_post.side_effect = requests.exceptions.ConnectionError("Offline")
    drains = []
    monkeypatch.setattr(upload_photo, "process_queue", lambda *a, **k: drains.append(True))

    upload_photo.ping_server()

    assert mock_post.call_count == len(upload_photo.SERVER_URLS)
    assert drains == []


def _run_sync(mock_post, camera_dir, monkeypatch, missing=None):
    monkeypatch.setattr(upload_photo, "CAMERA_DIR", camera_dir)
    offered = {}
    uploads = []

    def fake_post(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        if url.endswith("/reconcile"):
            offered["hashes"] = list(kwargs["json"]["hashes"])
            resp.json.return_value = {"missing_hashes": list(missing or [])}
        elif url.endswith("/upload"):
            uploads.append(url)
        return resp

    mock_post.side_effect = fake_post
    upload_photo.sync_photos()
    return offered.get("hashes", []), uploads


@patch('upload_photo.requests.post')
def test_sync_offers_heic_photos(mock_post, mock_queue_dir, tmp_path, mock_env, monkeypatch):
    camera = tmp_path / "camera"
    camera.mkdir()
    (camera / "meal.HEIC").write_bytes(b"heic-bytes")

    offered, uploads = _run_sync(mock_post, camera, monkeypatch)

    assert offered == [hashlib.md5(b"heic-bytes").hexdigest()]
    assert uploads == []


@patch('upload_photo.requests.post')
def test_sync_covers_today_and_yesterday_only(mock_post, mock_queue_dir, tmp_path, mock_env, monkeypatch):
    camera = tmp_path / "camera"
    camera.mkdir()
    now = time.time()
    today = camera / "today.jpg"
    yesterday = camera / "yesterday.jpg"
    ancient = camera / "ancient.jpg"
    today.write_bytes(b"today-bytes")
    yesterday.write_bytes(b"yesterday-bytes")
    ancient.write_bytes(b"ancient-bytes")
    os.utime(yesterday, (now - 86400, now - 86400))
    os.utime(ancient, (now - 3 * 86400, now - 3 * 86400))

    offered, uploads = _run_sync(mock_post, camera, monkeypatch)

    assert set(offered) == {
        hashlib.md5(b"today-bytes").hexdigest(),
        hashlib.md5(b"yesterday-bytes").hexdigest(),
    }
    assert uploads == []


@patch('upload_photo.requests.post')
def test_sync_uploads_missing_hashes(mock_post, mock_queue_dir, tmp_path, mock_env, monkeypatch):
    camera = tmp_path / "camera"
    camera.mkdir()
    (camera / "missed.jpg").write_bytes(b"missed-bytes")
    missing = [hashlib.md5(b"missed-bytes").hexdigest()]

    offered, uploads = _run_sync(mock_post, camera, monkeypatch, missing=missing)

    assert offered == missing
    assert len(uploads) == 1


def test_env_int_falls_back_for_bad_values(monkeypatch):
    monkeypatch.setenv("QUEUE_BATCH_LIMIT", "not-an-int")

    assert upload_photo._env_int("QUEUE_BATCH_LIMIT", 10, 1, 100) == 10


def test_queue_batch_limit_defaults_to_10(tmp_path):
    """Heartbeats now arrive every ~15 min instead of 5, so each drain moves
    up to 10 queued photos to keep backlog clearance time comparable."""
    env = dict(os.environ)
    env.pop("QUEUE_BATCH_LIMIT", None)
    env["PYTHONPATH"] = str(ANDROID_DIR)
    # Keep the subprocess import hermetic: no real user config file.
    env["CALORIE_TRACKER_ANDROID_CONFIG"] = str(tmp_path / "no_such_config.json")
    out = subprocess.check_output(
        [sys.executable, "-c", "import upload_photo; print(upload_photo.QUEUE_BATCH_LIMIT)"],
        env=env,
        text=True,
    )
    assert out.strip().splitlines()[-1] == "10"


def test_process_queue_default_drains_full_batch(mock_queue_dir, monkeypatch):
    """process_queue() with no args drains QUEUE_BATCH_LIMIT items, no more."""
    limit = upload_photo.QUEUE_BATCH_LIMIT
    mock_queue_dir.mkdir(exist_ok=True)
    for index in range(limit + 2):
        (mock_queue_dir / f"queued_{index:02d}.jpg").write_bytes(b"x")

    uploaded = []
    monkeypatch.setattr(
        upload_photo, "_upload_photo_status",
        lambda path: uploaded.append(path) or (200, True),
    )

    upload_photo.process_queue()

    assert len(uploaded) == limit
    remaining = [item for item in mock_queue_dir.iterdir() if item.suffix == ".jpg"]
    assert len(remaining) == 2


def test_get_headers_includes_vpn_fields(monkeypatch):
    monkeypatch.setattr(upload_photo, "detect_vpn_status", lambda: (False, "no_vpn_interface"))
    monkeypatch.setattr(upload_photo, "API_KEY", "test-upload-key")

    headers = upload_photo.get_headers()

    assert headers["X-VPN-Active"] == "false"
    assert headers["X-VPN-Check"] == "no_vpn_interface"
    assert headers["X-VPN-Check-Reliable"] == "false"


def test_get_headers_requires_api_key(monkeypatch):
    monkeypatch.setattr(upload_photo, "API_KEY", "")

    with pytest.raises(RuntimeError):
        upload_photo.get_headers()


def test_get_status_payload_includes_timezone_and_vpn(monkeypatch):
    monkeypatch.setattr(upload_photo, "detect_vpn_status", lambda: (True, "tun0"))
    monkeypatch.setattr(upload_photo.time, "strftime", lambda fmt, *args: "+0800" if fmt == "%z" else "00:00:00")

    payload = upload_photo.get_status_payload()

    assert payload["timezone"] == "+0800"
    assert payload["vpn_active"] is True
    assert payload["vpn_check"] == "tun0"
    assert payload["vpn_check_reliable"] is True


def test_vpn_detection_cached_across_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(
        upload_photo, "detect_vpn_status",
        lambda: calls.append(1) or (False, "no_vpn_interface"),
    )
    monkeypatch.setattr(upload_photo, "API_KEY", "test-upload-key")

    upload_photo.get_status_payload()
    upload_photo.get_headers()
    upload_photo.get_status_payload()

    assert len(calls) == 1


def test_vpn_cache_expires_after_ttl(monkeypatch):
    calls = []
    monkeypatch.setattr(
        upload_photo, "detect_vpn_status",
        lambda: calls.append(1) or (False, "no_vpn_interface"),
    )

    upload_photo.get_status_payload()
    assert len(calls) == 1

    # Age the cache entry past the TTL; the next call must re-detect.
    upload_photo._VPN_CACHE["checked_at"] -= upload_photo._VPN_CACHE_TTL_SECONDS + 1
    upload_photo.get_status_payload()
    assert len(calls) == 2


# --- _upload_photo_status tuple contract: (status, body_is_json) ---

def _make_upload_response(status_code, text, json_value=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    if json_value is None:
        resp.json.side_effect = ValueError("not JSON")
    else:
        resp.json.return_value = json_value
    return resp


@patch('upload_photo.requests.post')
def test_upload_status_app_reject_reports_json_body(mock_post, mock_env, tmp_path):
    mock_post.return_value = _make_upload_response(
        413, '{"error": "too large"}', json_value={"error": "too large"}
    )
    img = tmp_path / "big.jpg"
    img.write_bytes(b"big")

    assert upload_photo._upload_photo_status(str(img)) == (413, True)


@patch('upload_photo.requests.post')
def test_upload_status_proxy_reject_reports_non_json_body(mock_post, mock_env, tmp_path):
    mock_post.return_value = _make_upload_response(
        413, "<html><h1>413 Request Entity Too Large</h1><hr>nginx</html>"
    )
    img = tmp_path / "big.jpg"
    img.write_bytes(b"big")

    assert upload_photo._upload_photo_status(str(img)) == (413, False)


@patch('upload_photo.requests.post')
def test_upload_status_network_error_returns_none_tuple(mock_post, mock_env, tmp_path):
    mock_post.side_effect = requests.exceptions.ConnectionError("Offline")
    img = tmp_path / "p.jpg"
    img.write_bytes(b"x")

    assert upload_photo._upload_photo_status(str(img)) == (None, False)


def test_process_queue_proxy_413_keeps_file_queued_and_stops(mock_queue_dir, monkeypatch):
    """A port-80 nginx with a default 1MB client_max_body_size 413s every real
    photo with an HTML page: the photo must stay queued, never quarantined."""
    mock_queue_dir.mkdir(exist_ok=True)
    photo = mock_queue_dir / "real_meal.jpg"
    photo.write_bytes(b"real meal bytes")

    monkeypatch.setattr(upload_photo, "_upload_photo_status", lambda path: (413, False))

    upload_photo.process_queue(max_items=5)

    assert photo.read_bytes() == b"real meal bytes"
    assert not (mock_queue_dir / "rejected").exists()


# --- main(): fallback probe, queue-on-failure, exit codes ---

def _stub_upload_results(monkeypatch, results):
    """Stub _upload_photo_status with a scripted result sequence."""
    calls = []
    remaining = list(results)

    def fake_upload(path):
        calls.append(path)
        return remaining.pop(0)

    monkeypatch.setattr(upload_photo, "_upload_photo_status", fake_upload)
    return calls


def _stub_refresh(monkeypatch, new_url):
    probes = []

    def fake_refresh():
        probes.append(True)
        upload_photo.SERVER_URL = new_url
        return new_url

    monkeypatch.setattr(upload_photo, "refresh_server_url", fake_refresh)
    return probes


def test_main_http_failure_retries_when_probe_moves_url(mock_queue_dir, mock_env, monkeypatch):
    """A foreign port-80 listener answering 404 must trigger a re-probe, and
    the upload is retried because the probe landed on a different URL."""
    calls = _stub_upload_results(monkeypatch, [(404, False), (200, True)])
    probes = _stub_refresh(monkeypatch, "http://mock-wifi.test:5000")
    drains = []
    monkeypatch.setattr(upload_photo, "process_queue", lambda *a, **k: drains.append(k))

    rc = upload_photo.main(["upload_photo.py", "/fake/photo.jpg"])

    assert rc == 0
    assert len(calls) == 2
    assert probes == [True]
    assert drains == [{"max_items": 1}]


def test_main_http_failure_skips_retry_when_probe_stays_put(mock_queue_dir, mock_env, monkeypatch):
    """404 + re-probe landing on the SAME URL: no pointless retry, photo queued."""
    calls = _stub_upload_results(monkeypatch, [(404, False)])
    probes = _stub_refresh(monkeypatch, upload_photo.SERVER_URL)
    queued = []
    monkeypatch.setattr(upload_photo, "queue_photo", lambda path: queued.append(path) or True)

    rc = upload_photo.main(["upload_photo.py", "/fake/photo.jpg"])

    assert rc == 0
    assert len(calls) == 1
    assert probes == [True]
    assert queued == ["/fake/photo.jpg"]


def test_main_network_failure_retries_even_on_same_url(mock_queue_dir, mock_env, monkeypatch):
    """status None means we never reached anything: retry after the probe even
    when it did not move the URL."""
    calls = _stub_upload_results(monkeypatch, [(None, False), (200, True)])
    _stub_refresh(monkeypatch, upload_photo.SERVER_URL)
    monkeypatch.setattr(upload_photo, "process_queue", lambda *a, **k: None)

    rc = upload_photo.main(["upload_photo.py", "/fake/photo.jpg"])

    assert rc == 0
    assert len(calls) == 2


def test_main_success_skips_probe_and_drains_one(mock_queue_dir, mock_env, monkeypatch):
    calls = _stub_upload_results(monkeypatch, [(200, True)])
    probes = _stub_refresh(monkeypatch, "http://mock-wifi.test:5000")
    drains = []
    monkeypatch.setattr(upload_photo, "process_queue", lambda *a, **k: drains.append(k))

    rc = upload_photo.main(["upload_photo.py", "/fake/photo.jpg"])

    assert rc == 0
    assert len(calls) == 1
    assert probes == []
    assert drains == [{"max_items": 1}]


def test_main_exits_1_when_photo_neither_uploaded_nor_queued(mock_queue_dir, mock_env, monkeypatch):
    """The watcher's retry/give-up contract depends on exit 1 here: photo not
    uploaded AND queueing failed means the photo would otherwise be lost."""
    _stub_upload_results(monkeypatch, [(500, True)])
    _stub_refresh(monkeypatch, upload_photo.SERVER_URL)
    monkeypatch.setattr(upload_photo, "queue_photo", lambda path: False)

    rc = upload_photo.main(["upload_photo.py", "/fake/photo.jpg"])

    assert rc == 1


def test_main_exits_0_when_photo_queued_after_failure(mock_queue_dir, mock_env, monkeypatch):
    _stub_upload_results(monkeypatch, [(500, True)])
    _stub_refresh(monkeypatch, upload_photo.SERVER_URL)
    monkeypatch.setattr(upload_photo, "queue_photo", lambda path: True)

    rc = upload_photo.main(["upload_photo.py", "/fake/photo.jpg"])

    assert rc == 0


def test_main_usage_error_exits_1():
    assert upload_photo.main(["upload_photo.py"]) == 1


# --- CAMERA_DIR env fallback (evaluated at import time -> subprocess) ---

def _camera_dir_with_env(tmp_path, overrides):
    env = dict(os.environ)
    env.pop("CALORIE_CAMERA_DIR", None)
    env.pop("CAMERA_DIR", None)
    env.update(overrides)
    env["PYTHONPATH"] = str(ANDROID_DIR)
    # Keep the subprocess import hermetic: no real user config file.
    env["CALORIE_TRACKER_ANDROID_CONFIG"] = str(tmp_path / "no_such_config.json")
    out = subprocess.check_output(
        [sys.executable, "-c", "import upload_photo; print(upload_photo.CAMERA_DIR)"],
        env=env,
        text=True,
    )
    return out.strip().splitlines()[-1]


def test_camera_dir_falls_back_to_watcher_env_var(tmp_path):
    assert _camera_dir_with_env(tmp_path, {"CAMERA_DIR": "/custom/from/watcher"}) == "/custom/from/watcher"


def test_camera_dir_prefers_calorie_specific_env_var(tmp_path):
    assert _camera_dir_with_env(
        tmp_path,
        {"CALORIE_CAMERA_DIR": "/calorie/dir", "CAMERA_DIR": "/watcher/dir"},
    ) == "/calorie/dir"


def test_camera_dir_default_when_no_env(tmp_path):
    assert _camera_dir_with_env(tmp_path, {}) == "/storage/emulated/0/DCIM/Camera"


@patch('upload_photo.requests.post')
def test_full_offline_to_online_lifecycle(mock_post, mock_queue_dir, mock_env, tmp_path):
    """Scenario: photo taken while the server is unreachable -> queued with
    exit 0; server comes back -> the next heartbeat drains the queue and the
    photo is uploaded exactly once."""
    img_path = tmp_path / "meal.jpg"
    img_path.write_bytes(b"offline meal bytes")

    server_up = {"value": False}
    successful_uploads = []

    def fake_post(url, **kwargs):
        if not server_up["value"]:
            raise requests.exceptions.ConnectionError("server down")
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"status": "processing_in_background"}
        if url.endswith("/upload"):
            successful_uploads.append(url)
        return resp

    mock_post.side_effect = fake_post

    # Phase 1: server down. CLI exits 0 (photo safely queued, not lost).
    assert upload_photo.main(["upload_photo.py", str(img_path)]) == 0
    queued = [p for p in upload_photo.QUEUE_DIR.iterdir() if p.is_file()]
    assert len(queued) == 1
    assert successful_uploads == []

    # Phase 2: server returns. The heartbeat probes, then drains the queue.
    server_up["value"] = True
    upload_photo.ping_server()

    assert [p for p in upload_photo.QUEUE_DIR.iterdir() if p.is_file()] == []
    assert len(successful_uploads) == 1


def _noise_jpeg(width=2400, height=1800, seed=42, orientation=None):
    """Deterministic high-entropy JPEG that genuinely shrinks when downscaled."""
    import io as _io
    import random as _random

    from PIL import Image

    rng = _random.Random(seed)
    img = Image.frombytes("RGB", (width, height), rng.randbytes(width * height * 3))
    out = _io.BytesIO()
    save_kwargs = {"format": "JPEG", "quality": 92}
    if orientation is not None:
        exif = Image.Exif()
        exif[0x0112] = orientation
        save_kwargs["exif"] = exif
    img.save(out, **save_kwargs)
    return out.getvalue()


def test_recompress_shrinks_large_jpeg():
    import io as _io

    from PIL import Image

    original = _noise_jpeg()
    result = upload_photo._recompress_for_upload(original)

    assert result is not None
    assert len(result) < len(original)
    decoded = Image.open(_io.BytesIO(result))
    assert max(decoded.size) <= upload_photo.RECOMPRESS_MAX_EDGE
    assert decoded.mode == "RGB"


def test_recompress_falls_back_safely(monkeypatch):
    # Non-image bytes: decode fails, original kept.
    assert upload_photo._recompress_for_upload(b"not an image at all") is None
    # Feature disabled via env knob.
    monkeypatch.setattr(upload_photo, "RECOMPRESS_MAX_EDGE", 0)
    assert upload_photo._recompress_for_upload(_noise_jpeg(400, 300)) is None


def test_recompress_honors_exif_orientation():
    """The transpose now runs AFTER the downscale (on the small image, where
    the EXIF tag survives on the Image object); the output must still come
    out with swapped dimensions for a rotated orientation."""
    import io as _io

    from PIL import Image

    original = _noise_jpeg(800, 600, orientation=6)
    result = upload_photo._recompress_for_upload(original)

    assert result is not None
    decoded = Image.open(_io.BytesIO(result))
    # Orientation 6 (90° rotation) swaps width and height.
    assert decoded.size == (600, 800)


def test_recompress_large_jpeg_draft_decode_respects_max_edge():
    """A JPEG more than 2x the target edge takes the draft() reduced-scale
    decode path; the result must still honor RECOMPRESS_MAX_EDGE."""
    import io as _io

    from PIL import Image

    original = _noise_jpeg(3600, 2400)
    result = upload_photo._recompress_for_upload(original)

    assert result is not None
    assert len(result) < len(original)
    decoded = Image.open(_io.BytesIO(result))
    assert max(decoded.size) <= upload_photo.RECOMPRESS_MAX_EDGE
    assert decoded.mode == "RGB"


def test_recompress_defers_over_budget_images_to_original(monkeypatch):
    """Past the decode pixel budget (checked on the header, before any pixel
    data is decoded), recompression steps aside so the original bytes upload
    instead of exhausting phone memory."""
    original = _noise_jpeg(400, 300)
    monkeypatch.setattr(upload_photo, "RECOMPRESS_MAX_PIXELS", 400 * 300 - 1)

    assert upload_photo._recompress_for_upload(original) is None


@patch('upload_photo.requests.post')
def test_upload_sends_original_hash_with_recompressed_bytes(mock_post, mock_env, tmp_path):
    original = _noise_jpeg()
    img_path = tmp_path / "meal.heic"  # name keeps original ext; upload becomes .jpg
    img_path.write_bytes(original)
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"status": "processing_in_background"}
    mock_post.return_value = resp

    status, body_is_json = upload_photo._upload_photo_status(str(img_path))

    assert (status, body_is_json) == (200, True)
    kwargs = mock_post.call_args.kwargs
    assert kwargs["data"] == {"original_hash": hashlib.md5(original).hexdigest()}
    upload_name, upload_bytes = kwargs["files"]["photo"]
    assert upload_name == "meal.jpg"
    assert len(upload_bytes) < len(original)


@patch('upload_photo.requests.post')
def test_upload_falls_back_to_original_bytes(mock_post, mock_env, tmp_path, monkeypatch):
    monkeypatch.setattr(upload_photo, "_recompress_for_upload", lambda original_bytes: None)
    original = b"undecodable original bytes"
    img_path = tmp_path / "meal.jpg"
    img_path.write_bytes(original)
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"status": "processing_in_background"}
    mock_post.return_value = resp

    status, _ = upload_photo._upload_photo_status(str(img_path))

    assert status == 200
    kwargs = mock_post.call_args.kwargs
    assert kwargs["files"]["photo"] == ("meal.jpg", original)
    assert kwargs["data"] == {"original_hash": hashlib.md5(original).hexdigest()}


@patch('upload_photo.requests.post')
def test_queue_drain_unlinks_on_duplicate_reply(mock_post, mock_queue_dir, mock_env):
    """A 200 'duplicate' reply removes the queued copy. Safe only because the
    server releases orphaned in-flight reservations at boot — pinned here
    because this is the data-loss edge if that guarantee ever regresses."""
    upload_photo.QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    queued = upload_photo.QUEUE_DIR / "dup_meal.jpg"
    queued.write_bytes(b"already logged meal")

    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"status": "duplicate"}
    mock_post.return_value = resp

    upload_photo.process_queue()

    assert not queued.exists()


@patch('upload_photo.requests.post')
def test_repeated_failed_syncs_do_not_duplicate_queue_copies(
        mock_post, mock_queue_dir, tmp_path, mock_env, monkeypatch):
    camera = tmp_path / "camera"
    camera.mkdir()
    (camera / "meal.jpg").write_bytes(b"meal-bytes")
    monkeypatch.setattr(upload_photo, "CAMERA_DIR", camera)
    missing = hashlib.md5(b"meal-bytes").hexdigest()

    def fake_post(url, **kwargs):
        resp = MagicMock()
        if url.endswith("/ping"):
            resp.status_code = 200
        elif url.endswith("/reconcile"):
            resp.status_code = 200
            resp.json.return_value = {"missing_hashes": [missing]}
        else:  # /upload: the server is persistently erroring
            resp.status_code = 500
            resp.text = "internal error"
        return resp

    mock_post.side_effect = fake_post

    upload_photo.sync_photos()   # outage day 1: queues meal.jpg
    upload_photo.sync_photos()   # outage day 2: must NOT queue a second copy

    queued = [p for p in upload_photo.QUEUE_DIR.iterdir() if p.is_file()]
    assert len(queued) == 1, (
        f"one camera photo produced {len(queued)} queued copies across two "
        f"failed syncs: {[p.name for p in queued]}"
    )
