"""Cross-cutting WEIRD cases across telegram_bot + database + daily_report.

Deterministic, fully offline tests for the corners nobody plans for:
unicode extremes (emoji/ZWJ, RTL, 10k-char descriptions), clock weirdness
(+1400/-1200/+0545 offsets, exact-midnight meals), DB pathology (duplicate
JSON keys, 100-level nesting, analysis='null', corrected=2, 10-year range
scans), numeric hell (2**53, -0.0, 1e308, "12.5.3", raw Infinity/NaN),
command floods (500 meals vs Telegram's 4096-char limit), and photo-hash
weirdness (unicode/1000-char/empty-vs-NULL hashes).

Real bugs found here were originally pinned as xfail; they are fixed now,
so every test below is a hard assertion.
"""

import json
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone

import pytest

import daily_report
import database
import telegram_bot
from utils import telegram_message_chunks

CHAT = 424242

# 04:00 UTC == 12:00 noon at +0800 on Tuesday 2026-07-14.
NOON_UTC = datetime(2026, 7, 14, 4, 0, 0)
TODAY = "2026-07-14"


# ─── Fixtures & helpers ────────────────────────────────────────────
@pytest.fixture(autouse=True)
def isolated_db(monkeypatch, tmp_path):
    """Fresh temp DB per test; report reads the test chat id."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "weird_cases.db")
    database.init_db()
    monkeypatch.setattr(daily_report, "CHAT_ID", str(CHAT))


def _freeze(monkeypatch, fixed_utc):
    """Freeze database's clock at a fixed naive-UTC instant."""

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is not None:
                return fixed_utc.replace(tzinfo=timezone.utc).astimezone(tz)
            return fixed_utc

    monkeypatch.setattr(database, "datetime", _Frozen)


@pytest.fixture
def frozen_noon(monkeypatch):
    """User-local clock pinned to 2026-07-14 12:00 at +0800."""
    _freeze(monkeypatch, NOON_UTC)
    monkeypatch.setattr(
        database, "get_android_timezone", lambda device_name="android_watcher": "+0800"
    )


def _analysis(desc="Meal", cal=500, p=20, c=50, f=15, items=None, is_food=True):
    return {
        "is_food": is_food,
        "meal_description": desc,
        "total_calories": cal,
        "total_protein_g": p,
        "total_carbs_g": c,
        "total_fat_g": f,
        "food_items": items if items is not None else [],
    }


def _save(date_str, analysis, time_str="12:00 PM", image_hash=""):
    return database.save_meal(
        CHAT, date_str, time_str, f"{date_str}T12:00:00", "test",
        image_hash, "", analysis,
    )


def _raw_insert(date_str, analysis_text, image_hash="", corrected=0,
                time_str="12:00 PM"):
    """Bypass save_meal's json.dumps to plant pathological analysis blobs."""
    with sqlite3.connect(database.DB_PATH) as conn:
        conn.execute(
            "INSERT INTO meals (chat_id, date, time, timestamp, source, "
            "image_hash, file_id, analysis, corrected) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (CHAT, date_str, time_str, f"{date_str}T12:00:00", "test",
             image_hash, "", analysis_text, corrected),
        )
        conn.commit()


def _capture_bot():
    """A real TelegramBot whose API layer is a capturing stub."""
    bot = telegram_bot.TelegramBot("test-token")
    calls = []

    def fake_call(method, **kwargs):
        calls.append({"method": method, **kwargs})
        return {"message_id": len(calls)}

    bot._call = fake_call
    return bot, calls


def _utf16_units(text):
    """Telegram measures its 4096 limit in UTF-16 code units, not codepoints."""
    return len(text.encode("utf-16-le")) // 2


def _strip_valid_tags(text):
    return daily_report._TELEGRAM_HTML_TAG_RE.sub("", text)


