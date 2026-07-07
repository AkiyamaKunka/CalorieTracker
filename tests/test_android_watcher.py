"""Shell-driven tests for android/android_watcher.sh.

The real watcher runs under bash with a temp HOME, a temp camera dir (via
the CAMERA_DIR override), a fake /proc (via CALORIE_PROC_DIR) and PATH shims
for python3, termux-wake-lock and sleep so the polling loops run in
milliseconds. One session covers the whole lifecycle to keep the suite fast.
"""

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

BASH = shutil.which("bash")
REPO_ROOT = Path(__file__).resolve().parent.parent
WATCHER = REPO_ROOT / "android" / "android_watcher.sh"

pytestmark = pytest.mark.skipif(BASH is None, reason="bash is not available")

# Upload exit code is read from a file so one watcher session can exercise
# both the success path and the retry/give-up path.
PYTHON3_SHIM = """#!/bin/sh
case "$*" in
  *--ping*) echo "PING" >> "$HOME/python_calls.log" ;;
  *--sync*) echo "SYNC" >> "$HOME/python_calls.log" ;;
  *)
    echo "UPLOAD $2" >> "$HOME/python_calls.log"
    exit "$(cat "$HOME/upload_exit" 2>/dev/null || echo 0)"
    ;;
esac
exit 0
"""

# Shrink every sleep in the watcher so polling/stability waits stay fast.
SLEEP_SHIM = "#!/bin/sh\nexec /bin/sleep 0.05\n"

WAKE_LOCK_SHIM = "#!/bin/sh\nexit 0\n"


def _write_shim(path, body):
    path.write_text(body)
    path.chmod(0o755)


def _make_env(tmp_path):
    home = tmp_path / "home"
    camera = tmp_path / "camera"
    bin_dir = tmp_path / "bin"
    fake_proc = tmp_path / "proc"
    for directory in (home, camera, bin_dir, fake_proc):
        directory.mkdir()
    _write_shim(bin_dir / "python3", PYTHON3_SHIM)
    _write_shim(bin_dir / "sleep", SLEEP_SHIM)
    _write_shim(bin_dir / "termux-wake-lock", WAKE_LOCK_SHIM)
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    env["CAMERA_DIR"] = str(camera)
    env["CALORIE_PROC_DIR"] = str(fake_proc)
    return env, home, camera, fake_proc


def _wait_for(predicate, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.03)
    return False


def _file_contains(path, needle):
    # sort -u -o briefly replaces the history file; tolerate the window.
    try:
        return needle in path.read_text()
    except OSError:
        return False


