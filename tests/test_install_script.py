"""Shell-driven tests for android/install_and_start.sh.

The installer runs under bash with a temp HOME, a temp source dir (via the
CALORIE_INSTALL_SRC override) and PATH shims. The ps shim returns nothing so
the installer's kill sweep can never touch real processes on this machine.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

BASH = shutil.which("bash")
REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALLER = REPO_ROOT / "android" / "install_and_start.sh"

pytestmark = pytest.mark.skipif(BASH is None, reason="bash is not available")

PYTHON3_SHIM = '#!/bin/sh\necho "$@" >> "$HOME/python_calls.log"\nexit 0\n'
NOOP_SHIM = "#!/bin/sh\nexit 0\n"


def _write_shim(path, body):
    path.write_text(body)
    path.chmod(0o755)


def _make_env(tmp_path, with_sources=True):
    home = tmp_path / "home"
    src = tmp_path / "src"
    bin_dir = tmp_path / "bin"
    home.mkdir()
    src.mkdir()
    bin_dir.mkdir()
    _write_shim(bin_dir / "python3", PYTHON3_SHIM)
    _write_shim(bin_dir / "ps", NOOP_SHIM)
    _write_shim(bin_dir / "sleep", NOOP_SHIM)
    _write_shim(bin_dir / "termux-wake-lock", NOOP_SHIM)
    if with_sources:
        (src / "upload_photo.py").write_text("# payload\n")
        (src / "android_watcher.sh").write_text("#!/bin/sh\nexit 0\n")
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    env["CALORIE_INSTALL_SRC"] = str(src)
    return env, home, src


def _run(env):
    return subprocess.run(
        [BASH, str(INSTALLER)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_missing_source_aborts_before_touching_watcher_state(tmp_path):
    env, home, src = _make_env(tmp_path, with_sources=False)
    (src / "upload_photo.py").write_text("# payload\n")  # watcher payload missing
    pid_file = home / ".calorie_watcher.pid"
    pid_file.write_text("12345")

    result = _run(env)

    assert result.returncode == 1
    assert "android_watcher.sh not found" in result.stderr
    # The kill/cleanup section never ran: the running watcher stays in place.
    assert pid_file.read_text() == "12345"
    assert not (home / "android_watcher.sh").exists()


def test_reinstall_preserves_offline_queue_and_clears_stale_lock(tmp_path):
    env, home, src = _make_env(tmp_path)
    (home / ".calorie_tracker_upload.json").write_text(
        '{"ANDROID_API_KEY": "k", "SERVER_URLS": ["http://example.test"]}'
    )
    queue = home / ".offline_queue"
    queue.mkdir()
    (queue / "pending_meal.jpg").write_bytes(b"queued upload")
    (queue / ".process_lock").mkdir()

    result = _run(env)

    assert result.returncode == 0
    # Pending uploads survive the reinstall; only the stale lock is cleared.
    assert (queue / "pending_meal.jpg").read_bytes() == b"queued upload"
    assert not (queue / ".process_lock").exists()
    assert not [p for p in home.iterdir() if p.name.startswith(".offline_queue.backup")]
    # The --ping afterwards is what drains the kept queue.
    assert "--ping" in (home / "python_calls.log").read_text()
    assert os.access(home / "upload_photo.py", os.X_OK)
    assert os.access(home / "android_watcher.sh", os.X_OK)


def test_first_run_creates_config_template_and_stops(tmp_path):
    env, home, src = _make_env(tmp_path)

    result = _run(env)

    assert result.returncode == 1
    config = home / ".calorie_tracker_upload.json"
    assert "replace-with-the-same-random-value-as-server" in config.read_text()
    assert "rerun this installer" in result.stdout
    # It never pinged or started anything with a placeholder config.
    assert not (home / "python_calls.log").exists()