# ─── Unicode extremes ──────────────────────────────────────────────
def test_emoji_zwj_only_description_renders_everywhere(frozen_noon):
    family = "👨‍👩‍👧‍👦"          # ZWJ family
    flag = "🏳️‍🌈"                      # VS16 + ZWJ flag
    desc = family + flag + "🧑🏽‍🚀"          # skin-tone + ZWJ astronaut
    _save(TODAY, _analysis(desc, 555, items=[
        {"name": "🌯🌯", "estimated_calories": 555,
         "protein_g": 20, "carbs_g": 50, "fat_g": 15},
    ]))

    meals_list = telegram_bot.format_meals_list(CHAT)
    assert desc in meals_list

    summary = telegram_bot.format_today_summary(CHAT)
    assert "555 kcal" in summary
    assert "Meals: 1" in summary

    report = daily_report.generate_report(TODAY)
    assert desc in report
    assert "🌯🌯" in report

    plain = daily_report._html_to_plain(report)
    assert desc in plain
    assert "<b>" not in plain  # WeChat mirror carries no leftover Telegram tags


def test_rtl_names_with_markup_stay_escaped_and_html_balanced(frozen_noon):
    # Arabic + Hebrew + an explicit RIGHT-TO-LEFT OVERRIDE + injected markup.
    desc = "‮شاورما <b>دجاج</b> مع رز"
    item_name = "חומוס <i>טחינה</i>"
    _save(TODAY, _analysis(desc, 640, items=[
        {"name": item_name, "estimated_calories": 640,
         "protein_g": 25, "carbs_g": 60, "fat_g": 20},
    ]))

    meals_list = telegram_bot.format_meals_list(CHAT)
    assert "&lt;b&gt;دجاج&lt;/b&gt;" in meals_list        # user markup escaped
    assert "<b>دجاج</b>" not in meals_list                 # never raw
    assert meals_list.count("<b>") == meals_list.count("</b>")
    assert "<" not in _strip_valid_tags(meals_list)        # no broken tags

    summary = telegram_bot.format_today_summary(CHAT)
    assert "Today's Summary" in summary
    assert "<" not in _strip_valid_tags(summary)

    report = daily_report.generate_report(TODAY)
    assert "&lt;b&gt;دجاج&lt;/b&gt;" in report
    assert "&lt;i&gt;טחינה&lt;/i&gt;" in report
    assert report.count("<b>") == report.count("</b>")
    assert "<" not in _strip_valid_tags(report)

    # WeChat plain mirror: tags stripped, entities restored to the literal
    # user text (rendered literally under PushPlus' "txt" template).
    plain = daily_report._html_to_plain(report)
    assert "شاورما <b>دجاج</b> مع رز" in plain
    assert "‮" in plain                               # RLO survives


def test_10k_char_description_no_truncation_until_send(frozen_noon):
    desc = "x" * 10000
    _save(TODAY, _analysis(desc, 640))

    # The formatter itself never truncates — the full 10k desc is in the text.
    meals_list = telegram_bot.format_meals_list(CHAT)
    assert desc in meals_list
    assert len(meals_list) > 10000

    # send_message truncates to 4000 chars + a "(truncated)" marker: under
    # Telegram's 4096-char limit, so the send survives (HTML may not).
    bot, calls = _capture_bot()
    bot.send_message(CHAT, meals_list)
    sent = calls[0]["text"]
    assert len(sent) <= 4096
    assert sent.endswith("<i>(truncated)</i>")
    assert desc not in sent  # tail (and grand total) is lost — pinned behavior

    # The daily-report path chunks instead of truncating: every chunk fits
    # and the full description survives across consecutive chunks.
    report = daily_report.generate_report(TODAY)
    assert desc in report
    chunks = telegram_message_chunks(report)
    assert len(chunks) >= 3
    assert all(len(chunk) <= 3900 for chunk in chunks)
    assert desc in "".join(chunks)


def test_astral_emoji_message_fits_telegram_utf16_limit(frozen_noon):
    _save(TODAY, _analysis("🍕" * 3000, 800))

    # ~3100 codepoints but ~6200 UTF-16 units: over Telegram's real budget.
    meals_list = telegram_bot.format_meals_list(CHAT)
    assert len(meals_list) <= 4000
    assert _utf16_units(meals_list) > 4096

    # send_message measures its truncation guard in UTF-16 units, so the
    # send survives; the truncated prefix never splits a surrogate pair.
    bot, calls = _capture_bot()
    bot.send_message(CHAT, meals_list)
    sent = calls[0]["text"]
    assert _utf16_units(sent) <= 4096
    marker = "\n\n<i>(truncated)</i>"
    assert sent.endswith(marker)
    assert meals_list.startswith(sent[:-len(marker)])

    chunks = telegram_message_chunks("🍔" * 8000)
    assert max(_utf16_units(chunk) for chunk in chunks) <= 4096


