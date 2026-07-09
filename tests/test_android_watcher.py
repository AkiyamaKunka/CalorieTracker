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
# both the success path and the retry/give-up path. Creating $HOME/upload_block
# makes every upload hang (real sleep, not the shimmed one) long enough for
# the stop shortcut's grace window to expire and force the kill -9 escalation.
PYTHON3_SHIM = """#!/bin/sh
case "$*" in
  *--ping*) echo "PING" >> "$HOME/python_calls.log" ;;
  *--sync*) echo "SYNC" >> "$HOME/python_calls.log" ;;
  *)
    echo "UPLOAD $2" >> "$HOME/python_calls.log"
    [ -f "$HOME/upload_block" ] && /bin/sleep 15
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

        # --- unstable-file cap: a file that stays 0-byte past the cap is
        # given up on loudly, lands in history (so it stops forcing rescans
        # and stability stalls) and leaves no counter file behind ---
        void = camera / "void.jpg"
        void.write_bytes(b"")
        assert _wait_for(lambda: _file_contains(log, f"GIVING UP on {void}"), timeout=30)
        assert _wait_for(lambda: _file_contains(history, str(void)))
        assert f"UPLOAD {void}" not in calls.read_text()
        fail_dir = home / ".calorie_upload_failures"
        assert _wait_for(lambda: not any(fail_dir.iterdir()))
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


def test_scan_prunes_fail_counters_for_deleted_photos(tmp_path):
    """A fail counter (plain or .unstable) whose photo was deleted used to
    survive until the 7-day housekeeping expiry and force a full rescan every
    tick the whole time; now any scan prunes counters whose source path is no
    longer present, while counters for photos still on disk are kept."""
    env, home, camera, fake_proc = _make_env(tmp_path)
    log = home / "watcher.log"

    def counter_key(path):
        return subprocess.check_output(
            [BASH, "-c", r'printf %s "$1" | cksum | tr -s " \t" _', "_", str(path)],
            text=True,
        ).strip()

    # Old photos: seeded into history at startup, never uploaded.
    two_hours_ago = time.time() - 2 * 3600
    kept = camera / "kept.jpg"
    doomed = camera / "doomed.jpg"
    for photo in (kept, doomed):
        photo.write_bytes(b"photo bytes")
        os.utime(photo, (two_hours_ago, two_hours_ago))

    fail_dir = home / ".calorie_upload_failures"
    fail_dir.mkdir()
    kept_counter = fail_dir / counter_key(kept)
    doomed_counter = fail_dir / counter_key(doomed)
    doomed_unstable = fail_dir / (counter_key(doomed) + ".unstable")
    kept_counter.write_text("1")
    doomed_counter.write_text("1")
    doomed_unstable.write_text("2")

    proc = subprocess.Popen(
        [BASH, str(WATCHER)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert _wait_for(lambda: _file_contains(log, "Started polling loop"))
        # While both photos exist, several scans keep every counter.
        time.sleep(0.5)
        assert kept_counter.exists()
        assert doomed_counter.exists()
        assert doomed_unstable.exists()

        # Deleting the photo orphans its counters; the next scan prunes both
        # forms but leaves the surviving photo's counter alone.
        doomed.unlink()
        assert _wait_for(lambda: not doomed_counter.exists())
        assert _wait_for(lambda: not doomed_unstable.exists())
        assert kept_counter.exists()
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_widget_shortcuts_start_status_stop(tmp_path):
    """The Termux:Widget one-tap scripts drive the real watcher: start brings
    it up (and reports the PID), status reflects running/stopped, and stop
    terminates it gracefully so the trap cleans PID/lock state."""
    env, home, camera, fake_proc = _make_env(tmp_path)
    shortcuts = REPO_ROOT / "android" / "shortcuts"
    # The widget scripts expect the watcher at ~/android_watcher.sh.
    shutil.copy(WATCHER, home / "android_watcher.sh")

    def run_shortcut(name):
        return subprocess.run(
            [BASH, str(shortcuts / name)], env=env,
            capture_output=True, text=True, timeout=15,
        )

    status = run_shortcut("calorie-status.sh")
    assert "🔴 Watcher stopped" in status.stdout

    start = run_shortcut("calorie-start.sh")
    assert start.returncode == 0
    assert "🟢 Watcher running (PID" in start.stdout
    pid = int((home / ".calorie_watcher.pid").read_text().strip())

    try:
        status = run_shortcut("calorie-status.sh")
        assert "🟢 Watcher running" in status.stdout

        # On Android /proc is real; mirror that in the fake proc dir so the
        # liveness check can see the first watcher (otherwise a second start
        # would wrongly treat the lock as stale).
        cmdline_dir = fake_proc / str(pid)
        cmdline_dir.mkdir()
        (cmdline_dir / "cmdline").write_bytes(
            f"bash\x00{home}/android_watcher.sh\x00".encode()
        )

        # Double-tap on start is a no-op: same watcher instance keeps the lock.
        again = run_shortcut("calorie-start.sh")
        assert again.returncode == 0
        assert int((home / ".calorie_watcher.pid").read_text().strip()) == pid

        stop = run_shortcut("calorie-stop.sh")
        assert stop.returncode == 0
        assert "Watcher stopped" in stop.stdout or "force-killed" in stop.stdout
        assert _wait_for(lambda: not _pid_alive(pid))
        assert not (home / ".calorie_watcher.pid").exists()
        assert not (home / ".calorie_watcher.lock").exists()

        status = run_shortcut("calorie-status.sh")
        assert "🔴 Watcher stopped" in status.stdout

        # Stop when nothing runs is informative, not an error.
        idle_stop = run_shortcut("calorie-stop.sh")
        assert idle_stop.returncode == 0
        assert "not running" in idle_stop.stdout

        # --- PID reuse: a live PID whose cmdline is not the watcher must be
        # reported as stopped by status, and stop must refuse to kill it ---
        decoy = subprocess.Popen(["/bin/sleep", "30"])
        try:
            (home / ".calorie_watcher.pid").write_text(str(decoy.pid))
            (home / ".calorie_watcher.lock").mkdir()
            decoy_proc = fake_proc / str(decoy.pid)
            decoy_proc.mkdir()
            (decoy_proc / "cmdline").write_bytes(b"/bin/sleep\x0030\x00")

            status = run_shortcut("calorie-status.sh")
            assert "🔴 Watcher stopped" in status.stdout

            reuse_stop = run_shortcut("calorie-stop.sh")
            assert reuse_stop.returncode == 0
            assert "refusing to kill" in reuse_stop.stdout
            assert _pid_alive(decoy.pid)  # the innocent process survived
            # The stale state was still cleared so a fresh start can work.
            assert not (home / ".calorie_watcher.pid").exists()
            assert not (home / ".calorie_watcher.lock").exists()
        finally:
            decoy.kill()
            decoy.wait()
    finally:
        # Belt and braces: reap anything still holding this test's temp HOME.
        subprocess.run(["pkill", "-9", "-f", str(home)], capture_output=True)


def _watcher_processes_alive(home):
    """True while any process under this test HOME (watcher, its ping/sync
    subshells, or an uploader invocation) is still running."""
    out = subprocess.run(["ps", "-ef"], capture_output=True, text=True).stdout
    return any(
        f"{home}/android_watcher.sh" in line or f"{home}/upload_photo.py" in line
        for line in out.splitlines()
    )


def test_widget_stop_escalates_and_reaps_orphaned_loops(tmp_path):
    """bash defers the TERM trap while a foreground upload runs, so a wedged
    upload forces calorie-stop.sh past its grace window into kill -9 — which
    bypasses the watcher's cleanup trap. The stop shortcut must then reap the
    orphaned ping/sync subshells (and the stuck uploader) instead of leaving
    a 'stopped' watcher heartbeating forever."""
    env, home, camera, fake_proc = _make_env(tmp_path)
    shortcuts = REPO_ROOT / "android" / "shortcuts"
    shutil.copy(WATCHER, home / "android_watcher.sh")
    calls = home / "python_calls.log"

    # Every upload now hangs far longer than stop's whole grace window.
    (home / "upload_block").write_text("1")

    def run_shortcut(name):
        return subprocess.run(
            [BASH, str(shortcuts / name)], env=env,
            capture_output=True, text=True, timeout=30,
        )

    start = run_shortcut("calorie-start.sh")
    assert start.returncode == 0
    pid = int((home / ".calorie_watcher.pid").read_text().strip())

    try:
        # Mirror the real /proc so the shortcuts can identify the watcher.
        cmdline_dir = fake_proc / str(pid)
        cmdline_dir.mkdir()
        (cmdline_dir / "cmdline").write_bytes(
            f"bash\x00{home}/android_watcher.sh\x00".encode()
        )

        # A new photo wedges the main loop inside the blocking upload.
        photo = camera / "wedged_meal.jpg"
        photo.write_bytes(b"upload blocks for a long time")
        assert _wait_for(lambda: _file_contains(calls, f"UPLOAD {photo}"))

        stop = run_shortcut("calorie-stop.sh")
        assert stop.returncode == 0
        assert "force-killed" in stop.stdout
        assert not (home / ".calorie_watcher.pid").exists()
        assert not (home / ".calorie_watcher.lock").exists()

        # Neither the watcher, its ping/sync subshells, nor the stuck
        # uploader survive the escalation.
        assert _wait_for(lambda: not _pid_alive(pid))
        assert _wait_for(lambda: not _watcher_processes_alive(home))

        # The orphaned ping loop is really gone: the calls log stops growing.
        size_after_stop = calls.stat().st_size
        time.sleep(0.35)
        assert calls.stat().st_size == size_after_stop
    finally:
        # Belt and braces: reap anything still holding this test's temp HOME.
        subprocess.run(["pkill", "-9", "-f", str(home)], capture_output=True)


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


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
