"""Crash-recovery torture (S8 hunt).

Leaves the upload pipeline in each intermediate state a process kill could
produce (after reserve / after stage / mid-analysis), simulates a restart,
runs the boot sweep, and asserts the invariants are restored: no stranded
staging file, no permanently-stuck 'processing' row, the photo is
recoverable, and recovery logs exactly one meal.
"""

import io
import json
import sqlite3
from types import SimpleNamespace

import pytest
from PIL import Image

import database
import telegram_bot


class FakeBot:
    def __init__(self):
        self.sent = []

    def send_message(self, chat_id, text, parse_mode="HTML", reply_markup=None):
        self.sent.append({"text": text})
        return {"message_id": len(self.sent)}

    def answer_callback_query(self, *a, **k):
        pass

    def delete_message(self, *a, **k):
        pass


class DeadThread:
    """Captures the background target but never runs it — the crash happens
    the instant before analysis would start."""
    last = None

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        DeadThread.last = (target, args, kwargs or {})

    def start(self):
        pass

    def join(self, timeout=None):
        pass


class ScriptedGemini:
    def __init__(self, payload):
        self.payload = payload
        self.models = SimpleNamespace(generate_content=self._gen)

    def _gen(self, **kwargs):
        return SimpleNamespace(text=json.dumps(self.payload))


def _jpeg():
    out = io.BytesIO()
    Image.new("RGB", (48, 48), (12, 34, 56)).save(out, format="JPEG")
    return out.getvalue()


def _wire(monkeypatch, tmp_path, thread_cls):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "crash.db")
    database.init_db()
    monkeypatch.setattr(database, "get_android_timezone",
                        lambda device_name="android_watcher": "+0800")
    monkeypatch.setattr(telegram_bot, "ALLOWED_CHAT_ID", 12345)
    monkeypatch.setattr(telegram_bot, "ANDROID_API_KEY", "k")
    monkeypatch.setattr(telegram_bot, "API_UPLOAD_PENDING_DIR", tmp_path / "pending")
    monkeypatch.setattr(telegram_bot, "API_UPLOAD_FAILED_DIR", tmp_path / "failed")
    monkeypatch.setattr(telegram_bot, "SERVICE_HEALTH_PATH", tmp_path / "health.json")
    monkeypatch.setattr(telegram_bot, "threading", SimpleNamespace(Thread=thread_cls))
    telegram_bot._api_upload_processing_hashes.clear()


def _ledger(chat_id=12345):
    with sqlite3.connect(database.DB_PATH) as conn:
        return {h: (s, m) for h, s, m in conn.execute(
            "SELECT image_hash, status, meal_id FROM photo_ingestions WHERE chat_id=?",
            (chat_id,))}


def _simulate_restart(monkeypatch):
    """A fresh process has an empty in-memory processing set."""
    telegram_bot._api_upload_processing_hashes.clear()


def test_crash_after_reserve_before_stage(monkeypatch, tmp_path):
    """Reservation taken, process dies before the file is staged: the boot
    sweep must release the orphaned 'processing' row so the photo can be
    re-sent (no permanent duplicate suppression)."""
    _wire(monkeypatch, tmp_path, DeadThread)
    h = "aa" * 16
    # Directly reserve, then crash — nothing staged.
    assert database.reserve_photo_hash(12345, h, "api_upload")
    assert _ledger()[h][0] == "processing"

    _simulate_restart(monkeypatch)
    telegram_bot._sweep_stranded_pending_uploads(FakeBot())

    # Orphaned reservation with no backing file is released.
    assert h not in _ledger()
    # And the photo can now be reserved again (re-upload works).
    assert database.reserve_photo_hash(12345, h, "api_upload")