# ─── Clock weirdness ───────────────────────────────────────────────
def test_heartbeat_timezone_flip_kiribati_to_baker_island(monkeypatch):
    _freeze(monkeypatch, datetime(2026, 7, 14, 11, 30, 0))

    # Kiribati: UTC+14, the world's earliest clock.
    database.update_android_heartbeat("android_watcher", "+1400")
    assert database.get_android_timezone() == "+1400"
    assert database.user_local_now() == datetime(2026, 7, 15, 1, 30, 0)
    kiribati_today = database.user_local_today()
    assert kiribati_today.isoformat() == "2026-07-15"

    # Same UTC instant, device flies to Baker Island: UTC-12.
    database.update_android_heartbeat("android_watcher", "-1200")
    assert database.user_local_now() == datetime(2026, 7, 13, 23, 30, 0)
    baker_today = database.user_local_today()
    assert baker_today.isoformat() == "2026-07-13"

    # The two extremes disagree on "today" by two full days.
    assert (kiribati_today - baker_today).days == 2

    # A bare liveness ping must NOT clobber the stored offset.
    database.update_android_heartbeat("android_watcher")
    assert database.get_android_timezone() == "-1200"
    assert database.user_local_today().isoformat() == "2026-07-13"


def test_nepal_half_hour_offset(monkeypatch):
    _freeze(monkeypatch, datetime(2026, 7, 14, 11, 30, 0))
    database.update_android_heartbeat("android_watcher", "+0545")

    assert database.parse_timezone_offset("+0545") == timedelta(hours=5, minutes=45)
    assert database.user_local_now() == datetime(2026, 7, 14, 17, 15, 0)
    assert database.user_local_today().isoformat() == "2026-07-14"

    # 18:30 UTC is already 00:15 next day in Nepal: date rolls at :15, not :00.
    _freeze(monkeypatch, datetime(2026, 7, 14, 18, 30, 0))
    assert database.user_local_now() == datetime(2026, 7, 15, 0, 15, 0)
    assert database.user_local_today().isoformat() == "2026-07-15"


def test_parse_timezone_offset_extremes():
    assert database.parse_timezone_offset("+1400") == timedelta(hours=14)
    assert database.parse_timezone_offset("-1200") == -timedelta(hours=12)
    assert database.parse_timezone_offset("+1500") is None   # beyond +14
    assert database.parse_timezone_offset("+1460") is None   # minutes > 59
    assert database.parse_timezone_offset("+05:45") is None  # colon form
    assert database.parse_timezone_offset("0545") is None    # missing sign
    assert database.parse_timezone_offset("") is None
    assert database.parse_timezone_offset(None) is None


def test_meal_at_exact_local_midnight_boundary(monkeypatch):
    monkeypatch.setattr(
        database, "get_android_timezone", lambda device_name="android_watcher": "+0800"
    )

    # 15:59:59 UTC == 23:59:59 local on the 14th — meal lands on the 14th.
    _freeze(monkeypatch, datetime(2026, 7, 14, 15, 59, 59))
    telegram_bot.save_meal(CHAT, _analysis("Pre-midnight snack", 100), "test")
    meals = database.get_meals(CHAT, "2026-07-14", "2026-07-15")
    assert [(m["date"], m["time"]) for m in meals] == [("2026-07-14", "11:59 PM")]
    assert [m["analysis"]["meal_description"]
            for m in telegram_bot.get_todays_meals(CHAT)] == ["Pre-midnight snack"]

    # One second later, 16:00:00 UTC == exactly 00:00:00 local on the 15th —
    # the meal must land on the NEW day, rendered as 12:00 AM.
    _freeze(monkeypatch, datetime(2026, 7, 14, 16, 0, 0))
    telegram_bot.save_meal(CHAT, _analysis("Midnight noodles", 200), "test")
    meals = {m["analysis"]["meal_description"]: m
             for m in database.get_meals(CHAT, "2026-07-14", "2026-07-15")}
    assert meals["Midnight noodles"]["date"] == "2026-07-15"
    assert meals["Midnight noodles"]["time"] == "12:00 AM"

    # "Today" now only contains the midnight meal, not yesterday's 23:59:59 one.
    todays = telegram_bot.get_todays_meals(CHAT)
    assert [m["analysis"]["meal_description"] for m in todays] == ["Midnight noodles"]
    assert "(12:00 AM)" in telegram_bot.format_meals_list(CHAT)


