"""A full simulated day through the real pipeline (S7 integration hunt).

Drives the actual Flask app factory, the real SQLite ledger/meals tables,
the text-intent and callback handlers, the failed-upload retry path, and
the auto daily report — then asserts GLOBAL invariants that no per-feature
unit test checks: ledger<->meals consistency, no stranded staging files,
and a report whose totals match what was saved.
"""

import io
import json
import sqlite3
import sys
from datetime import datetime
from types import SimpleNamespace

from PIL import Image

import daily_report
import database
import telegram_bot


class FakeBot:
    def __init__(self):
        self.sent = []
        self.answered = []
        self.deleted = []

    def send_message(self, chat_id, text, parse_mode="HTML", reply_markup=None):
        self.sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        return {"message_id": len(self.sent)}

    def send_photo(self, chat_id, photo_bytes, caption="", parse_mode="HTML"):
        # The caption is what the user reads, so record it as the message
        # text — every "in m['text']" assertion sees the same words the
        # user would, whether the card arrived as a caption or plain text.
        self.sent.append({"chat_id": chat_id, "text": caption, "reply_markup": None,
                          "photo_bytes": photo_bytes})
        return {"message_id": len(self.sent)}

    def answer_callback_query(self, callback_query_id, text=""):
        self.answered.append((callback_query_id, text))

    def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))


class ImmediateThread:
    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        if self._target:
            self._target(*self._args, **self._kwargs)

    def join(self, timeout=None):
        pass


class ScriptedGemini:
    """models.generate_content pops the next canned JSON response."""

    def __init__(self):
        self.queue = []
        self.models = SimpleNamespace(generate_content=self._generate)

    def push(self, payload):
        self.queue.append(payload)

    def _generate(self, **kwargs):
        if not self.queue:
            raise AssertionError("ScriptedGemini queue exhausted — unexpected Gemini call")
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return SimpleNamespace(text=json.dumps(item))


def _jpeg(seed):
    img = Image.new("RGB", (64, 64), (seed % 255, (seed * 7) % 255, (seed * 13) % 255))
    out = io.BytesIO()
    img.save(out, format="JPEG")
    return out.getvalue()


def _analysis(desc, cal, items=None):
    return {
        "is_food": True,
        "meal_description": desc,
        "total_calories": cal,
        "total_protein_g": 20,
        "total_carbs_g": 30,
        "total_fat_g": 10,
        "food_items": items or [{"name": desc, "estimated_calories": cal,
                                 "protein_g": 20, "carbs_g": 30, "fat_g": 10}],
    }