def test_watcher_end_to_end_lifecycle(tmp_path):
    env, home, camera, fake_proc = _make_env(tmp_path)
    log = home / "watcher.log"
    calls = home / "python_calls.log"
    history = home / "uploaded_files.log"

    # Pre-seed a >1MB log: startup must rotate it to watcher.log.1 before the
    # banner, and keep logging into a fresh watcher.log.
    log.write_bytes(b"OLDLOG line from a previous long run\n" * 30000)

    # Stale state from a SIGKILL'd previous run: lock dir present, PID file
    # pointing at a live-but-unrelated process (no /proc cmdline entry).
    decoy = subprocess.Popen(["/bin/sleep", "30"])
    (home / ".calorie_watcher.pid").write_text(str(decoy.pid))
    (home / ".calorie_watcher.lock").mkdir()

    # True backlog (old mtime): seeded at startup, never uploaded.
    existing = camera / "existing.jpg"
    existing.write_bytes(b"seeded before startup")
    two_hours_ago = time.time() - 2 * 3600
    os.utime(existing, (two_hours_ago, two_hours_ago))

    # Fresh photo taken just before the watcher starts (mtime now): must be
    # uploaded by the first polls, not seeded away until the 11 PM sync.
    fresh_before_start = camera / "fresh_before_start.jpg"
    fresh_before_start.write_bytes(b"meal snapped right before startup")

    proc = subprocess.Popen(
        [BASH, str(WATCHER)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        # Stale PID/lock must not block startup (finding 10)
        assert _wait_for(lambda: _file_contains(log, "Started polling loop"))
        assert (home / ".calorie_watcher.pid").read_text().strip() == str(proc.pid)

        # --- log rotation: the >1MB pre-seeded log moved to watcher.log.1
        # at startup and the watcher keeps logging into a fresh file ---
        rotated = home / "watcher.log.1"
        assert rotated.exists()
        assert rotated.stat().st_size > 1_048_576
        assert "OLDLOG" not in log.read_text()

        # --- success path: new photo uploaded, then recorded in history ---
        new_photo = camera / "new_meal.jpg"
        (camera / ".pending-123.jpg").write_bytes(b"mediastore partial")
        new_photo.write_bytes(b"fresh photo bytes")

        assert _wait_for(lambda: _file_contains(calls, f"UPLOAD {new_photo}"))
        assert _wait_for(lambda: _file_contains(history, str(new_photo)))
        # Fresh pre-start photo is uploaded (scenario C), old backlog is not.
        assert _wait_for(lambda: _file_contains(calls, f"UPLOAD {fresh_before_start}"))
        text = calls.read_text()
        assert "existing.jpg" not in text  # old backlog seeded at startup, never uploaded
        assert ".pending-123" not in text  # in-progress MediaStore names excluded
        assert _file_contains(history, str(camera / "existing.jpg"))

        # --- FIX 5: a 0-byte file that never stabilizes is skipped, not
        # uploaded, and stays out of history so the next poll retries it ---
        hollow = camera / "hollow.jpg"
        hollow.write_bytes(b"")

        assert _wait_for(lambda: _file_contains(log, f"Skipping {hollow}"))
        assert f"UPLOAD {hollow}" not in calls.read_text()
        assert not _file_contains(history, str(hollow))

        # Once the camera finishes writing it, the retry poll uploads it.
        hollow.write_bytes(b"now the photo has real bytes")
        assert _wait_for(lambda: _file_contains(calls, f"UPLOAD {hollow}"))
        assert _wait_for(lambda: _file_contains(history, str(hollow)))

        # --- failure path: retries, then gives up loudly (finding 8) ---
        (home / "upload_exit").write_text("1")
        cursed = camera / "cursed.jpg"
        cursed.write_bytes(b"upload always fails")

        assert _wait_for(lambda: _file_contains(log, "GIVING UP"))
        # The history append happens just after the GIVING UP log line.
        assert _wait_for(lambda: _file_contains(history, str(cursed)))
        assert calls.read_text().count(f"UPLOAD {cursed}") == 3
        time.sleep(0.25)  # no hot retry loop after giving up
        assert calls.read_text().count(f"UPLOAD {cursed}") == 3

        # --- mtime-gated polling: after a quiet stretch (well past the
        # 12-tick safety-net cycle, so the watcher has settled into the
        # skip-to-sleep path) a brand-new photo is still detected ---
        (home / "upload_exit").write_text("0")
        time.sleep(0.9)  # > 12 shimmed ticks with no camera-dir changes
        quiet = camera / "quiet_after_idle.jpg"
        quiet.write_bytes(b"photo taken after a long quiet period")
        assert _wait_for(lambda: _file_contains(calls, f"UPLOAD {quiet}"))
        assert _wait_for(lambda: _file_contains(history, str(quiet)))

        # --- mtime-gated polling: a failed upload is retried even though
        # the camera-dir mtime does not change between attempts (the fail
        # counter forces a rescan) ---
        (home / "upload_exit").write_text("1")
        flaky = camera / "flaky_retry.jpg"
        flaky.write_bytes(b"first attempts fail, then the upload works")
        assert _wait_for(lambda: _file_contains(log, f"Upload attempt 1 failed for {flaky}"))
        (home / "upload_exit").write_text("0")
        assert _wait_for(lambda: _file_contains(history, str(flaky)))
        assert f"UPLOAD {flaky}" in calls.read_text()
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        decoy.kill()
        decoy.wait()

    # Lock/PID cleanup on exit
    assert not (home / ".calorie_watcher.pid").exists()
    assert not (home / ".calorie_watcher.lock").exists()

    # The ping/sync background loops must not be orphaned (finding 11):
    # once the watcher is dead, the calls log must stop growing.
    time.sleep(0.3)
    size_after_exit = calls.stat().st_size
    time.sleep(0.35)
    assert calls.stat().st_size == size_after_exit


def test_housekeeping_prunes_deleted_photos_and_stale_fail_counters(tmp_path):
    """Every CALORIE_HOUSEKEEP_POLLS iterations (3 here) the watcher prunes
    history entries whose photos are gone from disk and deletes week-old
    fail counters, while keeping entries for photos that still exist."""
    env, home, camera, fake_proc = _make_env(tmp_path)
    env["CALORIE_HOUSEKEEP_POLLS"] = "3"
    log = home / "watcher.log"
    history = home / "uploaded_files.log"

    # A real photo, old enough to be seeded into history (not uploaded).
    keeper = camera / "keeper.jpg"
    keeper.write_bytes(b"still on disk")
    two_hours_ago = time.time() - 2 * 3600
    os.utime(keeper, (two_hours_ago, two_hours_ago))

    # History entry for a photo that was deleted from the camera roll.
    ghost = camera / "ghost.jpg"
    history.write_text(f"{ghost}\n")

    # A week-old fail counter left behind by some long-gone photo. Its
    # presence also forces a full scan every tick, so housekeeping triggers
    # quickly under the shimmed sleeps.
    fail_dir = home / ".calorie_upload_failures"
    fail_dir.mkdir()
    stale_counter = fail_dir / "stale_counter"
    stale_counter.write_text("2")
    eight_days_ago = time.time() - 8 * 86400
    os.utime(stale_counter, (eight_days_ago, eight_days_ago))

    proc = subprocess.Popen(
        [BASH, str(WATCHER)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert _wait_for(lambda: _file_contains(log, "Started polling loop"))
        # Housekeeping pruned the deleted photo's entry and the stale counter.
        assert _wait_for(lambda: not _file_contains(history, str(ghost)))
        assert _wait_for(lambda: not stale_counter.exists())
        # The photo still on disk keeps its history entry (no re-upload).
        assert _file_contains(history, str(keeper))
        calls = home / "python_calls.log"
        if calls.exists():
            assert f"UPLOAD {keeper}" not in calls.read_text()
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_live_watcher_blocks_second_instance(tmp_path):
    env, home, camera, fake_proc = _make_env(tmp_path)

    decoy = subprocess.Popen(["/bin/sleep", "30"])
    try:
        # Fake /proc entry that says the decoy PID really is a watcher.
        cmdline_dir = fake_proc / str(decoy.pid)
        cmdline_dir.mkdir()
        (cmdline_dir / "cmdline").write_bytes(b"bash\x00/data/home/android_watcher.sh\x00")
        (home / ".calorie_watcher.pid").write_text(str(decoy.pid))
        (home / ".calorie_watcher.lock").mkdir()

        result = subprocess.run(
            [BASH, str(WATCHER)],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert result.returncode == 0
        assert "already running" in (home / "watcher.log").read_text()
        # The live holder's PID file and lock are left untouched.
        assert (home / ".calorie_watcher.pid").read_text().strip() == str(decoy.pid)
        assert (home / ".calorie_watcher.lock").is_dir()
    finally:
        decoy.kill()
        decoy.wait()
