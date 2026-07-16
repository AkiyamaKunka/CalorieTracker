#!/usr/bin/env python3
"""
CalorieTracker Daily Report

Generates a detailed daily calorie & macro report and sends it to Telegram.
Designed to be triggered by launchd at 11:30 PM daily.

Usage:
    python3 daily_report.py              # Report for today
    python3 daily_report.py 2026-03-28   # Report for a specific date
"""

import os
import re
import sys
import traceback
from datetime import datetime, timezone, timedelta
from html import escape, unescape
from pathlib import Path
from typing import Optional

import requests

# Add the project directory to path
sys.path.insert(0, str(Path(__file__).parent))
import database
import service_health
from config import (
    PUSHPLUS_TOKEN,
    PUSHPLUS_TOPIC,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    REPORTS_DIR,
)
from utils import (
    meal_calorie_mismatch,
    parse_boolish,
    push_wechat,
    safe_food_items,
    safe_number,
    telegram_message_chunks as _telegram_chunks,
)

# ─── Config ────────────────────────────────────────────────────────
BOT_TOKEN = TELEGRAM_BOT_TOKEN or ""
CHAT_ID = TELEGRAM_CHAT_ID or ""
SERVICE_HEALTH_PATH = service_health.DEFAULT_PATH
# Body-weight trend window for the report's Weight section.
WEIGHT_TREND_WINDOW_DAYS = 7
# How far back the report may reach for a weight anchor at/before its date.
WEIGHT_ANCHOR_LOOKBACK_DAYS = 366


def _warn(message):
    print(f"[WARN] {message}")


def _load_service_health():
    return service_health.load(SERVICE_HEALTH_PATH, warn=_warn)


def _save_service_health(data):
    service_health.save(data, SERVICE_HEALTH_PATH, warn=_warn)


def _record_daily_report_health(ok: bool, target_date: str, source: str = "cron", report_path: str = "", error_summary: str = ""):
    service_health.update(
        lambda data: service_health.apply_report_health(
            data, ok, target_date,
            source=source, report_path=report_path, error_summary=error_summary,
        ),
        SERVICE_HEALTH_PATH,
        warn=_warn,
    )


def _record_activity_health(ok: bool, target_date: str, source: str = "garmin", error_summary: str = ""):
    service_health.update(
        lambda data: service_health.apply_activity_health(
            data, ok, target_date, source=source, error_summary=error_summary,
        ),
        SERVICE_HEALTH_PATH,
        warn=_warn,
    )


_TELEGRAM_HTML_TAG_RE = re.compile(
    r"</?(?:b|i|u|s|code|pre|a)(?:\s[^>]*)?>", re.IGNORECASE
)


def _html_to_plain(text: str) -> str:
    """Convert Telegram-HTML report text to plain text for files and WeChat."""
    return unescape(_TELEGRAM_HTML_TAG_RE.sub("", text))


def send_wechat(text, target_date):
    """Send a plain-text version of the report to WeChat via PushPlus."""
    if not PUSHPLUS_TOKEN:
        print("[INFO] PUSHPLUS_TOKEN not set. Skipping WeChat notification.")
        return

    # push_wechat's "txt" template renders the content literally.
    # _html_to_plain unescapes entities, so user-typed markup (e.g.
    # <script>) survives into the payload — under "markdown" PushPlus
    # would render it as HTML. push_wechat never raises.
    ok = push_wechat(
        f"📊 Daily Calorie Report ({target_date})",
        _html_to_plain(text),
        topic=PUSHPLUS_TOPIC,
    )
    if ok:
        print("[INFO] WeChat report sent via PushPlus successfully!")
    else:
        print("[ERROR] Failed to send WeChat report via PushPlus.")


def _net_use_total() -> bool:
    """Whether the energy-balance Net line should subtract TOTAL (not active) burn.

    Defaults to active-calorie basis (the exercise-attributable burn). Set
    GARMIN_NET_USE_TOTAL=1 to net against Garmin's whole-day total instead.
    """
    return parse_boolish(os.environ.get("GARMIN_NET_USE_TOTAL")) is True


