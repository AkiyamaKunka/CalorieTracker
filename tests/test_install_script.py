"""Shell-driven tests for android/install_and_start.sh.

The installer runs under bash with a temp HOME, a temp source dir (via the
CALORIE_INSTALL_SRC override) and PATH shims. The ps shim returns nothing so
the installer's kill sweep can never touch real processes on this machine.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

BASH = shutil.which("bash")
REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALLER = REPO_ROOT / "android" / "install_and_start.sh"

pytestmark = pytest.mark.skipif(BASH is None, reason="bash is not available")

# `-c` invocations are the installer's payload syntax pre-flight, not an app
# call: succeed silently so the log only records real uploader invocations.
PYTHON3_SHIM = (
    '#!/bin/sh\n'
    'case "$1" in -c) exit 0 ;; esac\n'
    'echo "$@" >> "$HOME/python_calls.log"\n'
    'exit 0\n'
)
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


def test_installer_installs_widget_shortcuts_when_present(tmp_path):
    env, home, src = _make_env(tmp_path)
    (home / ".calorie_tracker_upload.json").write_text(
        '{"ANDROID_API_KEY": "k", "SERVER_URLS": ["http://example.test"]}'
    )
    shortcuts_src = src / "shortcuts"
    shortcuts_src.mkdir()
    for entry in (REPO_ROOT / "android" / "shortcuts").iterdir():
        shutil.copy(entry, shortcuts_src / entry.name)

    result = _run(env)

    assert result.returncode == 0
    assert "Installed Termux:Widget shortcuts: start / stop / status / sync / update." in result.stdout
    installed = sorted(p.name for p in (home / ".shortcuts").iterdir())
    assert installed == ["calorie-start.sh", "calorie-status.sh",
                         "calorie-stop.sh", "calorie-sync.sh",
                         "calorie-update.sh"]
    for p in (home / ".shortcuts").iterdir():
        assert os.access(p, os.X_OK)


def test_update_shortcut_runs_installer_from_source_dir(tmp_path):
    src = tmp_path / "payload"
    src.mkdir()
    (src / "install_and_start.sh").write_text('#!/bin/sh\necho "INSTALLER RAN from $0"\n')
    env = dict(os.environ)
    env["CALORIE_INSTALL_SRC"] = str(src)

    result = subprocess.run(
        [BASH, str(REPO_ROOT / "android" / "shortcuts" / "calorie-update.sh")],
        env=env, capture_output=True, text=True, timeout=15,
    )

    assert result.returncode == 0
    assert "INSTALLER RAN" in result.stdout

    # Missing payload aborts loudly instead of half-installing.
    env["CALORIE_INSTALL_SRC"] = str(tmp_path / "empty")
    result = subprocess.run(
        [BASH, str(REPO_ROOT / "android" / "shortcuts" / "calorie-update.sh")],
        env=env, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 1
    assert "push the scripts first" in result.stdout


def test_installer_without_shortcuts_folder_still_succeeds(tmp_path):
    env, home, src = _make_env(tmp_path)
    (home / ".calorie_tracker_upload.json").write_text(
        '{"ANDROID_API_KEY": "k", "SERVER_URLS": ["http://example.test"]}'
    )

    result = _run(env)

    assert result.returncode == 0
    assert not (home / ".shortcuts").exists()


def test_installer_with_empty_shortcuts_dir_notes_missing_files(tmp_path):
    """A shortcuts/ folder with no calorie-*.sh files must not claim the
    widget buttons were installed; it prints a no-files note instead."""
    env, home, src = _make_env(tmp_path)
    (home / ".calorie_tracker_upload.json").write_text(
        '{"ANDROID_API_KEY": "k", "SERVER_URLS": ["http://example.test"]}'
    )
    (src / "shortcuts").mkdir()

    result = _run(env)

    assert result.returncode == 0
    assert "No Termux:Widget shortcut files found" in result.stdout
    assert "Installed Termux:Widget shortcuts" not in result.stdout
    assert not (home / ".shortcuts").exists()


def test_truncated_python_payload_aborts_before_touching_watcher(tmp_path):
    """A truncated adb push (syntactically broken upload_photo.py) must abort
    the install BEFORE the kill sweep, leaving the running watcher and its
    PID file untouched and naming the corruption in the error."""
    env, home, src = _make_env(tmp_path)
    (src / "upload_photo.py").write_text("def broken(\n")
    # Delegate to the real interpreter so the ast pre-flight actually parses.
    _write_shim(
        tmp_path / "bin" / "python3",
        f'#!/bin/sh\nexec "{sys.executable}" "$@"\n',
    )
    pid_file = home / ".calorie_watcher.pid"
    pid_file.write_text("12345")

    result = _run(env)

    assert result.returncode == 1
    assert "truncated" in result.stderr or "corrupt" in result.stderr
    # The kill/cleanup/copy section never ran.
    assert pid_file.read_text() == "12345"
    assert not (home / "android_watcher.sh").exists()
    assert not (home / "upload_photo.py").exists()


def test_first_run_creates_config_template_and_stops(tmp_path):
    env, home, src = _make_env(tmp_path)

    result = _run(env)

    assert result.returncode == 1
    config = home / ".calorie_tracker_upload.json"
    assert "replace-with-the-same-random-value-as-server" in config.read_text()
    assert "rerun this installer" in result.stdout
    # It never pinged or started anything with a placeholder config.
    assert not (home / "python_calls.log").exists()
