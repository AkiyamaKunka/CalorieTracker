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

    # Stale state from a SIGKILL'd previous run: lock dir present, PID file
    # pointing at a live-but-unrelated process (no /proc cmdline entry).
    decoy = subprocess.Popen(["/bin/sleep", "30"])
    (home / ".calorie_watcher.pid").write_text(str(decoy.pid))
    (home / ".calorie_watcher.lock").mkdir()

    (camera / "existing.jpg").write_bytes(b"seeded before startup")

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

        # --- success path: new photo uploaded, then recorded in history ---
        new_photo = camera / "new_meal.jpg"
        (camera / ".pending-123.jpg").write_bytes(b"mediastore partial")
        new_photo.write_bytes(b"fresh photo bytes")

        assert _wait_for(lambda: _file_contains(calls, f"UPLOAD {new_photo}"))
        assert _wait_for(lambda: _file_contains(history, str(new_photo)))
        text = calls.read_text()
        assert "existing.jpg" not in text  # seeded at startup, never uploaded
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