def _row_extra_number(row, *keys):
    """First positive numeric among ``keys`` in the row's JSON ``raw`` blob.

    Steps and whole-day total calories are not first-class activity columns;
    when Garmin persists them they ride along inside ``raw``.
    """
    raw = row.get("raw") if isinstance(row, dict) else None
    if isinstance(raw, dict):
        for key in keys:
            value = safe_number(raw.get(key))
            if value > 0:
                return value
    return 0


def _weekly_weight_slope(weights):
    """Least-squares kg/week body-weight slope, or None if <2 usable weigh-ins.

    Fits weight against the weigh-in's ordinal day and scales the per-day slope
    to a weekly rate. Junk weights/dates are coerced/skipped via safe_number so
    a hostile row can never raise.
    """
    points = []
    for row in weights or []:
        if not isinstance(row, dict):
            continue
        try:
            day = datetime.strptime(str(row.get("date"))[:10], "%Y-%m-%d").date()
        except (TypeError, ValueError):
            continue
        kg = safe_number(row.get("weight_kg"))
        if kg > 0:
            points.append((day.toordinal(), kg))

    if len(points) < 2:
        return None

    n = len(points)
    mean_x = sum(x for x, _ in points) / n
    mean_y = sum(y for _, y in points) / n
    numerator = sum((x - mean_x) * (y - mean_y) for x, y in points)
    denominator = sum((x - mean_x) ** 2 for x, _ in points)
    if denominator == 0:
        return None
    return round((numerator / denominator) * 7, 2)


def _section_diet_targets(lines, chat_id, target_date, profile, anchor_weight):
    """Append the Diet Targets block — only when a diet_mode is configured."""
    diet_mode = (profile or {}).get("diet_mode")
    if diet_mode:
        import nutrition
        weight_kg = safe_number((anchor_weight or {}).get("weight_kg")) or None
        targets = nutrition.profile_diet_targets(diet_mode, weight_kg, profile)
        meals_today = database.get_meals(chat_id, target_date, target_date)
        analysis_result = nutrition.analyze_macros(meals_today, targets)
        block = nutrition.format_macro_report(analysis_result, targets, diet_mode)
        if block:
            lines.append("")
            lines.append(block)


def _section_energy_balance(lines, activities_today, grand_cal):
    """Append Energy Balance — when any burn, steps or distance exists today.

    A distance/steps-only day (e.g. "ran 9.2 km" logged without a kcal
    figure) still renders its lines; the burn and Net lines stay gated on
    active burn > 0 so the net math never runs off a burn that was never
    logged.
    """
    active = 0
    total = 0
    steps = 0
    distance_km = 0
    has_active = False
    for row in activities_today or []:
        if not isinstance(row, dict):
            continue
        row_active = safe_number(row.get("active_calories"))
        if row_active > 0:
            has_active = True
            active += row_active
        total += _row_extra_number(row, "total_calories", "totalKilocalories", "totalCalories")
        steps += _row_extra_number(row, "steps", "totalSteps")
        distance_km += safe_number(row.get("distance_km"))

    if not (has_active or steps > 0 or distance_km > 0):
        return

    lines.append("")
    lines.append("<b>⚡ Energy Balance</b>")
    if has_active:
        lines.append(f"🔥 Active burn: ~{int(round(active)):,} kcal")
    if steps > 0:
        lines.append(f"👟 Steps: {int(round(steps)):,}")
    if distance_km > 0:
        lines.append(f"📏 Distance: {distance_km:.1f} km")
    if has_active:
        use_total = _net_use_total() and total > 0
        burn = total if use_total else active
        net = safe_number(grand_cal) - burn
        basis = "total" if use_total else "active"
        # Net defaults to consumed − ACTIVE burn (exercise-attributable);
        # GARMIN_NET_USE_TOTAL=1 nets against whole-day total when present.
        lines.append(f"⚖️ Net: ~{int(round(net)):,} kcal (consumed − {basis} burn)")


def _section_weight(lines, chat_id, target_date, anchor_weight):
    """Append Weight — only when weigh-ins exist in the trailing window."""
    dt = datetime.strptime(target_date, "%Y-%m-%d")
    window_start = (dt - timedelta(days=WEIGHT_TREND_WINDOW_DAYS)).date().isoformat()
    weights = database.get_body_weights(chat_id, window_start, target_date)
    if weights:
        lines.append("")
        lines.append("<b>⚖️ Weight</b>")
        latest_kg = safe_number((anchor_weight or {}).get("weight_kg"))
        if latest_kg > 0:
            lines.append(f"Latest: <b>{latest_kg:.1f} kg</b>")
        slope = _weekly_weight_slope(weights)
        if slope is not None:
            arrow = "⬆️" if slope > 0 else ("⬇️" if slope < 0 else "➡️")
            lines.append(f"{WEIGHT_TREND_WINDOW_DAYS}-day trend: {arrow} {slope:+.2f} kg/wk")