# ─── DB pathology ──────────────────────────────────────────────────
def test_analysis_null_literal_survives_today_and_meals(frozen_noon):
    _raw_insert(TODAY, "null")
    _save(TODAY, _analysis("Real meal", 500))

    summary = telegram_bot.format_today_summary(CHAT)  # AttributeError today
    assert "Today's Summary" in summary
    assert "Real meal" in telegram_bot.format_meals_list(CHAT)


def test_analysis_null_literal_survives_daily_report():
    _raw_insert(TODAY, "null")
    _save(TODAY, _analysis("Real meal", 500))

    report = daily_report.generate_report(TODAY)
    assert "Daily Calorie Report" in report


def test_duplicate_json_keys_last_value_wins(frozen_noon):
    _raw_insert(
        TODAY,
        '{"is_food": true, "meal_description": "Twice keyed", '
        '"total_calories": 111, "total_calories": 222, '
        '"total_protein_g": 1, "total_protein_g": 9, '
        '"total_carbs_g": 2, "total_fat_g": 3, "food_items": []}',
    )

    meals = database.get_meals(CHAT, TODAY, TODAY)
    assert meals[0]["analysis"]["total_calories"] == 222  # json.loads: last wins
    assert meals[0]["analysis"]["total_protein_g"] == 9

    meals_list = telegram_bot.format_meals_list(CHAT)
    assert "~222 kcal" in meals_list
    assert "111" not in meals_list
    assert "222 kcal" in telegram_bot.format_today_summary(CHAT)
    assert "Total Calories: ~222 kcal" in daily_report.generate_report(TODAY)


def test_100_level_nested_analysis_sums_to_zero_without_crash(frozen_noon):
    nested = 0
    deep_list = []
    for _ in range(100):
        nested = {"deep": nested}
        deep_list = [deep_list]
    analysis = _analysis("Bottomless bowl", nested, p=20, c=50, f=15)
    analysis["food_items"] = [deep_list]  # non-dict entry: must be ignored
    _save(TODAY, analysis)

    meals_list = telegram_bot.format_meals_list(CHAT)
    assert "Bottomless bowl" in meals_list
    assert "~0 kcal" in meals_list                # nested dict coerces to 0

    summary = telegram_bot.format_today_summary(CHAT)
    assert "0 kcal" in summary

    report = daily_report.generate_report(TODAY)
    assert "Bottomless bowl" in report
    assert "Subtotal: ~0 kcal" in report
    daily_report._html_to_plain(report)           # WeChat mirror: no crash


def test_corrected_flag_stored_as_2_renders_as_corrected(frozen_noon):
    _raw_insert(TODAY, json.dumps(_analysis("Twice corrected", 500)), corrected=2)

    meals = database.get_meals(CHAT, TODAY, TODAY)
    assert meals[0]["corrected"] is True          # bool(2) coercion

    assert "✏️" in telegram_bot.format_meals_list(CHAT)
    assert "✏️" in daily_report.generate_report(TODAY)


