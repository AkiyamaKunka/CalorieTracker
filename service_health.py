"""Shared service-health ledger stored in logs/service_health.json.

telegram_bot.py and daily_report.py both record into this file; the load/save
mechanics and the daily_report record schema live here so the two writers
cannot drift.
"""

import json
import os
import threading
from datetime import datetime
from pathlib import Path

DEFAULT_PATH = Path.home() / "CalorieTracker" / "logs" / "service_health.json"

MAX_EVENTS = 50


def timestamp() -> str:
    return datetime.now().isoformat(timespec="seconds")


def load(path=DEFAULT_PATH, warn=print) -> dict:
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    except OSError as e:
        warn(f"Could not read service health file: {e}")
        return {}


def save(data, path=DEFAULT_PATH, warn=print):
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp_path.write_text(json.dumps(data, indent=2, sort_keys=True))
        tmp_path.replace(path)
    except OSError as e:
        warn(f"Could not write service health file: {e}")


def apply_report_health(data: dict, ok: bool, target_date: str, *, source: str,
                        report_path: str = "", error_summary: str = "") -> dict:
    """Mutate `data` in place with a daily_report attempt record."""
    report = data.setdefault("daily_report", {})
    now = timestamp()
    report["last_attempt_at"] = now
    report["last_target_date"] = target_date
    report["last_source"] = source
    report["last_ok"] = ok
    if report_path:
        report["last_report_path"] = report_path

    if ok:
        report["last_success_at"] = now
        report["consecutive_failures"] = 0
        report.pop("last_error_summary", None)
    else:
        report["last_error_at"] = now
        report["last_error_summary"] = str(error_summary or "")[:500]
        report["consecutive_failures"] = int(report.get("consecutive_failures", 0)) + 1

    events = report.setdefault("events", [])
    events.append({
        "at": now,
        "ok": ok,
        "target_date": target_date,
        "source": source,
    })
    report["events"] = events[-MAX_EVENTS:]
    return data