def test_simulated_full_day(monkeypatch, tmp_path):
    chat_id = 12345
    pending = tmp_path / "pending"
    failed = tmp_path / "failed"
    reports = tmp_path / "reports"
    health = tmp_path / "health.json"

    monkeypatch.setattr(database, "DB_PATH", tmp_path / "day.db")
    database.init_db()
    monkeypatch.setattr(database, "get_android_timezone",
                        lambda device_name="android_watcher": "+0800")
    # Freeze the user-local clock for the whole simulated day: every upload
    # and query below recomputes user_local_today() inside product code, so a
    # real midnight rollover mid-test would scatter the day across two dates.
    _frozen_now = database.user_local_now()
    monkeypatch.setattr(database, "user_local_now",
                        lambda device_name="android_watcher": _frozen_now)
    monkeypatch.setattr(telegram_bot, "ALLOWED_CHAT_ID", chat_id)
    monkeypatch.setattr(telegram_bot, "ANDROID_API_KEY", "day-key")
    monkeypatch.setattr(telegram_bot, "API_UPLOAD_PENDING_DIR", pending)
    monkeypatch.setattr(telegram_bot, "API_UPLOAD_FAILED_DIR", failed)
    monkeypatch.setattr(telegram_bot, "SERVICE_HEALTH_PATH", health)
    monkeypatch.setattr(telegram_bot, "threading", SimpleNamespace(Thread=ImmediateThread))
    monkeypatch.setattr(telegram_bot, "_pending_nl_deletes", {})

    bot = FakeBot()
    gemini = ScriptedGemini()
    app = telegram_bot._build_api_app(bot, gemini)
    client = app.test_client()
    headers = {"X-API-Key": "day-key"}
    local_today = database.user_local_now().date().isoformat()

    # ── 08:00 morning heartbeat ────────────────────────────────────
    assert client.post("/ping", headers=headers,
                       json={"timezone": "+0800"}).status_code == 200

    # ── 08:30 breakfast upload (recompressing client declares hash) ─
    breakfast_bytes = _jpeg(1)
    breakfast_hash = "aa" * 16
    gemini.push(_analysis("Congee with egg", 420))
    resp = client.post("/upload", headers=headers,
                       data={"photo": (io.BytesIO(breakfast_bytes), "b.jpg"),
                             "original_hash": breakfast_hash})
    assert resp.get_json() == {"status": "processing_in_background"}
    assert any("Congee" in m["text"] for m in bot.sent)

    # queue retry of the same photo dedupes cleanly
    resp = client.post("/upload", headers=headers,
                       data={"photo": (io.BytesIO(breakfast_bytes), "b.jpg"),
                             "original_hash": breakfast_hash})
    assert resp.get_json() == {"status": "duplicate"}

    # ── 12:30 lunch upload fails analysis, then succeeds on retry ──
    lunch_hash = "bb" * 16
    gemini.push(RuntimeError("503 transient"))
    gemini.push(RuntimeError("503 transient"))
    gemini.push(RuntimeError("503 transient"))  # exhausts the 3 attempts
    monkeypatch.setattr(telegram_bot.time, "sleep", lambda s: None)
    resp = client.post("/upload", headers=headers,
                       data={"photo": (io.BytesIO(_jpeg(2)), "l.jpg"),
                             "original_hash": lunch_hash})
    assert resp.get_json() == {"status": "processing_in_background"}
    assert len(list(failed.iterdir())) == 1
    assert any("/retry_failed" in m["text"] for m in bot.sent)

    gemini.push(_analysis("Beef noodle soup", 650))
    result = telegram_bot._retry_failed_upload_path(gemini, next(failed.iterdir()))
    assert result["status"] == "logged"
    assert list(failed.iterdir()) == []

    # ── 14:00 text correction of the lunch meal ────────────────────
    meals = database.get_recent_meals(chat_id, days=1)
    lunch_index = next(i for i, m in enumerate(meals)
                       if m["analysis"]["meal_description"] == "Beef noodle soup")
    gemini.push({"intent": "correction", "meal_index": lunch_index,
                 "analysis": _analysis("Beef noodle soup (large)", 800),
                 "reply": "Updated to a large portion."})
    telegram_bot.handle_text_message(gemini, bot, chat_id, "actually it was a large bowl")
    corrected = database.get_recent_meals(chat_id, days=1)[lunch_index]
    assert corrected["analysis"]["total_calories"] == 800
    assert corrected["corrected"] is True

    # ── 15:00 snack logged then deleted via confirmed NL delete ────
    snack_hash = "cc" * 16
    gemini.push(_analysis("Bubble tea", 300))
    client.post("/upload", headers=headers,
                data={"photo": (io.BytesIO(_jpeg(3)), "s.jpg"),
                      "original_hash": snack_hash})
    meals = database.get_recent_meals(chat_id, days=1)
    snack_index = next(i for i, m in enumerate(meals)
                       if m["analysis"]["meal_description"] == "Bubble tea")
    snack_id = meals[snack_index]["id"]
    gemini.push({"intent": "delete", "meal_indices": [snack_index],
                 "reply": "Deleting the bubble tea."})
    telegram_bot.handle_text_message(gemini, bot, chat_id, "delete the bubble tea")
    assert any(m["id"] == snack_id for m in database.get_recent_meals(chat_id, days=1))

    token = telegram_bot._pending_nl_deletes[chat_id]["token"]
    telegram_bot.handle_callback_query(gemini, bot, {
        "id": "cb1", "data": f"nl_delete_confirm:{token}",
        "message": {"chat": {"id": chat_id}, "message_id": 99},
    })
    assert not any(m["id"] == snack_id for m in database.get_recent_meals(chat_id, days=1))

    # ── 19:00 dinner arrives via the nightly-style reconcile ───────
    dinner_hash = "dd" * 16
    resp = client.post("/reconcile", headers=headers,
                       json={"hashes": [breakfast_hash, lunch_hash, snack_hash, dinner_hash]})
    missing = resp.get_json()["missing_hashes"]
    assert missing == [dinner_hash]  # deleted snack stays suppressed

    gemini.push(_analysis("Steamed fish and rice", 700))
    client.post("/upload", headers=headers,
                data={"photo": (io.BytesIO(_jpeg(4)), "d.jpg"),
                      "original_hash": dinner_hash})

    # ── 23:30 automatic daily report ────────────────────────────────
    telegram_sends = []

    def fake_post(url, json=None, timeout=None, **kwargs):
        telegram_sends.append(json)
        return SimpleNamespace(status_code=200,
                               json=lambda: {"ok": True, "code": 200},
                               raise_for_status=lambda: None,
                               text="ok")

    monkeypatch.setattr(daily_report, "BOT_TOKEN", "day-token")
    monkeypatch.setattr(daily_report, "CHAT_ID", str(chat_id))
    monkeypatch.setattr(daily_report, "REPORTS_DIR", reports)
    monkeypatch.setattr(daily_report, "SERVICE_HEALTH_PATH", health)
    monkeypatch.setattr(daily_report, "PUSHPLUS_TOKEN", None)
    monkeypatch.setattr(daily_report.requests, "post", fake_post)
    monkeypatch.setattr(daily_report, "get_local_time",
                        lambda tz: datetime.fromisoformat(f"{local_today}T23:30:00"))
    monkeypatch.setattr(sys, "argv", ["daily_report.py"])

    assert daily_report.main() == 0

    report_text = "\n".join(chunk["text"] for chunk in telegram_sends)
    assert "Congee with egg" in report_text
    assert "Beef noodle soup (large)" in report_text
    assert "Steamed fish and rice" in report_text
    assert "Bubble tea" not in report_text          # deleted meal stays gone
    assert "~1,920 kcal" in report_text             # 420 + 800 + 700
    assert (reports / f"report_{local_today}.md").exists()

    # A second auto run the same evening is deduped by the health ledger.
    telegram_sends.clear()
    assert daily_report.main() == 0
    assert telegram_sends == []

    # ── GLOBAL INVARIANTS ───────────────────────────────────────────
    assert gemini.queue == []                       # every scripted call consumed
    assert list(pending.iterdir()) == []            # nothing stranded mid-pipeline
    assert list(failed.iterdir()) == []             # nothing left failed

    with sqlite3.connect(database.DB_PATH) as conn:
        meals_rows = conn.execute(
            "SELECT id, image_hash FROM meals WHERE chat_id = ?", (chat_id,)).fetchall()
        ledger = {h: (s, mid) for h, s, mid in conn.execute(
            "SELECT image_hash, status, meal_id FROM photo_ingestions WHERE chat_id = ?",
            (chat_id,))}

    # Every saved meal's hash has a 'saved' ledger row pointing back at it.
    for meal_id, image_hash in meals_rows:
        assert ledger[image_hash] == ("saved", meal_id), (image_hash, ledger.get(image_hash))
    # The deleted snack left a tombstone, not a hole.
    assert ledger[snack_hash][0] == "deleted"
    assert ledger[snack_hash][1] is None
    # No reservation left in-flight at end of day.
    assert all(status != "processing" for status, _ in ledger.values())