def test_get_meals_ten_year_range_1000_rows_under_one_second():
    base = date(2016, 1, 1)
    analysis_text = json.dumps(_analysis("Archive meal", 400))
    rows = []
    for i in range(1000):
        d = (base + timedelta(days=(i * 3653) // 1000)).isoformat()
        rows.append((CHAT, d, "12:00 PM", f"{d}T12:00:00", "test", "", "",
                     analysis_text, 0))
    with sqlite3.connect(database.DB_PATH) as conn:
        conn.executemany(
            "INSERT INTO meals (chat_id, date, time, timestamp, source, "
            "image_hash, file_id, analysis, corrected) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        conn.commit()

    start = time.perf_counter()
    meals = database.get_meals(CHAT, "2016-01-01", "2026-12-31")
    elapsed = time.perf_counter() - start

    assert len(meals) == 1000
    assert all(m["analysis"]["is_food"] for m in meals)
    assert elapsed < 1.0, f"10-year get_meals took {elapsed:.2f}s"


# ─── Numeric hell ──────────────────────────────────────────────────
def test_numeric_hell_totals_never_emit_nan_or_inf(frozen_noon):
    _save(TODAY, _analysis("Meal huge", 2 ** 53))            # > 1e9: rejected
    _save(TODAY, _analysis("Meal negzero", -0.0))            # kept: sums to 0.0
    _save(TODAY, _analysis("Meal e308", 1e308))              # > 1e9: rejected
    _save(TODAY, _analysis("Meal dotted", "12.5.3"))         # non-numeric string
    _raw_insert(TODAY, (                                     # raw NaN/Infinity
        '{"is_food": true, "meal_description": "Meal raw", '
        '"total_calories": Infinity, "total_protein_g": NaN, '
        '"total_carbs_g": -Infinity, "total_fat_g": 3, "food_items": []}'
    ))

    summary = telegram_bot.format_today_summary(CHAT)
    assert "nan" not in summary.lower()
    assert "inf" not in summary.lower()
    assert "Meals: 5" in summary
    # Every hostile calorie coerces to 0; the -0.0 makes the sum float 0.0.
    assert "0.0 kcal" in summary
    assert "Fat: 63g" in summary                              # 15*4 + 3

    for text in (telegram_bot.format_meals_list(CHAT),
                 telegram_bot.format_daily_totals(CHAT)):
        assert "nan" not in text.lower()
        assert "inf" not in text.lower()

    report = daily_report.generate_report(TODAY)
    assert "nan" not in report.lower()
    assert "inf" not in report.lower()
    assert "Total Calories: ~0.0 kcal" in report


def test_report_item_lines_never_leak_inf_nan(frozen_noon):
    _raw_insert(TODAY, (
        '{"is_food": true, "meal_description": "Mystery bowl", '
        '"total_calories": 500, "total_protein_g": 20, '
        '"total_carbs_g": 50, "total_fat_g": 15, "food_items": ['
        '{"name": "Mystery item", "estimated_calories": Infinity, '
        '"protein_g": NaN, "carbs_g": 5, "fat_g": 5}]}'
    ))

    report = daily_report.generate_report(TODAY)
    assert "inf" not in report.lower()
    assert "nan" not in report.lower()


# ─── Command floods ────────────────────────────────────────────────
def test_500_meal_flood_today_bounded_meals_truncated(frozen_noon):
    analysis_text = json.dumps(_analysis("Flood meal", 400, 10, 20, 5))
    rows = [
        (CHAT, TODAY, "12:00 PM", f"{TODAY}T{8 + i // 60:02d}:{i % 60:02d}:00",
         "test", "", "", analysis_text, 0)
        for i in range(500)
    ]
    with sqlite3.connect(database.DB_PATH) as conn:
        conn.executemany(
            "INSERT INTO meals (chat_id, date, time, timestamp, source, "
            "image_hash, file_id, analysis, corrected) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        conn.commit()

    # /today aggregates, so 500 meals still fit in one message.
    summary = telegram_bot.format_today_summary(CHAT)
    assert "200,000 kcal" in summary
    assert "Meals: 500" in summary
    assert len(summary) < 4096

    # /meals enumerates every meal: the raw formatter blows way past the
    # Telegram limit and relies entirely on send_message truncation.
    meals_list = telegram_bot.format_meals_list(CHAT)
    assert len(meals_list) > 4096
    assert "Total: ~200,000 kcal" in meals_list   # grand total exists...

    bot, calls = _capture_bot()
    result = bot.send_message(CHAT, meals_list)
    assert result is not None
    sent = calls[0]["text"]
    assert calls[0]["parse_mode"] == "HTML"
    assert len(sent) <= 4096                       # stays under the hard limit
    assert sent.endswith("<i>(truncated)</i>")
    # ...but truncation drops the tail: the grand total (and most meals)
    # never reach the user. Pinned current behavior, not aspiration.
    assert "Total: ~200,000 kcal" not in sent
    assert "500." not in sent

    # [:4000] can cut mid-tag and Telegram then rejects the HTML parse; the
    # bot must fall back to a plain-text send instead of dropping the reply.
    flaky_bot = telegram_bot.TelegramBot("test-token")
    flaky_calls = []

    def flaky_call(method, **kwargs):
        flaky_calls.append(kwargs)
        if "parse_mode" in kwargs:
            raise RuntimeError("Telegram 400 calling sendMessage")
        return {"message_id": len(flaky_calls)}

    flaky_bot._call = flaky_call
    assert flaky_bot.send_message(CHAT, meals_list) is not None
    assert len(flaky_calls) == 2
    assert "parse_mode" not in flaky_calls[1]      # plain-text fallback


# ─── Photo hash weirdness ──────────────────────────────────────────
def test_unicode_image_hash_normalizes_and_dedupes(frozen_noon):
    unicode_hash = "ΣΝΟΨ☃哈希AB"
    _save(TODAY, _analysis("Unicode hash meal", 500), image_hash=unicode_hash)

    meals = database.get_meals(CHAT, TODAY, TODAY)
    assert meals[0]["image_hash"] == "σνοψ☃哈希ab"   # unicode-lowercased

    # Dedup queries normalize the probe the same way: mixed case still hits.
    assert database.meal_image_hash_exists(CHAT, unicode_hash) is True
    assert database.meal_image_hash_exists(CHAT, "σνοψ☃哈希ab") is True
    assert database.reserve_photo_hash(CHAT, unicode_hash) is False
    assert database.get_today_hashes(CHAT) == ["σνοψ☃哈希ab"]

    # The upload-filename prefix helper strips non-hex: a unicode hash
    # degrades to just its hex-ish letters. Pinned current behavior.
    assert telegram_bot._image_hash_prefix(unicode_hash) == "ab"


def test_1000_char_image_hash_reservation_lifecycle():
    long_hash = "ab" * 500  # 1000 chars, all hex
    assert database.reserve_photo_hash(CHAT, long_hash) is True
    assert database.reserve_photo_hash(CHAT, long_hash) is False       # in flight
    assert database.reserve_photo_hash(CHAT, long_hash.upper()) is False

    database.mark_photo_hash_status(CHAT, long_hash, "saved", meal_id=77)
    assert long_hash in database.get_reserved_photo_hashes(CHAT)
    assert database.find_photo_hash_by_prefix(CHAT, long_hash[:12]) == long_hash

    # release only clears 'processing' rows; a saved ledger entry survives.
    database.release_photo_hash(CHAT, long_hash)
    assert long_hash in database.get_reserved_photo_hashes(CHAT)


def test_empty_string_vs_null_image_hash_both_hashless(frozen_noon):
    _save(TODAY, _analysis("Hashless twin", 300), image_hash="")
    _raw_insert(TODAY, json.dumps(_analysis("Hashless twin", 300)),
                image_hash=None)

    meals = database.get_meals(CHAT, TODAY, TODAY)
    assert len(meals) == 2
    assert sorted(m["image_hash"] for m in meals if m["image_hash"] is not None) == [""]
    assert any(m["image_hash"] is None for m in meals)

    # Neither '' nor NULL may leak into dedup surfaces or block uploads.
    assert database.get_today_hashes(CHAT) == []
    assert database.meal_image_hash_exists(CHAT, "") is False
    assert database.reserve_photo_hash(CHAT, "") is True
    assert database.reserve_photo_hash(CHAT, "") is True   # never dedupes empty
    assert telegram_bot.is_duplicate_photo(CHAT, "") is False

    # The report's display-only dup flag treats '' and NULL identically:
    # both fall back to the (time, description, calories) key and collide.
    report = daily_report.generate_report(TODAY)
    assert report.count("Possible duplicate of meal") == 1