def test_crash_after_stage_before_analysis(monkeypatch, tmp_path):
    """File staged, ledger 'processing', analysis never ran. Boot sweep must
    move the file to failed under the DECLARED hash and mark it 'failed', and
    /retry_failed must then log exactly one meal."""
    _wire(monkeypatch, tmp_path, DeadThread)
    declared = "bb" * 16
    bot = FakeBot()
    app = telegram_bot._build_api_app(bot, ScriptedGemini({"is_food": True}))
    resp = app.test_client().post(
        "/upload", headers={"X-API-Key": "k"},
        data={"photo": (io.BytesIO(_jpeg()), "m.jpg"), "original_hash": declared})
    assert resp.get_json() == {"status": "processing_in_background"}

    pending = tmp_path / "pending"
    failed = tmp_path / "failed"
    assert len(list(pending.iterdir())) == 1      # staged, analysis never ran
    assert _ledger()[declared][0] == "processing"

    _simulate_restart(monkeypatch)
    telegram_bot._sweep_stranded_pending_uploads(bot)

    assert list(pending.iterdir()) == []          # nothing stranded
    assert len(list(failed.iterdir())) == 1
    assert _ledger()[declared][0] == "failed"     # keyed under the declared hash

    # Recover via /retry_failed and confirm exactly one meal, keyed to declared.
    gemini = ScriptedGemini({"is_food": True, "total_calories": 500,
                             "meal_description": "recovered meal", "food_items": []})
    result = telegram_bot._retry_failed_upload_path(gemini, next(failed.iterdir()))
    assert result["status"] == "logged"

    with sqlite3.connect(database.DB_PATH) as conn:
        rows = conn.execute("SELECT image_hash FROM meals WHERE chat_id=12345").fetchall()
    assert rows == [(declared,)]
    assert _ledger()[declared][0] == "saved"
    assert list(failed.iterdir()) == []


def test_reconcile_after_crash_before_recovery_does_not_double_log(monkeypatch, tmp_path):
    """If the phone reconciles while a crashed upload still sits staged (before
    the boot sweep runs), /reconcile must still suppress the hash so the photo
    is not re-uploaded and double-logged."""
    _wire(monkeypatch, tmp_path, DeadThread)
    declared = "cc" * 16
    bot = FakeBot()
    app = telegram_bot._build_api_app(bot, ScriptedGemini({"is_food": True}))
    client = app.test_client()
    client.post("/upload", headers={"X-API-Key": "k"},
                data={"photo": (io.BytesIO(_jpeg()), "m.jpg"), "original_hash": declared})

    _simulate_restart(monkeypatch)
    # Phone reconciles BEFORE the boot sweep: the 'processing' row (fresh, not
    # stale) plus the staged file must keep the hash suppressed.
    resp = client.post("/reconcile", headers={"X-API-Key": "k"},
                       json={"hashes": [declared]})
    assert resp.get_json()["missing_hashes"] == []


# ---- Kill windows around save_meal / mark-saved / discard / echo ----
#
# The 1d0c59c reorder discards the staged file BEFORE the photo echo. These
# tests model a hard kill (BaseException — `except Exception` cannot contain
# it, like OOM/SIGKILL) at each cut point of the success path:
#   save_meal -> mark saved -> discard staged file -> echo photo