def _section_training(lines, chat_id, profile, target_date):
    """Append Today's Training — always renders (default_profile fallback)."""
    import fitness_plan
    plan_profile = profile or fitness_plan.default_profile(chat_id, target_date)
    workout = fitness_plan.todays_workout(plan_profile, target_date)
    lines.append("")
    lines.append("<b>🏃 Today's Training</b>")
    lines.append(f"• {escape(str(workout))}")

    goal_race_date = (profile or {}).get("goal_race_date")
    if goal_race_date:
        today_day = datetime.strptime(target_date, "%Y-%m-%d").date()
        weeks = fitness_plan.weeks_to_race(profile, today_day)
        if weeks is not None:
            # race_label names the VDOT-source race (a past result), not the
            # goal — date the countdown to the goal race instead. weeks is
            # non-None only when goal_race_date normalised to a real date, so
            # its first 10 chars are the ISO day for str/date/datetime alike.
            when = escape(str(goal_race_date)[:10])
            lines.append(f"🎯 {weeks:.1f} weeks to race ({when})")


def _fitness_sections(chat_id, target_date, grand_cal):
    """Optional fitness blocks appended to the daily report.

    Returns a list of Telegram-HTML lines (possibly empty). The report is
    byte-identical to the pre-fitness output when the user has NO fitness data
    at all (no profile, weigh-in, activity or workout): the whole block is
    skipped. When any fitness data exists, each subsection is individually
    guarded and omitted when its own inputs are absent. Every number flows
    through utils.safe_number and every user/model string is html-escaped so a
    hostile value can never raise here nor break _html_to_plain downstream.
    """
    try:
        profile = database.get_fitness_profile(chat_id)
    except Exception:
        profile = None
    try:
        latest_weight = database.get_latest_body_weight(chat_id)
    except Exception:
        latest_weight = None
    # Weight anchor for THIS report's date: the latest weigh-in at or before
    # target_date, so a catch-up report for yesterday never uses this
    # morning's weigh-in. Identical to latest_weight when nothing newer exists.
    try:
        anchor_floor = (
            datetime.strptime(target_date, "%Y-%m-%d")
            - timedelta(days=WEIGHT_ANCHOR_LOOKBACK_DAYS)
        ).date().isoformat()
        past_weights = database.get_body_weights(chat_id, anchor_floor, target_date)
        anchor_weight = past_weights[-1] if past_weights else None
    except Exception:
        anchor_weight = None
    try:
        activities_today = database.get_activities(chat_id, target_date, target_date)
    except Exception:
        activities_today = []
    try:
        workouts_today = database.get_workouts(chat_id, target_date, target_date)
    except Exception:
        workouts_today = []

    # Byte-identical guard: a user with no fitness footprint gets nothing.
    if not (profile or latest_weight or activities_today or workouts_today):
        return []

    lines = []

    # 1. Diet Targets — only when a diet_mode is configured.
    try:
        _section_diet_targets(lines, chat_id, target_date, profile, anchor_weight)
    except Exception:
        pass

    # 2. Energy Balance — only when today has any burn, steps or distance.
    try:
        _section_energy_balance(lines, activities_today, grand_cal)
    except Exception:
        pass

    # 3. Weight — only when weigh-ins exist in the trailing 7-day window.
    try:
        _section_weight(lines, chat_id, target_date, anchor_weight)
    except Exception:
        pass

    # 4. Today's Training — always renders (default_profile fallback); only a
    #    failed fitness_plan import degrades it to nothing.
    try:
        _section_training(lines, chat_id, profile, target_date)
    except Exception:
        pass

    return lines


