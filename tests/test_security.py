import io
import json
from datetime import datetime

import meal_relay


class _FakeRFile:
    def __init__(self, body=b""):
        self._body = body
        self.read_called = False

    def read(self, length):
        self.read_called = True
        return self._body[:length]


class _CountingRFile:
    """Sentinel rfile: serves up to `available` bytes, counting every read."""

    def __init__(self, available):
        self.available = available
        self.reads = 0
        self.bytes_read = 0
        self.max_chunk_requested = 0

    def read(self, length):
        self.reads += 1
        self.max_chunk_requested = max(self.max_chunk_requested, length)
        n = min(length, self.available - self.bytes_read)
        if n <= 0:
            return b""
        self.bytes_read += n
        return b"x" * n


def _make_handler(headers, body=b""):
    handler = object.__new__(meal_relay.MealHandler)
    handler.headers = headers
    handler.rfile = _FakeRFile(body)
    handler.wfile = io.BytesIO()
    statuses = []
    handler.send_response = statuses.append
    handler.end_headers = lambda: None
    return handler, statuses


def test_meal_relay_requires_matching_api_key(monkeypatch):
    handler = object.__new__(meal_relay.MealHandler)
    handler.headers = {"X-API-Key": "good-key"}

    monkeypatch.setattr(meal_relay, "RELAY_API_KEY", "")
    assert handler._authorized() is False

    monkeypatch.setattr(meal_relay, "RELAY_API_KEY", "good-key")
    assert handler._authorized() is True

    handler.headers = {"X-API-Key": "wrong-key"}
    assert handler._authorized() is False


def test_do_post_invalid_content_length_returns_400(monkeypatch):
    monkeypatch.setattr(meal_relay, "RELAY_API_KEY", "good-key")
    monkeypatch.setattr(meal_relay, "TELEGRAM_CHAT_ID", "12345")

    handler, statuses = _make_handler(
        {"X-API-Key": "good-key", "Content-Length": "banana"}
    )
    handler.do_POST()

    assert statuses == [400]
    assert b"invalid Content-Length" in handler.wfile.getvalue()
    assert handler.rfile.read_called is False


def test_do_post_oversized_body_drains_then_returns_413(monkeypatch):
    monkeypatch.setattr(meal_relay, "RELAY_API_KEY", "good-key")
    monkeypatch.setattr(meal_relay, "TELEGRAM_CHAT_ID", "12345")

    length = meal_relay.MAX_BODY_BYTES + 1
    handler, statuses = _make_handler(
        {"X-API-Key": "good-key", "Content-Length": str(length)}
    )
    handler.rfile = _CountingRFile(available=length)
    handler.do_POST()

    assert statuses == [413]
    assert b"payload too large" in handler.wfile.getvalue()
    # The whole declared body must be consumed (drained) before responding,
    # otherwise the client sees a TCP RST instead of the 413.
    assert handler.rfile.reads > 0
    assert handler.rfile.bytes_read == length
    # Drained incrementally, never buffering more than 64KB at a time.
    assert handler.rfile.max_chunk_requested <= 64 * 1024


def test_do_post_oversized_drain_respects_cap(monkeypatch):
    monkeypatch.setattr(meal_relay, "RELAY_API_KEY", "good-key")
    monkeypatch.setattr(meal_relay, "TELEGRAM_CHAT_ID", "12345")

    cap = 4 * meal_relay.MAX_BODY_BYTES
    declared = 100 * meal_relay.MAX_BODY_BYTES  # hostile Content-Length
    handler, statuses = _make_handler(
        {"X-API-Key": "good-key", "Content-Length": str(declared)}
    )
    handler.rfile = _CountingRFile(available=declared)
    handler.do_POST()

    assert statuses == [413]
    assert b"payload too large" in handler.wfile.getvalue()
    # Never streams past the drain cap regardless of the declared length.
    assert handler.rfile.bytes_read == cap


def test_do_post_negative_content_length_returns_400_without_reading(monkeypatch):
    monkeypatch.setattr(meal_relay, "RELAY_API_KEY", "good-key")
    monkeypatch.setattr(meal_relay, "TELEGRAM_CHAT_ID", "12345")

    handler, statuses = _make_handler(
        {"X-API-Key": "good-key", "Content-Length": "-5"}
    )
    handler.do_POST()

    assert statuses == [400]
    assert b"invalid Content-Length" in handler.wfile.getvalue()
    assert handler.rfile.read_called is False


def test_do_post_save_failure_releases_hash_and_hides_error(monkeypatch):
    monkeypatch.setattr(meal_relay, "RELAY_API_KEY", "good-key")
    monkeypatch.setattr(meal_relay, "TELEGRAM_CHAT_ID", "12345")

    payload = json.dumps(
        {
            "analysis": {"is_food": True, "meal_description": "soup", "total_calories": 200},
            "image_hash": "abc123",
            "time": "12:30 PM",
        }
    ).encode()

    handler, statuses = _make_handler(
        {"X-API-Key": "good-key", "Content-Length": str(len(payload))},
        body=payload,
    )

    monkeypatch.setattr(
        meal_relay.database, "user_local_now", lambda: datetime(2026, 7, 4, 12, 30)
    )
    monkeypatch.setattr(
        meal_relay.database, "reserve_photo_hash", lambda *args, **kwargs: True
    )

    def boom(*args, **kwargs):
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(meal_relay.database, "save_meal", boom)

    released = []
    monkeypatch.setattr(
        meal_relay.database,
        "release_photo_hash",
        lambda chat_id, image_hash: released.append((chat_id, image_hash)),
    )

    handler.do_POST()

    assert statuses == [500]
    assert handler.wfile.getvalue() == b'{"error": "internal error"}'
    assert b"secret" not in handler.wfile.getvalue()
    assert released == [(12345, "abc123")]