class RunNowThread:
    """Runs the background target synchronously inside the request."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target, self._args, self._kwargs = target, args, kwargs or {}

    def start(self):
        if self._target:
            self._target(*self._args, **self._kwargs)


class KillSignal(BaseException):
    pass


DECLARED = "dd" * 16


def _upload_food_photo_killed_at(monkeypatch, tmp_path, kill_at):
    """POST a food photo to /upload and 'kill the process' once at the named
    cut point inside the background worker; later calls behave normally
    (the restarted process runs unpatched code)."""
    _wire(monkeypatch, tmp_path, RunNowThread)
    analysis = {"is_food": True, "total_calories": 500,
                "meal_description": "kill-window meal", "food_items": []}
    monkeypatch.setattr(telegram_bot, "analyze_food_photo_with_retries",
                        lambda client, image_bytes, *a, **k: dict(analysis))

    state = {"killed": False}

    def _once(real, die_after=False):
        def wrapper(*args, **kwargs):
            if state["killed"]:
                return real(*args, **kwargs)
            state["killed"] = True
            if die_after:
                real(*args, **kwargs)
            raise KillSignal()
        return wrapper

    if kill_at == "after_save_before_mark":
        monkeypatch.setattr(telegram_bot, "save_meal",
                            _once(telegram_bot.save_meal, die_after=True))
    elif kill_at == "after_mark_before_discard":
        monkeypatch.setattr(telegram_bot, "_discard_api_upload",
                            _once(telegram_bot._discard_api_upload))
    elif kill_at == "after_discard_before_echo":
        monkeypatch.setattr(telegram_bot, "_echo_meal_photo",
                            _once(telegram_bot._echo_meal_photo))
    else:
        raise AssertionError(f"unknown kill point {kill_at}")

    bot = FakeBot()
    app = telegram_bot._build_api_app(bot, object())
    with pytest.raises(KillSignal):
        app.test_client().post(
            "/upload", headers={"X-API-Key": "k"},
            data={"photo": (io.BytesIO(_jpeg()), "m.jpg"), "original_hash": DECLARED})

    _simulate_restart(monkeypatch)
    return bot


def _meal_hashes(chat_id=12345):
    with sqlite3.connect(database.DB_PATH) as conn:
        return [r[0] for r in conn.execute(
            "SELECT image_hash FROM meals WHERE chat_id=?", (chat_id,))]


def test_kill_between_discard_and_echo_leaves_nothing_for_the_sweep(monkeypatch, tmp_path):
    """Kill inside the photo echo (the biggest memory spike on this path —
    the reason 2986c13 moved the discard BEFORE the echo): the staged file is
    already gone and the ledger says 'saved', so the boot sweep must find
    nothing, touch nothing, and stay silent."""
    _upload_food_photo_killed_at(monkeypatch, tmp_path, "after_discard_before_echo")

    assert list((tmp_path / "pending").iterdir()) == []
    assert _ledger()[DECLARED][0] == "saved"
    assert _meal_hashes() == [DECLARED]

    boot_bot = FakeBot()
    telegram_bot._sweep_stranded_pending_uploads(boot_bot)

    assert boot_bot.sent == []                # no false recovery notice
    assert _ledger()[DECLARED][0] == "saved"  # row untouched
    assert not (tmp_path / "failed").exists() or list((tmp_path / "failed").iterdir()) == []
    assert _meal_hashes() == [DECLARED]


@pytest.mark.parametrize("kill_at", ["after_save_before_mark", "after_mark_before_discard"])
def test_saved_meal_never_double_logs_after_sweep_mislabel(monkeypatch, tmp_path, kill_at):
    """Kill in the save->discard window strands the staged file although the
    meal IS saved. The boot sweep consults the meals table and quietly
    discards the stranded copy (no saved->failed mislabel, nothing left to
    retry), and no path may log the meal twice: reserve_photo_hash's
    meals-table check blocks /upload and Telegram re-sends."""
    _upload_food_photo_killed_at(monkeypatch, tmp_path, kill_at)
    assert _meal_hashes() == [DECLARED]

    telegram_bot._sweep_stranded_pending_uploads(FakeBot())

    # The reservation can never be re-taken while the meal row exists —
    # not by the strict /upload path, not by a deliberate Telegram re-send
    # (which may reclaim failed/skipped/deleted, but never a logged meal).
    assert database.reserve_photo_hash(12345, DECLARED, "api_upload") is False
    assert database.reserve_photo_hash(
        12345, DECLARED, "telegram",
        reclaim_statuses={"failed", "skipped", "deleted"}) is False

    # The stranded staged copy was discarded, not swept into the failed dir,
    # so no retry path (or human) can ever feed the photo back in.
    assert list((tmp_path / "pending").iterdir()) == []
    failed_dir = tmp_path / "failed"
    assert not failed_dir.exists() or list(failed_dir.iterdir()) == []

    assert _meal_hashes() == [DECLARED]        # exactly one meal, ever
    assert _ledger()[DECLARED][0] == "saved"   # the sweep restored the truth


def test_boot_sweep_stays_quiet_for_already_saved_meal(monkeypatch, tmp_path):
    _upload_food_photo_killed_at(monkeypatch, tmp_path, "after_mark_before_discard")
    assert _ledger()[DECLARED][0] == "saved"
    assert _meal_hashes() == [DECLARED]

    boot_bot = FakeBot()
    telegram_bot._sweep_stranded_pending_uploads(boot_bot)

    # The meal is saved: quietly dropping the stranded staged copy is the
    # only correct recovery — no user notice, no saved->failed flip.
    assert boot_bot.sent == []
    assert _ledger()[DECLARED][0] == "saved"
    assert list((tmp_path / "pending").iterdir()) == []
    assert _ledger()[DECLARED][0] == "saved"