def generate_report(target_date: str) -> str:
    """Generate a detailed daily report."""
    if not CHAT_ID:
        print("[ERROR] TELEGRAM_CHAT_ID is not set in config.")
        return ""

    try:
        chat_id = int(CHAT_ID)
    except ValueError:
        print(f"[ERROR] TELEGRAM_CHAT_ID is not a valid integer: {CHAT_ID!r}")
        return ""

    meals = database.get_meals(chat_id, target_date, target_date)
    food_meals = [m for m in meals if m.get("analysis", {}).get("is_food")]

    # Header
    dt = datetime.strptime(target_date, "%Y-%m-%d")
    day_name = dt.strftime("%A, %B %d, %Y")
    lines = [
        "📊 <b>Daily Calorie Report</b>",
        f"📅 {day_name}",
        "",
    ]

    if not food_meals:
        lines.append("No meals logged today. 🍽️")
        lines.append("")
        lines.append("<i>Send food photos to @junjia_calorie_bot to start tracking!</i>")
        lines.extend(_fitness_sections(chat_id, target_date, 0))
        return "\n".join(lines)

    # ─── Per-meal breakdown ────────────────────────────────────
    lines.append("<b>🍽️ Meals:</b>\n")

    grand_cal = 0
    grand_p = 0
    grand_c = 0
    grand_f = 0
    seen_meal_keys = {}

    for i, meal in enumerate(food_meals, 1):
        a = meal["analysis"]
        desc = escape(str(a.get("meal_description", "Unknown")))
        # safe_number: stored analyses can carry hostile shapes ("640", [],
        # 1e400) that would crash the += accumulation below.
        cal = safe_number(a.get("total_calories"))
        p = safe_number(a.get("total_protein_g"))
        c = safe_number(a.get("total_carbs_g"))
        f = safe_number(a.get("total_fat_g"))
        time_str = escape(str(meal.get("time", "??:??")))
        corrected = " ✏️" if meal.get("corrected") else ""

        grand_cal += cal
        grand_p += p
        grand_c += c
        grand_f += f

        lines.append(f"<b>{i}. {desc}</b> — {time_str}{corrected}")

        # Per-item breakdown
        for item in safe_food_items(a):
            item_name = escape(str(item.get("name", "?")))
            # safe_number: JSON Infinity/NaN in item fields must not leak
            # 'inf kcal' / 'P:nang' into the report.
            item_cal = safe_number(item.get("estimated_calories"))
            item_p = safe_number(item.get("protein_g"))
            item_c = safe_number(item.get("carbs_g"))
            item_f = safe_number(item.get("fat_g"))
            lines.append(f"  • {item_name}: {item_cal} kcal")
            lines.append(f"    P:{item_p}g | C:{item_c}g | F:{item_f}g")

        lines.append(f"  📊 Subtotal: ~{cal} kcal | P:{p}g C:{c}g F:{f}g")

        item_sum = meal_calorie_mismatch(a)
        if item_sum is not None and not meal.get("corrected"):
            lines.append(
                f"  ⚠️ Item calories sum to ~{item_sum} kcal but the meal total is ~{cal} kcal — this entry may be wrong."
            )

        # Display-only duplicate flag: covers historic rows and hash-less
        # paths (manual text meals, relay payloads with empty hash) that
        # the ingestion ledger can't dedupe. No DB writes.
        # str() keeps the fallback key hashable even for list/dict field values.
        dup_key = meal.get("image_hash") or None
        if not dup_key or not isinstance(dup_key, str):
            dup_key = (str(meal.get("time")), str(a.get("meal_description")),
                       str(a.get("total_calories")))
        if dup_key in seen_meal_keys:
            lines.append(f"  ⚠️ Possible duplicate of meal {seen_meal_keys[dup_key]}.")
        else:
            seen_meal_keys[dup_key] = i

        lines.append("")

    # ─── Daily totals ──────────────────────────────────────────
    lines.append("━━━━━━━━━━━━━━━━━━")
    lines.append("<b>📊 Daily Summary</b>\n")
    lines.append(f"🔥 <b>Total Calories: ~{grand_cal:,} kcal</b>")
    lines.append(f"🥩 <b>Protein:</b> {grand_p}g")
    lines.append(f"🍞 <b>Carbs:</b> {grand_c}g")
    lines.append(f"🧈 <b>Fat:</b> {grand_f}g")
    lines.append(f"📸 <b>Meals logged:</b> {len(food_meals)}")

    # ─── 7-day average comparison ──────────────────────────────
    prior_start = (dt - timedelta(days=7)).date().isoformat()
    prior_end = (dt - timedelta(days=1)).date().isoformat()
    prior_day_totals = {}
    for m in database.get_meals(chat_id, prior_start, prior_end):
        prior_analysis = m.get("analysis", {})
        if not prior_analysis.get("is_food"):
            continue
        # Bounds-check before accumulating: hallucinated totals like a
        # 400-digit int or 1e400 (json.loads -> inf) would blow up the
        # average arithmetic (OverflowError) or poison it. Note: not
        # math.isfinite — that itself raises OverflowError on huge ints.
        cal = prior_analysis.get("total_calories") or 0
        if not (
            isinstance(cal, (int, float))
            and not isinstance(cal, bool)
            and 0 < cal < 1e9
        ):
            continue
        day = m.get("date")
        prior_day_totals[day] = prior_day_totals.get(day, 0) + cal
    if len(prior_day_totals) >= 2:
        avg = round(sum(prior_day_totals.values()) / len(prior_day_totals))
        lines.append(f"📈 <b>7-day avg:</b> ~{avg:,} kcal")
        delta = grand_cal - avg
        delta_line = f"    Today vs avg: {delta:+,} kcal"
        if avg != 0:
            delta_line += f" ({delta / avg * 100:+.0f}%)"
        lines.append(delta_line)

    # ─── Macro percentages ─────────────────────────────────────
    total_macro_cal = (grand_p * 4) + (grand_c * 4) + (grand_f * 9)
    if total_macro_cal > 0:
        p_pct = round((grand_p * 4) / total_macro_cal * 100)
        c_pct = round((grand_c * 4) / total_macro_cal * 100)
        f_pct = round((grand_f * 9) / total_macro_cal * 100)
        lines.append("")
        lines.append("<b>Macro Split:</b>")
        lines.append(f"  🥩 Protein: {p_pct}%")
        lines.append(f"  🍞 Carbs: {c_pct}%")
        lines.append(f"  🧈 Fat: {f_pct}%")

    lines.extend(_fitness_sections(chat_id, target_date, grand_cal))

    return "\n".join(lines)


def save_report_file(target_date: str, report: str):
    """Save report as a markdown file."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    filepath = REPORTS_DIR / f"report_{target_date}.md"
    filepath.write_text(_html_to_plain(report))
    return filepath


def _post_telegram_message(text, parse_mode=None):
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode

    resp = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json=payload,
        timeout=15,
    )

    try:
        result = resp.json()
    except ValueError:
        result = {"ok": False, "description": resp.text[:300]}

    if result.get("ok"):
        return True

    print(f"[ERROR] Telegram API error ({resp.status_code}): {result}")
    return False


def send_telegram(text):
    """Send a report to Telegram and return whether every chunk succeeded."""
    if not BOT_TOKEN:
        print("[ERROR] TELEGRAM_BOT_TOKEN is not set.")
        return False
    if not CHAT_ID:
        print("[ERROR] TELEGRAM_CHAT_ID is not set.")
        return False

    all_sent = True
    try:
        for chunk in _telegram_chunks(text):
            if _post_telegram_message(chunk, parse_mode="HTML"):
                continue

            print("⚠️ HTML send failed, retrying plain text...")
            if not _post_telegram_message(chunk):
                all_sent = False

        if all_sent:
            print("✅ Report sent to Telegram")
        else:
            print("❌ One or more Telegram report chunks failed.")
    except Exception as e:
        print(f"❌ Failed to send: {e}")
        return False

    return all_sent



def get_local_time(tz_str: str) -> datetime:
    """Convert current UTC time to user's local time based on +0800 string.

    Delegates offset parsing to database.parse_timezone_offset so the report
    clock and the meal-date clock can never disagree on what counts as a
    valid offset (e.g. '+2500' is rejected by both, not just one).
    """
    offset = database.parse_timezone_offset(tz_str)
    if offset is None:
        return datetime.now()  # Fallback to server local time
    return datetime.now(timezone.utc).replace(tzinfo=None) + offset

def _auto_candidate_date(local_time: datetime) -> str:
    """The date an auto run should report on: today at 23:xx, else yesterday."""
    if local_time.hour >= 23:
        return local_time.strftime("%Y-%m-%d")
    return (local_time - timedelta(days=1)).strftime("%Y-%m-%d")


def resolve_auto_target_date(local_time: datetime, health_data: dict) -> Optional[str]:
    """Pick the report date for an auto run, or None if it was already sent.

    Runs before 23:00 act as catch-up for the previous day (e.g. a launchd
    job fired late because the Mac was asleep at 23:30).
    """
    target = _auto_candidate_date(local_time)
    report = (health_data or {}).get("daily_report") or {}
    # Only a successful CRON run for this date counts as "already sent".
    # A manual /report or CLI run writes the same ledger record but must
    # not suppress that night's automatic report.
    if (
        report.get("last_target_date") == target
        and report.get("last_ok")
        and report.get("last_source") == "cron"
    ):
        return None
    return target


def _sync_garmin_activity(target_date: str):
    """Best-effort Garmin daily-activity pull + persist for ``target_date``.

    This is the ONLY place the report path touches the network; generate_report
    stays a pure offline formatter. Gated on garmin.is_configured(); import is
    lazy/guarded so a missing optional dependency is a no-op. Every failure is
    swallowed and recorded in service_health so Garmin can never block or crash
    the report.
    """
    try:
        import garmin
    except Exception:
        return

    try:
        if not garmin.is_configured():
            return
    except Exception:
        return

    if not CHAT_ID:
        return
    try:
        chat_id = int(CHAT_ID)
    except (TypeError, ValueError):
        return

    ok = False
    error_summary = ""
    try:
        daily = garmin.fetch_daily_activity(target_date)
        if daily is not None:
            kwargs = garmin.to_activity_kwargs(daily)
            # The row carries WHOLE-DAY aggregates (day burn, day steps, day
            # distance), so its identity must be the day itself — always use
            # the date-derived id. A per-activity id would change once the
            # day's primary activity appears (garmin-day-X -> garmin-<id>),
            # leaving two rows that each claim the full day and double every
            # sum. The primary activity's own id stays in raw for reference.
            kwargs["external_id"] = f"garmin-day-{target_date}"
            database.save_activity(chat_id, target_date, **kwargs)
            ok = True
        else:
            error_summary = "Garmin returned no activity for this date."
    except Exception as e:
        error_summary = f"{type(e).__name__}: {e}"
        print(f"[WARN] Garmin activity sync failed: {error_summary}")

    try:
        _record_activity_health(ok, target_date, source="garmin", error_summary=error_summary)
    except Exception:
        pass


def main():
    is_auto = len(sys.argv) == 1
    source = "cron" if is_auto else "manual"

    if is_auto:
        local_time = get_local_time(database.get_android_timezone())
        target_date = resolve_auto_target_date(local_time, _load_service_health())
        if target_date is None:
            print(f"[SKIP] report for {_auto_candidate_date(local_time)} already sent")
            return 0
    else:
        target_date = sys.argv[1]

    print(f"📊 Generating daily report for {target_date}...")

    # Best-effort network pull of Garmin cardio for the report date. Fully
    # self-guarded: a Garmin failure is logged/recorded but never blocks the
    # report below.
    _sync_garmin_activity(target_date)

    try:
        # Generate report
        report = generate_report(target_date)
        if not report:
            print("[ERROR] Report generation returned empty content. Aborting send.")
            _record_daily_report_health(False, target_date, source=source, error_summary="Report generation returned empty content.")
            return 1

        # Save to file
        filepath = save_report_file(target_date, report)
        print(f"💾 Saved to {filepath}")

        print("Sending reports...")

        # 1. Send to Telegram
        telegram_ok = send_telegram(report)
        _record_daily_report_health(
            telegram_ok,
            target_date,
            source=source,
            report_path=str(filepath),
            error_summary="" if telegram_ok else "Telegram send failed.",
        )

        # 2. Send to WeChat via PushPlus
        send_wechat(report, target_date)
    except Exception as e:
        traceback.print_exc()
        _record_daily_report_health(False, target_date, source=source, error_summary=f"{type(e).__name__}: {e}")
        if BOT_TOKEN and CHAT_ID:
            try:
                _post_telegram_message(
                    f"⚠️ Daily report for {target_date} failed: {type(e).__name__}: {e}"
                )
            except Exception:
                pass
        return 1

    print("Daily reporting process complete.")
    return 0 if telegram_ok else 1


if __name__ == "__main__":
    sys.exit(main())
